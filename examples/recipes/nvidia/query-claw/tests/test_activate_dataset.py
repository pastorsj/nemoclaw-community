# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import activate_dataset as activation  # noqa: E402


class ActivateDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "aiq3"
        self.root.mkdir()
        (self.root / "build").mkdir()
        (self.root / "build/data.duckdb").write_bytes(b"\0" * 8 + b"DUCK fixture")
        (self.root / "build/model.yaml").write_text(
            "data_layer:\n  databases: []\n", encoding="utf-8"
        )
        (self.root / "build/prediction.json").write_text(
            '{"schema_version": 1}\n', encoding="utf-8"
        )
        (self.root / "prediction").mkdir()
        (self.root / "prediction/pql.json").write_text(
            '{"schema_version": 1, "examples": []}\n', encoding="utf-8"
        )
        (self.root / "corpus/nested").mkdir(parents=True)
        (self.root / "corpus/manifest.json").write_text("{}\n", encoding="utf-8")
        (self.root / "corpus/nested/guide.md").write_text("# Guide\n", encoding="utf-8")
        (self.root / "questions").mkdir()
        (self.root / "questions/matrix.json").write_text("[]\n", encoding="utf-8")
        (self.root / "questions/conversations.json").write_text(
            "[]\n", encoding="utf-8"
        )
        (self.root / "not-declared.txt").write_text("private\n", encoding="utf-8")
        self.dataset = self.root / "dataset.json"
        self.output = Path(self.temporary.name) / "active"
        self.write_manifest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manifest(self) -> dict:
        return {
            "schema_version": "1.0",
            "id": "cloud-operations",
            "name": "Cloud Operations",
            "industry": {"id": "cloud-services", "name": "Cloud Services"},
            "build": {
                "outputs": [
                    {
                        "id": "database",
                        "kind": "database",
                        "path": "build/data.duckdb",
                        "format": "duckdb",
                    },
                    {
                        "id": "ontology",
                        "kind": "ontology-model",
                        "path": "build/model.yaml",
                        "format": "yaml",
                    },
                    {
                        "id": "prediction",
                        "kind": "prediction-graph",
                        "path": "build/prediction.json",
                        "format": "json",
                    },
                    {
                        "id": "pql",
                        "kind": "pql-examples",
                        "path": "prediction/pql.json",
                        "format": "json",
                    },
                    {
                        "id": "documents",
                        "kind": "documents",
                        "path": "corpus",
                        "format": "markdown-corpus-v2",
                    },
                ]
            },
            "structured": {
                "database_name": "cloud_operations",
                "database_output": "database",
                "ontology_output": "ontology",
                "prediction": {
                    "graph_output": "prediction",
                    "pql_examples_output": "pql",
                },
            },
            "documents": {
                "corpus_output": "documents",
                "collection": "aiq-cloud-operations-v1",
            },
            "evaluation": {
                "question_matrix": "questions/matrix.json",
                "stateful_cases": "questions/conversations.json",
                "report_output": ".booth/results/report.json",
            },
        }

    def write_manifest(self, value: dict | None = None) -> None:
        self.dataset.write_text(
            json.dumps(value or self.manifest(), indent=2) + "\n", encoding="utf-8"
        )

    def activate(self) -> dict:
        path = activation.activate_external(self.dataset, self.root, self.output)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_activates_only_declared_database_documents_and_question_metadata(
        self,
    ) -> None:
        result = self.activate()
        self.assertEqual(result["id"], "cloud-operations")
        self.assertEqual(result["title"], "Cloud Operations")
        self.assertEqual(
            result["industry"], {"id": "cloud-services", "title": "Cloud Services"}
        )
        self.assertEqual(
            result["database"],
            {
                "engine": "duckdb",
                "name": "cloud_operations",
                "path": "database/cloud_operations.duckdb",
            },
        )
        self.assertEqual(result["ontology"], {"path": "ontology/model.gsf.yaml"})
        self.assertEqual(
            result["documents"],
            {"collection": "aiq-cloud-operations-v1", "path": "documents"},
        )
        self.assertEqual(
            result["prediction"],
            {
                "mode": "reviewed",
                "graph_path": "prediction/graph.json",
                "pql_examples_path": "prediction/pql-examples.json",
            },
        )
        self.assertTrue(result["prediction_contract_exists"])
        self.assertEqual(
            result["evaluation_question_paths"],
            {
                "question_matrix": "questions/matrix.json",
                "stateful_cases": "questions/conversations.json",
            },
        )
        self.assertRegex(result["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            (self.output / "database/cloud_operations.duckdb").read_bytes(),
            b"\0" * 8 + b"DUCK fixture",
        )
        self.assertEqual(
            (self.output / "documents/nested/guide.md").read_text(encoding="utf-8"),
            "# Guide\n",
        )
        self.assertEqual(
            (self.output / "ontology/model.gsf.yaml").read_text(encoding="utf-8"),
            "data_layer:\n  databases: []\n",
        )
        self.assertEqual(
            json.loads((self.output / "prediction/graph.json").read_text()),
            {"schema_version": 1},
        )
        self.assertEqual(
            json.loads((self.output / "prediction/pql-examples.json").read_text()),
            {"schema_version": 1, "examples": []},
        )
        self.assertFalse((self.output / "not-declared.txt").exists())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o750)
        for relative in (
            activation.MANIFEST_NAME,
            "database/cloud_operations.duckdb",
            "ontology/model.gsf.yaml",
            "prediction/graph.json",
            "prediction/pql-examples.json",
        ):
            self.assertEqual(
                stat.S_IMODE((self.output / relative).stat().st_mode), 0o640
            )

    def test_identical_activation_preserves_inode_and_drift_is_replaced(
        self,
    ) -> None:
        first = self.activate()["fingerprint"]
        inode = self.output.stat().st_ino
        second = self.activate()["fingerprint"]
        self.assertEqual(first, second)
        self.assertEqual(inode, self.output.stat().st_ino)

        (self.output / "old.txt").write_text("old\n", encoding="utf-8")
        third = self.activate()["fingerprint"]
        self.assertEqual(first, third)
        self.assertNotEqual(inode, self.output.stat().st_ino)
        self.assertFalse((self.output / "old.txt").exists())
        self.assertFalse(
            any(
                path.name.startswith(".active.")
                for path in self.output.parent.iterdir()
            )
        )

    def test_bundled_sample_uses_the_same_singular_contract(self) -> None:
        generated = self.root / "generated"
        (generated / "service/structured").mkdir(parents=True)
        (generated / "service/documents").mkdir()
        (generated / "service/structured/suppliers.csv").write_text(
            "supplier_id,name\nSUP-001,Northstar\n", encoding="utf-8"
        )
        (generated / "service/documents/notice.md").write_text(
            "# Supplier notice\n", encoding="utf-8"
        )

        manifest = activation.activate_bundled(generated, self.output)
        result = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertEqual(result["id"], "supply-chain")
        self.assertEqual(
            result["database"],
            {
                "engine": "postgres-csv",
                "name": "query_claw",
                "path": "structured",
            },
        )
        self.assertIsNone(result["ontology"])
        self.assertEqual(result["prediction"], {"mode": "native"})
        self.assertTrue(result["prediction_contract_exists"])
        self.assertEqual(
            result["documents"],
            {"collection": "query-claw-supply-chain", "path": "documents"},
        )
        self.assertTrue((self.output / "structured/suppliers.csv").is_file())
        self.assertTrue((self.output / "documents/notice.md").is_file())
        self.assertNotIn("datasets", result)

    def test_supports_structured_only_and_documents_only(self) -> None:
        for mode in ("structured", "documents"):
            with self.subTest(mode=mode):
                value = self.manifest()
                if mode == "structured":
                    value.pop("documents")
                else:
                    value.pop("structured")
                self.write_manifest(value)
                result = self.activate()
                self.assertEqual(result["database"] is not None, mode == "structured")
                self.assertEqual(result["ontology"] is not None, mode == "structured")
                self.assertEqual(result["documents"] is not None, mode == "documents")
                self.assertEqual(result["prediction"] is not None, mode == "structured")
                self.assertEqual(
                    result["prediction_contract_exists"], mode == "structured"
                )

    def test_rejects_duplicate_json_keys(self) -> None:
        self.dataset.write_text(
            '{"schema_version":"1.0","id":"one","id":"two"}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(activation.ActivationError, "duplicate JSON key"):
            self.activate()

    def test_rejects_escaped_output_and_preserves_previous_activation(self) -> None:
        self.output.mkdir()
        (self.output / "sentinel").write_text("keep\n", encoding="utf-8")
        value = self.manifest()
        value["build"]["outputs"][0]["path"] = "../secret.duckdb"
        self.write_manifest(value)
        with self.assertRaisesRegex(activation.ActivationError, "relative POSIX path"):
            self.activate()
        self.assertEqual(
            (self.output / "sentinel").read_text(encoding="utf-8"), "keep\n"
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_symlinked_outputs_and_document_entries(self) -> None:
        outside = Path(self.temporary.name) / "outside.duckdb"
        outside.write_bytes(b"outside")
        database = self.root / "build/data.duckdb"
        database.unlink()
        database.symlink_to(outside)
        with self.assertRaisesRegex(activation.ActivationError, "symlink"):
            self.activate()

        database.unlink()
        database.write_bytes(b"\0" * 8 + b"DUCK fixture")
        (self.root / "corpus/link.md").symlink_to(self.root / "not-declared.txt")
        with self.assertRaisesRegex(activation.ActivationError, "symlink"):
            self.activate()

    def test_rejects_missing_source_contracts_and_bad_output_references(self) -> None:
        variants: list[tuple[str, dict, str]] = []
        no_sources = self.manifest()
        no_sources.pop("structured")
        no_sources.pop("documents")
        variants.append(
            ("no sources", no_sources, "DuckDB database or document corpus")
        )
        missing = self.manifest()
        missing["structured"]["database_output"] = "not-declared"
        variants.append(("undeclared reference", missing, "declared build output"))
        duplicate = self.manifest()
        duplicate["build"]["outputs"].append(duplicate["build"]["outputs"][0])
        variants.append(("duplicate output", duplicate, "duplicate build output id"))
        wrong_format = self.manifest()
        wrong_format["build"]["outputs"][0]["format"] = "sqlite"
        variants.append(("wrong database format", wrong_format, "incompatible"))
        bad_duckdb = self.manifest()
        (self.root / "build/data.duckdb").write_bytes(b"not a duckdb")
        variants.append(("invalid database", bad_duckdb, "DuckDB file"))
        for label, value, message in variants:
            with self.subTest(label=label):
                self.write_manifest(value)
                with self.assertRaisesRegex(activation.ActivationError, message):
                    self.activate()

    def test_evaluation_paths_remain_relative_metadata_and_must_be_contained(
        self,
    ) -> None:
        value = self.manifest()
        value["evaluation"]["question_matrix"] = "/tmp/questions.json"
        self.write_manifest(value)
        with self.assertRaisesRegex(activation.ActivationError, "relative POSIX path"):
            self.activate()

    def test_artifact_content_changes_fingerprint_and_invalid_json_is_rejected(
        self,
    ) -> None:
        first = self.activate()["fingerprint"]
        (self.root / "build/prediction.json").write_text(
            '{"schema_version": 1, "revision": "two"}\n', encoding="utf-8"
        )
        second = self.activate()["fingerprint"]
        self.assertNotEqual(first, second)

        (self.root / "prediction/pql.json").write_text(
            '{"schema_version": 1, "schema_version": 2}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(activation.ActivationError, "duplicate JSON key"):
            self.activate()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_symlinked_ontology_and_prediction_artifacts(self) -> None:
        outside = self.root / "not-declared.txt"
        for relative in (
            "build/model.yaml",
            "build/prediction.json",
            "prediction/pql.json",
        ):
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.unlink()
                path.symlink_to(outside)
                with self.assertRaisesRegex(activation.ActivationError, "symlink"):
                    self.activate()
                path.unlink()
                path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()

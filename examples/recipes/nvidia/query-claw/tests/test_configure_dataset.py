# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))

import configure_dataset as configure  # noqa: E402


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ConfigureDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "active"
        (self.root / "database").mkdir(parents=True)
        (self.root / "ontology").mkdir()
        (self.root / "documents").mkdir()
        (self.root / "prediction").mkdir()
        (self.root / "database/cloud.duckdb").write_bytes(b"database")
        (self.root / "ontology/model.gsf.yaml").write_text(
            "data_layer: {}\n", encoding="utf-8"
        )
        (self.root / "documents/evidence.md").write_text("evidence\n", encoding="utf-8")
        (self.root / "prediction/graph.json").write_text("{}\n", encoding="utf-8")
        self.pql = {
            "schema_version": 1,
            "database_name": "cloud",
            "examples": [
                {
                    "name": "Delay",
                    "description": "Predict delay",
                    "pql": "PREDICT delay",
                }
            ],
        }
        (self.root / "prediction/pql-examples.json").write_text(
            json.dumps(self.pql), encoding="utf-8"
        )
        self.manifest = self.root / "active-dataset.json"
        self.document = {
            "schema_version": 1,
            "id": "cloud-operations",
            "fingerprint": "0" * 64,
            "database": {
                "engine": "duckdb",
                "name": "cloud",
                "path": "database/cloud.duckdb",
            },
            "ontology": {"path": "ontology/model.gsf.yaml"},
            "documents": {"path": "documents", "collection": "cloud-v1"},
            "prediction": {
                "mode": "reviewed",
                "graph_path": "prediction/graph.json",
                "pql_examples_path": "prediction/pql-examples.json",
            },
            "prediction_contract_exists": True,
        }
        self.write_manifest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self) -> None:
        self.manifest.write_text(json.dumps(self.document), encoding="utf-8")

    def test_loads_only_the_normalized_complete_contract(self) -> None:
        active = configure.load_active(self.manifest)
        self.assertEqual(active.database_engine, "duckdb")
        self.assertEqual(active.database_name, "cloud")
        self.assertEqual(active.collection, "cloud-v1")
        self.assertEqual(
            active.database_path, (self.root / "database/cloud.duckdb").resolve()
        )

        self.document["database"]["path"] = "../cloud.duckdb"
        self.write_manifest()
        with self.assertRaisesRegex(
            configure.DatasetConfigurationError, "normalized path"
        ):
            configure.load_active(self.manifest)

    def test_allows_one_declared_capability_without_inventing_the_other(self) -> None:
        self.document["documents"] = None
        self.write_manifest()
        structured = configure.load_active(self.manifest)
        self.assertIsNone(structured.documents_path)
        self.assertEqual(structured.database_name, "cloud")

        self.document.update(
            {
                "database": None,
                "ontology": None,
                "documents": {"path": "documents", "collection": "cloud-v1"},
                "prediction": None,
                "prediction_contract_exists": False,
            }
        )
        self.write_manifest()
        documents = configure.load_active(self.manifest)
        self.assertIsNone(documents.database_path)
        self.assertEqual(documents.collection, "cloud-v1")

    def test_loads_bundled_postgres_csv_with_native_gsf_prediction(self) -> None:
        (self.root / "structured").mkdir()
        (self.root / "structured/suppliers.csv").write_text(
            "supplier_id\nSUP-001\n", encoding="utf-8"
        )
        self.document.update(
            {
                "database": {
                    "engine": "postgres-csv",
                    "name": "query_claw",
                    "path": "structured",
                },
                "ontology": None,
                "prediction": {"mode": "native"},
            }
        )
        self.write_manifest()

        active = configure.load_active(self.manifest)

        self.assertEqual(active.database_engine, "postgres-csv")
        self.assertEqual(active.prediction_mode, "native")
        self.assertIsNone(active.ontology_path)
        self.assertIsNone(active.graph_path)
        self.assertEqual({}, configure.prediction_context(active))

    def test_rejects_unknown_database_engine_and_prediction_mode(self) -> None:
        self.document["database"]["engine"] = "sqlite"
        self.write_manifest()
        with self.assertRaisesRegex(
            configure.DatasetConfigurationError, "database engine"
        ):
            configure.load_active(self.manifest)

        self.document["database"]["engine"] = "duckdb"
        self.document["prediction"]["mode"] = "automatic"
        self.write_manifest()
        with self.assertRaisesRegex(
            configure.DatasetConfigurationError, "prediction mode"
        ):
            configure.load_active(self.manifest)

    def test_cli_reports_prediction_independently_from_structured_data(self) -> None:
        active = configure.load_active(self.manifest)
        self.assertIsNotNone(active.graph_path)

        self.document["prediction"] = None
        self.document["prediction_contract_exists"] = False
        self.write_manifest()
        active = configure.load_active(self.manifest)
        self.assertIsNone(active.graph_path)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_symlinked_activation_artifacts(self) -> None:
        model = self.root / "ontology/model.gsf.yaml"
        model.unlink()
        model.symlink_to(self.root / "documents/evidence.md")
        with self.assertRaisesRegex(configure.DatasetConfigurationError, "symlink"):
            configure.load_active(self.manifest)

    def test_imports_reviewed_ontology_through_the_supported_api(self) -> None:
        requests = []

        def send(request, timeout):
            requests.append((request, timeout))
            return Response(b'{"success":true,"summary":{}}')

        with patch.object(configure, "urlopen", send):
            configure.import_ontology(
                configure.load_active(self.manifest), "http://127.0.0.1:3001"
            )
        request, timeout = requests[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:3001/api/model/import?replace=true&embed=true",
        )
        self.assertEqual(request.data, b"data_layer: {}\n")
        self.assertEqual(timeout, 900)

    def test_seeds_database_scoped_pql(self) -> None:
        requests = []

        def send(request, timeout):
            requests.append((request, timeout))
            return Response(b'{"data":{"id":"created"}}')

        active = configure.load_active(self.manifest)
        with patch.object(configure, "urlopen", send):
            self.assertEqual(configure.seed_pql(active, "http://localhost:3001"), 1)
        request, timeout = requests[0]
        self.assertEqual(request.full_url, "http://localhost:3001/api/pql-analyses")
        self.assertEqual(
            json.loads(request.data),
            {
                "database_name": "cloud",
                "name": "Delay",
                "description": "Predict delay",
                "pql": "PREDICT delay",
            },
        )
        self.assertEqual(timeout, 900)

        self.pql["database_name"] = "wrong"
        (self.root / "prediction/pql-examples.json").write_text(
            json.dumps(self.pql), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            configure.DatasetConfigurationError, "active database"
        ):
            configure.seed_pql(active, "http://127.0.0.1:3001")

    def test_selects_bounded_reviewed_prediction_context(self) -> None:
        scope = {
            "anchor_time": "2026-09-05T12:00:00+00:00",
            "entity_table": "services",
            "entity_column": "service_id",
            "population_view": "reviewed_services",
            "population_column": "service_id",
            "population_rows": 10,
        }
        (self.root / "prediction/graph.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "database_name": "cloud",
                    "prediction_scope": scope,
                }
            ),
            encoding="utf-8",
        )
        context = configure.prediction_context(configure.load_active(self.manifest))
        self.assertEqual("services.service_id", context["scope"]["entity"])
        self.assertEqual(["Delay"], context["targets"])

        (self.root / "prediction/graph.json").write_text(
            json.dumps({"schema_version": 1, "database_name": "cloud"}),
            encoding="utf-8",
        )
        self.assertIsNone(
            configure.prediction_context(configure.load_active(self.manifest))["scope"]
        )


if __name__ == "__main__":
    unittest.main()

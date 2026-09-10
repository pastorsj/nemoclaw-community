# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = EXAMPLE_ROOT / "scripts" / "data_packs.py"
    spec = importlib.util.spec_from_file_location("query_claw_data_packs", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PACKS = _load_module()


def _write_pack(
    root: Path,
    pack_id: str,
    *,
    documents: bool = True,
    predictions: bool = True,
    marker: str = "visible",
) -> Path:
    pack = root / pack_id
    (pack / "structured").mkdir(parents=True)
    (pack / "structured" / "rows.csv").write_text(
        f"id,value\n1,{marker}\n", encoding="utf-8"
    )
    views = {"structured": "structured"}
    bindings = {"ontology": {"database": f"query_claw_{pack_id}"}}
    if documents:
        (pack / "documents").mkdir()
        (pack / "documents" / "brief.md").write_text(marker, encoding="utf-8")
        views["documents"] = "documents"
        bindings["retriever"] = {"collection": f"query-claw-{pack_id}"}
    if predictions:
        (pack / "predictions").mkdir()
        (pack / "predictions" / "entities.csv").write_text(
            "entity_id\n1\n", encoding="utf-8"
        )
        views["predictions"] = "predictions"
        bindings["ontology"]["prediction_database"] = (
            f"query_claw_{pack_id}_predictions"
        )
        bindings["ontology"]["prediction_probe"] = (
            f"Predict a supported outcome for {pack_id}."
        )
    definition = {
        "schema_version": 1,
        "id": pack_id,
        "title": pack_id.replace("-", " ").title(),
        "description": f"The {marker} test pack.",
        "industry": "Manufacturing",
        "views": views,
        "bindings": bindings,
    }
    (pack / "pack.json").write_text(
        json.dumps(definition, indent=2) + "\n", encoding="utf-8"
    )
    return pack


class DataPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="query-claw-packs-")
        self.root = Path(self.temporary.name) / "packs"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_discovers_and_strictly_validates_pack_definitions(self) -> None:
        _write_pack(self.root, "supply-chain")
        registry = PACKS.discover_packs(self.root)
        self.assertEqual(["supply-chain"], list(registry))
        self.assertEqual(
            "query_claw_supply-chain",
            registry["supply-chain"].definition["bindings"]["ontology"]["database"],
        )

        manifest = self.root / "supply-chain" / "pack.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["surprise"] = True
        manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(PACKS.DataPackError, "unknown keys"):
            PACKS.discover_packs(self.root)

    def test_rejects_duplicate_json_keys(self) -> None:
        pack = _write_pack(self.root, "duplicate-json")
        manifest = pack / "pack.json"
        manifest.write_text(
            manifest.read_text(encoding="utf-8")[:-2] + ', "id": "duplicate-json"}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PACKS.DataPackError, "duplicate JSON key"):
            PACKS.discover_packs(self.root)

    def test_predictive_pack_requires_a_bounded_probe(self) -> None:
        pack = _write_pack(self.root, "predictive")
        manifest = pack / "pack.json"
        definition = json.loads(manifest.read_text(encoding="utf-8"))
        definition["bindings"]["ontology"].pop("prediction_probe")
        manifest.write_text(json.dumps(definition), encoding="utf-8")
        with self.assertRaisesRegex(PACKS.DataPackError, "prediction_probe"):
            PACKS.discover_packs(self.root)

    def test_rejects_unsafe_ids_and_containment_traversal(self) -> None:
        pack = _write_pack(self.root, "safe-pack")
        definition = json.loads((pack / "pack.json").read_text(encoding="utf-8"))
        definition["views"]["structured"] = "../outside.csv"
        (pack / "pack.json").write_text(json.dumps(definition), encoding="utf-8")
        (self.root / "outside.csv").write_text("private", encoding="utf-8")
        with self.assertRaisesRegex(PACKS.DataPackError, "contained"):
            PACKS.discover_packs(self.root)

        (pack / "pack.json").unlink()
        unsafe = _write_pack(self.root, "unsafe-id")
        definition = json.loads((unsafe / "pack.json").read_text(encoding="utf-8"))
        definition["id"] = "Unsafe_ID"
        (unsafe / "pack.json").write_text(json.dumps(definition), encoding="utf-8")
        with self.assertRaisesRegex(PACKS.DataPackError, "safe kebab-case"):
            PACKS.discover_packs(self.root)

    def test_rejects_symlinks_in_referenced_content(self) -> None:
        pack = _write_pack(self.root, "linked-pack")
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        (pack / "documents" / "linked.md").symlink_to(outside)
        with self.assertRaisesRegex(PACKS.DataPackError, "symlink"):
            PACKS.discover_packs(self.root)

    def test_ignores_undeclared_content(self) -> None:
        pack = _write_pack(self.root, "focused-pack")
        unreferenced = pack / "operator-notes"
        unreferenced.mkdir()
        (unreferenced / "private.txt").write_text("not active", encoding="utf-8")
        before = PACKS.select_packs(PACKS.discover_packs(self.root), "focused-pack")
        (unreferenced / "private.txt").write_text("changed", encoding="utf-8")
        selected = PACKS.select_packs(PACKS.discover_packs(self.root), "focused-pack")
        self.assertEqual(before.fingerprint, selected.fingerprint)

        output = Path(self.temporary.name) / "active"
        PACKS.materialize_active_packs(selected, output)
        self.assertFalse(
            (output / "packs" / "focused-pack" / "operator-notes").exists()
        )

    def test_rejects_unsafe_service_binding_names(self) -> None:
        pack = _write_pack(self.root, "unsafe-binding")
        manifest = pack / "pack.json"
        original = json.loads(manifest.read_text(encoding="utf-8"))
        cases = (
            ("ontology", "database", "database/name"),
            ("ontology", "prediction_database", "database/name"),
            ("retriever", "collection", "collection/name"),
        )
        for binding, field, value in cases:
            with self.subTest(binding=binding, field=field):
                definition = json.loads(json.dumps(original))
                definition["bindings"][binding][field] = value
                manifest.write_text(json.dumps(definition), encoding="utf-8")
                with self.assertRaisesRegex(PACKS.DataPackError, "service name"):
                    PACKS.discover_packs(self.root)

    def test_allowlist_is_required_unique_safe_and_known(self) -> None:
        _write_pack(self.root, "alpha")
        registry = PACKS.discover_packs(self.root)
        for value in ("", "alpha,", "alpha,alpha", "Unsafe_ID"):
            with self.subTest(value=value), self.assertRaises(PACKS.DataPackError):
                PACKS.select_packs(registry, value)
        with self.assertRaisesRegex(PACKS.DataPackError, "unavailable"):
            PACKS.select_packs(registry, "missing")
        selected = PACKS.select_packs(registry, environ={PACKS.DATASETS_ENV: "alpha"})
        self.assertEqual(("alpha",), selected.ids)

    def test_selection_and_fingerprint_are_order_independent(self) -> None:
        _write_pack(self.root, "alpha")
        beta = _write_pack(self.root, "beta")
        registry = PACKS.discover_packs(self.root)
        first = PACKS.select_packs(registry, "beta,alpha")
        second = PACKS.select_packs(registry, "alpha,beta")
        self.assertEqual(("alpha", "beta"), first.ids)
        self.assertEqual(first.fingerprint, second.fingerprint)

        (beta / "documents" / "brief.md").write_text("changed", encoding="utf-8")
        changed = PACKS.select_packs(PACKS.discover_packs(self.root), "alpha,beta")
        self.assertNotEqual(first.fingerprint, changed.fingerprint)

    def test_selection_rejects_cross_dataset_binding_collisions(self) -> None:
        alpha = _write_pack(self.root, "alpha")
        beta = _write_pack(self.root, "beta")
        alpha_definition = json.loads((alpha / "pack.json").read_text())
        beta_definition = json.loads((beta / "pack.json").read_text())
        for kind, field in (("ontology", "database"), ("retriever", "collection")):
            with self.subTest(binding=kind):
                modified = json.loads(json.dumps(beta_definition))
                modified["bindings"][kind][field] = alpha_definition["bindings"][kind][
                    field
                ]
                (beta / "pack.json").write_text(json.dumps(modified), encoding="utf-8")
                with self.assertRaisesRegex(PACKS.DataPackError, "share"):
                    PACKS.select_packs(PACKS.discover_packs(self.root), "alpha,beta")
                (beta / "pack.json").write_text(
                    json.dumps(beta_definition), encoding="utf-8"
                )

    def test_unselected_content_does_not_affect_fingerprint(self) -> None:
        _write_pack(self.root, "active")
        hidden = _write_pack(self.root, "hidden")
        before = PACKS.select_packs(PACKS.discover_packs(self.root), "active")
        (hidden / "documents" / "brief.md").write_text("changed", encoding="utf-8")
        after = PACKS.select_packs(PACKS.discover_packs(self.root), "active")
        self.assertEqual(before.fingerprint, after.fingerprint)

    def test_materialization_contains_only_active_pack_and_resolvable_views(
        self,
    ) -> None:
        _write_pack(self.root, "active", marker="public-marker")
        _write_pack(self.root, "hidden", marker="do-not-disclose")
        selected = PACKS.select_packs(PACKS.discover_packs(self.root), "active")
        output = Path(self.temporary.name) / "runtime"
        manifest = PACKS.materialize_active_packs(selected, output)
        text = manifest.read_text(encoding="utf-8")
        value = json.loads(text)
        self.assertEqual(["active"], [item["id"] for item in value["datasets"]])
        self.assertNotIn("source_root", value["datasets"][0])
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("do-not-disclose", text)
        self.assertTrue((output / "packs" / "active" / "pack.json").is_file())
        self.assertFalse((output / "packs" / "hidden").exists())
        for path in value["datasets"][0]["views"].values():
            self.assertTrue((output / path).exists())

        (output / "stale.txt").write_text("stale", encoding="utf-8")
        PACKS.materialize_active_packs(selected, output)
        self.assertFalse((output / "stale.txt").exists())

        switched = PACKS.select_packs(PACKS.discover_packs(self.root), "hidden")
        PACKS.materialize_active_packs(switched, output)
        self.assertFalse((output / "packs" / "active").exists())
        self.assertTrue((output / "packs" / "hidden" / "pack.json").is_file())

    def test_materialization_rejects_preexisting_output_symlinks(self) -> None:
        _write_pack(self.root, "active")
        selected = PACKS.select_packs(PACKS.discover_packs(self.root), "active")
        output = Path(self.temporary.name) / "runtime"
        output.mkdir()
        (output / "unsafe").symlink_to(self.root / "active", target_is_directory=True)
        with self.assertRaisesRegex(PACKS.DataPackError, "symlinks"):
            PACKS.materialize_active_packs(selected, output)

    def test_external_mode_materializes_preprovisioned_structured_pack(self) -> None:
        _write_pack(self.root, "external")
        output = Path(self.temporary.name) / "active"
        subprocess.run(
            [
                sys.executable,
                str(EXAMPLE_ROOT / "scripts" / "prepare_data_packs.py"),
                "--no-install-built-in",
                "--packs-root",
                str(self.root),
                "--active-dir",
                str(output),
                "--datasets",
                "external",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        manifest = json.loads(
            (output / PACKS.MANIFEST_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(["external"], [item["id"] for item in manifest["datasets"]])
        self.assertFalse((self.root / "supply-chain").exists())

    def test_prepare_cli_rejects_symlink_active_directory(self) -> None:
        _write_pack(self.root, "external")
        target = Path(self.temporary.name) / "active-target"
        target.mkdir()
        (target / "keep.txt").write_text("unchanged", encoding="utf-8")
        link = Path(self.temporary.name) / "active-link"
        link.symlink_to(target, target_is_directory=True)

        result = subprocess.run(
            [
                sys.executable,
                str(EXAMPLE_ROOT / "scripts" / "prepare_data_packs.py"),
                "--no-install-built-in",
                "--packs-root",
                str(self.root),
                "--active-dir",
                str(link),
                "--datasets",
                "external",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--active-dir must not be a symlink", result.stderr)
        self.assertEqual("unchanged", (target / "keep.txt").read_text())

    def test_generate_cli_rejects_symlink_output_directory(self) -> None:
        target = Path(self.temporary.name) / "generated-target"
        target.mkdir()
        (target / "keep.txt").write_text("unchanged", encoding="utf-8")
        link = Path(self.temporary.name) / "generated-link"
        link.symlink_to(target, target_is_directory=True)

        result = subprocess.run(
            [
                sys.executable,
                str(EXAMPLE_ROOT / "scripts" / "generate_data.py"),
                "--output",
                str(link),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--output must not be a symlink", result.stderr)
        self.assertEqual("unchanged", (target / "keep.txt").read_text())


if __name__ == "__main__":
    unittest.main()

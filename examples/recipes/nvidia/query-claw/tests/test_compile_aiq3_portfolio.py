# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


COMPILER = _load_module(
    "query_claw_compile_aiq3_portfolio",
    EXAMPLE_ROOT / "evaluations" / "compile_aiq3_portfolio.py",
)
RESPONSES_RUNNER = _load_module(
    "query_claw_run_compiled_aiq3",
    EXAMPLE_ROOT / "evaluations" / "run.py",
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _portable_task(task_id: str, structured: str, documents: str, index: int) -> dict:
    contracts = (
        ("structured_analytic", [structured]),
        ("structured_predictive", [structured]),
        ("document_research", [documents]),
        ("hybrid", [structured, documents]),
        ("clarification", []),
    )
    profile, sources = contracts[index % len(contracts)]
    return {
        "id": task_id,
        "cohort": f"cohort-{index % 3}",
        "question": f"Canonical question {task_id}?",
        "source_ids": sources,
        "profile": profile,
        "evidence": {"fixture": task_id},
    }


def _write_portfolio(root: Path) -> None:
    industry_paths: list[str] = []
    for industry_index in range(9):
        industry_id = f"industry-{industry_index}"
        dataset_id = f"dataset-{industry_index}"
        structured = f"industry_{industry_index}_structured"
        documents = f"industry_{industry_index}_documents"
        industry_path = f"industries/{industry_id}/industry.v1.json"
        dataset_path = f"industries/{industry_id}/dataset.v1.json"
        questions_path = f"industries/{industry_id}/questions.v1.json"
        task_count = 33 if industry_index == 0 else 30
        tasks = [
            _portable_task(
                f"task-{industry_index}-{task_index}",
                structured,
                documents,
                task_index,
            )
            for task_index in range(task_count)
        ]

        if industry_index == 0:
            first = tasks[0]
            tasks[0] = {
                "id": first["id"],
                "cohort": first["cohort"],
                "sources": first["source_ids"],
                "questions": [first["question"], "An alternate authored wording?"],
                "expected": {
                    "profile": first["profile"],
                    "evidence": first["evidence"],
                },
            }
            dataset = {
                "schema_version": "1.0",
                "id": dataset_id,
                "name": f"Dataset {industry_index}",
                "source_ids": [structured, documents],
                "authored_assets": {"prediction_population": "fixture.json"},
            }
            questions = {
                "schema_version": "1.0",
                "industry_id": industry_id,
                "tasks": tasks,
            }
        else:
            dataset = {
                "schema_version": "1.0",
                "id": dataset_id,
                "name": f"Dataset {industry_index}",
                "structured": {"source_id": structured, "prediction": {}},
                "documents": {"source_id": documents},
            }
            questions = {
                "schema_version": "1.0",
                "pack_id": dataset_id,
                "tasks": tasks,
            }

        _write_json(root / dataset_path, dataset)
        _write_json(root / questions_path, questions)
        _write_json(
            root / industry_path,
            {
                "schema_version": "1.0",
                "id": industry_id,
                "name": f"Industry {industry_index}",
                "source_ids": [structured, documents],
                "datasets": [{"id": dataset_id, "manifest": dataset_path}],
                "questions": {"standalone": questions_path},
            },
        )
        industry_paths.append(industry_path)
    _write_json(
        root / "industries" / "portfolio.v1.json",
        {"schema_version": "1.0", "industries": industry_paths},
    )


class CompileAiq3PortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="query-claw-aiq3-")
        self.root = Path(self.temporary.name) / "aiq3"
        self.output = Path(self.temporary.name) / "compiled"
        _write_portfolio(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_compiles_all_canonical_cases_and_sidecar_contracts(self) -> None:
        with mock.patch.object(
            COMPILER, "_repository_commit", return_value="c" * 40
        ):
            index = COMPILER.compile_portfolio(self.root, self.output)

        self.assertEqual(
            {"industries": 9, "datasets": 9, "cases": 273},
            {key: index["totals"][key] for key in ("industries", "datasets", "cases")},
        )
        self.assertEqual(274, index["totals"]["authored_variants"])
        self.assertEqual("c" * 40, index["source"]["repository_commit"])
        self.assertEqual(9, len(index["suites"]))
        self.assertEqual(0o700, stat.S_IMODE(self.output.stat().st_mode))
        self.assertTrue(
            all(
                stat.S_IMODE(path.stat().st_mode) == 0o600
                for path in self.output.iterdir()
                if path.is_file()
            )
        )

        first = index["suites"][0]["case_contracts"][0]
        self.assertEqual("task-0-0", first["id"])
        self.assertEqual(["industry_0_structured"], first["source_ids"])
        self.assertEqual("structured_analytic", first["profile"])
        self.assertEqual("cohort-0", first["cohort"])
        self.assertEqual(2, first["authored_variants"])
        self.assertEqual({"fixture": "task-0-0"}, first["evidence"])

        contracts = RESPONSES_RUNNER.load_suite_contract(
            self.output / "index.json", index["suites"][0]["suite"]
        ).cases
        self.assertEqual({"fixture": "task-0-0"}, contracts["task-0-0"].evidence)

        suite = json.loads((self.output / index["suites"][0]["suite"]).read_text())
        self.assertEqual("Canonical question task-0-0?", suite["cases"][0]["prompt"])
        predictive = suite["cases"][1]
        self.assertEqual(["ontology"], predictive["expected"]["routes"])
        predictive_contract = index["suites"][0]["case_contracts"][1]
        self.assertEqual(
            ["gsf_kumo_structured_prediction"],
            predictive_contract["expected_capabilities"],
        )
        hybrid = suite["cases"][3]
        self.assertEqual(["ontology", "retriever"], hybrid["expected"]["routes"])

    def test_every_generated_suite_matches_its_runner_contract(self) -> None:
        index = COMPILER.compile_portfolio(self.root, self.output)

        loaded = 0
        for suite in index["suites"]:
            payload = json.loads((self.output / suite["suite"]).read_text())
            self.assertEqual(3, payload["schema_version"])
            self.assertEqual(Path(suite["suite"]).stem, payload["name"])
            cases = payload["cases"]
            contracts = RESPONSES_RUNNER.load_suite_contract(
                self.output / "index.json", suite["suite"]
            ).cases
            self.assertEqual(
                {case["id"] for case in cases},
                set(contracts),
            )
            loaded += len(cases)
        self.assertEqual(273, loaded)

    def test_rejects_unknown_profile_without_writing_an_index(self) -> None:
        path = self.root / "industries" / "industry-1" / "questions.v1.json"
        questions = json.loads(path.read_text(encoding="utf-8"))
        questions["tasks"][0]["profile"] = "invented_route"
        _write_json(path, questions)

        with self.assertRaisesRegex(COMPILER.CompileError, "unsupported profile"):
            COMPILER.compile_portfolio(self.root, self.output)
        self.assertFalse((self.output / "index.json").exists())

    def test_resolves_reviewed_prediction_population_into_private_index(self) -> None:
        population_path = self.root / "industries" / "industry-1" / "population.json"
        _write_json(
            population_path,
            {
                "database_name": "database-1",
                "entity_type": "service",
                "entity_ids": ["service-a", "service-b"],
            },
        )
        questions_path = self.root / "industries" / "industry-1" / "questions.v1.json"
        questions = json.loads(questions_path.read_text(encoding="utf-8"))
        questions["tasks"][0]["evidence"]["prediction_result_contract"] = {
            "database_name": "database-1",
            "entity_type": "service",
            "population_cardinality": 2,
            "eligible_population_ref": (
                "industries/industry-1/population.json#/entity_ids"
            ),
        }
        _write_json(questions_path, questions)

        index = COMPILER.compile_portfolio(self.root, self.output)

        contract = index["suites"][1]["case_contracts"][0]
        self.assertEqual(["service-a", "service-b"], contract["prediction_population"])

    def test_requires_the_complete_nine_industry_inventory(self) -> None:
        path = self.root / "industries" / "portfolio.v1.json"
        portfolio = json.loads(path.read_text(encoding="utf-8"))
        portfolio["industries"].pop()
        _write_json(path, portfolio)

        with self.assertRaisesRegex(COMPILER.CompileError, "9 industries"):
            COMPILER.compile_portfolio(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_a_nonempty_output_directory(self) -> None:
        self.output.mkdir()
        (self.output / "keep.txt").write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(COMPILER.CompileError, "new or empty"):
            COMPILER.compile_portfolio(self.root, self.output)
        self.assertEqual("keep", (self.output / "keep.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

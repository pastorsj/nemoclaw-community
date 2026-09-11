#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compile an external AIQ3 industry portfolio into Query Claw v3 suites."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


EXPECTED_INDUSTRIES = 9
EXPECTED_CASES = 273
QUERY_CLAW_ROUTES = frozenset({"ontology", "retriever"})
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
KEBAB_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

GSF_STRUCTURED = "gsf_structured_retrieval"
GSF_KUMO_PREDICTIVE = "gsf_kumo_structured_prediction"
NEMO_RETRIEVER = "nemo_retriever_unstructured_retrieval"


@dataclass(frozen=True)
class ProfileContract:
    routes: tuple[str, ...]
    capabilities: tuple[str, ...]
    response: tuple[str, ...] = ("answer",)


PROFILE_CONTRACTS = {
    "source_help": ProfileContract((), ()),
    "inline_meta": ProfileContract((), ()),
    "clarification": ProfileContract((), (), ("clarification",)),
    "no_sources_unavailable": ProfileContract((), (), ("abstention",)),
    "documents_only_structured_mismatch": ProfileContract((), (), ("abstention",)),
    "structured_only_document_mismatch": ProfileContract((), (), ("abstention",)),
    "structured_analytic": ProfileContract(("ontology",), (GSF_STRUCTURED,)),
    "multi_structured_analytic": ProfileContract(("ontology",), (GSF_STRUCTURED,)),
    "structured_analytic_optional_documents": ProfileContract(
        ("ontology",), (GSF_STRUCTURED,)
    ),
    "structured_predictive": ProfileContract(("ontology",), (GSF_KUMO_PREDICTIVE,)),
    "document_research": ProfileContract(("retriever",), (NEMO_RETRIEVER,)),
    "deep_document_research": ProfileContract(("retriever",), (NEMO_RETRIEVER,)),
    "multi_document_research": ProfileContract(("retriever",), (NEMO_RETRIEVER,)),
    "hybrid": ProfileContract(
        ("ontology", "retriever"), (GSF_STRUCTURED, NEMO_RETRIEVER)
    ),
    "analytic_predictive": ProfileContract(
        ("ontology",), (GSF_STRUCTURED, GSF_KUMO_PREDICTIVE)
    ),
    "predictive_document": ProfileContract(
        ("ontology", "retriever"), (GSF_KUMO_PREDICTIVE, NEMO_RETRIEVER)
    ),
    "all_evidence_paths": ProfileContract(
        ("ontology", "retriever"),
        (GSF_STRUCTURED, GSF_KUMO_PREDICTIVE, NEMO_RETRIEVER),
    ),
}


class CompileError(ValueError):
    """The external portfolio does not satisfy the expected contract."""


def _repository_commit(root: Path) -> str | None:
    """Return the source checkout commit when ``root`` belongs to Git."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", commit) else None


def _write_private_json(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CompileError(f"could not read JSON file {path}: {error}") from error
    if not isinstance(value, dict):
        raise CompileError(f"{path} must contain a JSON object")
    return value


def _source_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CompileError(f"{label} must be a non-empty relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in value
    ):
        raise CompileError(f"{label} must be a contained relative POSIX path")
    root = root.resolve(strict=True)
    candidate = root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise CompileError(f"{label} must resolve inside --aiq3-root") from error
    if not resolved.is_file():
        raise CompileError(f"{label} must name a regular file")
    return resolved


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompileError(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise CompileError(f"{label} must be a list of unique non-empty strings")
    return value


def _source_views(
    dataset: dict[str, Any], industry_sources: list[str], label: str
) -> dict[str, tuple[str, ...]]:
    mapping: dict[str, tuple[str, ...]] = {}
    structured = dataset.get("structured")
    documents = dataset.get("documents")
    if structured is not None or documents is not None:
        if isinstance(structured, dict):
            source_id = _text(
                structured.get("source_id"), f"{label}.structured.source_id"
            )
            views = ["records"]
            if isinstance(structured.get("prediction"), dict):
                views.append("predictions")
            mapping[source_id] = tuple(views)
        if isinstance(documents, dict):
            source_id = _text(
                documents.get("source_id"), f"{label}.documents.source_id"
            )
            mapping[source_id] = ("documents",)
    else:
        legacy_sources = _string_list(dataset.get("source_ids"), f"{label}.source_ids")
        authored_assets = dataset.get("authored_assets", {})
        if not isinstance(authored_assets, dict):
            raise CompileError(f"{label}.authored_assets must be an object")
        supports_prediction = any(
            key.startswith("prediction") or key == "pql_examples"
            for key in authored_assets
        )
        for source_id in legacy_sources:
            if source_id.endswith("_documents"):
                mapping[source_id] = ("documents",)
            elif source_id.endswith("_structured"):
                mapping[source_id] = (
                    ("records", "predictions")
                    if supports_prediction
                    else ("records",)
                )
            else:
                raise CompileError(
                    f"{label} legacy source {source_id!r} does not identify its view"
                )
    if set(mapping) != set(industry_sources):
        raise CompileError(f"{label} source IDs do not match its industry inventory")
    return mapping


def _task_fields(
    task: Any, label: str
) -> tuple[str, str, list[str], str, str, dict[str, Any], int]:
    if not isinstance(task, dict):
        raise CompileError(f"{label} must be an object")
    task_id = _text(task.get("id"), f"{label}.id")
    if not ID_PATTERN.fullmatch(task_id):
        raise CompileError(f"{label}.id is not compatible with Query Claw")
    cohort = _text(task.get("cohort"), f"{label}.cohort")

    if "question" in task:
        prompt = _text(task.get("question"), f"{label}.question")
        source_ids = _string_list(task.get("source_ids"), f"{label}.source_ids")
        profile = _text(task.get("profile"), f"{label}.profile")
        evidence = task.get("evidence", {})
        authored_variants = 1
    else:
        questions = _string_list(task.get("questions"), f"{label}.questions")
        prompt = questions[0]
        source_ids = _string_list(task.get("sources"), f"{label}.sources")
        expected = task.get("expected")
        if not isinstance(expected, dict):
            raise CompileError(f"{label}.expected must be an object")
        profile = _text(expected.get("profile"), f"{label}.expected.profile")
        evidence = expected.get("evidence", {})
        authored_variants = len(questions)

    if profile not in PROFILE_CONTRACTS:
        raise CompileError(f"{label} uses unsupported profile {profile!r}")
    if not isinstance(evidence, dict):
        raise CompileError(f"{label} evidence must be an object")
    return task_id, prompt, source_ids, profile, cohort, evidence, authored_variants


def _prediction_population(
    root: Path, evidence: dict[str, Any], label: str
) -> list[str | int] | None:
    """Resolve the private reviewed population used by a prediction contract."""

    contract = evidence.get("prediction_result_contract")
    if contract is None:
        return None
    if not isinstance(contract, dict):
        raise CompileError(f"{label}.prediction_result_contract must be an object")
    reference = contract.get("eligible_population_ref")
    if not isinstance(reference, str):
        raise CompileError(
            f"{label}.prediction_result_contract.eligible_population_ref must be a string"
        )
    relative, separator, pointer = reference.partition("#")
    if separator != "#" or pointer != "/entity_ids":
        raise CompileError(
            f"{label}.prediction_result_contract.eligible_population_ref must select /entity_ids"
        )
    descriptor = _load_json(
        _source_path(root, relative, f"{label}.prediction_result_contract population")
    )
    population = descriptor.get("entity_ids")
    if (
        descriptor.get("database_name") != contract.get("database_name")
        or descriptor.get("entity_type") != contract.get("entity_type")
        or not isinstance(population, list)
        or not population
        or any(
            isinstance(value, bool)
            or not isinstance(value, (str, int))
            or (isinstance(value, str) and not value)
            for value in population
        )
        or len(population) != len(set(population))
        or any(type(value) is not type(population[0]) for value in population[1:])
        or contract.get("population_cardinality") != len(population)
    ):
        raise CompileError(
            f"{label}.prediction_result_contract population does not match its contract"
        )
    return population


def _case(
    *,
    task_id: str,
    prompt: str,
    source_ids: list[str],
    source_views: dict[str, tuple[str, ...]],
    dataset_id: str,
    profile: str,
    cohort: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    unknown_sources = set(source_ids) - set(source_views)
    if unknown_sources:
        raise CompileError(
            f"task {task_id!r} selects unknown sources: {', '.join(sorted(unknown_sources))}"
        )
    views = sorted({view for source in source_ids for view in source_views[source]})
    contract = PROFILE_CONTRACTS[profile]
    available_routes = {
        "ontology" for view in views if view in {"records", "predictions"}
    } | ({"retriever"} if "documents" in views else set())
    if not set(contract.routes) <= available_routes:
        raise CompileError(f"task {task_id!r} cannot satisfy profile {profile!r}")

    case = {
        "schema_version": 3,
        "id": task_id,
        "origin_id": task_id,
        "cohort": cohort,
        "session": "",
        "turn": 1,
        "prompt": prompt,
        "sources": source_ids,
        "datasets": ([{"id": dataset_id, "views": views}] if views else []),
        "expected": {
            "routes": list(contract.routes),
            "forbidden_routes": sorted(QUERY_CLAW_ROUTES - set(contract.routes)),
            "route_only": True,
            "response": list(contract.response),
        },
    }
    metadata = {
        "id": task_id,
        "source_ids": source_ids,
        "profile": profile,
        "cohort": cohort,
        "expected_capabilities": list(contract.capabilities),
    }
    return case, metadata


def compile_portfolio(aiq3_root: Path, output_dir: Path) -> dict[str, Any]:
    """Compile exactly one canonical question per AIQ3 industry task."""

    root = aiq3_root.resolve(strict=True)
    portfolio_path = root / "industries" / "portfolio.v1.json"
    portfolio = _load_json(portfolio_path)
    industry_refs = portfolio.get("industries")
    if portfolio.get("schema_version") != "1.0" or not isinstance(industry_refs, list):
        raise CompileError("industries/portfolio.v1.json is invalid")
    if len(industry_refs) != EXPECTED_INDUSTRIES:
        raise CompileError(
            f"portfolio must contain {EXPECTED_INDUSTRIES} industries, got {len(industry_refs)}"
        )
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise CompileError("--output-dir must be new or empty")

    suites: list[dict[str, Any]] = []
    suite_payloads: list[tuple[str, dict[str, Any]]] = []
    seen_industries: set[str] = set()
    seen_datasets: set[str] = set()
    seen_tasks: set[str] = set()
    total_cases = 0
    for industry_ref in industry_refs:
        industry_path = _source_path(root, industry_ref, "portfolio industry")
        industry = _load_json(industry_path)
        industry_id = _text(industry.get("id"), f"{industry_path}.id")
        if not KEBAB_PATTERN.fullmatch(industry_id) or industry_id in seen_industries:
            raise CompileError(f"invalid or duplicate industry ID {industry_id!r}")
        seen_industries.add(industry_id)
        industry_name = _text(industry.get("name"), f"{industry_path}.name")
        industry_sources = _string_list(
            industry.get("source_ids"), f"{industry_path}.source_ids"
        )
        datasets = industry.get("datasets")
        if (
            not isinstance(datasets, list)
            or len(datasets) != 1
            or not isinstance(datasets[0], dict)
        ):
            raise CompileError(f"{industry_path} must declare exactly one dataset")
        dataset_ref = datasets[0]
        dataset_id = _text(dataset_ref.get("id"), f"{industry_path}.datasets[0].id")
        if not KEBAB_PATTERN.fullmatch(dataset_id) or dataset_id in seen_datasets:
            raise CompileError(f"invalid or duplicate dataset ID {dataset_id!r}")
        seen_datasets.add(dataset_id)
        dataset_path = _source_path(
            root, dataset_ref.get("manifest"), f"{industry_path}.datasets[0].manifest"
        )
        dataset = _load_json(dataset_path)
        if dataset.get("id") != dataset_id:
            raise CompileError(
                f"{dataset_path} ID does not match its industry inventory"
            )
        source_views = _source_views(dataset, industry_sources, str(dataset_path))

        questions = industry.get("questions")
        if not isinstance(questions, dict):
            raise CompileError(f"{industry_path}.questions must be an object")
        question_path = _source_path(
            root, questions.get("standalone"), f"{industry_path}.questions.standalone"
        )
        inventory = _load_json(question_path)
        if inventory.get("schema_version") != "1.0":
            raise CompileError(f"{question_path} has an unsupported schema version")
        owner = inventory.get("pack_id", inventory.get("industry_id"))
        if owner not in {dataset_id, industry_id}:
            raise CompileError(
                f"{question_path} does not identify its owning dataset or industry"
            )
        tasks = inventory.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise CompileError(f"{question_path}.tasks must be non-empty")

        cases: list[dict[str, Any]] = []
        case_contracts: list[dict[str, Any]] = []
        profile_counts: Counter[str] = Counter()
        capability_counts: Counter[str] = Counter()
        authored_variant_count = 0
        for index, task in enumerate(tasks):
            (
                task_id,
                prompt,
                source_ids,
                profile,
                cohort,
                evidence,
                authored_variants,
            ) = _task_fields(task, f"{question_path}.tasks[{index}]")
            if task_id in seen_tasks:
                raise CompileError(f"duplicate task ID {task_id!r}")
            seen_tasks.add(task_id)
            case, metadata = _case(
                task_id=task_id,
                prompt=prompt,
                source_ids=source_ids,
                source_views=source_views,
                dataset_id=dataset_id,
                profile=profile,
                cohort=cohort,
            )
            metadata["authored_variants"] = authored_variants
            metadata["evidence"] = evidence
            prediction_population = _prediction_population(
                root, evidence, f"{question_path}.tasks[{index}].evidence"
            )
            if prediction_population is not None:
                metadata["prediction_population"] = prediction_population
            cases.append(case)
            case_contracts.append(metadata)
            profile_counts[profile] += 1
            capability_counts.update(PROFILE_CONTRACTS[profile].capabilities)
            authored_variant_count += authored_variants

        suite_name = f"aiq3-{industry_id}-{dataset_id}"
        suite_file = f"{suite_name}.json"
        suite = {"schema_version": 3, "name": suite_name, "cases": cases}
        suite_payloads.append((suite_file, suite))
        suites.append(
            {
                "industry_id": industry_id,
                "industry_name": industry_name,
                "dataset_id": dataset_id,
                "dataset_name": _text(dataset.get("name"), f"{dataset_path}.name"),
                "suite": suite_file,
                "cases": len(cases),
                "authored_variants": authored_variant_count,
                "source_ids": industry_sources,
                "profiles": dict(sorted(profile_counts.items())),
                "expected_capabilities": dict(sorted(capability_counts.items())),
                "case_contracts": case_contracts,
            }
        )
        total_cases += len(cases)

    if total_cases != EXPECTED_CASES:
        raise CompileError(
            f"portfolio must contain {EXPECTED_CASES} standalone tasks, got {total_cases}"
        )

    index = {
        "schema_version": 1,
        "source": {
            "portfolio": "industries/portfolio.v1.json",
            "portfolio_sha256": hashlib.sha256(portfolio_path.read_bytes()).hexdigest(),
            "repository_commit": _repository_commit(root),
            "selection": "first authored wording for each canonical industry task",
        },
        "totals": {
            "industries": len(seen_industries),
            "datasets": len(seen_datasets),
            "cases": total_cases,
            "authored_variants": sum(item["authored_variants"] for item in suites),
        },
        "capability_vocabulary": [GSF_STRUCTURED, GSF_KUMO_PREDICTIVE, NEMO_RETRIEVER],
        "profile_contracts": {
            profile: {
                "routes": list(contract.routes),
                "expected_capabilities": list(contract.capabilities),
                "response": list(contract.response),
            }
            for profile, contract in sorted(PROFILE_CONTRACTS.items())
        },
        "suites": suites,
    }
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    for suite_file, suite in suite_payloads:
        _write_private_json(output_dir / suite_file, suite)
    _write_private_json(output_dir / "index.json", index)
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aiq3-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        index = compile_portfolio(args.aiq3_root, args.output_dir)
    except CompileError as error:
        parser.error(str(error))
    print(
        f"Compiled {index['totals']['cases']} cases across "
        f"{index['totals']['industries']} industry suites into {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

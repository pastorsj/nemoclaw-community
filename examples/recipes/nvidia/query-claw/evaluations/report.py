#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aggregate judged Query Claw JSONL into complete JSON and Markdown reports."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


RUBRIC_ID = "enterprise_response_usefulness.v3"
RUBRIC_SHA256 = "09af73cf74293611992cbbbcbb8f463e26a0e273fc52b263c43b2f71882b0d5b"
RUN_STATUSES = frozenset({"pass", "fail"})
JUDGE_STATUSES = frozenset({"evaluated", "unavailable", "not_run", "not_observable"})
VERDICTS = frozenset({"usable", "degraded", "unusable"})
MAX_RESULTS = 1_000
AIQ3_STANDALONE_TASKS = 281
AIQ3_INDUSTRY_BOUND_TASKS = 273
CAPABILITY_NAMES = {
    "gsf_kumo_structured_prediction": "GSF / Kumo prediction",
    "gsf_structured_retrieval": "GSF structured retrieval",
    "nemo_retriever_unstructured_retrieval": "NeMo Retriever",
}
CAPABILITIES = frozenset(CAPABILITY_NAMES)


class ReportError(ValueError):
    """One or more judged results cannot form a trustworthy report."""


def _capability_set(
    record: Mapping[str, Any], key: str, label: str
) -> frozenset[str]:
    value = record.get(key)
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or item not in CAPABILITIES for item in value)
        or len(value) != len(set(value))
    ):
        raise ReportError(f"{label} {key} must be a unique list of known capabilities")
    return frozenset(value)


def _capability_route(
    record: Mapping[str, Any], label: str
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    expected, attempted, observed = (
        _capability_set(record, key, label)
        for key in (
            "expected_capabilities",
            "attempted_capabilities",
            "observed_capabilities",
        )
    )
    if not observed <= attempted:
        raise ReportError(f"{label} has observed capabilities without attempts")
    return expected, attempted, observed


def _required_text(record: Mapping[str, Any], key: str, label: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ReportError(f"{label} has no valid {key}")
    return value


def load_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Load and validate judged records, rejecting duplicate or mixed identities."""

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    dataset_owners: dict[str, str] = {}
    shared_provenance: tuple[Any, ...] | None = None
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ReportError(f"could not read {path}") from exc
        for line_number, line in enumerate(lines, 1):
            label = f"{path}:{line_number}"
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReportError(f"{label} is not valid JSON") from exc
            if not isinstance(record, dict):
                raise ReportError(f"{label} must be a JSON object")
            case_id = _required_text(record, "case_id", label)
            industry_id = _required_text(record, "industry_id", label)
            dataset_id = _required_text(record, "dataset_id", label)
            for key in ("industry_name", "dataset_name", "question"):
                _required_text(record, key, label)
            if not isinstance(record.get("answer"), str):
                raise ReportError(f"{label} has no valid answer")
            _capability_route(record, label)
            if case_id in seen:
                raise ReportError(f"duplicate case_id {case_id!r}")
            owner = dataset_owners.setdefault(dataset_id, industry_id)
            if owner != industry_id:
                raise ReportError(
                    f"dataset {dataset_id!r} appears in multiple industries"
                )
            status = record.get("status")
            semantic = record.get("semantic_judge")
            if status not in RUN_STATUSES or not isinstance(semantic, dict):
                raise ReportError(f"{label} has invalid run or judge status")
            judge_status = semantic.get("status")
            if judge_status not in JUDGE_STATUSES:
                raise ReportError(f"{label} has invalid semantic judge status")
            verdict = semantic.get("verdict")
            if judge_status == "evaluated" and verdict not in VERDICTS:
                raise ReportError(f"{label} has no valid evaluated verdict")
            if judge_status != "evaluated" and verdict is not None:
                raise ReportError(f"{label} has a verdict without an evaluated judgment")
            provenance = record.get("judge_provenance")
            if (
                not isinstance(provenance, dict)
                or provenance.get("rubric_id") != RUBRIC_ID
                or provenance.get("rubric_sha256") != RUBRIC_SHA256
            ):
                raise ReportError(f"{label} was not judged with {RUBRIC_ID}")
            identity = tuple(
                provenance.get(key)
                for key in ("protocol", "model", "rubric_id", "rubric_sha256")
            )
            if shared_provenance is None:
                shared_provenance = identity
            elif shared_provenance != identity:
                raise ReportError("judge provenance differs across inputs")
            seen.add(case_id)
            records.append(record)
            if len(records) > MAX_RESULTS:
                raise ReportError(f"report exceeds {MAX_RESULTS} results")
    if not records:
        raise ReportError("at least one judged result is required")
    return records


def validate_coverage(
    records: Iterable[Mapping[str, Any]], index_path: Path, *, allow_partial: bool = False
) -> dict[str, Any]:
    """Require exactly the canonical compiled portfolio unless explicitly partial."""

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportError(f"could not read compiled index {index_path}") from exc
    if not isinstance(index, dict):
        raise ReportError("compiled portfolio index is invalid")
    suites = index.get("suites")
    if index.get("schema_version") != 1 or not isinstance(suites, list):
        raise ReportError("compiled portfolio index is invalid")
    source = index.get("source")
    if not isinstance(source, dict):
        raise ReportError("compiled portfolio source provenance is invalid")
    portfolio = _required_text(source, "portfolio", "compiled portfolio source")
    selection = _required_text(source, "selection", "compiled portfolio source")
    portfolio_sha256 = _required_text(
        source, "portfolio_sha256", "compiled portfolio source"
    )
    if not re.fullmatch(r"[0-9a-f]{64}", portfolio_sha256):
        raise ReportError("compiled portfolio source hash is invalid")
    repository_commit = source.get("repository_commit")
    if repository_commit is not None and (
        not isinstance(repository_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", repository_commit)
    ):
        raise ReportError("compiled portfolio source commit is invalid")
    expected: dict[str, tuple[str, str, frozenset[str]]] = {}
    for suite in suites:
        if not isinstance(suite, dict) or not isinstance(
            suite.get("case_contracts"), list
        ):
            raise ReportError("compiled portfolio suite contract is invalid")
        industry_id = _required_text(suite, "industry_id", "compiled suite")
        dataset_id = _required_text(suite, "dataset_id", "compiled suite")
        for contract in suite["case_contracts"]:
            if not isinstance(contract, dict):
                raise ReportError("compiled case contract is invalid")
            case_id = _required_text(contract, "id", "compiled case contract")
            if case_id in expected:
                raise ReportError(f"compiled index repeats case_id {case_id!r}")
            capabilities = _capability_set(
                contract, "expected_capabilities", f"compiled case {case_id!r}"
            )
            expected[case_id] = (industry_id, dataset_id, capabilities)
    totals = index.get("totals")
    declared_total = totals.get("cases") if isinstance(totals, dict) else None
    if declared_total != len(expected) or not expected:
        raise ReportError("compiled portfolio case total is inconsistent")
    scope_keys = ("industries", "datasets", "cases", "authored_variants")
    if any(
        isinstance(totals.get(key), bool)
        or not isinstance(totals.get(key), int)
        or totals[key] < 1
        for key in scope_keys
    ):
        raise ReportError("compiled portfolio scope is invalid")

    observed = {str(record["case_id"]): record for record in records}
    extra = sorted(set(observed) - set(expected))
    missing = sorted(set(expected) - set(observed))
    if extra:
        raise ReportError(f"results contain cases outside the compiled index: {extra[:5]}")
    for case_id, record in observed.items():
        owner = (record["industry_id"], record["dataset_id"])
        if owner != expected[case_id][:2]:
            raise ReportError(f"result {case_id!r} has the wrong industry or dataset")
        capabilities = _capability_set(
            record, "expected_capabilities", f"result {case_id!r}"
        )
        if capabilities != expected[case_id][2]:
            raise ReportError(
                f"result {case_id!r} expected capabilities differ from compiled contract"
            )
    if missing and not allow_partial:
        raise ReportError(
            f"report is incomplete: {len(missing)} of {len(expected)} cases are missing"
        )
    return {
        "status": "partial" if missing else "complete",
        "observed_cases": len(observed),
        "expected_cases": len(expected),
        "missing_case_ids": missing,
        "source": {
            "portfolio": portfolio,
            "portfolio_sha256": portfolio_sha256,
            "repository_commit": repository_commit,
            "selection": selection,
        },
        "scope": {key: totals[key] for key in scope_keys},
    }


def _routing_analysis(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    routes = [
        _capability_route(
            item,
            f"result {item.get('case_id')!r}",
        )
        for item in records
    ]

    def stat(count: int, total: int) -> dict[str, Any]:
        return {"count": count, "rate": round(count / total, 4) if total else None}

    def funnel(
        group: list[tuple[frozenset[str], frozenset[str], frozenset[str]]]
    ) -> dict[str, Any]:
        tests = {
            "exact_attempt": lambda e, a, o: a == e,
            "exact_observation": lambda e, a, o: a == e == o,
            "missing_attempt": lambda e, a, o: bool(e - a),
            "attempted_not_observed": lambda e, a, o: bool(a - o),
            "unexpected_attempt": lambda e, a, o: bool(a - e),
        }
        return {"cases": len(group)} | {name: stat(sum(test(*route) for route in group), len(group)) for name, test in tests.items()}

    capabilities = sorted(set().union(*(e | a | o for e, a, o in routes)))
    by_capability: dict[str, Any] = {}
    for capability in capabilities:
        expected = sum(capability in route[0] for route in routes)
        attempted = sum(capability in route[0] & route[1] for route in routes)
        observed = sum(capability in route[0] & route[2] for route in routes)
        unexpected = sum(capability in route[1] - route[0] for route in routes)
        by_capability[capability] = {
            "expected": stat(expected, len(routes)),
            "attempted_when_expected": stat(attempted, expected),
            "observed_when_expected": stat(observed, expected),
            "attempt_to_observation": stat(observed, attempted),
            "unexpected_attempt": stat(unexpected, len(routes) - expected),
        }
    combinations = sorted({tuple(sorted(route[0])) for route in routes}, key=lambda value: (len(value), value))
    return {
        "overall": funnel(routes),
        "by_expected_capabilities": [
            {"expected_capabilities": list(combination), **funnel([route for route in routes if tuple(sorted(route[0])) == combination])}
            for combination in combinations],
        "by_capability": by_capability}


def _metrics(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items = list(records)
    run = Counter(str(item["status"]) for item in items)
    judge_status = Counter(str(item["semantic_judge"]["status"]) for item in items)
    verdicts = Counter(
        str(item["semantic_judge"]["verdict"])
        for item in items
        if item["semantic_judge"].get("status") == "evaluated"
        and item["semantic_judge"].get("verdict") in VERDICTS
    )
    evaluated = sum(verdicts.values())
    helpful = verdicts["usable"] + verdicts["degraded"]
    end_to_end_helpful = sum(
        item["status"] == "pass"
        and item["semantic_judge"].get("status") == "evaluated"
        and item["semantic_judge"].get("verdict") in {"usable", "degraded"}
        for item in items
    )

    def ratio(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    return {
        "cases": len(items),
        "run_status": {key: run[key] for key in sorted(RUN_STATUSES)},
        "judge_status": {key: judge_status[key] for key in sorted(JUDGE_STATUSES)},
        "verdicts": {key: verdicts[key] for key in sorted(VERDICTS)},
        "run_pass_rate": ratio(run["pass"], len(items)),
        "judge_coverage": ratio(evaluated, len(items)),
        "helpful_of_evaluated": ratio(helpful, evaluated),
        "end_to_end_helpful_rate": ratio(end_to_end_helpful, len(items)),
    }


def _case(record: Mapping[str, Any]) -> dict[str, Any]:
    semantic = record["semantic_judge"]
    partial = semantic.get("partial_evaluation")
    partial = partial if isinstance(partial, dict) else {}
    return {
        "case_id": record["case_id"],
        "position": record.get("position"),
        "cohort": record.get("cohort"),
        "profile": record.get("profile"),
        "question": record["question"],
        "answer": record["answer"],
        "response_class": record.get("response_class"),
        "status": record["status"],
        "failures": list(record.get("failures", [])),
        "source_ids": list(record.get("source_ids", [])),
        "selected_views": list(record.get("selected_views", [])),
        "expected_capabilities": list(record.get("expected_capabilities", [])),
        "attempted_capabilities": list(record.get("attempted_capabilities", [])),
        "observed_capabilities": list(record.get("observed_capabilities", [])),
        "tool_sequence": list(record.get("tool_sequence", [])),
        "deterministic_checks": list(record.get("deterministic_checks", [])),
        "elapsed_seconds": record.get("elapsed_seconds"),
        "judge_status": semantic["status"],
        "verdict": semantic.get("verdict"),
        "partial_verdict": partial.get("verdict"),
        "confidence_milli": semantic.get("confidence_milli"),
        "partial_confidence_milli": partial.get("confidence_milli"),
        "reason_codes": list(semantic.get("reason_codes", [])),
        "partial_reason_codes": list(partial.get("reason_codes", [])),
        "judge_reason": semantic.get("reason"),
        "semantic_judge": dict(semantic),
    }


def build_report(
    records: Iterable[Mapping[str, Any]], coverage: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Return a stable hierarchy with aggregate metrics and every result."""

    items = list(records)
    if not items:
        raise ReportError("at least one judged result is required")
    routing = _routing_analysis(items)
    industries: list[dict[str, Any]] = []
    industry_ids = sorted({str(item["industry_id"]) for item in items})
    for industry_id in industry_ids:
        industry_records = [
            item for item in items if item["industry_id"] == industry_id
        ]
        datasets: list[dict[str, Any]] = []
        for dataset_id in sorted(
            {str(item["dataset_id"]) for item in industry_records}
        ):
            dataset_records = [
                item for item in industry_records if item["dataset_id"] == dataset_id
            ]
            dataset_records.sort(
                key=lambda item: (item.get("position") or 0, item["case_id"])
            )
            datasets.append(
                {
                    "id": dataset_id,
                    "name": dataset_records[0]["dataset_name"],
                    "metrics": _metrics(dataset_records),
                    "cases": [_case(item) for item in dataset_records],
                }
            )
        industries.append(
            {
                "id": industry_id,
                "name": industry_records[0]["industry_name"],
                "metrics": _metrics(industry_records),
                "datasets": datasets,
            }
        )
    provenance = items[0]["judge_provenance"]
    result = {
        "schema_version": 1,
        "judge_provenance": dict(provenance),
        "overall": _metrics(items),
        "routing": routing,
        "industries": industries,
    }
    if coverage is not None:
        coverage_fields = {
            key: coverage[key]
            for key in ("status", "observed_cases", "expected_cases", "missing_case_ids")
        }
        result["coverage"] = coverage_fields
        result["source"] = dict(coverage["source"])
        result["scope"] = dict(coverage["scope"])
    return result


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _table_value(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _preformatted(value: str) -> str:
    return f"<pre>{html.escape(value) if value else '(empty)'}</pre>"


def _count_percent(count: int, rate: float | None) -> str:
    return f"{count} ({_percent(rate)})"


def _capability_label(capabilities: Iterable[str]) -> str:
    values = list(capabilities)
    return " + ".join(CAPABILITY_NAMES.get(value, value) for value in values) or "No answer tool"


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the complete report without interpreting model-authored Markdown."""

    overall = report["overall"]
    coverage = report.get("coverage", {})
    source = report.get("source", {})
    scope = report.get("scope", {})
    partial = isinstance(coverage, Mapping) and coverage.get("status") == "partial"
    lines = [
        "# Query Claw partial evaluation report" if partial else "# Query Claw evaluation report",
        "",
        *(
            [
                "**Partial result:** "
                f"{coverage['observed_cases']} of {coverage['expected_cases']} canonical cases are present.",
                "",
                "Missing case IDs: "
                f"`{_table_value(', '.join(coverage['missing_case_ids']))}`.",
                "",
            ]
            if partial
            else []
        ),
        f"Judge rubric: `{report['judge_provenance']['rubric_id']}`.",
        "Judge model: "
        f"`{_table_value(report['judge_provenance'].get('model') or 'unavailable')}`.",
        "",
        (
            f"Source: `{_table_value(source.get('portfolio', 'unknown'))}` at "
            f"repository commit `{_table_value(source.get('repository_commit') or 'unavailable')}`; "
            f"portfolio SHA-256 `{_table_value(source.get('portfolio_sha256', 'unknown'))}`."
        ),
        f"Selection: {_table_value(source.get('selection', 'unknown'))}.",
        (
            f"Scope: {scope.get('cases', overall['cases'])} canonical cases from "
            f"{scope.get('authored_variants', 'unknown')} authored variants across "
            f"{scope.get('industries', len(report['industries']))} industries and "
            f"{scope.get('datasets', 'unknown')} datasets."
        ),
        *(
            [
                "AIQ3 standalone-task scope: this workflow selects "
                f"{AIQ3_INDUSTRY_BOUND_TASKS} of {AIQ3_STANDALONE_TASKS} tasks; "
                f"the remaining {AIQ3_STANDALONE_TASKS - AIQ3_INDUSTRY_BOUND_TASKS} "
                "cross-industry or platform tasks are intentionally excluded."
            ]
            if scope.get("cases") == AIQ3_INDUSTRY_BOUND_TASKS
            else []
        ),
        "",
        "| Cases | Run pass | Judge coverage | Helpful of evaluated | End-to-end helpful |",
        "| ---: | ---: | ---: | ---: | ---: |",
        (
            f"| {overall['cases']} | {_percent(overall['run_pass_rate'])} | "
            f"{_percent(overall['judge_coverage'])} | "
            f"{_percent(overall['helpful_of_evaluated'])} | "
            f"{_percent(overall['end_to_end_helpful_rate'])} |"
        ),
        "",
        "## Capability routing",
        "",
        "Exact attempt means attempted capabilities equal the expected set. Exact observation additionally means every expected capability produced its required observable result.",
        "",
        "For prediction, a GSF result whose SQL begins with `PREDICT` is an attempt. It is observed only when it returns nonempty rows and every row contains a recognized finite numeric prediction. An unclassified GSF failure is not a prediction attempt; observation measures route/output success, not predictive accuracy.",
        "",
        "| Expected capability combination | Cases | Exact attempt | Exact observation | Missing attempt | Attempted, not observed | Unexpected attempt |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    routing_rows = [("Overall", report["routing"]["overall"])] + [
        (_capability_label(group["expected_capabilities"]), group) for group in report["routing"]["by_expected_capabilities"]]
    for label, routing in routing_rows:
        lines.append(
            f"| {_table_value(label)} | {routing['cases']} | "
            + " | ".join(
                _count_percent(routing[key]["count"], routing[key]["rate"])
                for key in ("exact_attempt", "exact_observation", "missing_attempt", "attempted_not_observed", "unexpected_attempt"))
            + " |")
    lines.extend(["", "| Capability | Expected | Attempted when expected | Observed when expected | Attempt-to-observation | Unexpected attempts |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for capability, metrics in report["routing"]["by_capability"].items():
        lines.append(
            f"| {_table_value(CAPABILITY_NAMES.get(capability, capability))} | "
            f"{_count_percent(metrics['expected']['count'], metrics['expected']['rate'])} | "
            + " | ".join(
                _count_percent(metrics[key]["count"], metrics[key]["rate"])
                for key in ("attempted_when_expected", "observed_when_expected", "attempt_to_observation", "unexpected_attempt"))
            + " |")
    lines.append("")
    for industry in report["industries"]:
        lines.extend([f"## {industry['name']}", ""])
        for dataset in industry["datasets"]:
            metrics = dataset["metrics"]
            lines.extend(
                [
                    f"### {dataset['name']}",
                    "",
                    "| Cases | Passed | Failed | Usable | Degraded | Unusable | Judge unavailable | Judge not run | Judge not observable |",
                    "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
                    (
                        f"| {metrics['cases']} | {metrics['run_status']['pass']} | "
                        f"{metrics['run_status']['fail']} | {metrics['verdicts']['usable']} | "
                        f"{metrics['verdicts']['degraded']} | {metrics['verdicts']['unusable']} | "
                        f"{metrics['judge_status']['unavailable']} | "
                        f"{metrics['judge_status']['not_run']} | "
                        f"{metrics['judge_status']['not_observable']} |"
                    ),
                    "",
                ]
            )
            for case in dataset["cases"]:
                verdict = case["verdict"] or case["judge_status"]
                if case["partial_verdict"]:
                    verdict += f" (partial: {case['partial_verdict']})"
                confidence_milli = (
                    case["confidence_milli"]
                    if case["confidence_milli"] is not None
                    else case["partial_confidence_milli"]
                )
                confidence = (
                    "n/a"
                    if confidence_milli is None
                    else f"{confidence_milli / 10:.1f}%"
                )
                lines.extend(
                    [
                        f"#### `{_table_value(case['case_id'])}`",
                        "",
                        "| Run status | Judge | Confidence | Profile |",
                        "| --- | --- | ---: | --- |",
                        (
                            f"| {_table_value(case['status'])} | {_table_value(verdict)} | "
                            f"{confidence} | {_table_value(case['profile'] or '')} |"
                        ),
                        "",
                        "**Question**",
                        "",
                        _preformatted(case["question"]),
                        "",
                        "**Answer**",
                        "",
                        _preformatted(case["answer"]),
                        "",
                    ]
                )
                if case["failures"]:
                    lines.extend(
                        [
                            "**Failures**",
                            "",
                            *[f"- {_table_value(item)}" for item in case["failures"]],
                            "",
                        ]
                    )
                unobserved = [
                    check["name"]
                    for check in case["deterministic_checks"]
                    if isinstance(check, dict)
                    and check.get("status") == "not_observable"
                ]
                lines.extend(
                    [
                        f"Tools: `{_table_value(', '.join(case['tool_sequence']) or 'none')}`  ",
                        f"Expected capabilities: `{_table_value(', '.join(case['expected_capabilities']) or 'none')}`  ",
                        f"Attempted capabilities: `{_table_value(', '.join(case['attempted_capabilities']) or 'none')}`  ",
                        f"Observed capabilities: `{_table_value(', '.join(case['observed_capabilities']) or 'none')}`  ",
                        f"Not observable: `{_table_value(', '.join(unobserved) or 'none')}`  ",
                        f"Judge reasons: `{_table_value(', '.join(case['reason_codes'] or case['partial_reason_codes']) or case['judge_reason'] or 'none')}`",
                        "",
                    ]
                )
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", nargs="+", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--markdown", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    args = parser.parse_args()
    destinations = {args.markdown.resolve(), args.json.resolve()}
    if len(destinations) != 2 or destinations & {path.resolve() for path in args.input}:
        parser.error("report outputs must be distinct from each other and all inputs")
    if args.markdown.exists() or args.json.exists():
        parser.error("refusing to overwrite a report output")
    try:
        records = load_records(args.input)
        coverage = validate_coverage(
            records, args.index, allow_partial=args.allow_partial
        )
        report = build_report(records, coverage)
    except ReportError as exc:
        parser.error(str(exc))
    _atomic_write(
        args.json,
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )
    _atomic_write(args.markdown, render_markdown(report))
    print(
        f"Reported {report['overall']['cases']} cases across {len(report['industries'])} industries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

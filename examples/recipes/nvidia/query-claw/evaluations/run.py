#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run one compiled Query Claw industry suite through Hermes Responses API."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


GSF_STRUCTURED = "gsf_structured_retrieval"
GSF_KUMO = "gsf_kumo_structured_prediction"
NEMO_RETRIEVER = "nemo_retriever_unstructured_retrieval"
CAPABILITIES = frozenset({GSF_STRUCTURED, GSF_KUMO, NEMO_RETRIEVER})
ANSWER_TOOLS = {
    "mcp__gsf__ask_question",
    "mcp__retriever__query",
}
SCAFFOLD_TOOLS = {
    "skills_list",
    "skill_view",
}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RESULT_ROWS = 100
MAX_RESULT_FIELDS = 40
MAX_RESULT_TEXT_CHARS = 1_000
MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (1.0, 2.0)
RESULT_VALUE_PREFIX = b"aiq-result-value-v1\0"
DECIMAL_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
SOURCES_FOOTER = re.compile(r"\n+\s*Sources used:\s*[^\n]*\s*$", re.IGNORECASE)


class EvaluationError(RuntimeError):
    """The suite, API response, or observed tool flow was invalid."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]
    output: Any


@dataclass(frozen=True)
class CaseContract:
    case_id: str
    source_ids: tuple[str, ...]
    profile: str
    cohort: str
    expected_capabilities: tuple[str, ...]
    evidence: Mapping[str, Any]
    prediction_population: tuple[str | int, ...] | None = None


@dataclass(frozen=True)
class SuiteContract:
    industry_id: str
    industry_name: str
    dataset_id: str
    dataset_name: str
    cases: Mapping[str, CaseContract]


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"could not read {path}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"{path} must contain a JSON object")
    return value


def _responses_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise EvaluationError("Hermes base URL is invalid")
    loopback = parsed.hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme != "https" and not loopback:
        raise EvaluationError("Hermes base URL must use HTTPS or loopback HTTP")
    root = base_url.rstrip("/")
    return root + (
        "/responses" if parsed.path.rstrip("/").endswith("/v1") else "/v1/responses"
    )


def _bounded_request(
    url: str, api_key: str, payload: Mapping[str, Any], timeout: int
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Query-Claw-Evaluation/1.0",
        },
        method="POST",
    )
    encoded: bytes | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
                encoded = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except HTTPError as exc:
            if exc.code != 429 and not 500 <= exc.code <= 599:
                raise EvaluationError(f"Hermes returned HTTP {exc.code}") from exc
            if attempt + 1 == MAX_ATTEMPTS:
                raise EvaluationError(f"Hermes returned HTTP {exc.code}") from exc
        except (OSError, TimeoutError, URLError, http.client.HTTPException) as exc:
            if attempt + 1 == MAX_ATTEMPTS:
                raise EvaluationError(
                    f"Hermes request failed: {type(exc).__name__}"
                ) from exc
        time.sleep(RETRY_DELAYS_SECONDS[attempt])
    if encoded is None:
        raise EvaluationError("Hermes request failed without a response")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise EvaluationError("Hermes response exceeded the safety bound")
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError("Hermes returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise EvaluationError("Hermes response must be an object")
    return value


def _text_parts(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in value
        if isinstance(part, dict)
        and part.get("type") in {"input_text", "output_text", "text"}
    )


def response_text(response: Mapping[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for item in response.get("output", []):
        if isinstance(item, dict) and item.get("type") == "message":
            parts.append(_text_parts(item.get("content")))
    answer = "".join(parts).strip()
    if not answer:
        raise EvaluationError("Hermes completed without an answer")
    return answer


def _decode_json(value: Any) -> Any:
    text = _text_parts(value) if isinstance(value, list) else value
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped:
        return ""
    closing_tag = "</untrusted_tool_result>"
    if stripped.startswith("<untrusted_tool_result ") and stripped.endswith(
        closing_tag
    ):
        opening_end = stripped.find(">")
        body = stripped[opening_end + 1 : -len(closing_tag)].strip()
        # Hermes places its fixed safety notice before the MCP JSON payload.
        _, separator, payload = body.partition("\n\n")
        if separator:
            stripped = payload.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _find_structured_result(
    value: Any, *, retrieval: bool = False
) -> dict[str, Any] | None:
    """Find a valid GSF or Retriever result inside a possible MCP envelope."""

    if isinstance(value, list):
        text = _text_parts(value)
        if text and (
            found := _find_structured_result(text, retrieval=retrieval)
        ) is not None:
            return found
        for item in value:
            if (
                found := _find_structured_result(item, retrieval=retrieval)
            ) is not None:
                return found
        return None
    if isinstance(value, str):
        try:
            return _find_structured_result(json.loads(value), retrieval=retrieval)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        hits = value.get("hits")
        evidence = value.get("evidence")
        if retrieval and any(
            isinstance(items, list)
            and all(isinstance(item, dict) for item in items)
            for items in (hits, evidence)
        ):
            return value
        if not retrieval and any(
            key in value for key in ("sql", "rows", "hits", "evidence", "answer")
        ):
            return value
        # NeMo Retriever's native MCP returns {"results": [{"hits": ...}]}.
        for key in (
            "structuredContent",
            "structured_content",
            "result",
            "results",
            "data",
            "content",
        ):
            if (
                key in value
                and (
                    found := _find_structured_result(
                        value[key], retrieval=retrieval
                    )
                )
                is not None
            ):
                return found
    return None


def _document_references(result: Mapping[str, Any]) -> list[str]:
    """Return bounded, citation-relevant identifiers from Retriever hits."""

    references: list[str] = []
    hits = result.get("hits")
    if not isinstance(hits, list):
        evidence = result.get("evidence")
        hits = evidence if isinstance(evidence, list) else []
    for hit in hits[:50]:
        if not isinstance(hit, dict):
            continue
        containers = [hit]
        if isinstance(hit.get("metadata"), dict):
            containers.append(hit["metadata"])
        for container in containers:
            for key in (
                "document_id",
                "doc_id",
                "source_id",
                "source",
                "filename",
                "pdf_basename",
                "path",
                "page_number",
            ):
                value = container.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    references.append(str(value).strip()[:500])
    return list(dict.fromkeys(references))[:100]


def tool_calls(response: Mapping[str, Any]) -> tuple[ToolCall, ...]:
    pending: dict[str, tuple[str, Mapping[str, Any]]] = {}
    completed: list[ToolCall] = []
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        call_id = item.get("call_id")
        if kind == "function_call" and isinstance(call_id, str):
            if call_id in pending:
                raise EvaluationError("response reused a tool call ID")
            raw_args = item.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError as exc:
                raise EvaluationError(
                    "tool call contained invalid arguments JSON"
                ) from exc
            if not isinstance(args, dict) or not isinstance(item.get("name"), str):
                raise EvaluationError("tool call had an invalid shape")
            pending[call_id] = (item["name"], args)
        elif kind == "function_call_output" and isinstance(call_id, str):
            if call_id not in pending:
                raise EvaluationError("tool output had no matching call")
            name, args = pending.pop(call_id)
            completed.append(ToolCall(name, args, _decode_json(item.get("output"))))
    if pending:
        raise EvaluationError("Hermes response contained an incomplete tool call")
    return tuple(completed)


def _canonical_scalar(value: Any) -> str | None:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"boolean:{str(value).lower()}"
    decimal: Decimal | None = None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            decimal = Decimal(str(value))
        except InvalidOperation:
            return None
    elif isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        if len(normalized) > MAX_RESULT_TEXT_CHARS:
            return None
        stripped = normalized.strip()
        if not DECIMAL_TEXT.fullmatch(stripped):
            return f"string:{normalized}"
        try:
            decimal = Decimal(stripped)
        except InvalidOperation:
            return f"string:{normalized}"
    else:
        return None
    if not decimal.is_finite():
        return None
    return "number:0" if decimal == 0 else f"number:{format(decimal.normalize(), 'f')}"


def _value_sha256(value: Any) -> str | None:
    scalar = _canonical_scalar(value)
    return (
        hashlib.sha256(RESULT_VALUE_PREFIX + scalar.encode()).hexdigest()
        if scalar is not None
        else None
    )


def _prediction_rows_are_scored(rows: list[Any]) -> bool:
    """Require every prediction row to carry an official GSF scalar output."""

    def is_score(name: str, value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return False
        try:
            score = Decimal(str(value))
        except InvalidOperation:
            return False
        if not score.is_finite():
            return False
        folded = name.casefold()
        if folded in {"prediction", "target_pred"} or folded.endswith("_pred"):
            return True
        probability = (
            folded in {"score", "probability", "true_prob"}
            or folded.endswith("_true")
            or (folded.endswith("_prob") and "false" not in folded)
        )
        return probability and Decimal("0") <= score <= Decimal("1")

    return bool(rows) and all(
        isinstance(row, dict)
        and any(is_score(str(name), value) for name, value in row.items())
        for row in rows
    )


def _tuple_matches(row: Mapping[str, Any], values: Iterable[Mapping[str, Any]]) -> bool:
    row_hashes = {
        name: _value_sha256(value)
        for name, value in list(row.items())[:MAX_RESULT_FIELDS]
    }
    candidates: list[list[str]] = []
    for expected in values:
        digests = (
            set(expected.get("field_sha256", {}).values())
            if isinstance(expected.get("field_sha256"), dict)
            else {expected.get("sha256")}
        )
        candidates.append(
            [name for name, digest in row_hashes.items() if digest in digests]
        )
    if any(not names for names in candidates):
        return False
    assigned: dict[str, int] = {}

    def assign(index: int, seen: set[str]) -> bool:
        for name in candidates[index]:
            if name in seen:
                continue
            seen.add(name)
            previous = assigned.get(name)
            if previous is None or assign(previous, seen):
                assigned[name] = index
                return True
        return False

    return all(assign(index, set()) for index in range(len(candidates)))


def _response_class(answer: str) -> str:
    body = SOURCES_FOOTER.sub("", answer).strip()
    if body.endswith("?") and len(body.split()) <= 80:
        return "clarification"
    # Boundary answers often explain that one requested conclusion is not
    # supported and then provide the supported analysis. Treat only an opening
    # refusal as an abstention; phrases such as "not available" in a result
    # cell or later limitation must not erase an otherwise complete answer.
    opening = re.split(r"(?<=[.!?])\s+", body.casefold(), maxsplit=1)[0][:180]
    if any(
        opening.startswith(phrase)
        for phrase in (
            "cannot answer",
            "i cannot answer",
            "i can't answer",
            "cannot support",
            "this cannot support",
            "unable to answer",
            "i am unable",
            "i'm unable",
            "not available",
            "not configured",
            "no access",
            "this is unsupported by",
            "unsupported by",
            "insufficient evidence",
        )
    ):
        return "abstention"
    return "answer"


def observed_capabilities(
    calls: Iterable[ToolCall],
    expected_collection: str | None = None,
    active_database: str | None = None,
) -> tuple[set[str], list[str], list[dict[str, Any]]]:
    observed: set[str] = set()
    failures: list[str] = []
    summaries: list[dict[str, Any]] = []
    for call in calls:
        if call.name not in ANSWER_TOOLS | SCAFFOLD_TOOLS:
            failures.append(f"out-of-contract tool: {call.name}")
            continue
        if call.name in SCAFFOLD_TOOLS:
            continue
        if call.name == "mcp__retriever__query":
            result = _find_structured_result(call.output, retrieval=True)
            if (
                call.arguments.get("rerank") is not True
                or call.arguments.get("format") != "hits"
            ):
                failures.append("Retriever query did not request reranked hits")
            top_k = call.arguments.get("top_k")
            if top_k != 5:
                failures.append("Retriever top_k was not 5")
            payload = call.arguments.get("payload")
            collection = (
                payload.get("collection_name") if isinstance(payload, dict) else None
            )
            if expected_collection is None:
                failures.append(
                    "Retriever was called without an active document collection"
                )
            elif collection != expected_collection:
                failures.append(
                    "Retriever collection differs: expected "
                    f"{expected_collection!r}, observed {collection!r}"
                )
            if result is None:
                failures.append(f"{call.name} returned no valid retrieval result")
            else:
                observed.add(NEMO_RETRIEVER)
            summaries.append(
                {
                    "tool": call.name,
                    "capability": NEMO_RETRIEVER,
                    "attempted_capability": NEMO_RETRIEVER,
                    "observed_capability": NEMO_RETRIEVER if result else None,
                    "document_refs": _document_references(result) if result else [],
                    "rerank": call.arguments.get("rerank"),
                    "format": call.arguments.get("format"),
                    "top_k": top_k,
                    "collection": collection,
                    "matched_count": result.get("matched_count") if result else None,
                    "exhaustive": result.get("exhaustive") if result else None,
                }
            )
            continue
        result = _find_structured_result(call.output)
        if result is None:
            failures.append(f"{call.name} returned no structured result")
            continue
        sql = str(result.get("sql") or "").lstrip()
        if not sql:
            failures.append("GSF ask_question returned no SQL/PQL evidence")
            continue
        capability = GSF_KUMO if re.match(r"(?i)^PREDICT\b", sql) else GSF_STRUCTURED
        rows = result.get("rows") if isinstance(result.get("rows"), list) else []
        row_count = result.get("row_count", len(rows))
        projected = rows[:MAX_RESULT_ROWS]
        successfully_observed = (
            capability == GSF_STRUCTURED or _prediction_rows_are_scored(rows)
        )
        if successfully_observed:
            observed.add(capability)
        summaries.append(
            {
                "tool": call.name,
                "capability": capability,
                "attempted_capability": capability,
                "observed_capability": capability if successfully_observed else None,
                "query_language": "pql" if capability == GSF_KUMO else "sql",
                "database_name": active_database,
                "row_count": row_count,
                "rows": projected,
                "truncated": bool(result.get("truncated"))
                or len(rows) > MAX_RESULT_ROWS
                or (
                    isinstance(row_count, int)
                    and not isinstance(row_count, bool)
                    and row_count > len(projected)
                ),
                "document_refs": [],
            }
        )
    return observed, failures, summaries


def _durable_tool_evidence(
    summaries: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only non-sensitive route metadata after transient contract checks."""

    evidence: list[dict[str, Any]] = []
    for summary in summaries:
        item: dict[str, Any] = {}
        for key, allowed in (
            ("tool", ANSWER_TOOLS),
            ("capability", CAPABILITIES),
            ("attempted_capability", CAPABILITIES),
            ("observed_capability", CAPABILITIES),
            ("query_language", frozenset({"sql", "pql"})),
            ("format", frozenset({"hits"})),
        ):
            value = summary.get(key)
            if value in allowed:
                item[key] = value
        for key in ("row_count", "top_k", "matched_count"):
            value = summary.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                item[key] = value
        for key in ("truncated", "rerank", "exhaustive"):
            value = summary.get(key)
            if isinstance(value, bool):
                item[key] = value
        evidence.append(item)
    return evidence


def _check(
    name: str,
    status: str,
    *,
    reason: str | None = None,
    blocking: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "status": status,
        "blocking": blocking,
    }
    if reason:
        result["reason"] = reason
    return result


def _prediction_contract_check(
    contract: CaseContract,
    summaries: list[dict[str, Any]],
    active_database: str | None,
) -> dict[str, Any]:
    result_contract = contract.evidence.get("prediction_result_contract")
    if not isinstance(result_contract, dict):
        raise EvaluationError("prediction_result_contract must be an object")
    population = contract.prediction_population
    cardinality = result_contract.get("population_cardinality")
    if (
        population is None
        or isinstance(cardinality, bool)
        or not isinstance(cardinality, int)
        or cardinality != len(population)
    ):
        return _check(
            "prediction_result_contract",
            "not_observable",
            reason="CONTRACT_POPULATION_UNRESOLVED",
        )
    expected_database = result_contract.get("database_name")
    if active_database != expected_database:
        return _check(
            "prediction_result_contract", "fail", reason="DATABASE_MISMATCH"
        )
    predictive = [
        summary for summary in summaries if summary.get("capability") == GSF_KUMO
    ]
    if not predictive:
        return _check(
            "prediction_result_contract",
            "not_observable",
            reason="NO_COMPLETED_PQL_RESULT",
        )

    allowed_entities = frozenset(population)
    entity_fields = {
        str(value).casefold() for value in result_contract.get("entity_fields", [])
    }
    score_fields = {
        str(value).casefold() for value in result_contract.get("score_fields", [])
    }
    matches = 0
    indeterminate = 0
    failures = 0
    for summary in predictive:
        rows = summary.get("rows", [])
        source_count = summary.get("row_count")
        truncated = summary.get("truncated") is True
        if not isinstance(rows, list) or not rows:
            failures += 1
            continue
        entities: list[str | int] = []
        scores: list[Decimal] = []
        valid = True
        for row in rows:
            if not isinstance(row, dict):
                valid = False
                continue
            entity_values = [
                value for name, value in row.items() if name.casefold() in entity_fields
            ]
            score_values = [
                value for name, value in row.items() if name.casefold() in score_fields
            ]
            if (
                not entity_values
                or any(type(value) is not type(entity_values[0]) for value in entity_values)
                or any(value != entity_values[0] for value in entity_values[1:])
                or isinstance(entity_values[0], bool)
                or not isinstance(entity_values[0], (str, int))
                or (isinstance(entity_values[0], str) and not entity_values[0])
            ):
                valid = False
            else:
                entities.append(entity_values[0])
            normalized_scores: list[Decimal] = []
            for value in score_values:
                if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                    valid = False
                    continue
                try:
                    score = Decimal(str(value))
                except InvalidOperation:
                    valid = False
                    continue
                if not score.is_finite() or not Decimal("0") <= score <= Decimal("1"):
                    valid = False
                    continue
                normalized_scores.append(score)
            if (
                not normalized_scores
                or any(value != normalized_scores[0] for value in normalized_scores[1:])
            ):
                valid = False
            else:
                scores.append(normalized_scores[0])
        shape_valid = (
            valid
            and len(entities) == len(rows) == len(scores)
            and len(entities) == len(set(entities))
            and (
                not result_contract.get("ranked")
                or all(left >= right for left, right in zip(scores, scores[1:]))
            )
        )
        exact = (
            shape_valid
            and not truncated
            and source_count == cardinality
            and len(rows) == cardinality
            and frozenset(entities) == allowed_entities
        )
        superset = (
            shape_valid
            and not truncated
            and source_count == len(rows)
            and len(rows) > cardinality
            and allowed_entities < frozenset(entities)
        )
        if exact:
            matches += 1
        elif truncated or superset:
            indeterminate += 1
        else:
            failures += 1
    if matches and not failures and not indeterminate:
        return _check("prediction_result_contract", "pass")
    if matches or indeterminate:
        return _check(
            "prediction_result_contract",
            "not_observable",
            reason=(
                "UNCORRELATED_NONMATCHING_PREDICTION_RESULT"
                if matches
                else "PREDICTION_ROWS_TRUNCATED_OR_SUPERSET"
            ),
        )
    return _check(
        "prediction_result_contract", "fail", reason="PREDICTION_RESULT_MISMATCH"
    )


def _contract_checks(
    contract: CaseContract,
    summaries: list[dict[str, Any]],
    *,
    active_database: str | None,
    prediction_contract_exists: bool,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    evidence = contract.evidence
    structured = [
        summary
        for summary in summaries
        if summary.get("capability") in {GSF_STRUCTURED, GSF_KUMO}
    ]
    rows = [
        row
        for summary in structured
        for row in summary.get("rows", [])
        if isinstance(row, dict)
    ]

    value_contracts = evidence.get("result_value_contracts", [])
    if isinstance(value_contracts, list) and value_contracts:
        matched = []
        for value_contract in value_contracts:
            if not isinstance(value_contract, dict):
                raise EvaluationError("result_value_contracts must contain objects")
            expected_database = value_contract.get("database_name")
            values = value_contract.get("values", [])
            matched.append(
                isinstance(values, list)
                and (not expected_database or expected_database == active_database)
                and any(_tuple_matches(row, values) for row in rows)
            )
        if all(matched):
            checks.append(_check("result_value_contracts", "pass"))
        elif not rows:
            checks.append(
                _check(
                    "result_value_contracts",
                    "not_observable",
                    reason="NO_BOUNDED_STRUCTURED_ROWS",
                )
            )
        elif any(summary.get("truncated") is True for summary in structured):
            checks.append(
                _check(
                    "result_value_contracts",
                    "not_observable",
                    reason="STRUCTURED_RESULT_TRUNCATED",
                )
            )
        else:
            checks.append(
                _check(
                    "result_value_contracts",
                    "fail",
                    reason="REVIEWED_RESULT_VALUE_NOT_OBSERVED",
                )
            )

    corpus = evidence.get("corpus_references", {})
    if isinstance(corpus, dict) and corpus:
        retriever_text = "\n".join(
            reference
            for summary in summaries
            if summary.get("capability") == NEMO_RETRIEVER
            for reference in summary.get("document_refs", [])
        ).casefold()
        missing = [
            str(document_id)
            for document_id in corpus
            if str(document_id).casefold() not in retriever_text
        ]
        checks.append(
            _check(
                "corpus_references",
                "fail" if missing else "pass",
                reason=("DOCUMENT_REFERENCE_NOT_OBSERVED" if missing else None),
            )
        )

    sql_contract = evidence.get("sql_result_contract")
    if isinstance(sql_contract, dict):
        relevant = [
            summary
            for summary in structured
            if summary.get("database_name") == sql_contract.get("database_name")
            and summary.get("capability") == GSF_STRUCTURED
        ]
        observable = [
            summary
            for summary in relevant
            if isinstance(summary.get("row_count"), int)
            and not isinstance(summary.get("row_count"), bool)
        ]
        zero_rows = [
            summary
            for summary in observable
            if summary["row_count"] == 0
            and not summary.get("rows")
            and summary.get("truncated") is False
        ]
        checks.append(
            _check(
                "sql_result_contract",
                "pass" if zero_rows else "fail" if observable else "not_observable",
                reason=(
                    None
                    if zero_rows
                    else "ZERO_ROW_SQL_RESULT_NOT_OBSERVED"
                    if observable
                    else "NO_COMPLETED_SQL_RESULT_FOR_DATABASE"
                ),
            )
        )

    no_match_contract = evidence.get("retriever_no_match_contract")
    if isinstance(no_match_contract, dict):
        retrieval = [
            summary
            for summary in summaries
            if summary.get("capability") == NEMO_RETRIEVER
        ]
        conclusive = [
            summary
            for summary in retrieval
            if summary.get("exhaustive") is True
            and isinstance(summary.get("matched_count"), int)
            and not isinstance(summary.get("matched_count"), bool)
        ]
        if any(summary["matched_count"] > 0 for summary in conclusive):
            status, reason = "fail", "EXACT_RETRIEVER_MATCH_OBSERVED"
        elif conclusive and len(conclusive) == len(retrieval):
            status, reason = "pass", None
        else:
            status, reason = "not_observable", "EXHAUSTIVE_MATCH_COUNT_NOT_EXPOSED"
        checks.append(_check("retriever_no_match_contract", status, reason=reason))

    database_names = evidence.get("database_names")
    if isinstance(database_names, list) and database_names:
        expected = {str(value) for value in database_names}
        checks.append(
            _check(
                "active_dataset_database_scope",
                "pass"
                if active_database is not None and expected == {active_database}
                else "not_observable"
                if active_database is None
                else "fail",
                reason=(
                    None
                    if expected == {active_database}
                    else "ACTIVE_DATABASE_UNAVAILABLE"
                    if active_database is None
                    else "ACTIVE_DATABASE_MISMATCH"
                ),
            )
        )

    if isinstance(evidence.get("prediction_result_contract"), dict):
        checks.append(
            _prediction_contract_check(contract, summaries, active_database)
        )
    if GSF_KUMO in contract.expected_capabilities:
        checks.append(
            _check(
                "prediction_graph_lineage",
                "not_observable",
                reason=(
                    "OFFICIAL_GSF_MCP_DOES_NOT_EXPOSE_GRAPH_RECEIPT"
                    if prediction_contract_exists
                    else "ACTIVE_DATASET_HAS_NO_PREDICTION_CONTRACT"
                ),
                blocking=False,
            )
        )
    for field in (
        "answer_anchors",
        "answer_forbidden_anchors",
        "result_field_anchors",
        "prediction_task_contract",
    ):
        if evidence.get(field):
            checks.append(
                _check(
                    field,
                    "not_observable",
                    reason="REQUIRES_SEMANTIC_OR_PROVIDER_LINEAGE_REVIEW",
                    blocking=False,
                )
            )
    return checks


def load_suite_contract(index_path: Path, suite_name: str) -> SuiteContract:
    index = _load_object(index_path)
    for suite in index.get("suites", []):
        if isinstance(suite, dict) and suite.get("suite") == suite_name:
            contracts: dict[str, CaseContract] = {}
            for item in suite.get("case_contracts", []):
                if not isinstance(item, dict):
                    raise EvaluationError("AIQ3 case contract must be an object")
                case_id = str(item.get("id", ""))
                capabilities = tuple(item.get("expected_capabilities", []))
                if not case_id or any(
                    value not in CAPABILITIES for value in capabilities
                ):
                    raise EvaluationError("AIQ3 case contract is invalid")
                population = item.get("prediction_population")
                if population is not None and (
                    not isinstance(population, list)
                    or not population
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (str, int))
                        or (isinstance(value, str) and not value)
                        for value in population
                    )
                    or len(population) != len(set(population))
                    or any(
                        type(value) is not type(population[0])
                        for value in population[1:]
                    )
                ):
                    raise EvaluationError("AIQ3 prediction population is invalid")
                contracts[case_id] = CaseContract(
                    case_id,
                    tuple(str(value) for value in item.get("source_ids", [])),
                    str(item.get("profile", "")),
                    str(item.get("cohort", "")),
                    capabilities,
                    item.get("evidence", {})
                    if isinstance(item.get("evidence", {}), dict)
                    else {},
                    tuple(population) if population is not None else None,
                )
            fields = {
                "industry_id": suite.get("industry_id"),
                "industry_name": suite.get("industry_name"),
                "dataset_id": suite.get("dataset_id"),
                "dataset_name": suite.get("dataset_name"),
            }
            if any(
                not isinstance(value, str) or not value for value in fields.values()
            ):
                raise EvaluationError("AIQ3 suite metadata is invalid")
            return SuiteContract(cases=contracts, **fields)
    raise EvaluationError(f"{suite_name} is absent from the AIQ3 index")


def _evaluation_instructions(
    dataset_id: str, views: Iterable[str], collection: str | None
) -> str:
    """Describe the evaluator's selected evidence boundary unambiguously."""

    selected = tuple(views)
    instructions = (
        f"This evaluation has activated only dataset {dataset_id!r}, with allowed views: "
        f"{', '.join(selected) if selected else 'none'}. Use no other data source. "
        "Use only the specialist skill and official data tool required by the question. "
    )
    if not selected:
        return instructions + (
            "No evidence view is selected for this case: call no data tool. If the "
            "question requires dataset evidence, state that the selected sources cannot "
            "support it. "
        )
    if collection and "documents" in selected:
        instructions += (
            f"For NeMo Retriever use collection {collection!r} via payload.collection_name, "
            "top_k=5, format='hits', and rerank=true. "
        )
    return instructions


def _atomic_jsonl(
    path: Path, records: Iterable[Mapping[str, Any]], *, replace: bool = False
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise EvaluationError(f"refusing to overwrite {path}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            for record in records:
                handle.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--active-dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("QUERY_CLAW_HERMES_URL", "http://127.0.0.1:8642/v1"),
    )
    parser.add_argument("--api-key", default=os.environ.get("API_SERVER_KEY", ""))
    parser.add_argument("--model", default="hermes-agent")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()
    if not args.api_key or args.timeout <= 0:
        parser.error("API_SERVER_KEY and a positive --timeout are required")

    suite = _load_object(args.suite)
    if suite.get("schema_version") != 3 or not isinstance(suite.get("cases"), list):
        parser.error("--suite must be a Query Claw schema-v3 suite")
    active = _load_object(args.active_dataset)
    active_database_contract = active.get("database")
    if active_database_contract is not None and (
        not isinstance(active_database_contract, dict)
        or active_database_contract.get("engine") not in {"duckdb", "postgres-csv"}
    ):
        raise EvaluationError("active dataset database engine is invalid")
    active_prediction_contract = active.get("prediction")
    if active_prediction_contract is not None and (
        not isinstance(active_prediction_contract, dict)
        or active_prediction_contract.get("mode") not in {"native", "reviewed"}
    ):
        raise EvaluationError("active dataset prediction mode is invalid")
    suite_contract = load_suite_contract(args.index, args.suite.name)
    contracts = suite_contract.cases
    selected = set(args.case)
    cases = [
        case
        for case in suite["cases"]
        if isinstance(case, dict) and (not selected or case.get("id") in selected)
    ]
    missing = selected - {str(case.get("id")) for case in cases}
    if missing:
        parser.error("unknown --case values: " + ", ".join(sorted(missing)))
    if not cases:
        parser.error("suite selection is empty")

    dataset_id = str(active.get("id", ""))
    industry = active.get("industry")
    if (
        dataset_id != suite_contract.dataset_id
        or not isinstance(industry, dict)
        or industry.get("id") != suite_contract.industry_id
    ):
        raise EvaluationError(
            "compiled suite does not match the activated industry dataset"
        )
    collection = (
        (active.get("documents") or {}).get("collection")
        if isinstance(active.get("documents"), dict)
        else None
    )
    active_database = (
        (active_database_contract or {}).get("name")
        if isinstance(active_database_contract, dict)
        else None
    )
    if active_database is not None and not isinstance(active_database, str):
        raise EvaluationError("active dataset database name is invalid")
    prediction_contract_exists = active.get("prediction_contract_exists") is True
    records: list[dict[str, Any]] = []
    _atomic_jsonl(args.output, records)
    endpoint = _responses_url(args.base_url)
    for position, case in enumerate(cases, start=1):
        case_id = str(case.get("id", ""))
        contract = contracts.get(case_id)
        if contract is None:
            raise EvaluationError(f"missing contract for {case_id}")
        declared = case.get("datasets", [])
        if declared and (len(declared) != 1 or declared[0].get("id") != dataset_id):
            raise EvaluationError(
                f"{case_id} does not target active dataset {dataset_id}"
            )
        views = declared[0].get("views", []) if declared else []
        instructions = _evaluation_instructions(dataset_id, views, collection)
        started = time.monotonic()
        failures: list[str] = []
        answer = ""
        calls: tuple[ToolCall, ...] = ()
        summaries: list[dict[str, Any]] = []
        observed: set[str] = set()
        attempted: set[str] = set()
        checks: list[dict[str, Any]] = []
        response_class = "missing"
        try:
            response = _bounded_request(
                endpoint,
                args.api_key,
                {
                    "model": args.model,
                    "input": case.get("prompt", ""),
                    "instructions": instructions,
                    "store": False,
                },
                args.timeout,
            )
            if response.get("status") != "completed":
                raise EvaluationError(f"Hermes status was {response.get('status')!r}")
            answer = response_text(response)
            calls = tool_calls(response)
            observed, flow_failures, summaries = observed_capabilities(
                calls, collection, active_database
            )
            attempted = {
                capability
                for summary in summaries
                if isinstance(capability := summary.get("attempted_capability"), str)
            }
            failures.extend(flow_failures)
            expected = set(contract.expected_capabilities)
            unexpected_attempts = attempted - expected
            if unexpected_attempts:
                failures.append(
                    "capabilities attempted outside contract: "
                    f"{sorted(unexpected_attempts)}"
                )
            if observed != expected:
                failures.append(
                    f"capabilities differ: expected {sorted(expected)}, observed {sorted(observed)}"
                )
            expected_responses = set(
                case.get("expected", {}).get("response", ["answer"])
            )
            response_class = _response_class(answer)
            if response_class not in expected_responses:
                failures.append(
                    f"response class differs: expected {sorted(expected_responses)}, observed {response_class}"
                )
            checks = _contract_checks(
                contract,
                summaries,
                active_database=active_database,
                prediction_contract_exists=prediction_contract_exists,
            )
            failures.extend(
                f"{check['name']}: {check.get('reason', 'failed')}"
                for check in checks
                if check["status"] == "fail" and check.get("blocking", True)
            )
        except EvaluationError as exc:
            failures.append(str(exc))
        records.append(
            {
                "schema_version": 1,
                "case_id": case_id,
                "position": position,
                "industry_id": suite_contract.industry_id,
                "industry_name": suite_contract.industry_name,
                "dataset_id": dataset_id,
                "dataset_name": suite_contract.dataset_name,
                "cohort": contract.cohort,
                "profile": contract.profile,
                "question": case.get("prompt", ""),
                "answer": answer,
                "response_class": response_class,
                "source_ids": list(contract.source_ids),
                "selected_views": views,
                "expected_capabilities": list(contract.expected_capabilities),
                "attempted_capabilities": sorted(attempted),
                "observed_capabilities": sorted(observed),
                "tool_sequence": [call.name for call in calls],
                "tool_evidence": _durable_tool_evidence(summaries),
                "deterministic_checks": checks,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "status": "pass" if not failures else "fail",
                "failures": failures,
            }
        )
        _atomic_jsonl(args.output, records, replace=True)
        print(
            f"[{position}/{len(cases)}] {case_id}: {records[-1]['status']}", flush=True
        )
    passed = sum(record["status"] == "pass" for record in records)
    print(
        f"Completed {len(records)} cases: {passed} pass, {len(records) - passed} fail"
    )
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())

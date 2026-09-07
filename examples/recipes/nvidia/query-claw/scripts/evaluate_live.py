#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise Query Claw routes through Hermes' structured run API."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import evaluation_judge


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
TOOL_CONTRACT_PATH = EXAMPLE_ROOT / "config" / "tool-contracts.json"
EVALUATOR_VERSION = 1
READ_ONLY_SCAFFOLD_TOOLS = frozenset({"skills_list", "skill_view"})
MAX_CALCULATION_CODE_CHARS = 2_000
MAX_CALCULATION_AST_NODES = 512
MAX_CALCULATION_STATEMENTS = 64
ABSTENTION_PATTERNS = (
    r"\b(?:cannot|can't|unable to|won't|will not) "
    r"(?:answer|provide|help|access|produce|generate)\b",
    r"\b(?:do|does) not have access\b",
    r"\bnot (?:an? )?(?:available )?(?:configured )?(?:source|view)\b",
    r"\bnot one of the configured\b",
    r"\b(?:is not|isn't) configured\b",
    r"\bnot available\b|\bunavailable\b|\binsufficient (?:evidence|information)\b",
)


@dataclass(frozen=True)
class Case:
    prompt: str
    order: tuple[str, ...] = ()
    required_answer_terms: tuple[str, ...] = ()
    allow_calculation: bool = False
    required_tools: frozenset[str] | None = None
    required_citation_terms: tuple[str, ...] = ()
    expect_abstention: bool = False
    session: str = ""
    expected_format: str = ""

    @property
    def required(self) -> frozenset[str]:
        return (
            self.required_tools
            if self.required_tools is not None
            else frozenset(self.order)
        )

    @property
    def routes(self) -> frozenset[str]:
        return frozenset(filter(None, (route_for_tool(name) for name in self.required)))

    @property
    def allowed_query_tools(self) -> frozenset[str]:
        return frozenset().union(*(ROUTE_TOOLS[route] for route in self.routes))

    @property
    def allowed_builtin(self) -> frozenset[str]:
        return frozenset({"execute_code"}) if self.allow_calculation else frozenset()


@dataclass(frozen=True)
class RunResult:
    tools: tuple[str, ...]
    answer: str = ""


def _load_tool_contract(path: Path = TOOL_CONTRACT_PATH) -> dict[str, frozenset[str]]:
    """Load the evaluator allowlist from the deployment's one tool contract."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        registration = document["registration"]["name"]
        route_tools = document["routes"]
        if document.get("schema_version") != 1 or not isinstance(route_tools, dict):
            raise KeyError("schema_version")
        routes = {
            route: frozenset(
                f"mcp__{registration.replace('-', '_')}__{name}" for name in tools
            )
            for route, tools in route_tools.items()
        }
        if not routes or any(not names for names in routes.values()):
            raise KeyError("routes")
        return routes
    except (AttributeError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid Query Claw tool contract: {path}") from exc


ROUTE_TOOLS = _load_tool_contract()
TOOL_ROUTES = {name: route for route, names in ROUTE_TOOLS.items() for name in names}
SHORT_TOOLS = {
    (route, name.rsplit("__", 1)[-1]): name
    for route, names in ROUTE_TOOLS.items()
    for name in names
}


def tool(route: str, name: str) -> str:
    """Resolve a short suite reference against the canonical tool contract."""
    return SHORT_TOOLS.get((route, name), "")


def route_for_tool(name: str) -> str:
    return TOOL_ROUTES.get(name, "")


def loopback_api_url(raw_port: str) -> str:
    """Return the fixed-loopback Hermes URL for a valid TCP port."""
    if not re.fullmatch(r"[0-9]{1,5}", raw_port):
        raise EvaluationError("NEMOCLAW_HERMES_API_PORT must be between 1 and 65535")
    port = int(raw_port)
    if not 1 <= port <= 65_535:
        raise EvaluationError("NEMOCLAW_HERMES_API_PORT must be between 1 and 65535")
    return f"http://127.0.0.1:{port}"


class EvaluationError(RuntimeError):
    """A live route did not satisfy the Query Claw contract."""

    def __init__(self, message: str, result: RunResult | None = None) -> None:
        super().__init__(message)
        self.result = result or RunResult(())


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise EvaluationError(f"{field} must be a list of nonempty strings")
    if len(value) != len(set(value)):
        raise EvaluationError(f"{field} contains duplicates")
    return tuple(value)


def _reject_unknown(document: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(document) - allowed
    if unknown:
        raise EvaluationError(
            f"{context} has unknown fields: {', '.join(sorted(unknown))}"
        )


def _identifier(value: Any, context: str, *, optional: bool = False) -> str:
    if not isinstance(value, str) or (
        (not optional and not value)
        or bool(value)
        and not re.fullmatch(r"[A-Za-z0-9_-]+", value)
    ):
        raise EvaluationError(
            f"{context} must contain only letters, numbers, '-' or '_'"
        )
    return value


def _suite_tool(reference: str) -> str:
    if reference == "execute_code":
        return reference
    try:
        route, name = reference.split(".", 1)
    except ValueError as exc:
        raise EvaluationError(f"invalid expected tool reference: {reference}") from exc
    candidate = tool(route, name)
    if route not in ROUTE_TOOLS or candidate not in ROUTE_TOOLS[route]:
        raise EvaluationError(
            f"expected tool is outside the Query Claw contract: {reference}"
        )
    return candidate


def parse_suite_case(raw: Any, source: str) -> tuple[str, Case]:
    if not isinstance(raw, dict):
        raise EvaluationError(f"{source} must contain case objects")
    _reject_unknown(
        raw, {"id", "session", "prompt", "expected", "allow_calculation"}, source
    )
    case_id = _identifier(raw.get("id"), f"{source} case id")
    prompt = raw.get("prompt")
    expected = raw.get("expected")
    if not isinstance(prompt, str) or not prompt.strip():
        raise EvaluationError(f"{source}:{case_id} prompt must be a nonempty string")
    if not isinstance(expected, dict):
        raise EvaluationError(f"{source}:{case_id} expected must be an object")
    _reject_unknown(
        expected,
        {"routes", "tools", "tool_order", "facts", "citations", "abstain", "format"},
        f"{source}:{case_id} expected",
    )

    routes = frozenset(_string_list(expected.get("routes", []), "expected.routes"))
    unknown_routes = routes - set(ROUTE_TOOLS)
    if unknown_routes:
        raise EvaluationError(
            f"{source}:{case_id} has unknown routes: {', '.join(sorted(unknown_routes))}"
        )
    tools = tuple(
        _suite_tool(item)
        for item in _string_list(expected.get("tools", []), "expected.tools")
    )
    order = tuple(
        _suite_tool(item)
        for item in _string_list(expected.get("tool_order", []), "expected.tool_order")
    )
    if not set(order).issubset(tools):
        raise EvaluationError(
            f"{source}:{case_id} tool_order must be a subset of tools"
        )
    tool_routes = frozenset(filter(None, (route_for_tool(name) for name in tools)))
    if tool_routes != routes:
        raise EvaluationError(
            f"{source}:{case_id} tools must cover exactly its expected routes"
        )

    allow_calculation = raw.get("allow_calculation", False)
    abstain = expected.get("abstain", False)
    if not isinstance(allow_calculation, bool) or not isinstance(abstain, bool):
        raise EvaluationError(
            f"{source}:{case_id} boolean fields must be true or false"
        )
    if ("execute_code" in tools) != allow_calculation:
        raise EvaluationError(
            f"{source}:{case_id} execute_code requires allow_calculation=true and vice versa"
        )
    session = _identifier(
        raw.get("session", ""), f"{source}:{case_id} session", optional=True
    )
    expected_format = expected.get("format", "")
    if not isinstance(expected_format, str) or expected_format not in {
        "",
        "markdown_report",
    }:
        raise EvaluationError(
            f"{source}:{case_id} expected.format must be 'markdown_report' when set"
        )
    return case_id, Case(
        prompt=prompt.strip(),
        order=order,
        required_answer_terms=_string_list(expected.get("facts", []), "expected.facts"),
        allow_calculation=allow_calculation,
        required_tools=frozenset(tools),
        required_citation_terms=_string_list(
            expected.get("citations", []), "expected.citations"
        ),
        expect_abstention=abstain,
        session=session,
        expected_format=expected_format,
    )


def load_suite(path: Path) -> tuple[str, list[tuple[str, Case]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvaluationError(f"could not read evaluation suite: {path}") from exc
    try:
        if path.suffix == ".jsonl":
            raw_cases = [json.loads(line) for line in text.splitlines() if line.strip()]
            suite_name = path.stem
        else:
            document = json.loads(text)
            if isinstance(document, list):
                raw_cases = document
                suite_name = path.stem
            elif isinstance(document, dict):
                if document.get("schema_version") not in {1, 2}:
                    raise EvaluationError(f"{path} schema_version must be 1 or 2")
                suite_name = document.get("name", path.stem)
                raw_cases = document.get("cases")
                if not isinstance(suite_name, str) or not suite_name:
                    raise EvaluationError(f"{path} name must be a nonempty string")
            else:
                raise EvaluationError(f"{path} must contain an object or case array")
    except json.JSONDecodeError as exc:
        raise EvaluationError(
            f"invalid JSON in evaluation suite: {path}:{exc.lineno}"
        ) from exc
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvaluationError(f"{path} must contain at least one case")
    cases = [parse_suite_case(raw, str(path)) for raw in raw_cases]
    names = [name for name, _ in cases]
    if len(names) != len(set(names)):
        raise EvaluationError(f"{path} contains duplicate case ids")
    active_session = ""
    finished_sessions: set[str] = set()
    for case_id, case in cases:
        if case.session == active_session:
            continue
        if active_session:
            finished_sessions.add(active_session)
        if case.session and case.session in finished_sessions:
            raise EvaluationError(
                f"{path}:{case_id} session '{case.session}' must be consecutive"
            )
        active_session = case.session
    return suite_name, cases


def request_json(
    base_url: str,
    api_key: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    method: str | None = None,
    timeout: int = 300,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"{base_url}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method=method or ("POST" if payload is not None else "GET"),
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback URL is fixed below
            return response.status, json.load(response)
    except HTTPError as exc:
        raise EvaluationError(
            f"Hermes API returned HTTP {exc.code} for {path}"
        ) from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise EvaluationError(
            f"Hermes API request failed for {path}: {type(exc).__name__}"
        ) from exc


def stream_events(
    base_url: str, api_key: str, run_id: str, *, deadline: float
) -> Iterable[dict[str, Any]]:
    request = Request(
        f"{base_url}/v1/runs/{run_id}/events",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"},
    )
    try:
        socket_timeout = max(1.0, deadline - time.monotonic())
        with urlopen(request, timeout=socket_timeout) as response:  # noqa: S310 - fixed loopback URL
            for raw_line in response:
                if time.monotonic() >= deadline:
                    raise EvaluationError("Hermes run exceeded its wall-clock timeout")
                line = raw_line.decode("utf-8", errors="strict").strip()
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if isinstance(event, dict):
                        yield event
    except HTTPError as exc:
        raise EvaluationError(f"Hermes event stream returned HTTP {exc.code}") from exc
    except (OSError, URLError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(
            f"Hermes event stream failed: {type(exc).__name__}"
        ) from exc


ARITHMETIC_NODES = {
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.UAdd,
    ast.USub,
}
SAFE_FUNCTIONS = {"abs": (1, 1), "max": (1, 20), "min": (1, 20), "round": (1, 2)}


def _numeric_value(node: ast.expr, variables: dict[str, int | float]) -> int | float:
    if isinstance(node, ast.Constant):
        value = node.value
    elif isinstance(node, ast.Name):
        value = variables[node.id]
    elif isinstance(node, ast.UnaryOp):
        operand = _numeric_value(node.operand, variables)
        value = operand if isinstance(node.op, ast.UAdd) else -operand
    elif isinstance(node, ast.BinOp):
        left = _numeric_value(node.left, variables)
        right = _numeric_value(node.right, variables)
        try:
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            elif isinstance(node.op, ast.Div):
                value = left / right
            elif isinstance(node.op, ast.FloorDiv):
                value = left // right
            else:
                value = left % right
        except (OverflowError, ZeroDivisionError) as exc:
            raise EvaluationError(
                "execute_code approval exceeded the arithmetic bound"
            ) from exc
    else:
        values = [_numeric_value(argument, variables) for argument in node.args]
        functions = {"abs": abs, "max": max, "min": min, "round": round}
        value = functions[node.func.id](*values)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or abs(value) > 1_000_000_000
    ):
        raise EvaluationError("execute_code approval exceeded the arithmetic bound")
    return value


def validate_numeric_expression(
    node: ast.expr, variables: dict[str, int | float]
) -> int | float:
    """Reject anything beyond bounded, side-effect-free scalar arithmetic."""
    for item in ast.walk(node):
        if isinstance(item, ast.Pow):
            raise EvaluationError("execute_code approval used unbounded exponentiation")
        if type(item) not in ARITHMETIC_NODES:
            raise EvaluationError("execute_code approval was not arithmetic-only")
        if isinstance(item, ast.Constant):
            value = item.value
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                message = (
                    "container repetition"
                    if isinstance(value, (str, bytes))
                    else "non-numeric scalar"
                )
                raise EvaluationError(f"execute_code approval used {message}")
            if abs(value) > 1_000_000_000 or not math.isfinite(value):
                raise EvaluationError("execute_code approval used an oversized number")
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load):
            if item.id not in set(variables) | set(SAFE_FUNCTIONS):
                raise EvaluationError("execute_code approval used unsafe indirection")
        if isinstance(item, ast.Call):
            name = item.func.id if isinstance(item.func, ast.Name) else ""
            bounds = SAFE_FUNCTIONS.get(name)
            if (
                not bounds
                or item.keywords
                or not bounds[0] <= len(item.args) <= bounds[1]
            ):
                raise EvaluationError("execute_code approval called an unsafe function")
            if name == "round" and len(item.args) == 2:
                digits = item.args[1]
                if (
                    not isinstance(digits, ast.Constant)
                    or isinstance(digits.value, bool)
                    or not isinstance(digits.value, int)
                    or abs(digits.value) > 12
                ):
                    raise EvaluationError(
                        "execute_code approval exceeded the arithmetic bound"
                    )
    return _numeric_value(node, variables)


def validate_calculation_approval(case: Case, event: dict[str, Any]) -> None:
    """Allow one bounded program of numeric-scalar assignments and output."""
    if not case.allow_calculation:
        raise EvaluationError("run requested an unexpected approval")
    keys = event.get("pattern_keys", [event.get("pattern_key")])
    if (
        event.get("pattern_key") != "execute_code"
        or keys != ["execute_code"]
        or event.get("smart_denied") is True
        or "once" not in (event.get("choices") or [])
    ):
        raise EvaluationError("run requested an unsafe execute_code approval")
    command = event.get("command")
    prefix, suffix = "execute_code <<'PY'\n", "\nPY"
    if (
        not isinstance(command, str)
        or not command.startswith(prefix)
        or not command.endswith(suffix)
    ):
        raise EvaluationError("execute_code approval omitted its bounded script")
    code = command[len(prefix) : -len(suffix)]
    if len(code) > MAX_CALCULATION_CODE_CHARS:
        raise EvaluationError("execute_code approval exceeded the arithmetic bound")
    try:
        tree = ast.parse(code)
    except (MemoryError, RecursionError, SyntaxError, ValueError) as exc:
        raise EvaluationError("execute_code approval contained invalid Python") from exc
    if (
        sum(1 for _ in ast.walk(tree)) > MAX_CALCULATION_AST_NODES
        or len(tree.body) > MAX_CALCULATION_STATEMENTS
    ):
        raise EvaluationError("execute_code approval exceeded the arithmetic bound")

    variables: dict[str, int | float] = {}
    output_count = 0
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if (
                not isinstance(target, ast.Name)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", target.id)
                or target.id in {"abs", "max", "min", "print", "round"}
            ):
                raise EvaluationError("execute_code approval used unsafe indirection")
            variables[target.id] = validate_numeric_expression(
                statement.value, variables
            )
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "print"
            and len(statement.value.args) == 1
            and not statement.value.keywords
        ):
            validate_numeric_expression(statement.value.args[0], variables)
            output_count += 1
            continue
        raise EvaluationError("execute_code approval was not arithmetic-only")
    if output_count != 1:
        raise EvaluationError(
            "execute_code approval must print exactly one numeric result"
        )


def validate_event(case: Case, event: dict[str, Any]) -> None:
    event_type = str(event.get("event", ""))
    if event_type == "approval.request":
        validate_calculation_approval(case, event)
        return
    if event_type in {"run.failed", "run.cancelled", "tool.failed"}:
        raise EvaluationError(f"run stopped at {event_type}")
    if event_type not in {"tool.started", "tool.completed"}:
        return
    name = str(event.get("tool", ""))
    if not name:
        raise EvaluationError(f"{event_type} omitted the tool name")
    if (
        event_type == "tool.completed"
        and event.get("error") is True
        and name not in case.allowed_query_tools
    ):
        raise EvaluationError(f"tool execution failed: {name}")
    if event_type != "tool.started":
        return
    lowered = name.lower().replace("-", "_")
    if name.startswith("mcp__") and name not in case.allowed_query_tools:
        raise EvaluationError(f"route used an out-of-contract MCP tool: {name}")
    if lowered.startswith("browser") or lowered == "web" or lowered.startswith("web_"):
        raise EvaluationError(f"route used a web fallback: {name}")
    if not name.startswith("mcp__") and name not in (
        case.allowed_builtin | READ_ONLY_SCAFFOLD_TOOLS
    ):
        raise EvaluationError(f"route used an out-of-contract builtin tool: {name}")


def validate_answer(case: Case, event_output: str, status: dict[str, Any]) -> str:
    output = str(status.get("output", "")).strip()
    if not event_output:
        raise EvaluationError("run did not complete with a nonempty answer")
    if status.get("status") != "completed" or not output:
        raise EvaluationError("Hermes final run status was not completed")
    if event_output != output:
        raise EvaluationError("Hermes event and final-status answers did not match")

    folded = output.casefold()
    for terms, label in (
        (case.required_answer_terms, "deterministic evidence"),
        (case.required_citation_terms, "citation"),
    ):
        missing = sum(term.casefold() not in folded for term in terms)
        if missing:
            raise EvaluationError(f"answer omitted {missing} expected {label} check(s)")

    normalized = folded.translate(str.maketrans({"’": "'", "‘": "'"}))
    if case.expect_abstention and not any(
        re.search(pattern, normalized) for pattern in ABSTENTION_PATTERNS
    ):
        raise EvaluationError("answer did not clearly abstain from unsupported claims")
    if case.expected_format == "markdown_report":
        headings = {
            value.casefold() for value in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", output)
        }
        if not {"findings", "evidence", "uncertainty"} <= headings:
            raise EvaluationError("answer was not a sectioned Markdown report")
    return output


def validate_run(
    case: Case,
    events: Iterable[dict[str, Any]],
    status: dict[str, Any],
    *,
    prevalidated: bool = False,
) -> list[str]:
    started: Counter[str] = Counter()
    completed: Counter[str] = Counter()
    successful: Counter[str] = Counter()
    order: list[str] = []
    event_output = ""
    approvals = Counter()

    for event in events:
        if not prevalidated:
            validate_event(case, event)
        kind = str(event.get("event", ""))
        if kind == "approval.request":
            approvals["requested"] += 1
        elif kind == "approval.responded":
            if (event.get("choice"), event.get("resolved")) != ("once", 1):
                raise EvaluationError("execute_code approval was not one-shot")
            approvals["responded"] += 1
        elif kind in {"tool.started", "tool.completed"}:
            name = str(event.get("tool", ""))
            (started if kind == "tool.started" else completed)[name] += 1
            if kind == "tool.started":
                order.append(name)
            elif event.get("error") is not True:
                successful[name] += 1
        elif kind == "run.completed":
            event_output = str(event.get("output", "")).strip()

    validate_answer(case, event_output, status)
    if started != completed:
        raise EvaluationError("tool start/completion events were not paired")
    overused = [
        name
        for name, count in started.items()
        if (name == "execute_code" and count > 1)
        or (name in case.allowed_query_tools and count > 2)
    ]
    if overused:
        raise EvaluationError(
            "tool exceeded its call bound: " + ", ".join(sorted(overused))
        )

    expected_approvals = bool(started["execute_code"])
    if any(
        approvals[name] != expected_approvals for name in ("requested", "responded")
    ):
        raise EvaluationError("execute_code did not use exactly one one-shot approval")

    # Hermes must read matching native skills before it follows them. Keep
    # those calls in the receipt, but exact route assertions cover only the
    # data tools and the explicitly approved calculation tool.
    used = set(successful) - READ_ONLY_SCAFFOLD_TOOLS
    missing, unexpected = case.required - used, used - case.required
    if missing:
        raise EvaluationError(
            f"required tools were not used: {', '.join(sorted(missing))}"
        )
    if unexpected:
        raise EvaluationError(
            "unexpected successful tools were used: " + ", ".join(sorted(unexpected))
        )
    positions = [order.index(name) for name in case.order]
    if positions != sorted(positions):
        raise EvaluationError("required route tools ran out of order")
    return order


def stop_run(base_url: str, api_key: str, run_id: str) -> None:
    try:
        request_json(
            base_url, api_key, f"/v1/runs/{run_id}/stop", payload={}, timeout=10
        )
    except EvaluationError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            _, status = request_json(base_url, api_key, f"/v1/runs/{run_id}", timeout=2)
        except EvaluationError:
            return
        if status.get("status") in {"completed", "failed", "cancelled"}:
            return
        time.sleep(0.2)


def delete_session(base_url: str, api_key: str, session_id: str) -> None:
    code, response = request_json(
        base_url,
        api_key,
        f"/api/sessions/{session_id}",
        method="DELETE",
        timeout=10,
    )
    if code != 200 or response.get("deleted") is not True:
        raise EvaluationError("Hermes did not delete the evaluation session")


def delete_session_best_effort(base_url: str, api_key: str, session_id: str) -> None:
    try:
        delete_session(base_url, api_key, session_id)
    except EvaluationError:
        pass


def run_case(
    name: str,
    case: Case,
    base_url: str,
    api_key: str,
    timeout: int,
    *,
    session_id: str | None = None,
    delete_after: bool = True,
) -> RunResult:
    session_id = session_id or f"query-claw-eval-{name}-{uuid.uuid4().hex}"
    calculation = (
        "Use execute_code only for requested arithmetic: numeric assignments and "
        "one print expression, without imports, comments, strings, containers, "
        "loops, or functions."
        if case.allow_calculation
        else "Do not use execute_code."
    )
    payload = {
        "session_id": session_id,
        "input": case.prompt,
        "instructions": (
            "Use the installed Query Claw skills. For skill_view use an exact bare name, "
            'such as {"skill_name":"query-claw"}; never join a name to itself with a '
            "colon. Split multi-view requests into complete route-specific subquestions "
            "without dropping filters. Use only the skills' read-only routes, do not "
            "browse the web, and follow the evidence-led answer format. If a read-only "
            "Query Claw data tool reports a transient transport error, retry that same "
            "tool once. "
            f"{calculation}"
        ),
    }
    run_id = ""
    events: list[dict[str, Any]] = []
    approvals = 0
    try:
        code, response = request_json(
            base_url, api_key, "/v1/runs", payload=payload, timeout=timeout
        )
        run_id = response.get("run_id", "")
        if code != 202 or not isinstance(run_id, str) or not run_id:
            raise EvaluationError("Hermes did not accept the run")
        for event in stream_events(
            base_url, api_key, run_id, deadline=time.monotonic() + timeout
        ):
            validate_event(case, event)
            events.append(event)
            if event.get("event") == "approval.request":
                approvals += 1
                if approvals > 1:
                    raise EvaluationError("run requested more than one approval")
                approval_code, approval = request_json(
                    base_url,
                    api_key,
                    f"/v1/runs/{run_id}/approval",
                    payload={"choice": "once"},
                    timeout=10,
                )
                if (
                    approval_code,
                    approval.get("choice"),
                    approval.get("resolved"),
                ) != (
                    200,
                    "once",
                    1,
                ):
                    raise EvaluationError("Hermes did not apply the one-shot approval")
        _, status = request_json(base_url, api_key, f"/v1/runs/{run_id}", timeout=10)
        tools = validate_run(case, events, status, prevalidated=True)
        result = RunResult(tuple(tools), str(status["output"]).strip())
    except EvaluationError as exc:
        observed = tuple(
            str(event.get("tool"))
            for event in events
            if event.get("event") == "tool.started" and event.get("tool")
        )
        exc.result = RunResult(observed)
        if run_id:
            stop_run(base_url, api_key, run_id)
            delete_session_best_effort(base_url, api_key, session_id)
        raise
    if delete_after:
        try:
            delete_session(base_url, api_key, session_id)
        except EvaluationError as exc:
            exc.result = result
            raise
    return result


def plan_sessions(
    selected: list[tuple[str, Case]],
) -> list[tuple[str, Case, str | None, bool]]:
    """Assign one ephemeral Hermes session to each consecutive named conversation."""
    session_ids: dict[str, str] = {}
    planned: list[tuple[str, Case, str | None, bool]] = []
    for index, (name, case) in enumerate(selected):
        if not case.session:
            planned.append((name, case, None, True))
            continue
        session_id = session_ids.setdefault(
            case.session,
            f"query-claw-eval-{case.session}-{uuid.uuid4().hex}",
        )
        next_session = (
            selected[index + 1][1].session if index + 1 < len(selected) else ""
        )
        planned.append((name, case, session_id, next_session != case.session))
    return planned


def select_cases(
    available: list[tuple[str, Case]], requested: list[str]
) -> list[tuple[str, Case]]:
    """Select suite-order cases without dropping a conversation prerequisite."""
    names = [name for name, _ in available]
    if len(requested) != len(set(requested)):
        raise EvaluationError("--case values must not be repeated")
    missing = [name for name in requested if name not in set(names)]
    if missing:
        raise EvaluationError("unknown --case value(s): " + ", ".join(missing))
    indices = [names.index(name) for name in requested]
    if indices != sorted(indices):
        raise EvaluationError("--case values must follow suite order")
    selected_indices = set(indices)
    for index in indices:
        session = available[index][1].session
        if not session:
            continue
        start = next(
            position
            for position, (_, case) in enumerate(available)
            if case.session == session
        )
        missing_prerequisites = [
            names[position]
            for position in range(start, index)
            if position not in selected_indices
        ]
        if missing_prerequisites:
            raise EvaluationError(
                f"{names[index]} requires earlier session case(s): "
                + ", ".join(missing_prerequisites)
            )
    return [available[index] for index in indices]


def display_tools(names: Iterable[str]) -> str:
    return ", ".join(name.rsplit("__", 1)[-1] for name in names)


def prepare_private_output(path: Path) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
    except OSError as exc:
        raise EvaluationError(
            f"could not create private evaluation output: {path}"
        ) from exc


def append_result(path: Path, record: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        raise EvaluationError(
            f"could not append private evaluation output: {path}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "evaluations" / "smoke.json",
        help="JSON or JSONL suite loaded on the evaluator host",
    )
    parser.add_argument(
        "--case", action="append", dest="cases", help="run one case id; repeatable"
    )
    parser.add_argument(
        "--output", type=Path, help="write private mode-0600 JSONL results"
    )
    parser.add_argument(
        "--fail-fast", action="store_true", help="stop after the first failed case"
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--judge",
        action="store_true",
        help="score successful answers with an OpenAI-compatible judge",
    )
    parser.add_argument(
        "--judge-base-url",
        default=os.environ.get("QUERY_CLAW_JUDGE_BASE_URL", ""),
    )
    parser.add_argument(
        "--judge-model", default=os.environ.get("QUERY_CLAW_JUDGE_MODEL", "")
    )
    parser.add_argument("--judge-timeout", type=int, default=120)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    api_key = os.environ.get("API_SERVER_KEY", "")
    if not api_key:
        parser.error(
            "API_SERVER_KEY is not set; export the NemoClaw Hermes gateway token"
        )
    try:
        base_url = loopback_api_url(os.environ.get("NEMOCLAW_HERMES_API_PORT", "8642"))
    except EvaluationError as exc:
        parser.error(str(exc))
    judge_api_key = os.environ.get("QUERY_CLAW_JUDGE_API_KEY", "")
    if args.judge and (
        not args.judge_base_url
        or not args.judge_model
        or not judge_api_key
        or args.judge_timeout <= 0
    ):
        parser.error(
            "--judge requires QUERY_CLAW_JUDGE_BASE_URL, QUERY_CLAW_JUDGE_MODEL, "
            "QUERY_CLAW_JUDGE_API_KEY, and a positive --judge-timeout"
        )
    try:
        suite_name, available = load_suite(args.suite)
        try:
            suite_sha256 = hashlib.sha256(args.suite.read_bytes()).hexdigest()
        except OSError as exc:
            raise EvaluationError(
                f"could not fingerprint evaluation suite: {args.suite}"
            ) from exc
        requested = args.cases or [name for name, _ in available]
        selected = select_cases(available, requested)
        if args.output:
            prepare_private_output(args.output)
    except EvaluationError as exc:
        parser.error(str(exc))

    passed = 0
    receipt_id = uuid.uuid4().hex
    run_started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    latencies: list[float] = []
    executed = 0
    failed_sessions: set[str] = set()
    for name, case, session_id, delete_after in plan_sessions(selected):
        started_at = time.monotonic()
        result = RunResult(())
        judge_result: evaluation_judge.JudgeResult | None = None
        error = ""
        try:
            if case.session and case.session in failed_sessions:
                raise EvaluationError("an earlier turn in this session failed")
            result = run_case(
                name,
                case,
                base_url,
                api_key,
                args.timeout,
                session_id=session_id,
                delete_after=delete_after,
            )
            if args.judge:
                try:
                    judge_result = evaluation_judge.score_answer(
                        base_url=args.judge_base_url,
                        api_key=judge_api_key,
                        model=args.judge_model,
                        question=case.prompt,
                        answer=result.answer,
                        expected_facts=case.required_answer_terms,
                        expected_citations=case.required_citation_terms,
                        expected_routes=tuple(sorted(case.routes)),
                        expected_format=case.expected_format,
                        expect_abstention=case.expect_abstention,
                        timeout=args.judge_timeout,
                    )
                except evaluation_judge.JudgeError as exc:
                    raise EvaluationError(str(exc), result) from exc
                if not judge_result.passed:
                    raise EvaluationError("semantic judge rejected the answer", result)
            passed += 1
        except EvaluationError as exc:
            error = str(exc)
            result = exc.result
            if case.session:
                failed_sessions.add(case.session)
            if session_id:
                delete_session_best_effort(base_url, api_key, session_id)
        latency = time.monotonic() - started_at
        latencies.append(latency)
        executed += 1
        record = {
            "schema_version": 2,
            "evaluator_version": EVALUATOR_VERSION,
            "receipt_id": receipt_id,
            "run_started_at": run_started_at,
            "suite": suite_name,
            "suite_sha256": suite_sha256,
            "case_id": name,
            "passed": not error,
            "latency_seconds": round(latency, 3),
            "tools": list(result.tools),
            "error": error or None,
            "declared_stack_versions": {
                "nemoclaw": "0.0.120",
                "hermes": "0.20.6",
                "openshell": "0.0.106",
            },
            "declared_model": (
                os.environ.get("NEMOCLAW_MODEL") or os.environ.get("LLM_MODEL") or None
            ),
        }
        if judge_result is not None:
            record["judge"] = judge_result.receipt()
            record["judge_rubric_version"] = evaluation_judge.RUBRIC_VERSION
            record["judge_model"] = args.judge_model
        if args.output:
            append_result(args.output, record)
        if error:
            print(f"FAIL {name:<24} {latency:6.1f}s  {error}", file=sys.stderr)
            if args.fail_fast:
                break
        else:
            print(f"PASS {name:<24} {latency:6.1f}s  {display_tools(result.tools)}")

    mean = sum(latencies) / len(latencies) if latencies else 0.0
    print(
        f"Query Claw Hermes E2E: {passed}/{executed} passed "
        f"({len(selected)} selected, {mean:.1f}s mean)"
    )
    return 0 if passed == len(selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())

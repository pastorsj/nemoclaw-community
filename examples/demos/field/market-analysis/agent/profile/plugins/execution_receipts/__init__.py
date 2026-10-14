# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The execution-receipts Hermes plugin: source scope, tool receipts and served models.

Hermes calls four hooks:

- ``pre_tool_call`` passes each data tool the job's selected sources that allow it (Auto Ontology needs one
  but takes no argument), and blocks MCP tools that are not in the tool registry and ``skill_manage``.
- ``post_tool_call`` and ``transform_tool_result`` turn a data tool's result into a receipt
  (``contracts/schemas/receipt.schema.json``) and post it to the job API, once per call, and
  ``transform_tool_result`` adds the receipt id to the result as ``evidence_id``, so the agent can cite it. It also
  shortens a result the agent could not read whole (``MAX_RESULT_CHARS``).
  Hermes' agent loop fires ``transform_tool_result`` first and a direct dispatch fires ``post_tool_call``
  first; a blocked or raised call gets only ``post_tool_call``. Whichever comes first records the receipt.
- ``post_api_request`` reports which model Switchyard served for each model call, and at which tier.

The job API starts every run with the job id as the Hermes session id, so ``session_id`` is the job id.
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path("/opt/agent/contracts/tool-registry.json")
TIMEOUT_SECONDS = 5
# A job answers 503 until it has recorded its Hermes run, which can trail the run's first calls by a moment.
ATTEMPTS = 3

# Hermes saves an MCP result longer than 50,000 characters to a file the sandboxed agent cannot read and shows the
# model a 1,500-character preview instead. The data tools keep their results under this many characters as the
# agent reads them; the plugin shortens one that does not (Auto Ontology's rows, say) the same way.
MAX_RESULT_CHARS = 30_000
SHORTENED_NOTE = "shortened_to_fit"

# The display-safe fitting rules from contracts/README.md. The job API rejects a receipt with any
# key that matches this pattern, so the plugin drops such keys first.
BANNED_KEY = re.compile(
    r"thought|reasoning|prompt|embedding|password|passwd|secret|token|credential|api[_-]?key|authorization|cookie"
    r"|connection(?:_string)?|private[_-]?key|access[_-]?key|filesystem|file[_-]?path|storage[_-]?uri|dsn",
    re.IGNORECASE,
)
# Receipt schema limits that a tool result can exceed. Other lists keep 100 items, other strings 32,000 characters.
LIST_LIMITS = {"hits": 25, "source_ids": 32, "warnings": 20, "limitations": 20, "resolution_lineage": 40}
STRING_LIMITS = {"snippet": 1500, "title": 1000, "url": 2048, "answer": 4000, "sql": 12000, "phrase": 500}
# The receipt schema refuses control characters other than tab, line feed and carriage return in text.
UNSAFE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_ITEMS = 100
MAX_TEXT = 32_000
SQL_ROWS = 25
SQL_COLUMNS = 40
LINEAGE_KEYS = ("phrase", "ontology_object", "table", "column")
# ask_question takes only the question here: a model-supplied thread, database, prediction, evidence or source
# scope is dropped (Auto Ontology has no source_ids argument, but the other data tools do, so the model may send it).
ASK_QUESTION_DROPPED = ("conversation_id", "target_db", "prediction", "evidence", "source_ids")

# Receipt fields whose keys are data (payload fields, source ids, SQL columns), so they keep their names.
OPEN_FIELDS = {
    "retrieval_evidence": {"metadata", "candidate_counts", "params", "search_params"},
    "analytics_result": {"payload", "public_parameters"},
    "structured_prediction": set(),
    "structured_query": {"rows"},
}


class JobApi:
    """The job API's internal routes. The key is an OpenShell placeholder that the sandbox swaps for the real one."""

    def __init__(self, base_url: str, key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key

    def get(self, job_id: str, route: str) -> dict:
        return self._send("GET", job_id, route)

    def post(self, job_id: str, route: str, body: dict) -> None:
        self._send("POST", job_id, route, body)

    def _send(self, method: str, job_id: str, route: str, body: dict | None = None) -> Any:
        request = Request(
            f"{self.base_url}/internal/hermes/jobs/{quote(job_id, safe='')}/{route}",
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-Receipt-Key": self.key, "Content-Type": "application/json"},
            method=method,
        )
        for attempt in range(1, ATTEMPTS + 1):
            try:
                with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                    data = response.read()
                return json.loads(data) if data else None
            except HTTPError as error:
                if error.code != 503 or attempt == ATTEMPTS:
                    raise
                time.sleep(float(error.headers.get("Retry-After") or 1))


class ExecutionReceipts:
    """The four hooks. Hermes passes every hook argument by keyword, and more arguments than a hook needs."""

    def __init__(self, registry: dict, api: JobApi) -> None:
        self.tools = {tool["hermes_name"]: tool for tool in registry["tools"]}
        self.api = api
        self._scope: tuple[str, dict] | None = None
        # Per tool call recorded by one of its two hooks: the evidence id, or None if there is none to cite.
        self._recorded: dict[str, str | None] = {}

    def scope(self, job_id: str) -> dict:
        """The job's execution scope. It never changes, and jobs run one at a time, so only the last one is kept."""
        if self._scope is None or self._scope[0] != job_id:
            self._scope = (job_id, self.api.get(job_id, "execution-scope"))
            self._recorded.clear()  # calls that got only one hook (blocked or raised) end with their job
        return self._scope[1]

    def pre_tool_call(
        self, *, tool_name: str = "", session_id: str = "", args: dict | None = None, **_: Any
    ) -> dict | None:
        if tool_name == "skill_manage":
            # skills.write_approval already stages every write; this is defense in depth.
            return _block("Skills are read-only in this application.")
        if not tool_name.startswith("mcp__"):
            return None
        tool = self.tools.get(tool_name)
        if tool is None:
            return _block(f"{tool_name} is not a registered data tool.")
        # Hermes runs the tool anyway if this hook raises, so any failure must block instead.
        try:
            scope = self.scope(session_id)
            source_ids = [source["id"] for source in scope["sources"] if tool["family"] in source["capabilities"]]
        except Exception as error:
            logger.warning("Could not read the execution scope of job %r: %s", session_id, error)
            return _block("The job's data-source scope is unavailable, so data tools are blocked.")
        if not source_ids:
            return _block(f"None of the job's selected sources allows {tool['label']}.")
        if tool["server"] == "auto_ontology":
            if tool["id"] == "ask_question":
                _drop_arguments(args, ASK_QUESTION_DROPPED, tool_name)
            return None  # Auto Ontology serves only the pack's database, so it takes no scope argument.
        return {"action": "modify", "args": {"source_ids": source_ids}}

    def post_tool_call(self, **call: Any) -> None:
        self._record(**call)

    def transform_tool_result(self, *, result: str | None = None, **call: Any) -> str | None:
        """Put ``evidence_id`` first in a result whose completed receipt the job API stored, so no cut drops it, and
        shorten a data tool's result that is too long for the agent to read whole."""
        evidence_id = self._record(result=result, **call)
        if result is None or call.get("tool_name") not in self.tools:
            return None
        try:
            envelope = json.loads(result)
        except ValueError:
            return None
        if not isinstance(envelope, dict):
            return None
        if evidence_id is not None:
            envelope = {"evidence_id": evidence_id, **envelope}
        output = json.dumps(envelope, ensure_ascii=False)
        if len(output) > MAX_RESULT_CHARS:
            return shorten(envelope)
        return output if evidence_id is not None else None

    def _record(
        self,
        *,
        tool_name: str = "",
        args: dict | None = None,
        result: str | None = None,
        session_id: str = "",
        tool_call_id: str = "",
        turn_id: str = "",
        duration_ms: int = 0,
        status: str = "ok",
        error_type: str | None = None,
        error_message: str | None = None,
        **_: Any,
    ) -> str | None:
        """Post a registered tool call's receipt on its first hook; return the evidence id on both hooks."""
        tool = self.tools.get(tool_name)
        if tool is None or not session_id or not tool_call_id:
            return None
        if tool_call_id in self._recorded:
            return self._recorded.pop(tool_call_id)
        evidence_id = None
        # Receipts are best effort: a failure here must never fail the tool call.
        try:
            receipt = build_receipt(
                tool,
                job_id=session_id,
                database_name=self.scope(session_id)["database_name"],
                tool_call_id=tool_call_id,
                turn_id=turn_id,
                args=args or {},
                result=result,
                status=status,
                error_type=error_type,
                error_message=error_message,
                duration_ms=duration_ms,
            )
            self.api.post(session_id, "tool-receipts", receipt)
            if receipt["status"] == "completed":
                evidence_id = receipt["receiptId"]
        except Exception as error:
            logger.warning("Could not record the receipt of %s call %s: %s", tool_name, tool_call_id, error)
        self._recorded[tool_call_id] = evidence_id
        return evidence_id

    def post_api_request(
        self,
        *,
        session_id: str = "",
        api_request_id: str = "",
        turn_id: str = "",
        response_model: str | None = None,
        usage: dict | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        **_: Any,
    ) -> None:
        if not session_id or not response_model:
            return
        try:
            models = self.scope(session_id).get("models") or {}
            usage = usage or {}
            self.api.post(
                session_id,
                "llm-calls",
                {
                    "api_request_id": api_request_id,
                    "turn_id": turn_id or None,
                    "served_model": response_model,
                    "tier": next((tier for tier, model in models.items() if model == response_model), None),
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "started_at": _timestamp(started_at),
                    "completed_at": _timestamp(ended_at),
                },
            )
        except Exception as error:
            logger.warning("Could not report model call %s of job %r: %s", api_request_id, session_id, error)


def build_receipt(
    tool: dict,
    *,
    job_id: str,
    database_name: str | None,
    tool_call_id: str,
    turn_id: str,
    args: dict,
    result: str | None,
    status: str,
    error_type: str | None,
    error_message: str | None,
    duration_ms: int,
) -> dict:
    """A ReceiptV2 for one registered tool call, from Hermes' ``post_tool_call`` arguments and the job's database."""
    kind = tool["receipt_kind"]
    content = None
    if status == "ok":
        content, failure = CONTENT[kind](structured_result(result), args, database_name)
        content = _camel_keys(_fit(content), OPEN_FIELDS[kind])
    else:
        # The call raised, the tool reported an MCP error, or pre_tool_call blocked it.
        failure = (error_type or "tool_error", error_message)
    return {
        "schemaVersion": "2",
        "artifactKind": kind,
        "receiptId": receipt_id(job_id, tool_call_id),
        "jobId": job_id,
        "invocationId": invocation_id(tool_call_id),
        "turnId": turn_id or None,
        "toolName": tool["hermes_name"],
        "status": "failed" if failure else "completed",
        "errorType": failure[0] if failure else None,
        "errorSummary": failure[1][:600] if failure and failure[1] else None,
        "durationMs": int(duration_ms),
        "traceId": None,
        "spanId": None,
        "occurredAt": _timestamp(),
        "content": content,
    }


def shorten(envelope: dict) -> str:
    """The Hermes MCP envelope, ``{"result": "<the tool's JSON text>", ...}``, at most ``MAX_RESULT_CHARS`` long.

    The longest list in the tool's result loses rows from its end (a result that says it is ``truncated`` says so),
    then the longest text is cut, until the envelope fits; ``shortened_to_fit`` says what was left out.
    """
    try:
        value = json.loads(envelope["result"]) if isinstance(envelope.get("result"), str) else envelope.get("result")
    except ValueError:
        value = envelope["result"]
    notes: dict[str, str] = {}
    if value and "structuredContent" in envelope:  # a second copy beside the text: the text is enough
        envelope = {key: item for key, item in envelope.items() if key != "structuredContent"}
        notes["structuredContent"] = "structuredContent is left out; result holds the same data"

    def render() -> str:
        result = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
        noted = {**envelope, "result": result, **({SHORTENED_NOTE: "; ".join(notes.values())} if notes else {})}
        return json.dumps(noted, ensure_ascii=False)

    output = render()
    original: dict[str, int] = {}
    while len(output) > MAX_RESULT_CHARS:
        found = _longest(value, lists=True)
        if found is not None:
            path, rows = found
            original.setdefault(path, len(rows))
            del rows[max(1, len(rows) - max(1, len(rows) // 4)) :]
            notes[path] = f"{path} lists the first {len(rows)} of {original[path]} items"
            if isinstance(value, dict) and "truncated" in value:
                value["truncated"] = True
        elif (text := _longest(value, lists=False)) is not None:
            path, (container, key) = text
            kept = max(0, len(container[key]) - (len(output) - MAX_RESULT_CHARS) - 200)
            container[key] = container[key][: min(kept, len(container[key]) // 2)] + "…"
            notes[path] = f"{path} is cut"
        elif isinstance(value, str) and len(value) > 1:
            value = value[: max(1, len(value) - (len(output) - MAX_RESULT_CHARS) - 200)] + "…"
            notes["result"] = "the result text is cut"
        else:
            break
        output = render()
    return output


def _longest(value: Any, *, lists: bool, path: str = "result") -> Any:
    """The longest list with more than one item (``lists``), or the longest string over 200 characters, inside
    ``value``: ``(path, list)`` or ``(path, (container, key))``."""
    best: Any = None
    best_size = 0

    def visit(item: Any, where: str, container: Any = None, key: Any = None) -> None:
        nonlocal best, best_size
        if isinstance(item, dict):
            for name, child in item.items():
                visit(child, f"{where}.{name}", item, name)
        elif isinstance(item, list):
            size = len(json.dumps(item, ensure_ascii=False))
            if lists and len(item) > 1 and size > best_size:
                best, best_size = (where, item), size
            for index, child in enumerate(item):
                visit(child, f"{where}[{index}]", item, index)
        elif isinstance(item, str) and not lists and container is not None and len(item) > 200:
            if len(item) > best_size:
                best, best_size = (where, (container, key)), len(item)

    visit(value, path)
    return best


def invocation_id(tool_call_id: str) -> str:
    """The tool call's node in the job's execution graph, derived as the job API derives it from Hermes' events."""
    return _correlation(f"hermes-tool:{_correlation(tool_call_id, 'tool-call')}", "invocation")


def _correlation(value: str, prefix: str) -> str:
    """``value`` when it is a usable id (at most 256 characters, no whitespace or control characters), else a hash."""
    candidate = value.strip()
    if candidate and len(candidate) <= 256 and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in candidate):
        return candidate
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()[:32]}"


def receipt_id(job_id: str, tool_call_id: str) -> str:
    """The evidence id: stable for one tool call of one job."""
    return "hermes-receipt:" + hashlib.sha256(f"{job_id}\0{tool_call_id}".encode()).hexdigest()


def structured_result(result: str) -> dict:
    """The tool's structured result inside Hermes' MCP envelope, ``{"result": ..., "structuredContent"?: ...}``."""
    envelope = json.loads(result)
    value = envelope.get("structuredContent", envelope["result"])
    return json.loads(value) if isinstance(value, str) else value


# Per receipt kind: the receipt content and, when the tool reports a failure, (errorType, errorSummary).


def _retrieval_content(result: dict, args: dict, database_name: str | None) -> tuple[dict, None]:
    hits = [{**hit, "published_at": _aware_timestamp(hit.get("published_at"))} for hit in result["hits"]]
    return {**result, "hits": hits}, None


def _analytics_content(result: dict, args: dict, database_name: str | None) -> tuple[dict, tuple[str, str] | None]:
    public_parameters = {name: value for name, value in args.items() if name != "source_ids"}
    error = result["error"]
    failure = (error["code"], error["message"]) if result["status"] == "failed" else None
    return {**result, "public_parameters": public_parameters}, failure


def _prediction_content(result: dict, args: dict, database_name: str | None) -> tuple[dict, tuple[str, str] | None]:
    return result, (None if result["available"] else ("evidence_unavailable", result["reason"]))


def _query_content(result: dict, args: dict, database_name: str | None) -> tuple[dict, None]:
    rows = result.get("rows") or []
    cut = len(rows) > SQL_ROWS or any(len(row) > SQL_COLUMNS for row in rows)
    return {
        "query": args["question"][:1000],
        "database_name": database_name,
        "answer": result.get("answer"),
        "sql": result.get("sql"),
        "rows": [dict(list(row.items())[:SQL_COLUMNS]) for row in rows[:SQL_ROWS]],
        "source_row_count": result.get("row_count", len(rows)),
        "truncated": bool(result.get("truncated")) or cut,
        # Auto Ontology leaves out a table or column it could not resolve; the receipt keeps only complete bindings.
        "resolution_lineage": [
            {key: binding[key] for key in LINEAGE_KEYS}
            for binding in result.get("resolution_lineage") or []
            if all(binding.get(key) for key in LINEAGE_KEYS)
        ],
    }, None


CONTENT = {
    "retrieval_evidence": _retrieval_content,
    "analytics_result": _analytics_content,
    "structured_prediction": _prediction_content,
    "structured_query": _query_content,
}


def _fit(value: Any, key: str | None = None) -> Any:
    """Drop banned keys, cut lists and strings to the schema's limits (flagging cut payload lists), and replace the
    control characters the schema refuses with spaces."""
    if isinstance(value, dict):
        fitted = {name: _fit(item, name) for name, item in value.items() if not BANNED_KEY.search(name)}
        for name, item in value.items():
            flag = f"{name}_truncated"
            if isinstance(item, list) and len(item) > LIST_LIMITS.get(name, MAX_ITEMS) and flag in fitted:
                fitted[flag] = True
        return fitted
    if isinstance(value, list):
        return [_fit(item) for item in value[: LIST_LIMITS.get(key, MAX_ITEMS)]]
    if isinstance(value, str):
        return UNSAFE_CONTROL.sub(" ", value[: STRING_LIMITS.get(key, MAX_TEXT)])
    return value


def _camel_keys(value: Any, open_fields: set[str]) -> Any:
    """Rename receipt fields to the schema's camelCase. The keys inside ``open_fields`` are data and stay as sent."""
    if isinstance(value, dict):
        return {_camel(name): v if name in open_fields else _camel_keys(v, open_fields) for name, v in value.items()}
    if isinstance(value, list):
        return [_camel_keys(item, open_fields) for item in value]
    return value


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.capitalize() for part in rest)


def _aware_timestamp(value: Any) -> str | None:
    """Keep a timestamp only when it has a timezone; the UI would read a naive one as local time."""
    try:
        return value if datetime.fromisoformat(value).tzinfo else None
    except (TypeError, ValueError):
        return None


def _timestamp(epoch_seconds: float | None = None) -> str:
    moment = datetime.now(UTC) if epoch_seconds is None else datetime.fromtimestamp(epoch_seconds, UTC)
    return moment.isoformat().replace("+00:00", "Z")


def _block(message: str) -> dict:
    return {"action": "block", "message": message}


def _drop_arguments(args: dict | None, names: tuple[str, ...], tool_name: str) -> None:
    """Remove ``names`` from the call's own argument dict, which Hermes passes to this hook and then dispatches.

    A ``modify`` directive cannot do this: Hermes merges its keys into the arguments and never removes one.
    """
    if not isinstance(args, dict):
        return
    dropped = [name for name in names if name in args]
    for name in dropped:
        del args[name]
    if dropped:
        logger.info("Dropped the model-supplied %s argument(s) of %s", ", ".join(dropped), tool_name)


def register(ctx: Any) -> None:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    api = JobApi(os.environ.get("HERMES_RECEIPT_API_URL", ""), os.environ.get("HERMES_RECEIPT_API_KEY", ""))
    plugin = ExecutionReceipts(registry, api)
    for hook in ("pre_tool_call", "post_tool_call", "transform_tool_result", "post_api_request"):
        ctx.register_hook(hook, getattr(plugin, hook))

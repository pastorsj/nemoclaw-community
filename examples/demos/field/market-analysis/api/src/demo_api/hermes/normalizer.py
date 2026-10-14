# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Turn Hermes Runs events into ``execution.v2`` events, the only event format the API stores.

One normalizer follows one run. It pairs each ``tool.completed`` with its ``tool.started`` (by
Hermes' ``tool_call_id``, else first-in-first-out per tool) and labels registered tools from the
tool registry. It never reads the question or infers a route, and it never copies model text:
token deltas are dropped and reasoning is recorded only as "reasoning in progress".
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL
from uuid import uuid5

from demo_api.events import COMPONENT_BY_FAMILY
from demo_api.events import DisplaySafeProjection
from demo_api.events import EventProvenance
from demo_api.events import ExecutionEventV2
from demo_api.events import ExecutionState
from demo_api.registry import ToolRegistry

from .client import RunEvent

NORMALIZATION_VERSION = "hermes-runs.v1"

_TERMINAL: dict[str, tuple[ExecutionState, str]] = {
    "run.completed": ("completed", "Run completed"),
    "run.failed": ("failed", "Run failed"),
    "run.cancelled": ("cancelled", "Run cancelled"),
    "run.interrupted": ("cancelled", "Run interrupted"),
}
_STATE_BY_SUFFIX: dict[str, ExecutionState] = {
    "start": "started",
    "started": "started",
    "complete": "completed",
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "interrupted": "cancelled",
}
_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens")
_RUN_CREATED_COUNTS = ("prior_turn_count", "conversation_message_count")
_OPEN_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SECRET_PATTERNS = (
    (re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(?:nvapi|sk)-[A-Za-z0-9._-]{8,}"), "[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?key|auth(?:orization)?|password|passwd|secret|cookie)"
            r"""(\s*[:=]\s*)(?:['"])?[^\s,;'"]+(?:['"])?"""
        ),
        r"\1\2[REDACTED]",
    ),
)


class EventNormalizer:
    def __init__(self, *, job_id: str, session_id: str, registry: ToolRegistry) -> None:
        self._job_id = job_id
        self._session_id = session_id
        self._registry = registry
        self._ordinal = 0
        self._tool_count = 0
        self._open_tools: list[tuple[str, str]] = []  # (Hermes tool name, invocation id)
        self.completed_registered_calls: set[str] = set()  # invocation ids that must have a receipt
        self.terminal_seen = False
        self.usage: dict[str, int] = {}  # the run's token usage, from its terminal event
        self.known_tool_duration_ms = 0  # the durations Hermes reported for the run's finished tool calls

    @property
    def tool_call_count(self) -> int:
        """Tool calls the run started (Hermes's own tools included)."""
        return self._tool_count

    def normalize(self, event: RunEvent) -> ExecutionEventV2 | None:
        """Project one Hermes event, or return None for events the product does not store."""
        self._ordinal += 1
        if event.event == "message.delta":
            return None  # the final answer arrives whole; per-token rows would add nothing inspectable
        root = run_invocation_id(event.run_id)
        fields: dict[str, Any] = {"component_id": "hermes.agent", "invocation_id": root}
        if event.event in _TERMINAL:
            self.terminal_seen = True
            state, label = _TERMINAL[event.event]
            summary = _safe_text(event.data.get("error"), 500) if event.event == "run.failed" else None
            self.usage = {key: value for key, value in _usage(event.data).items() if key in _USAGE_KEYS}
            display = DisplaySafeProjection(label=label, summary=summary, attributes=_usage(event.data))
        elif event.event == "run.created":
            state = "started"
            counts = {key: event.data[key] for key in _RUN_CREATED_COUNTS if _count(event.data.get(key))}
            display = DisplaySafeProjection(label="Run accepted", attributes=counts)
        elif event.event == "reasoning.available":
            state, display = "progress", DisplaySafeProjection(label="Reasoning in progress")
        elif event.event == "run.heartbeat":
            state, display = "progress", DisplaySafeProjection(label="Working on the answer")
        elif event.event in {"tool.started", "tool.completed"}:
            state, display, fields = self._tool(event, root)
        else:
            state = _STATE_BY_SUFFIX.get(event.event.rsplit(".", maxsplit=1)[-1].casefold(), "progress")
            fields["component_id"] = "hermes.runtime"
            label = f"Hermes event: {_safe_text(event.event, 128) or 'unknown'}"
            field_count = len(set(event.data) - {"event", "run_id", "timestamp"})
            display = DisplaySafeProjection(label=label, attributes={"field_count": field_count})

        source_event_id = self._source_event_id(event)
        kind = _open_identifier(event.event, fallback="hermes.unknown")
        return ExecutionEventV2(
            event_id=uuid5(NAMESPACE_URL, f"urn:nvidia:hermes-execution:{source_event_id}"),
            job_id=self._job_id,
            run_id=event.run_id,
            session_id=self._session_id,
            event_kind=kind,
            state=state,
            occurred_at=_occurred_at(event.timestamp),
            display=display,
            provenance=EventProvenance(
                source_system="hermes.runs",
                source_event_id=source_event_id,
                source_event_kind=kind,
                normalization_version=NORMALIZATION_VERSION,
            ),
            **fields,
        )

    def publication(
        self,
        run_id: str,
        *,
        resolution: dict[str, Any],
        metrics: dict[str, Any],
        published_at: float | None = None,
    ) -> list[ExecutionEventV2]:
        """The events of publishing the answer, after the run: the formatted response, its citation resolution
        (``resolution``: status and counts) and the run's metrics (``metrics``: runtime profile, tool calls,
        tokens). They are the original demo's last replay steps."""
        steps = (
            ("report.completed", "Response formatted", {}),
            ("report.reference_resolution", "Citations resolved", resolution),
            ("report.metrics", "Run metrics available", metrics),
        )
        events = []
        for kind, label, attributes in steps:
            self._ordinal += 1
            source_event_id = f"publication:{self._job_id}:{kind}"
            events.append(
                ExecutionEventV2(
                    event_id=uuid5(NAMESPACE_URL, f"urn:nvidia:hermes-execution:{source_event_id}"),
                    job_id=self._job_id,
                    run_id=run_id,
                    session_id=self._session_id,
                    event_kind=kind,
                    state="completed",
                    occurred_at=_occurred_at(published_at),
                    display=DisplaySafeProjection(label=label, attributes=attributes),
                    provenance=EventProvenance(
                        source_system="demo-api",
                        source_event_id=source_event_id,
                        source_event_kind=kind,
                        normalization_version=NORMALIZATION_VERSION,
                    ),
                    component_id="hermes.agent",
                    invocation_id=run_invocation_id(run_id),
                )
            )
        return events

    def _tool(self, event: RunEvent, root: str) -> tuple[ExecutionState, DisplaySafeProjection, dict[str, Any]]:
        raw_name = event.data.get("tool")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else "unknown"
        tool = self._registry.by_hermes_name(name)
        call_id = event.data.get("tool_call_id")
        invocation_id = tool_invocation_id(call_id) if isinstance(call_id, str) and call_id.strip() else None

        if event.event == "tool.started":
            self._tool_count += 1
            invocation_id = invocation_id or _invocation_id("tool", event.run_id, self._tool_count)
            self._open_tools.append((name, invocation_id))
            state: ExecutionState = "started"
            suffix = "started"
            attributes: dict[str, Any] = {}
        else:
            match = next(
                (
                    index
                    for index, (open_name, open_id) in enumerate(self._open_tools)
                    if open_id == invocation_id or (invocation_id is None and open_name == name)
                ),
                None,
            )
            if match is not None:
                invocation_id = self._open_tools.pop(match)[1]
            elif invocation_id is None:
                self._tool_count += 1
                invocation_id = _invocation_id("tool", event.run_id, self._tool_count)
            if tool is not None:
                self.completed_registered_calls.add(invocation_id)
            failed = event.data.get("error") is True
            state, suffix = ("failed", "failed") if failed else ("completed", "completed")
            attributes = {"reported_error": failed}
            duration = event.data.get("duration")
            if isinstance(duration, int | float) and not isinstance(duration, bool) and math.isfinite(duration):
                attributes["duration_seconds"] = max(float(duration), 0.0)
                self.known_tool_duration_ms += round(attributes["duration_seconds"] * 1000)

        label = tool.label if tool is not None else (_safe_text(name, 120) or "Unknown tool")
        fields: dict[str, Any] = {
            "component_id": COMPONENT_BY_FAMILY[tool.family] if tool is not None else "hermes.tool",
            "invocation_id": invocation_id,
            "parent_invocation_id": root,
            "tool_server": tool.server if tool is not None else None,
            "tool_name": tool.id if tool is not None else _open_identifier(name, fallback="unknown-tool"),
            "capability_id": tool.family if tool is not None else None,
        }
        display = DisplaySafeProjection(label=f"{label} {suffix}", attributes=attributes, inspectable=tool is not None)
        return state, display, fields

    def _source_event_id(self, event: RunEvent) -> str:
        payload = {
            "run_id": event.run_id,
            "ordinal": self._ordinal,
            "sse_id": event.sse_id,
            "event": event.event,
            "data": event.data,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return f"hermes-event:{hashlib.sha256(encoded).hexdigest()}"


def run_invocation_id(run_id: str) -> str:
    """The invocation id of the run itself: the root of the execution graph."""
    return _invocation_id("run", run_id, 0)


def tool_invocation_id(tool_call_id: str) -> str:
    """The invocation id of a tool call; the agent plugin derives the same one for its receipt."""
    return safe_correlation(f"hermes-tool:{safe_correlation(tool_call_id, prefix='tool-call')}", prefix="invocation")


def safe_correlation(value: str, *, prefix: str) -> str:
    candidate = value.strip()
    if candidate and len(candidate) <= 256 and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in candidate):
        return candidate
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()[:32]}"


def _invocation_id(kind: str, run_id: str, ordinal: int) -> str:
    return f"hermes-{kind}:{hashlib.sha256(run_id.encode('utf-8', errors='replace')).hexdigest()[:24]}:{ordinal}"


def _open_identifier(value: str, *, fallback: str) -> str:
    stripped = value.strip()
    if _OPEN_IDENTIFIER.fullmatch(stripped):
        return stripped
    return f"{fallback}.{hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()[:20]}"


def _usage(data: dict[str, Any]) -> dict[str, Any]:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    attributes: dict[str, Any] = {key: usage[key] for key in _USAGE_KEYS if _count(usage.get(key), minimum=0)}
    if isinstance(data.get("output"), str):
        attributes["output_characters"] = len(data["output"])
    return attributes


def _count(value: Any, *, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= 10**9


def _occurred_at(timestamp: float | None) -> datetime:
    if timestamp is not None and math.isfinite(timestamp):
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(UTC)


def _safe_text(value: Any, max_chars: int) -> str | None:
    """Display text with credentials masked and whitespace collapsed, or None."""
    if not isinstance(value, str):
        return None
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    text = " ".join(value.split())
    if not text:
        return None
    return text if len(text) <= max_chars else f"{text[: max_chars - 1].rstrip()}…"

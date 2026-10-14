# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Progress budgets for one Hermes run, judged only from its public events.

The guard never reads event text. It stops a run that goes silent, stops making progress,
calls too many tools, or repeats itself. The API's own heartbeats are not progress: the
application cannot vouch that the agent is working.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .client import RunEvent

_RUN_EVENTS = {"run.created", "run.cancelled", "run.completed", "run.failed", "run.interrupted"}


class HermesRunBudgetExceeded(RuntimeError):
    def __init__(self, run_id: str, budget: str) -> None:
        super().__init__(f"Hermes run {run_id} exceeded its {budget} budget")
        self.budget = budget


@dataclass(frozen=True, slots=True)
class ProgressLimits:
    idle_timeout_seconds: float = 600.0
    no_progress_timeout_seconds: float = 600.0
    max_tool_calls: int = 128
    max_duplicate_events: int = 64


class ProgressGuard:
    _MAX_REMEMBERED = 4096

    def __init__(
        self, run_id: str, limits: ProgressLimits | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._run_id = run_id
        self._limits = limits or ProgressLimits()
        self._clock = clock
        self._last_event_at = self._last_progress_at = clock()
        self._tool_calls = 0
        self._duplicates = 0
        self._open_tool_calls: set[str] = set()
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()

    def observe(self, event: RunEvent) -> None:
        """Record one Hermes event; raise at once when a count budget is exceeded."""
        if event.event == "run.heartbeat":
            return
        now = self._clock()
        self._last_event_at = now
        identity = _identity(event)
        if identity is not None and identity in self._seen:
            self._duplicates += 1
            if self._duplicates > self._limits.max_duplicate_events:
                raise HermesRunBudgetExceeded(self._run_id, "duplicate events")
        else:
            if identity is not None:
                self._remember(identity)
            self._last_progress_at = now
            call_id = event.data.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                if event.event == "tool.started":
                    self._open_tool_calls.add(call_id)
                elif event.event in {"tool.completed", "tool.failed"}:
                    self._open_tool_calls.discard(call_id)
            if event.event == "tool.started":
                self._tool_calls += 1
                if self._tool_calls > self._limits.max_tool_calls:
                    raise HermesRunBudgetExceeded(self._run_id, "tool calls")
        self.check()

    def check(self) -> None:
        """Enforce the time budgets; called on every event and on every heartbeat."""
        now = self._clock()
        # A tool may be silent until it returns, so an open call suspends the idle budget.
        # The no-progress budget and the run's wall deadline still bound a wedged tool.
        if not self._open_tool_calls and now - self._last_event_at > self._limits.idle_timeout_seconds:
            raise HermesRunBudgetExceeded(self._run_id, "idle")
        if now - self._last_progress_at > self._limits.no_progress_timeout_seconds:
            raise HermesRunBudgetExceeded(self._run_id, "no progress")

    def _remember(self, identity: str) -> None:
        self._seen.add(identity)
        self._seen_order.append(identity)
        if len(self._seen_order) > self._MAX_REMEMBERED:
            self._seen.discard(self._seen_order.popleft())


def _identity(event: RunEvent) -> str | None:
    """A stable identity only where the event carries one; token deltas may repeat legitimately."""
    if event.sse_id:
        return f"sse:{event.sse_id}"
    call_id = event.data.get("tool_call_id")
    if isinstance(call_id, str) and call_id:
        return f"tool:{call_id}:{event.event}"
    if event.event in _RUN_EVENTS:
        return f"run:{event.run_id}:{event.event}"
    return None

# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Start, follow and stop one Hermes run, within bounded time.

The caller persists events through ``on_event``; this class does not interpret them. Because the
Runs event stream cannot be replayed, a stream that breaks is not retried: the lifecycle polls
the run's status until it is terminal, which still returns the answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .client import HermesClient
from .client import HermesError
from .client import RunEvent
from .client import RunStatus


class HermesRunDeadlineExceeded(HermesError):
    """The run outlived its wall-clock budget and was asked to stop."""


@dataclass(frozen=True, slots=True)
class RunBinding:
    """A run Hermes accepted, bound to the job that started it."""

    run_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class RunLimits:
    wall_timeout_seconds: float = 900.0
    poll_interval_seconds: float = 1.0
    stop_grace_seconds: float = 30.0


class HermesRunLifecycle:
    def __init__(self, client: HermesClient, limits: RunLimits | None = None) -> None:
        self._client = client
        self._limits = limits or RunLimits()

    async def start(self, payload: dict[str, Any], *, idempotency_key: str) -> RunBinding:
        """Create exactly one run for this key and learn its session."""
        run_id = await self._client.create_run(payload, idempotency_key=idempotency_key)
        try:
            status = await self._client.get_run(run_id)
        except (Exception, asyncio.CancelledError):
            # Hermes accepted the run, so never leave it running unaddressed.
            await self._best_effort_stop(run_id)
            raise
        return RunBinding(run_id=run_id, session_id=status.session_id or payload.get("session_id") or run_id)

    async def collect(self, binding: RunBinding, *, on_event: Callable[[RunEvent], Awaitable[None]]) -> RunStatus:
        """Consume the live events once, then return the authoritative terminal status."""
        try:
            async with asyncio.timeout(self._limits.wall_timeout_seconds):
                try:
                    async for event in self._client.stream_events(binding.run_id):
                        try:
                            await on_event(event)
                        except Exception:
                            await self._best_effort_stop(binding.run_id)
                            raise
                        if event.is_terminal:
                            break
                except HermesError:
                    pass  # the stream is gone for good; the status still has the outcome
                return await self._poll_until_terminal(binding.run_id)
        except TimeoutError as error:
            await self._stop_within_grace(binding.run_id)
            raise HermesRunDeadlineExceeded(f"Hermes run {binding.run_id} exceeded its wall deadline") from error
        except asyncio.CancelledError:
            await self._best_effort_stop(binding.run_id)
            raise

    async def cancel(self, binding: RunBinding, *, reason: str) -> RunStatus:
        """Ask Hermes to stop the run and wait, within the stop grace, until it has."""
        del reason  # the Runs API takes no reason; callers pass one for readability
        stopped = await self._client.stop_run(binding.run_id)
        if stopped.is_terminal:
            return stopped
        try:
            async with asyncio.timeout(self._limits.stop_grace_seconds):
                return await self._poll_until_terminal(binding.run_id)
        except TimeoutError as error:
            raise HermesRunDeadlineExceeded(f"Hermes run {binding.run_id} did not stop in time") from error

    async def _poll_until_terminal(self, run_id: str) -> RunStatus:
        while not (status := await self._client.get_run(run_id)).is_terminal:
            await asyncio.sleep(self._limits.poll_interval_seconds)
        return status

    async def _stop_within_grace(self, run_id: str) -> None:
        try:
            async with asyncio.timeout(self._limits.stop_grace_seconds):
                if not (await self._client.stop_run(run_id)).is_terminal:
                    await self._poll_until_terminal(run_id)
        except (HermesError, TimeoutError):
            pass

    async def _best_effort_stop(self, run_id: str) -> None:
        """Stop the run even while this task is being cancelled (shielded, bounded)."""
        stop = asyncio.create_task(self._client.stop_run(run_id))
        try:
            await asyncio.wait_for(asyncio.shield(stop), timeout=self._limits.stop_grace_seconds)
        except (HermesError, TimeoutError):
            stop.cancel()

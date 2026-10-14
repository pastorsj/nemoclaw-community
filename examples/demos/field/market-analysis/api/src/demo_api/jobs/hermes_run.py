# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Race one bound Hermes run against user cancellation and the progress heartbeat.

The runner hands the executor an ``asyncio.Event`` that a cancel sets. The ordering rule matters:
ask Hermes to stop *before* cancelling the collector, because cancelling first can abort the
stop request and leave the run detached, still spending tokens.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import Protocol

logger = logging.getLogger(__name__)


class RunLifecycle(Protocol):
    """The part of ``HermesRunLifecycle`` this module needs."""

    async def collect(self, binding: Any, *, on_event: Callable[[Any], Awaitable[None]]) -> Any: ...

    async def cancel(self, binding: Any, *, reason: str) -> Any: ...


async def run_bound(
    lifecycle: RunLifecycle,
    binding: Any,
    *,
    cancelled: asyncio.Event,
    on_event: Callable[[Any], Awaitable[None]],
    on_heartbeat: Callable[[], Awaitable[None]],
    heartbeat_seconds: float = 15.0,
) -> Any | None:
    """Return the run's terminal status, or ``None`` if the user cancelled it.

    ``on_heartbeat`` records the progress heartbeat and enforces the progress budgets; if it
    raises, the run is stopped and the error propagates.
    """

    async def heartbeats() -> None:
        while True:
            await asyncio.sleep(heartbeat_seconds)
            await on_heartbeat()

    collector = asyncio.create_task(lifecycle.collect(binding, on_event=on_event))
    cancel_requested = asyncio.create_task(cancelled.wait())
    heartbeat = asyncio.create_task(heartbeats())
    try:
        await asyncio.wait({collector, cancel_requested, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
        if collector.done():
            return collector.result()
        await _stop(lifecycle, binding, reason="stop requested" if cancelled.is_set() else "progress check failed")
        if cancelled.is_set():
            return None
        return heartbeat.result()  # re-raises the heartbeat failure
    finally:
        # On success these are no-ops. On runner cancellation (deadline, shutdown) cancelling the
        # collector makes the lifecycle issue its own best-effort stop before it exits.
        for task in (collector, cancel_requested, heartbeat):
            task.cancel()
        await asyncio.gather(collector, cancel_requested, heartbeat, return_exceptions=True)


async def _stop(lifecycle: RunLifecycle, binding: Any, *, reason: str) -> None:
    try:
        await lifecycle.cancel(binding, reason=reason)
    except Exception as error:  # noqa: BLE001 - the product decision stands even if Hermes does not confirm
        logger.warning("Hermes did not confirm the stop (%s)", type(error).__name__)

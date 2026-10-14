# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Server-Sent Events stream of one job, read from the job store.

Every stored event is sent with its store cursor as the SSE ``id:``. Frames the stream makes up
(``stream.start``, ``stream.mode``, ``job.status``) carry no id, because the browser resends the
last id it saw when it reconnects, and a made-up id could skip stored events. A client resumes
after a cursor with ``/stream/{cursor}`` or the ``Last-Event-ID`` header.

The stream first replays stored events as fast as it can, announces ``stream.mode: live``, then
polls every half second. It ends with a ``job.status`` frame once the job is finished. A job's
terminal status and the events that explain it are written in one transaction, so nothing can
arrive after that frame.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from .store import Job
from .store import JobStore

POLL_SECONDS = 0.5
KEEPALIVE_SECONDS = 15.0
_TERMINAL_BATCH = 10_000


def format_sse(event_type: str, data: dict[str, Any], event_id: int | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event_type}\ndata: {json.dumps(data)}\n\n"


def resolve_cursor(value: str | None) -> int:
    """An untrusted cursor from the URL or ``Last-Event-ID``; anything invalid means "from the start"."""
    try:
        cursor = int((value or "").strip())
    except ValueError:
        return 0
    return max(cursor, 0)


async def job_stream(
    store: JobStore, job_id: str, *, after: int = 0, poll_seconds: float = POLL_SECONDS
) -> AsyncIterator[str]:
    cursor = after
    reconnected = after > 0
    replaying = True
    last_status = None
    last_sent = time.monotonic()

    def status_frame(job: Job) -> str:
        nonlocal reconnected
        data: dict[str, Any] = {"status": job.status}
        if job.error:
            data["error"] = job.error
        if reconnected:  # only the first status frame after a resume says so
            data["reconnected"] = True
            reconnected = False
        return format_sse("job.status", data)

    yield format_sse("stream.start", {"job_id": job_id, "after": after})
    yield format_sse("stream.mode", {"mode": "polling", "interval_ms": int(poll_seconds * 1000)})
    while True:
        job = await store.get(job_id)
        if job is None:
            yield format_sse("job.error", {"error": "Job not found"})
            return
        limit = _TERMINAL_BATCH if not job.is_active else 1000 if replaying else 100
        while True:
            events = await store.events(job_id, after_id=cursor, limit=limit)
            for event in events:
                cursor = event.pop("_id")
                yield format_sse(event.pop("type", "event"), event, cursor)
            if events:
                last_sent = time.monotonic()
            if job.is_active or len(events) < limit:
                break  # a finished job is drained completely before its final status
        if replaying and len(events) < limit:
            replaying = False
            yield format_sse("stream.mode", {"mode": "live"})
        if not job.is_active:
            yield status_frame(job)
            return
        if job.status != last_status:
            last_status = job.status
            yield status_frame(job)
        if replaying:
            continue
        if time.monotonic() - last_sent >= KEEPALIVE_SECONDS:
            last_sent = time.monotonic()
            yield ": keepalive\n\n"
        await asyncio.sleep(poll_seconds)

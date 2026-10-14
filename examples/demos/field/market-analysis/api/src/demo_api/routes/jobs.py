# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""``/v1/jobs/async``: submit a question, follow it, read its answer, cancel it, export it, benchmark it.

Unknown jobs, including ones deleted by retention, answer 404; the UI shows them as unavailable.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC
from datetime import datetime
from typing import Annotated
from typing import Any
from typing import Literal

from fastapi import APIRouter
from fastapi import Header
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import Field

from demo_api.benchmark import market_calls
from demo_api.benchmark import retrieval
from demo_api.benchmark import run_benchmark
from demo_api.jobs.export import export_turn
from demo_api.jobs.runner import QueueFullError
from demo_api.jobs.runner import RunnerUnavailableError
from demo_api.jobs.store import Job
from demo_api.jobs.store import JobExistsError
from demo_api.jobs.stream import job_stream
from demo_api.jobs.stream import resolve_cursor
from demo_api.pack import PackUnavailableError
from demo_api.phoenix import PhoenixUnavailableError
from demo_api.phoenix import find_trace_id
from demo_api.services import Services
from demo_api.services import ServicesDep

router = APIRouter(prefix="/v1/jobs/async", tags=["jobs"])

MAX_INPUT_CHARS = 32_768
RETRY_AFTER_SECONDS = 30
_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class SubmitRequest(BaseModel):
    agent_type: Literal["hermes"] = "hermes"
    input: str
    job_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    data_sources: list[str] | None = Field(
        default=None, description="Source ids to use; omit for every available source, [] for none."
    )


@router.post("/submit")
async def submit(
    body: SubmitRequest,
    services: ServicesDep,
    conversation_id: Annotated[str | None, Header(alias="conversation-id")] = None,
) -> dict[str, str]:
    question = body.input.strip()
    if not question:
        raise HTTPException(422, "The question is empty.")
    if len(question) > MAX_INPUT_CHARS:
        raise HTTPException(413, f"The question is longer than {MAX_INPUT_CHARS} characters.")
    if conversation_id is not None and not _CONVERSATION_ID.fullmatch(conversation_id):
        raise HTTPException(422, "The conversation-id header is not a valid id.")
    try:
        available = {source.id: source for source in services.pack.sources()}
    except PackUnavailableError as error:
        raise HTTPException(503, str(error)) from error
    source_ids = list(dict.fromkeys(body.data_sources)) if body.data_sources is not None else list(available)
    if unknown := [source_id for source_id in source_ids if source_id not in available]:
        detail = {"message": f"Unknown data source(s): {', '.join(unknown)}", "invalid_ids": unknown}
        raise HTTPException(422, detail | {"known_ids": sorted(available)})

    selected = [available[source_id] for source_id in source_ids]
    families = {family for source in selected for family in source.capabilities}
    request: dict[str, Any] = {
        "question": question,
        "source_ids": source_ids,
        "conversation_id": conversation_id,
        "catalog": [source.catalog_entry() for source in selected],
        "toolsets": services.registry.toolsets(families, services.settings.features),
        "database_name": next((source.database_name for source in selected if source.database_name), None),
    }
    job_id = body.job_id or str(uuid.uuid4())
    try:
        await services.runner.submit(job_id, request)
    except QueueFullError as error:
        message = "The agent is busy with other questions. Try again shortly."
        raise HTTPException(429, message, headers={"Retry-After": str(RETRY_AFTER_SECONDS)}) from error
    except JobExistsError as error:
        raise HTTPException(409, f"Job already exists: {job_id}") from error
    except RunnerUnavailableError as error:
        raise HTTPException(503, "The API is starting or stopping. Try again shortly.") from error
    return {"job_id": job_id, "status": "submitted"}


@router.get("/job/{job_id}")
async def status(job_id: str, services: ServicesDep) -> dict[str, Any]:
    job = await _job(services, job_id)
    created_at = datetime.fromtimestamp(job.created_at, tz=UTC).isoformat()
    return {"job_id": job_id, "status": job.status, "error": job.error, "created_at": created_at}


@router.get("/job/{job_id}/report")
async def report(job_id: str, services: ServicesDep) -> dict[str, Any]:
    markdown = ((await _job(services, job_id)).output or {}).get("report")
    return {"job_id": job_id, "has_report": bool(markdown), "report": markdown}


@router.post("/job/{job_id}/cancel")
async def cancel(job_id: str, services: ServicesDep) -> dict[str, Any]:
    """Cancel a queued or running job. A running job's Hermes run is stopped before its stream is dropped."""
    await _job(services, job_id)
    if not await services.runner.cancel(job_id):
        job = await _job(services, job_id)
        raise HTTPException(409, f"Job already finished (status: {job.status}).")
    return {"job_id": job_id, "status": "interrupted", "cancelled": True}


@router.get("/job/{job_id}/stream")
async def stream(
    job_id: str,
    services: ServicesDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Server-Sent Events from the start, or after the ``Last-Event-ID`` a reconnecting browser sends."""
    await _job(services, job_id)
    events = job_stream(services.store, job_id, after=resolve_cursor(last_event_id))
    return StreamingResponse(events, media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/job/{job_id}/stream/{last_event_id}")
async def stream_after(job_id: str, last_event_id: str, services: ServicesDep) -> StreamingResponse:
    """Server-Sent Events after a cursor."""
    await _job(services, job_id)
    events = job_stream(services.store, job_id, after=resolve_cursor(last_event_id))
    return StreamingResponse(events, media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/job/{job_id}/trace")
async def trace(job_id: str, services: ServicesDep) -> dict[str, str]:
    """The job's trace in Phoenix; open ``path`` on the Phoenix UI."""
    await _job(services, job_id)
    settings = services.settings
    try:
        trace_id = await find_trace_id(
            services.http, base_url=settings.aiq_phoenix_internal_url, project=settings.phoenix_project, job_id=job_id
        )
    except PhoenixUnavailableError as error:
        raise HTTPException(503, "Phoenix is unavailable.") from error
    if trace_id is None:
        raise HTTPException(404, "Phoenix has no trace for this job yet.")
    return {"job_id": job_id, "trace_id": trace_id, "path": f"/redirects/traces/{trace_id}"}


@router.get("/job/{job_id}/export")
async def export(job_id: str, services: ServicesDep) -> dict[str, Any]:
    """The job as one turn of the v2 recordings bundle (see ``jobs/export.py``)."""
    return await export_turn(services.store, await _job(services, job_id), services.pack.data_dir)


@router.get("/job/{job_id}/benchmark")
async def benchmark(job_id: str, services: ServicesDep) -> dict[str, Any]:
    """The job's CPU/GPU comparison (``demo_api.benchmark``), once one has run."""
    await _job(services, job_id)
    if (stored := await services.store.benchmark(job_id)) is None:
        raise HTTPException(404, "No comparison has run for this job.")
    return stored


@router.post("/job/{job_id}/benchmark")
async def run_comparison(job_id: str, services: ServicesDep) -> dict[str, Any]:
    """Replay the finished job's market analytics calls on the CPU and GPU engines, or return the stored result.

    An ``unavailable`` result (no GPU service, or none reachable) is returned but not stored.
    """
    job = await _job(services, job_id)
    if job.is_active:
        raise HTTPException(409, "The run is still going; compare its calls once it has finished.")
    if (stored := await services.store.benchmark(job_id)) is not None:
        return stored
    receipts = await services.store.receipts(job_id)
    if not market_calls(receipts):
        raise HTTPException(422, "This run did not call a market analytics tool, so there is nothing to compare.")
    if services.benchmark_lock.locked():
        message = "Another comparison is running. Try again shortly."
        raise HTTPException(429, message, headers={"Retry-After": str(RETRY_AFTER_SECONDS)})
    settings = services.settings
    async with services.benchmark_lock:
        result = await run_benchmark(
            services.http,
            job_id=job_id,
            receipts=receipts,
            url=settings.market_analytics_url,
            pairs=settings.benchmark_pairs,
            budget_seconds=settings.benchmark_budget_seconds,
        )
    body = result.model_dump(mode="json")
    if result.status != "unavailable":
        await services.store.save_benchmark(job_id, body)
    return body


@router.get("/job/{job_id}/retrieval-benchmark")
async def retrieval_benchmark(job_id: str, services: ServicesDep) -> dict[str, Any]:
    """The Milvus CPU/GPU index comparison that applies to the job's retrieval calls (``demo_api.benchmark.retrieval``).

    404 on a CPU-only stack, which has no GPU index; 409 while the run is going or once the index was rebuilt.
    """
    job = await _job(services, job_id)
    if job.is_active:
        raise HTTPException(409, "The run is still going; its retrieval comparison applies once it has finished.")
    try:
        benchmark = retrieval.for_run(services.pack.data_dir, await services.store.receipts(job_id))
    except retrieval.RetrievalBenchmarkUnavailable as error:
        raise HTTPException(error.status_code, str(error)) from error
    return benchmark.model_dump(mode="json")


async def _job(services: Services, job_id: str) -> Job:
    job = await services.store.get(job_id)
    if job is None:
        raise HTTPException(404, f"Job not found: {job_id}")
    return job

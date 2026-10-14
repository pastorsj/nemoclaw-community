# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""``/internal/hermes``: the routes the agent's ``execution-receipts`` plugin calls.

The sandbox reaches them at ``http://host.openshell.internal:8000``. The plugin sends the
``X-Receipt-Key`` header; inside the sandbox the key is an OpenShell placeholder that the
supervisor swaps for the real one on these routes only. The UI proxy never forwards ``/internal``.

- ``GET  .../jobs/{id}/execution-scope``: what the job's tools may touch. The plugin passes each
  tool the selected sources whose capabilities include the tool's family.
- ``POST .../jobs/{id}/tool-receipts``: one tool receipt. It is stored once, with one
  ``execution.v2`` event that points at it.
- ``POST .../jobs/{id}/llm-calls``: one model call, with the model Switchyard served and its tier,
  stored as an ``llm.call`` event so the UI can show when a run escalates.
"""

from __future__ import annotations

import secrets
from datetime import UTC
from datetime import datetime
from typing import Annotated
from typing import Any
from uuid import NAMESPACE_URL
from uuid import uuid5

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from fastapi import Request
from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import TypeAdapter
from pydantic import ValidationError

from demo_api.events import COMPONENT_BY_FAMILY
from demo_api.events import DisplaySafeProjection
from demo_api.events import EventProvenance
from demo_api.events import ExecutionEventV2
from demo_api.events import RoutingTier
from demo_api.hermes.normalizer import run_invocation_id
from demo_api.hermes.normalizer import safe_correlation
from demo_api.jobs.store import Job
from demo_api.receipts import ReceiptV2
from demo_api.registry import Tool
from demo_api.services import Services
from demo_api.services import ServicesDep

MAX_RECEIPT_BYTES = 512 * 1024
RECEIPTS: TypeAdapter[ReceiptV2] = TypeAdapter(ReceiptV2)
_AVAILABLE_LABELS = {
    "retrieval_evidence": "Unstructured retrieval evidence available",
    "analytics_result": "Market analytics result available",
    "structured_query": "Auto Ontology result available",
    "structured_prediction": "NVIDIA Kumo prediction available",
}


def require_receipt_key(services: ServicesDep, x_receipt_key: Annotated[str, Header()] = "") -> None:
    expected = services.settings.hermes_receipt_api_key.get_secret_value()
    if not expected:
        raise HTTPException(503, "HERMES_RECEIPT_API_KEY is not configured.")
    if not secrets.compare_digest(x_receipt_key.encode(), expected.encode()):
        raise HTTPException(401, "Invalid X-Receipt-Key.")


router = APIRouter(prefix="/internal/hermes", include_in_schema=False, dependencies=[Depends(require_receipt_key)])


@router.get("/jobs/{job_id}/execution-scope")
async def execution_scope(job_id: str, services: ServicesDep) -> dict[str, Any]:
    """The job's source ids (fixed at submit), its database, and the document collection."""
    job = await _live_job(services, job_id)
    collection = None
    if any(entry["kind"] == "documents" for entry in job.request["catalog"]):
        collection = (services.pack.collection_manifest() or {}).get("collection")
    settings = services.settings
    models = {"efficient": settings.agent_efficient_model, "capable": settings.agent_capable_model}
    return {
        "job_id": job_id,
        "source_ids": job.request["source_ids"],
        "sources": [{"id": entry["id"], "capabilities": entry["capabilities"]} for entry in job.request["catalog"]],
        "database_name": job.request["database_name"],
        "collection": collection,
        "models": {tier: model for tier, model in models.items() if model},
    }


@router.post("/jobs/{job_id}/tool-receipts")
async def tool_receipt(job_id: str, request: Request, services: ServicesDep) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_RECEIPT_BYTES:
        raise HTTPException(413, "The receipt is larger than 512 KiB.")
    try:
        receipt = RECEIPTS.validate_json(body)
    except ValidationError as error:
        raise HTTPException(422, "The receipt does not match the ReceiptV2 contract.") from error
    if receipt.job_id != job_id:
        raise HTTPException(409, "The receipt belongs to a different job.")
    tool = services.registry.by_hermes_name(receipt.tool_name)
    if tool is None or tool.receipt_kind != receipt.artifact_kind:
        raise HTTPException(422, "The receipt's tool and artifactKind do not match the tool registry.")
    job = await _bound_live_job(services, job_id)
    wire = RECEIPTS.dump_python(receipt, mode="json")
    event = _receipt_event(wire, tool, job)
    stored = await services.store.add_receipt(job_id, wire, event.to_event_store_dict())
    return {"receipt_id": receipt.receipt_id, "duplicate": not stored}


class LlmCall(BaseModel):
    """One model call, as the plugin's ``post_api_request`` hook saw it."""

    model_config = ConfigDict(extra="forbid")

    api_request_id: str = Field(min_length=1, max_length=256)
    turn_id: str | None = None
    served_model: str = Field(min_length=1, max_length=256)
    tier: RoutingTier | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None


@router.post("/jobs/{job_id}/llm-calls")
async def llm_call(job_id: str, call: LlmCall, services: ServicesDep) -> dict[str, Any]:
    job = await _bound_live_job(services, job_id)
    invocation_id = safe_correlation(f"hermes-llm:{call.api_request_id}", prefix="hermes-llm")
    attributes: dict[str, Any] = {"served_model": call.served_model}
    if call.tier is not None:
        attributes["tier"] = call.tier
    attributes |= {key: value for key in ("input_tokens", "output_tokens") if (value := getattr(call, key)) is not None}
    event = ExecutionEventV2(
        event_id=uuid5(NAMESPACE_URL, f"urn:nvidia:hermes-llm-call:{job_id}:{call.api_request_id}"),
        job_id=job_id,
        run_id=job.hermes_run_id,
        session_id=job.request.get("conversation_id") or job_id,
        turn_id=call.turn_id,
        event_kind="llm.call",
        state="completed",
        component_id="switchyard.router",
        invocation_id=invocation_id,
        parent_invocation_id=run_invocation_id(job.hermes_run_id),
        occurred_at=call.completed_at or datetime.now(UTC),
        display=DisplaySafeProjection(label="Model call completed", attributes=attributes),
        provenance=EventProvenance(
            source_system="hermes.plugin",
            source_event_id=invocation_id,
            source_event_kind="post_api_request",
            normalization_version="hermes-plugin.v1",
        ),
    )
    await services.store.append_event(job_id, event.to_event_store_dict())
    return {"invocation_id": invocation_id}


async def _bound_live_job(services: Services, job_id: str) -> Job:
    """A live job that has recorded its Hermes run, which every event it gets must name."""
    job = await _live_job(services, job_id)
    if job.hermes_run_id is None:
        # Hermes can call a tool before the job has recorded its run; the plugin retries.
        raise HTTPException(503, "The job has not recorded its Hermes run yet.", headers={"Retry-After": "1"})
    return job


async def _live_job(services: Services, job_id: str) -> Job:
    job = await services.store.get(job_id)
    if job is None:
        raise HTTPException(404, f"Job not found: {job_id}")
    if not job.is_active:
        raise HTTPException(409, f"The job has finished (status: {job.status}).")
    return job


def _receipt_event(receipt: dict[str, Any], tool: Tool, job: Job) -> ExecutionEventV2:
    """``artifact.available`` for a completed receipt, ``tool.observed`` for a failed or empty one."""
    available = receipt["status"] == "completed" and receipt["content"] is not None
    attributes: dict[str, Any] = {"duration_ms": receipt["durationMs"], "tool_status": receipt["status"]}
    if receipt["errorType"] is not None:
        attributes["error_type"] = receipt["errorType"]
    if receipt["traceId"] is not None:
        attributes |= {"trace_id": receipt["traceId"], "span_id": receipt["spanId"]}
    if available:
        label = _AVAILABLE_LABELS[tool.receipt_kind]
    else:
        label = f"{tool.label} failed" if receipt["status"] == "failed" else f"{tool.label} returned no result"
    return ExecutionEventV2(
        event_id=uuid5(NAMESPACE_URL, f"urn:nvidia:hermes-receipt-event:{receipt['receiptId']}"),
        job_id=job.job_id,
        run_id=job.hermes_run_id,
        session_id=job.request.get("conversation_id") or job.job_id,
        turn_id=receipt["turnId"],
        event_kind="artifact.available" if available else "tool.observed",
        state="failed" if receipt["status"] == "failed" else "completed",
        component_id=COMPONENT_BY_FAMILY[tool.family],
        invocation_id=receipt["invocationId"],
        tool_server=tool.server,
        tool_name=tool.id,
        capability_id=tool.family,
        artifact_refs=(receipt["receiptId"],),
        occurred_at=receipt["occurredAt"],
        display=DisplaySafeProjection(
            label=label,
            summary=receipt["errorSummary"] if receipt["status"] == "failed" else None,
            attributes=attributes,
            inspectable=True,
        ),
        provenance=EventProvenance(
            source_system="hermes.plugin",
            source_event_id=receipt["receiptId"],
            source_event_kind="post_tool_call",
            normalization_version="hermes-plugin-receipt.v1",
        ),
    )

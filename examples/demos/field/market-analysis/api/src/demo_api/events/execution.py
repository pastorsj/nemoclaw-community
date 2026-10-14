# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Canonical durable product-event contract for Hermes execution.

Every execution observation is one ``execution.v2`` event. The event store assigns
each stored row a monotonic per-job cursor. The SSE stream at
``/v1/jobs/async/job/{job_id}/stream`` sends the stored ``type`` as the SSE event
name, the cursor as the SSE ``id:`` and the remaining fields as the JSON payload.
A client resumes after a cursor with ``/v1/jobs/async/job/{job_id}/stream/{cursor}``
or the ``Last-Event-ID`` header. ``to_event_store_dict`` and
``from_event_store_dict`` are the small seam between this model and that store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator
from pydantic import with_config
from typing_extensions import TypedDict

from .models import ContentDigest
from .models import ContractModel
from .models import CorrelationIdentifier
from .models import JsonValue
from .models import OpenIdentifier
from .models import normalize_aware_datetime
from .models import require_unique
from .models import validate_bounded_display_json
from .models import validate_display_text

EVENT_STORE_TYPE = "execution.v2"

# The componentId of every tool.* and artifact.* event for a registered tool, keyed
# by the tool's `family` in contracts/tool-registry.json.
COMPONENT_BY_FAMILY = {
    "unstructured_retrieval": "milvus.retrieval",
    "market_analytics": "nvidia.market_analytics",
    "structured_retrieval": "nvidia.ontology",
    "structured_prediction": "nvidia.kumo",
}

ExecutionState = Literal["started", "progress", "completed", "failed", "cancelled"]
ObservationKind = Literal["observed", "prepared", "derived"]
RoutingTier = Literal["efficient", "capable"]


@with_config(ConfigDict())  # Own config, so the model alias generator leaves keys as-is.
class DisplayAttributes(TypedDict, total=False, extra_items=JsonValue):
    """Open, bounded display facts; keys stay snake_case on the wire.

    Only the routing keys are typed. LLM call events set them so the UI can show
    which model Switchyard served and on which tier. Every other key is an open
    JSON value, bounded like the rest of the display projection.
    """

    served_model: str
    tier: RoutingTier


class DisplaySafeProjection(ContractModel):
    """Small browser-safe representation of an observed execution event."""

    label: str = Field(min_length=1, max_length=256)
    summary: str | None = Field(default=None, max_length=2_000)
    attributes: DisplayAttributes = Field(default_factory=dict)
    inspectable: bool = False

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        return validate_display_text(value, field_name="label", max_chars=256)

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_display_text(value, field_name="summary", max_chars=2_000)

    @field_validator("attributes")
    @classmethod
    def _validate_attributes(cls, value: DisplayAttributes) -> DisplayAttributes:
        validate_bounded_display_json(value, field_name="attributes")
        return value


class EventProvenance(ContractModel):
    """Origin and normalization facts without raw/private runtime payloads."""

    source_system: OpenIdentifier
    source_event_id: CorrelationIdentifier | None = None
    source_event_kind: OpenIdentifier | None = None
    normalization_version: OpenIdentifier
    observation: ObservationKind = "observed"
    config_digest: ContentDigest | None = None


class ExecutionEventV2(ContractModel):
    """One immutable, correlated event used by live execution and replay.

    Event, component, capability, and tool identities are bounded open strings.
    Adding a tool therefore does not require adding a route enum or changing this
    schema.
    """

    schema_version: Literal["2"] = "2"
    event_id: UUID
    cursor: int | None = Field(default=None, ge=1)
    job_id: CorrelationIdentifier
    run_id: CorrelationIdentifier
    session_id: CorrelationIdentifier
    turn_id: CorrelationIdentifier | None = None
    event_kind: OpenIdentifier
    state: ExecutionState
    component_id: OpenIdentifier
    invocation_id: CorrelationIdentifier | None = None
    parent_invocation_id: CorrelationIdentifier | None = None
    tool_server: OpenIdentifier | None = None
    tool_name: OpenIdentifier | None = None
    capability_id: OpenIdentifier | None = None
    artifact_refs: tuple[CorrelationIdentifier, ...] = ()
    metrics_ref: CorrelationIdentifier | None = None
    occurred_at: datetime
    display: DisplaySafeProjection
    provenance: EventProvenance

    @field_validator("artifact_refs")
    @classmethod
    def _validate_artifact_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 64:
            raise ValueError("artifact_refs must not contain more than 64 references")
        return require_unique(value, field_name="artifact_refs")

    @field_validator("occurred_at")
    @classmethod
    def _validate_occurred_at(cls, value: datetime) -> datetime:
        return normalize_aware_datetime(value, field_name="occurred_at")

    @model_validator(mode="after")
    def _validate_lineage(self) -> ExecutionEventV2:
        if self.invocation_id is not None and self.invocation_id == self.parent_invocation_id:
            raise ValueError("an invocation cannot be its own parent")
        if self.parent_invocation_id is not None and self.invocation_id is None:
            raise ValueError("parent_invocation_id requires invocation_id")
        return self

    def to_event_store_dict(self) -> dict[str, Any]:
        """Serialize into the generic event-store/SSE envelope.

        ``cursor`` is excluded because the event-store row ID is authoritative.
        """

        return {
            "type": EVENT_STORE_TYPE,
            **self.model_dump(mode="json", exclude={"cursor"}),
        }

    @classmethod
    def from_event_store_dict(
        cls,
        event: dict[str, Any],
        *,
        cursor: int | None = None,
    ) -> ExecutionEventV2:
        """Validate a stored v2 row and restore its authoritative cursor."""

        envelope = dict(event)
        event_type = envelope.pop("type", None)
        if event_type != EVENT_STORE_TYPE:
            raise ValueError(f"expected event-store type {EVENT_STORE_TYPE!r}")

        stored_cursor = envelope.pop("_id", None)
        payload_cursor = envelope.pop("cursor", None)
        if stored_cursor is not None and payload_cursor is not None and stored_cursor != payload_cursor:
            raise ValueError("payload cursor does not match event-store row cursor")
        if cursor is not None and stored_cursor is not None and cursor != stored_cursor:
            raise ValueError("explicit cursor does not match event-store row cursor")
        if cursor is not None and payload_cursor is not None and cursor != payload_cursor:
            raise ValueError("explicit cursor does not match payload cursor")
        resolved_cursor = cursor
        if resolved_cursor is None:
            resolved_cursor = stored_cursor if stored_cursor is not None else payload_cursor
        if resolved_cursor is not None:
            envelope["cursor"] = resolved_cursor
        return cls.model_validate(envelope)


__all__ = [
    "COMPONENT_BY_FAMILY",
    "EVENT_STORE_TYPE",
    "DisplayAttributes",
    "DisplaySafeProjection",
    "EventProvenance",
    "ExecutionEventV2",
    "ExecutionState",
    "ObservationKind",
    "RoutingTier",
]

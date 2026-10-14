# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Typed, display-safe receipts for observed tool results.

The Hermes plugin builds one receipt per registered tool call and posts it to
``POST /internal/hermes/jobs/{job_id}/tool-receipts``. The API stores it and serves
it to the UI unchanged. ``receiptId`` is also the evidence id the agent cites, and
execution events point at it through ``artifactRefs``.

``ReceiptV2`` is a union discriminated by ``artifactKind``. Every tool in
``contracts/tool-registry.json`` names exactly one kind as its ``receipt_kind``, so
the plugin knows the kind before the call returns. Each content model mirrors the
result its tool returns, bounded for display, so the plugin copies rather than
derives. A completed receipt always carries content. A failed receipt carries
whatever bounded content the tool returned, or none.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from typing import Literal

from pydantic import AwareDatetime
from pydantic import Field
from pydantic import NonNegativeInt
from pydantic import StringConstraints
from pydantic import field_validator
from pydantic import model_validator

from demo_api.events.models import MAX_JSON_ITEMS
from demo_api.events.models import ContractModel
from demo_api.events.models import CorrelationIdentifier
from demo_api.events.models import JsonValue
from demo_api.events.models import OpenIdentifier
from demo_api.events.models import normalize_aware_datetime
from demo_api.events.models import validate_bounded_display_json
from demo_api.events.models import validate_display_text

ReceiptStatus = Literal["completed", "failed"]
ArtifactKind = Literal["retrieval_evidence", "analytics_result", "structured_query", "structured_prediction"]
MarketOperation = Literal[
    "market_scan",
    "market_anomaly_scan",
    "price_context",
    "sentiment_timeline",
    "analyze_news_price_relationship",
    "analyze_market_relationships",
    "intraday_scan",
]

TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
SpanId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
CollectionName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,254}$")]
Row = dict[str, JsonValue]

MAX_ROWS = 25
MAX_HITS = 25  # retrieve_evidence's largest top_k


class RetrievalHit(ContractModel):
    """One passage from `retrieve_evidence`, ordered by its rerank logit (`score`); the vector score is cosine."""

    rank: int = Field(ge=1)
    score: float
    vector_score: float
    source_id: OpenIdentifier
    document_id: str = Field(min_length=1, max_length=512)
    chunk_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=1_000)
    url: str | None = Field(default=None, max_length=2_048)
    published_at: AwareDatetime | None = None
    snippet: str = Field(max_length=1_500)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class RetrievalModels(ContractModel):
    embed: str = Field(min_length=1, max_length=256)
    rerank: str = Field(min_length=1, max_length=256)


class RetrievalIndex(ContractModel):
    type: str = Field(min_length=1, max_length=64)
    metric: str = Field(min_length=1, max_length=64)
    params: dict[str, int]
    search_params: dict[str, int]


class RetrievalTimings(ContractModel):
    """Wall-clock stages of one retrieval, in milliseconds."""

    embed_ms: float = Field(ge=0)
    search_ms: float = Field(ge=0)
    rerank_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)


class RetrievalEvidence(ContractModel):
    """A `retrieve_evidence` result: per-source vector search, one merged rerank, the best hits.

    `collection` is the alias the tool searched; the collection version is the index build behind it.
    """

    query: str = Field(min_length=1, max_length=4_000)
    source_ids: tuple[OpenIdentifier, ...] = Field(min_length=1, max_length=32)
    collection: CollectionName
    collection_version: CollectionName
    hits: tuple[RetrievalHit, ...] = Field(max_length=MAX_HITS)
    candidate_counts: dict[str, NonNegativeInt]
    models: RetrievalModels
    index: RetrievalIndex
    timings: RetrievalTimings

    @model_validator(mode="after")
    def _validate_hits(self) -> RetrievalEvidence:
        if any(hit.source_id not in self.source_ids for hit in self.hits):
            raise ValueError("every hit must come from a selected source")
        if len(self.hits) > sum(self.candidate_counts.values()):
            raise ValueError("hits cannot outnumber the candidates they were ranked from")
        return self


class AnalyticsEngine(ContractModel):
    """The device and library that computed a market analytics result.

    `engine_id` names the engine by method and device (`cudf-gpu.v1`, its CPU twin `pandas-cpu.v1`); results
    recorded before it was added have none.
    """

    device: Literal["cpu", "gpu"]
    library: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    engine_id: OpenIdentifier | None = None


class AnalyticsTiming(ContractModel):
    """Milliseconds, to the microsecond: the worker's calculation (`compute_ms`), its other work on the call
    (`setup_ms`: the arguments before, the result after), the two together (`engine_ms`), and the call end to end
    in the tool service, including the wait for the worker (`total_ms`). Results recorded before setup and engine
    times were added have neither.
    """

    compute_ms: float = Field(ge=0)
    setup_ms: float | None = Field(default=None, ge=0)
    engine_ms: float | None = Field(default=None, ge=0)
    total_ms: float = Field(ge=0)


class AnalyticsError(ContractModel):
    code: OpenIdentifier
    message: str = Field(min_length=1, max_length=1_000)


class AnalyticsResult(ContractModel):
    """One market analytics result; `payload` is operation-specific JSON.

    The tool does not echo its arguments, so the plugin adds them, without the
    source ids, as the public parameters. `engine` is null when the operation
    failed before it ran. `asset_count` is the number of distinct assets in the
    rows scanned; results recorded before it was added have none.
    """

    operation_id: MarketOperation
    status: Literal["succeeded", "empty", "failed"]
    source_id: OpenIdentifier
    database_name: OpenIdentifier
    public_parameters: dict[str, JsonValue]
    payload: dict[str, JsonValue] | None = None
    error: AnalyticsError | None = None
    engine: AnalyticsEngine | None = None
    timing: AnalyticsTiming
    rows_scanned: NonNegativeInt = 0
    asset_count: NonNegativeInt | None = None
    warnings: tuple[str, ...] = Field(default=(), max_length=20)
    limitations: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def _validate_outcome(self) -> AnalyticsResult:
        if self.status == "succeeded" and self.payload is None:
            raise ValueError("a succeeded result requires a payload")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("an error is required exactly when the operation failed")
        if self.status != "failed" and self.engine is None:
            raise ValueError("a result that ran must name its engine")
        return self


class LineageBinding(ContractModel):
    """How Auto Ontology bound one phrase of the question to a column."""

    phrase: str = Field(min_length=1, max_length=500)
    ontology_object: str = Field(min_length=1, max_length=256)
    table: str = Field(min_length=1, max_length=256)
    column: str = Field(min_length=1, max_length=256)


class StructuredQuery(ContractModel):
    """An ontology-grounded SQL answer from Auto Ontology `ask_question`.

    `sql` is null when Auto Ontology resolved the question's terms but could not
    construct a query.
    """

    query: str = Field(min_length=1, max_length=1_000)
    database_name: OpenIdentifier
    answer: str | None = Field(default=None, max_length=4_000)
    sql: str | None = Field(default=None, max_length=12_000)
    rows: tuple[Row, ...] = Field(default=(), max_length=MAX_ROWS)
    source_row_count: int = Field(ge=0)
    truncated: bool
    resolution_lineage: tuple[LineageBinding, ...] = Field(default=(), max_length=40)


class PredictionHorizon(ContractModel):
    """How far past the anchor the outcome is counted, read from the PQL window."""

    value: int
    unit: str = Field(min_length=1, max_length=32)


class AssetProbability(ContractModel):
    asset_id: OpenIdentifier
    probability: float = Field(ge=0, le=1)


class StructuredPrediction(ContractModel):
    """A curated PQL template scored per asset by NVIDIA Kumo (`predict_asset_outcomes`).

    `available` is false, with a `reason`, when the prediction could not run.
    """

    available: bool
    reason: str | None = Field(default=None, max_length=500)
    template_id: OpenIdentifier
    pql: str = Field(min_length=1, max_length=8_000)
    anchor: AwareDatetime
    horizon: PredictionHorizon
    rows: tuple[AssetProbability, ...] = Field(default=(), max_length=MAX_JSON_ITEMS)
    model: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_availability(self) -> StructuredPrediction:
        if self.available == (self.reason is not None):
            raise ValueError("a reason is required exactly when the prediction is unavailable")
        if not self.available and self.rows:
            raise ValueError("an unavailable prediction has no rows")
        return self


class ReceiptBase(ContractModel):
    """Fields every receipt shares; each variant narrows ``artifact_kind`` and ``content``."""

    schema_version: Literal["2"] = "2"
    artifact_kind: ArtifactKind
    receipt_id: CorrelationIdentifier
    job_id: CorrelationIdentifier
    invocation_id: CorrelationIdentifier
    turn_id: CorrelationIdentifier | None = None
    tool_name: OpenIdentifier
    status: ReceiptStatus
    error_type: OpenIdentifier | None = None
    error_summary: str | None = Field(default=None, max_length=600)
    duration_ms: int = Field(ge=0, le=86_400_000)
    trace_id: TraceId | None = None
    span_id: SpanId | None = None
    occurred_at: datetime
    content: ContractModel | None = None

    @field_validator("error_summary")
    @classmethod
    def _validate_error_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_display_text(value, field_name="error_summary", max_chars=600)

    @field_validator("occurred_at")
    @classmethod
    def _validate_occurred_at(cls, value: datetime) -> datetime:
        return normalize_aware_datetime(value, field_name="occurred_at")

    @model_validator(mode="after")
    def _validate_receipt(self) -> ReceiptBase:
        if self.status == "completed" and (self.content is None or self.error_summary is not None):
            raise ValueError("a completed receipt requires content and no error summary")
        if (self.trace_id is None) != (self.span_id is None):
            raise ValueError("trace_id and span_id must be provided together")
        if self.trace_id == "0" * 32 or self.span_id == "0" * 16:
            raise ValueError("trace_id and span_id must be non-zero")
        if self.content is not None:
            validate_bounded_display_json(self.content.model_dump(mode="json"), field_name="content")
        return self


class RetrievalEvidenceReceipt(ReceiptBase):
    artifact_kind: Literal["retrieval_evidence"]
    content: RetrievalEvidence | None = None


class AnalyticsResultReceipt(ReceiptBase):
    artifact_kind: Literal["analytics_result"]
    content: AnalyticsResult | None = None

    @model_validator(mode="after")
    def _validate_operation(self) -> AnalyticsResultReceipt:
        if self.content is None:
            return self
        if self.tool_name.rsplit("__", maxsplit=1)[-1] != self.content.operation_id:
            raise ValueError("analytics content must come from the tool that ran its operation")
        if self.status == "completed" and self.content.status == "failed":
            raise ValueError("a failed operation cannot be a completed receipt")
        return self


class StructuredQueryReceipt(ReceiptBase):
    artifact_kind: Literal["structured_query"]
    content: StructuredQuery | None = None


class StructuredPredictionReceipt(ReceiptBase):
    artifact_kind: Literal["structured_prediction"]
    content: StructuredPrediction | None = None

    @model_validator(mode="after")
    def _validate_prediction(self) -> StructuredPredictionReceipt:
        if self.status == "completed" and self.content is not None and not self.content.rows:
            raise ValueError("a completed prediction requires scored rows")
        return self


ReceiptV2 = Annotated[
    RetrievalEvidenceReceipt | AnalyticsResultReceipt | StructuredQueryReceipt | StructuredPredictionReceipt,
    Field(discriminator="artifact_kind"),
]

__all__ = [
    "AnalyticsEngine",
    "AnalyticsError",
    "AnalyticsResult",
    "AnalyticsResultReceipt",
    "AnalyticsTiming",
    "ArtifactKind",
    "AssetProbability",
    "LineageBinding",
    "MarketOperation",
    "PredictionHorizon",
    "ReceiptBase",
    "ReceiptStatus",
    "ReceiptV2",
    "RetrievalEvidence",
    "RetrievalEvidenceReceipt",
    "RetrievalHit",
    "RetrievalIndex",
    "RetrievalModels",
    "RetrievalTimings",
    "StructuredPrediction",
    "StructuredPredictionReceipt",
    "StructuredQuery",
    "StructuredQueryReceipt",
]

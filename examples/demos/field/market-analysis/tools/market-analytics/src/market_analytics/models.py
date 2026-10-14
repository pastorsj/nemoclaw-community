# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Argument types and results of the market tools.

Every tool returns a `MarketResult`: the tool's payload plus what the execution receipt shows (source, engine,
timing, rows scanned). Failures are results too (`status="failed"`), because MCP clients drop the structured
content of an error response.
"""

from __future__ import annotations

from datetime import UTC
from datetime import date
from datetime import datetime
from typing import Annotated
from typing import Literal

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import Field

Metric = Literal["return", "volume", "volatility", "peer_relative_return"]
Comparison = Literal["absolute", "magnitude", "zscore"]
Direction = Literal["highest", "lowest"]
Frequency = Literal["daily", "weekly", "monthly"]
SentimentLabel = Literal["positive", "neutral", "negative"]
IntradayMetric = Literal["intraday_range", "realized_volatility", "open_to_close_return", "max_drawdown", "volume"]
ErrorCode = Literal[
    "invalid_request",
    "source_not_selected",
    "deadline_exceeded",
    "execution_failed",
    "news_unavailable",  # the pack has no ticker-linked news table
    "minute_bars_unavailable",  # the pack has no minute bars
]
MAX_MESSAGE_LENGTH = 1_000  # what the execution receipt accepts for an error message


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# The worker computes on naive UTC timestamps (see data.py); results carry the UTC offset again, so they
# serialize as ISO 8601 with "Z" (2026-08-24T21:00:00Z).
UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class InvalidRequest(ValueError):
    """The arguments are well-formed but cannot be answered, e.g. an unknown asset or an empty window."""


class Engine(BaseModel):
    device: Literal["cpu", "gpu"]
    library: str = Field(description="pandas, scikit-learn and networkx on CPU; cudf.pandas, cuml.accel, nx-cugraph")
    version: str
    engine_id: str = Field(description="The engine by method and device: cudf-gpu.v1, or its CPU twin pandas-cpu.v1")


class Timing(BaseModel):
    """Milliseconds, with sub-millisecond precision: the service's call, the worker's share and its calculation."""

    compute_ms: float = Field(description="The worker's calculation")
    setup_ms: float | None = Field(
        None, description="The worker's other work on the call: reading the arguments, then building the result"
    )
    engine_ms: float | None = Field(None, description="The worker's whole handling of the call: setup and compute")
    total_ms: float = Field(description="End to end in the service, including the wait for the worker")


def _clipped(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE_LENGTH else message[: MAX_MESSAGE_LENGTH - 1] + "…"


class Failure(BaseModel):
    code: ErrorCode
    message: Annotated[str, AfterValidator(_clipped)]


class MarketResult[PayloadT: BaseModel](BaseModel):
    operation_id: str
    status: Literal["succeeded", "empty", "failed"]
    source_id: str
    database_name: str
    payload: PayloadT | None = None
    error: Failure | None = None
    engine: Engine | None = None
    timing: Timing
    rows_scanned: int = Field(0, description="Input rows the calculation ran over")
    asset_count: int | None = Field(None, description="Distinct assets in those rows")
    warnings: list[str] = []
    limitations: list[str] = []


# market_scan


class RankedAsset(BaseModel):
    rank: int
    asset_id: str
    score: float
    values: dict[Metric, float | None]
    zscores: dict[Metric, float | None] = Field(
        description=(
            "Each metric's z-score among the universe's assets over the window: how many standard deviations the "
            "value sits from their mean. Not a percentage"
        )
    )
    observation_count: int
    coverage_ratio: float = Field(description="Observations relative to the best-covered asset")


class MarketScanPayload(BaseModel):
    universe_id: str
    primary_metric: Metric
    comparison: Comparison
    direction: Direction
    assets_ranked: int
    observations: list[RankedAsset]


# market_anomaly_scan


class AnomalyObservation(BaseModel):
    rank: int
    asset_id: str
    timestamp: UtcDatetime
    anomaly_score: float = Field(description="PCA reconstruction error; higher is more unusual")
    decision_score: float = Field(description="Threshold minus score; negative means flagged")
    cohort_percentile: float
    is_anomaly: bool
    observed_deviations: dict[str, float] = Field(description="Robust z-score of each feature against training")


class MarketAnomalyPayload(BaseModel):
    policy_id: Literal["pca-reconstruction-market-v1"] = Field(
        "pca-reconstruction-market-v1",
        description="The scoring policy: PCA reconstruction error, flagged above the baseline's 95th percentile",
    )
    universe_id: str
    feature_names: list[str]
    training_observations: int
    scoring_observations: int
    flagged_observations: int
    observations: list[AnomalyObservation]


# price_context


class AssetPriceSummary(BaseModel):
    asset_id: str
    start_timestamp: UtcDatetime = Field(description="The window's first session")
    end_timestamp: UtcDatetime = Field(description="The window's last session")
    start_price: float = Field(
        description="The close before the window's first session (its own close for an asset with no earlier one)"
    )
    end_price: float = Field(description="The window's last close")
    total_return: float = Field(description="end_price / start_price - 1, a fraction")
    minimum_price: float
    maximum_price: float
    average_volume: float
    observation_count: int


class PricePoint(BaseModel):
    asset_id: str
    timestamp: UtcDatetime
    adjusted_close: float
    volume: float


class PriceContextPayload(BaseModel):
    frequency: Frequency
    summaries: list[AssetPriceSummary]
    series: list[PricePoint]
    series_truncated: bool


# sentiment_timeline


class SentimentPoint(BaseModel):
    period_start: UtcDatetime
    article_count: int
    positive_count: int
    neutral_count: int
    negative_count: int
    mean_sentiment: float = Field(description="Mean of negative=-1, neutral=0, positive=+1")


class SentimentTimelinePayload(BaseModel):
    frequency: Frequency
    articles_considered: int
    points: list[SentimentPoint]
    points_truncated: bool


# analyze_news_price_relationship


class NewsPriceEvent(BaseModel):
    news_id: str
    asset_id: str
    published_at: UtcDatetime
    sentiment_label: SentimentLabel
    aligned_session: UtcDatetime | None = Field(
        description="The asset's first session at or after publication; null when the data has none"
    )
    session_return: float | None = Field(
        description="The aligned session's own return, from the previous session's close, a fraction"
    )
    outcome_session: UtcDatetime | None = Field(
        description="return_horizon_sessions after the aligned session; null when the data ends before it"
    )
    forward_return: float | None = Field(
        description="From the aligned session's close to the outcome session's, a fraction; null without an outcome"
    )


class SentimentReturnSummary(BaseModel):
    sentiment_label: SentimentLabel
    event_count: int
    mean_forward_return: float
    median_forward_return: float


class AssetNewsSummary(BaseModel):
    asset_id: str
    article_count: int = Field(description="Every article about the asset in the window, aligned or not")
    positive_count: int
    neutral_count: int
    negative_count: int
    aligned_event_count: int = Field(description="Articles with a forward return")
    mean_forward_return: float | None = Field(description="Over the aligned articles; null when none")


class NewsPriceRelationshipPayload(BaseModel):
    return_horizon_sessions: int
    eligible_event_count: int = Field(description="Every article that matched the window and filters")
    aligned_event_count: int = Field(description="Articles with a forward return; the summaries cover only these")
    coverage_ratio: float
    sentiment_return_correlation: float | None
    summaries: list[SentimentReturnSummary]
    asset_summaries: list[AssetNewsSummary] = Field(
        description="Per asset, most articles first: label counts over every article and the mean forward return"
    )
    events: list[NewsPriceEvent] = Field(description="Every article, in publication order, aligned or not")
    events_truncated: bool


# analyze_market_relationships


class CentralAsset(BaseModel):
    rank: int
    asset_id: str
    centrality: float = Field(description="PageRank score; all scores sum to 1")


class RelationshipEdge(BaseModel):
    source_asset_id: str
    target_asset_id: str
    correlation: float


class MarketRelationshipsPayload(BaseModel):
    window_start: date
    window_end: date
    node_count: int
    edge_count: int
    central_assets: list[CentralAsset]
    strongest_edges: list[RelationshipEdge]


# intraday_scan


class IntradaySession(BaseModel):
    rank: int
    asset_id: str
    session: date = Field(description="The trading date, in the exchange's time zone")
    open: float
    high: float
    low: float
    close: float
    vwap: float = Field(description="Volume-weighted average of the minute closes")
    volume: float
    bar_count: int = Field(description="Minute bars in the regular session")
    open_to_close_return: float
    intraday_range: float = Field(description="High over low, minus 1")
    realized_volatility: float = Field(description="Square root of the summed squared minute returns")
    max_drawdown: float = Field(
        description=(
            "Deepest fall of a minute close from the session's running high close, as a negative fraction "
            "(-0.08 is an 8% fall); the deepest rank first with direction=lowest"
        )
    )
    opening_volume_share: float = Field(description="Share of the session's volume in its first 30 minutes")
    closing_volume_share: float = Field(description="Share of the session's volume in its last 30 minutes")


class IntradayScanPayload(BaseModel):
    rank_by: IntradayMetric
    direction: Direction
    assets_scanned: int = Field(description="Assets with at least one minute bar in the window")
    sessions_scanned: int = Field(description="Asset sessions ranked")
    files_read: int
    batches: int
    observations: list[IntradaySession]

# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""sentiment_timeline and analyze_news_price_relationship over the pack's short-form news labels."""

from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from ..data import SENTIMENT_SCORES
from ..data import MarketData
from ..models import AssetNewsSummary
from ..models import Frequency
from ..models import NewsPriceEvent
from ..models import NewsPriceRelationshipPayload
from ..models import SentimentPoint
from ..models import SentimentReturnSummary
from ..models import SentimentTimelinePayload
from .common import Output
from .common import check_window
from .common import period_start

SENTIMENT_LIMITATIONS = (
    "Sentiment values are labels stored in the data pack, not model inference performed at query time.",
    "Narrative volume and observed sentiment are descriptive and do not establish a causal market explanation.",
)
NEWS_PRICE_LIMITATIONS = (
    "The analysis is descriptive correlation and does not establish causation.",
    "Publication timing, label quality, overlapping events and omitted variables can affect the relationship.",
    "Results are not investment advice.",
)


def sentiment_timeline(
    data: MarketData,
    *,
    start: datetime,
    end: datetime,
    asset_ids: list[str] | None = None,
    universe_id: str | None = None,
    source_names: list[str] | None = None,
    frequency: Frequency = "weekly",
    point_limit: int = 100,
) -> Output:
    articles = _articles(data, start, end, asset_ids=asset_ids, universe_id=universe_id, source_names=source_names)
    if articles.empty:
        payload = SentimentTimelinePayload(
            frequency=frequency, articles_considered=0, points=[], points_truncated=False
        )
        return Output(
            payload, rows_scanned=0, assets=0, empty=True, warnings=("No articles matched the filters and window.",)
        )

    labels = articles["sentiment_label"]
    periods = (
        articles.assign(
            period_start=period_start(articles["published_at"], frequency),
            score=sentiment_score(labels),
            positive=labels == "positive",
            neutral=labels == "neutral",
            negative=labels == "negative",
        )
        .groupby("period_start")
        .agg(
            article_count=("news_id", "count"),
            positive_count=("positive", "sum"),
            neutral_count=("neutral", "sum"),
            negative_count=("negative", "sum"),
            mean_sentiment=("score", "mean"),
        )
        .reset_index()
    )
    recent = periods.tail(point_limit)
    payload = SentimentTimelinePayload(
        frequency=frequency,
        articles_considered=len(articles),
        points=[SentimentPoint(**row) for row in recent.to_dict("records")],
        points_truncated=len(recent) < len(periods),
    )
    warnings = (f"Only the most recent {point_limit} periods are returned.",) if payload.points_truncated else ()
    return Output(payload, rows_scanned=len(articles), assets=articles["asset_id"].nunique(), warnings=warnings)


def news_price_relationship(
    data: MarketData,
    *,
    published_from: datetime,
    published_to: datetime,
    asset_ids: list[str] | None = None,
    universe_id: str | None = None,
    source_names: list[str] | None = None,
    return_horizon_sessions: int = 2,
    event_limit: int = 25,
) -> Output:
    """Return from each article's first session at or after publication to `return_horizon_sessions` later.

    Every article is listed and counted per asset, aligned or not: an article near the end of the data still has
    its publication session's return, and only its forward return is missing. The per-label summaries and the
    correlation use the aligned articles alone.
    """
    articles = _articles(
        data, published_from, published_to, asset_ids=asset_ids, universe_id=universe_id, source_names=source_names
    )
    prices = data.prices[data.prices["asset_id"].isin(articles["asset_id"])]
    aligned = prices[["asset_id", "session", "timestamp", "adjusted_close", "adjusted_return_1d"]]
    outcome = aligned[["asset_id", "session", "timestamp", "adjusted_close"]]
    outcome = outcome.assign(session=outcome["session"] - return_horizon_sessions)
    # Left joins keep every article: session 0 (no session follows it) matches no price row.
    events = articles.merge(aligned, on=["asset_id", "session"], how="left").merge(
        outcome, on=["asset_id", "session"], how="left", suffixes=("", "_outcome")
    )
    events["forward_return"] = events["adjusted_close_outcome"] / events["adjusted_close"] - 1
    events = events.sort_values(["published_at", "news_id"])
    complete = events[events["forward_return"].notna()]
    unaligned = len(events) - len(complete)
    if events.empty:
        payload = NewsPriceRelationshipPayload(
            return_horizon_sessions=return_horizon_sessions,
            eligible_event_count=0,
            aligned_event_count=0,
            coverage_ratio=0.0,
            sentiment_return_correlation=None,
            summaries=[],
            asset_summaries=[],
            events=[],
            events_truncated=False,
        )
        return Output(payload, rows_scanned=0, assets=0, empty=True, warnings=("No articles matched.",))

    # A list of aggregations, renamed: cudf.pandas has no named aggregation on a single column.
    summaries = (
        complete.groupby("sentiment_label")["forward_return"]
        .agg(["count", "mean", "median"])
        .rename(columns={"count": "event_count", "mean": "mean_forward_return", "median": "median_forward_return"})
        .reset_index()
    )
    correlation = sentiment_score(complete["sentiment_label"]).corr(complete["forward_return"])
    labels = events["sentiment_label"]
    per_asset = (
        events.assign(
            positive=labels == "positive",
            neutral=labels == "neutral",
            negative=labels == "negative",
            aligned=events["forward_return"].notna(),
        )
        .groupby("asset_id")
        .agg(
            article_count=("news_id", "count"),
            positive_count=("positive", "sum"),
            neutral_count=("neutral", "sum"),
            negative_count=("negative", "sum"),
            aligned_event_count=("aligned", "sum"),
            mean_forward_return=("forward_return", "mean"),
        )
        .reset_index()
        .sort_values(["article_count", "asset_id"], ascending=[False, True])
    )
    payload = NewsPriceRelationshipPayload(
        return_horizon_sessions=return_horizon_sessions,
        eligible_event_count=len(events),
        aligned_event_count=len(complete),
        coverage_ratio=len(complete) / len(events),
        sentiment_return_correlation=_finite(correlation),
        summaries=[SentimentReturnSummary(**row) for row in summaries.to_dict("records")],
        asset_summaries=[
            AssetNewsSummary(**{**row, "mean_forward_return": _finite(row["mean_forward_return"])})
            for row in per_asset.to_dict("records")
        ],
        events=[
            NewsPriceEvent(
                news_id=row["news_id"],
                asset_id=row["asset_id"],
                published_at=row["published_at"],
                sentiment_label=row["sentiment_label"],
                aligned_session=_moment(row["timestamp"]),
                session_return=_finite(row["adjusted_return_1d"]),
                outcome_session=_moment(row["timestamp_outcome"]),
                forward_return=_finite(row["forward_return"]),
            )
            for row in events.head(event_limit).to_dict("records")
        ],
        events_truncated=len(events) > event_limit,
    )
    warnings = []
    if complete.empty:
        warnings.append("No article had a complete forward-return window.")
    elif unaligned:
        warnings.append(
            f"{unaligned} of {len(events)} articles have no forward return (forward_return is null): the data ends "
            f"before {return_horizon_sessions} sessions after them. They are listed and counted per asset, but the "
            "per-label summaries and the correlation leave them out."
        )
    if payload.events_truncated:
        warnings.append(f"Only the first {event_limit} events are listed; asset_summaries count every article.")
    assets = articles["asset_id"].nunique()
    return Output(payload, rows_scanned=len(articles), assets=assets, empty=complete.empty, warnings=tuple(warnings))


def _finite(value: float | None) -> float | None:
    """A float, or None for a missing or non-finite one (no outcome, or a correlation of constant values)."""
    return None if value is None or not math.isfinite(value) else float(value)


def _moment(value: datetime | None) -> datetime | None:
    """A frame timestamp, or None where a left join found no session (NaT). An identity check: under cudf.pandas,
    pd.isna on a scalar falls back to pandas."""
    return None if value is None or value is pd.NaT else value


def sentiment_score(labels: pd.Series) -> pd.Series:
    """negative=-1, neutral=0, positive=+1. Built from comparisons: under cudf.pandas, Series.map from strings to
    numbers falls back to pandas. The contract limits the labels to these three."""
    return sum((labels == label).astype("int64") * score for label, score in SENTIMENT_SCORES.items() if score)


def _articles(
    data: MarketData,
    start: datetime,
    end: datetime,
    *,
    asset_ids: list[str] | None,
    universe_id: str | None,
    source_names: list[str] | None,
) -> pd.DataFrame:
    check_window(start, end)
    news = data.news
    # `selected = selected & ...`, not `&=`: cudf.pandas has no in-place `&` and would fall back to pandas.
    selected = news["published_at"].between(start, end)
    if asset_ids:
        selected = selected & news["asset_id"].isin(data.resolve_assets(asset_ids))
    if universe_id:
        selected = selected & news["asset_id"].isin(data.universe(universe_id))
    if source_names:
        selected = selected & news["source_name"].isin(source_names)
    return news[selected]

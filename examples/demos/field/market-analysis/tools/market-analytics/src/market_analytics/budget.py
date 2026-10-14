# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep every result short enough for the agent to read whole.

Hermes saves an MCP tool result longer than 50,000 characters to a file the sandboxed agent cannot read and shows
the model only a 1,500-character preview, and it does the same to the largest results of one turn past 200,000
characters together (docs/architecture.md, "Tool result size"). So a result lists at most `CAPS` rows of each list,
and at most `MAX_RESULT_CHARS` characters as the agent reads it; the rows it leaves out are summarized in its
`warnings`, and the payload's `*_truncated` flag, where it has one, is set.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pydantic_core
from pydantic import BaseModel

from .models import MarketResult

# 60% of Hermes' 50,000-character limit for one MCP result, as the agent reads it. Six such results, the most one
# recorded turn made at once, also stay within its 200,000-character budget for a turn.
MAX_RESULT_CHARS = 30_000
# What the execution-receipts plugin puts first in every result: {"evidence_id": "hermes-receipt:<sha256>"}.
EVIDENCE_ID = "hermes-receipt:" + "0" * 64

Row = dict[str, Any]


@dataclass(frozen=True)
class Listing:
    """One list in a payload that a result may shorten, and how to describe the rows it leaves out."""

    field: str
    cap: int | None  # rows listed at most, whatever the call asked for
    describe: Callable[[list[Row], list[Row], Row], str]  # (kept, omitted, payload) -> a warning
    flag: str | None = None  # the payload's own truncation flag
    keep_last: bool = False  # keep the last rows (the most recent periods) rather than the first


def agent_chars(result: BaseModel) -> int:
    """The result's length as the agent reads it: the MCP server sends it as indented JSON text, Hermes puts that
    text in a JSON string under "result", and the execution-receipts plugin adds the evidence id."""
    text = pydantic_core.to_json(result, fallback=str, indent=2).decode()
    return len(json.dumps({"evidence_id": EVIDENCE_ID, "result": text}, ensure_ascii=False))


def fit[R: MarketResult](result: R) -> R:
    """The result with each list cut to its cap and then, while it is still too long, shortened further."""
    listings = LISTINGS.get(result.operation_id, ())
    if result.payload is None or not listings:
        return result
    payload = result.payload.model_dump(mode="json")
    original = {listing.field: list(payload[listing.field]) for listing in listings}
    for listing in listings:
        if listing.cap is not None and len(payload[listing.field]) > listing.cap:
            payload[listing.field] = _keep(listing, payload[listing.field], listing.cap)

    def candidate() -> R:
        return _with(result, payload, listings, original)

    current = candidate()
    for listing in listings:  # the first listing is the one to shorten first
        while agent_chars(current) > MAX_RESULT_CHARS and len(payload[listing.field]) > 1:
            rows = payload[listing.field]
            payload[listing.field] = _keep(listing, rows, max(1, len(rows) - max(1, len(rows) // 10)))
            current = candidate()
    return current


def _keep(listing: Listing, rows: list[Row], count: int) -> list[Row]:
    return rows[-count:] if listing.keep_last else rows[:count]


def _with[R: MarketResult](result: R, payload: Row, listings: tuple[Listing, ...], original: dict[str, list]) -> R:
    warnings = list(result.warnings)
    for listing in listings:
        kept, every = payload[listing.field], original[listing.field]
        if len(kept) == len(every):
            continue
        if listing.flag:
            payload[listing.flag] = True
        omitted = every[: len(every) - len(kept)] if listing.keep_last else every[len(kept) :]
        warnings.append(listing.describe(kept, omitted, payload))
    shortened = type(result.payload).model_validate(payload)
    return result.model_copy(update={"payload": shortened, "warnings": warnings})


def _number(value: Any) -> str:
    return "n/a" if value is None else f"{value:.4g}"


def _span(rows: list[Row], key: str) -> str:
    values = [row[key] for row in rows if isinstance(row.get(key), int | float)]
    return f"from {_number(min(values))} to {_number(max(values))}" if values else "with no values"


def _ranked(noun: str, key: Callable[[Row], str]) -> Callable[[list[Row], list[Row], Row], str]:
    """Rows in rank order: say which ranks are left out and the range of the value that ranked them."""

    def describe(kept: list[Row], omitted: list[Row], payload: Row) -> str:
        return (
            f"Listed ranks {kept[0]['rank']}-{kept[-1]['rank']} of the {len(kept) + len(omitted)} {noun} the call "
            f"asked for, so the result can be read whole; ranks {omitted[0]['rank']}-{omitted[-1]['rank']} are left "
            f"out, with {key(payload)} {_span(omitted, key(payload))}. Ask for a smaller limit, or a narrower "
            "universe or window, to see others."
        )

    return describe


def _series(kept: list[Row], omitted: list[Row], payload: Row) -> str:
    return (
        f"The series lists the first {len(kept)} of {len(kept) + len(omitted)} points, so the result can be read "
        "whole; every asset's summary still covers the whole window. Ask for fewer assets, a weekly or monthly "
        "frequency, or a shorter window for the rest."
    )


def _summaries(kept: list[Row], omitted: list[Row], payload: Row) -> str:
    return (
        f"Listed the summaries of {len(kept)} of {len(kept) + len(omitted)} assets; ask again for "
        f"{', '.join(row['asset_id'] for row in omitted[:10])}{' and others' if len(omitted) > 10 else ''}."
    )


def _periods(kept: list[Row], omitted: list[Row], payload: Row) -> str:
    counts = {label: sum(row[f"{label}_count"] for row in omitted) for label in ("positive", "neutral", "negative")}
    return (
        f"Listed the {len(kept)} most recent of {len(kept) + len(omitted)} periods; the {len(omitted)} earlier "
        f"periods held {sum(row['article_count'] for row in omitted)} articles ({counts['positive']} positive, "
        f"{counts['neutral']} neutral, {counts['negative']} negative)."
    )


def _events(kept: list[Row], omitted: list[Row], payload: Row) -> str:
    labels = {label: sum(row["sentiment_label"] == label for row in omitted) for label in ("positive", "neutral")}
    negative = len(omitted) - sum(labels.values())
    return (
        f"Listed the first {len(kept)} of {len(kept) + len(omitted)} events (through {kept[-1]['published_at']}); "
        f"the other {len(omitted)} ({labels['positive']} positive, {labels['neutral']} neutral, {negative} "
        "negative) are left out, but asset_summaries and summaries count them."
    )


def _assets(kept: list[Row], omitted: list[Row], payload: Row) -> str:
    return (
        f"Listed the {len(kept)} assets with the most articles of {len(kept) + len(omitted)}; the other "
        f"{len(omitted)} had {sum(row['article_count'] for row in omitted)} articles "
        f"({sum(row['negative_count'] for row in omitted)} negative). Name assets to see theirs."
    )


def _links(noun: str) -> Callable[[list[Row], list[Row], Row], str]:
    def describe(kept: list[Row], omitted: list[Row], payload: Row) -> str:
        return f"Listed {len(kept)} of the {len(kept) + len(omitted)} {noun} the call asked for."

    return describe


LISTINGS: dict[str, tuple[Listing, ...]] = {
    "market_scan": (Listing("observations", 50, _ranked("assets", lambda _: "score")),),
    "market_anomaly_scan": (Listing("observations", 25, _ranked("sessions", lambda _: "anomaly_score")),),
    "intraday_scan": (Listing("observations", 30, _ranked("sessions", lambda payload: payload["rank_by"])),),
    "price_context": (
        Listing("series", None, _series, flag="series_truncated"),
        Listing("summaries", None, _summaries),
    ),
    "sentiment_timeline": (Listing("points", 100, _periods, flag="points_truncated", keep_last=True),),
    "analyze_news_price_relationship": (
        Listing("events", 50, _events, flag="events_truncated"),
        Listing("asset_summaries", 50, _assets),
    ),
    "analyze_market_relationships": (
        Listing("strongest_edges", None, _links("links")),
        Listing("central_assets", None, _links("assets")),
    ),
}

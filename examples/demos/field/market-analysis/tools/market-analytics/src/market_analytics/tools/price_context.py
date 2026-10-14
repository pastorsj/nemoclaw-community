# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""price_context: return, range and volume for named assets, with an optional price series."""

from __future__ import annotations

from datetime import datetime

from ..data import MarketData
from ..models import AssetPriceSummary
from ..models import Frequency
from ..models import PriceContextPayload
from ..models import PricePoint
from .common import Output
from .common import check_window
from .common import period_start

LIMITATIONS = ("Prices are adjusted historical observations and are not investment advice.",)


def run(
    data: MarketData,
    *,
    asset_ids: list[str],
    start: datetime,
    end: datetime,
    frequency: Frequency = "daily",
    include_series: bool = True,
    point_limit: int = 250,
) -> Output:
    check_window(start, end)
    prices = data.prices
    bars = prices[prices["asset_id"].isin(data.resolve_assets(asset_ids)) & prices["timestamp"].between(start, end)]
    daily = bars[["asset_id", "timestamp", "adjusted_close", "volume"]]
    series = daily
    if frequency != "daily":
        # One point per asset and period: the period's last close and its total volume.
        series = (
            series.assign(period=period_start(series["timestamp"], frequency))
            .groupby(["asset_id", "period"])
            .agg(timestamp=("timestamp", "last"), adjusted_close=("adjusted_close", "last"), volume=("volume", "sum"))
            .reset_index()
            .drop(columns="period")
        )
    if series.empty:
        payload = PriceContextPayload(frequency=frequency, summaries=[], series=[], series_truncated=False)
        return Output(
            payload, rows_scanned=0, assets=0, empty=True, warnings=("No prices matched the assets and window.",)
        )

    # The summary always comes from the daily bars; frequency shapes only the series. Its return runs from the close
    # before the window's first session (data.py's return_base), as market_scan's does.
    summary = bars.groupby("asset_id").agg(
        start_timestamp=("timestamp", "first"),
        end_timestamp=("timestamp", "last"),
        start_price=("return_base", "first"),
        end_price=("adjusted_close", "last"),
        minimum_price=("adjusted_close", "min"),
        maximum_price=("adjusted_close", "max"),
        average_volume=("volume", "mean"),
        observation_count=("adjusted_close", "count"),
    )
    summary["total_return"] = summary["end_price"] / summary["start_price"] - 1
    points = series.head(point_limit) if include_series else series.head(0)
    truncated = include_series and len(series) > point_limit
    payload = PriceContextPayload(
        frequency=frequency,
        summaries=[AssetPriceSummary(**row) for row in summary.reset_index().to_dict("records")],
        series=[PricePoint(**row) for row in points.to_dict("records")],
        series_truncated=truncated,
    )
    warnings = (f"The series is cut to the first {point_limit} points.",) if truncated else ()
    return Output(payload, rows_scanned=len(bars), assets=len(summary), warnings=warnings)

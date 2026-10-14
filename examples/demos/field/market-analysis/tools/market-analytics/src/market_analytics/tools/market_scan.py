# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""market_scan: rank a universe by return, volume, volatility or peer-relative return over a window."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime

import numpy as np

from ..data import MarketData
from ..models import Comparison
from ..models import Direction
from ..models import InvalidRequest
from ..models import MarketScanPayload
from ..models import Metric
from ..models import RankedAsset
from .common import Output
from .common import check_window
from .common import host
from .common import mean
from .common import population_std
from .common import rank

LIMITATIONS = ("Results are descriptive historical observations and are not investment advice.",)

COLUMNS: dict[Metric, str] = {
    "return": "total_return",
    "volume": "total_volume",
    "volatility": "volatility",
    "peer_relative_return": "peer_relative_return",
}


def both_windows(universe_id: str, end: datetime, sessions: int, metrics: list[Metric]) -> str:
    """The rejection of a call with both start and sessions, written so the model can fix the call in one retry:
    which argument to drop, and the call to make instead, built from its own arguments."""
    retry = json.dumps(
        {
            "universe_id": universe_id,
            "end": (end if end.tzinfo else end.replace(tzinfo=UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "sessions": sessions,
            "metrics": metrics,
        }
    )
    return (
        "start and sessions cannot both be given: sessions already sets the window's start, counting back from end. "
        f"Call market_scan again without start, for the {sessions} sessions ending at end: {retry} "
        "(keep your other arguments). For a date range instead, drop sessions and keep start and end."
    )


def run(
    data: MarketData,
    *,
    universe_id: str,
    end: datetime,
    metrics: list[Metric],
    start: datetime | None = None,
    sessions: int | None = None,
    comparison: Comparison = "absolute",
    direction: Direction = "highest",
    limit: int = 10,
) -> Output:
    if len(set(metrics)) != len(metrics):
        raise InvalidRequest("metrics must be unique")
    prices = data.prices
    prices = prices[prices["asset_id"].isin(data.universe(universe_id))]
    if sessions is not None and start is not None:
        raise InvalidRequest(both_windows(universe_id, end, sessions, metrics))
    if sessions is not None:
        # "The N sessions ending D": the N latest sessions on or before D. Selecting them by value keeps a
        # timestamp read from the frame out of scalar comparisons, which cudf.pandas runs on the CPU.
        latest = prices.loc[prices["timestamp"] <= end, "timestamp"].drop_duplicates().nlargest(sessions)
        bars = prices[prices["timestamp"].isin(latest)]
    elif start is None:
        raise InvalidRequest("give start, or sessions for the sessions ending at end")
    else:
        check_window(start, end)
        bars = prices[prices["timestamp"].between(start, end)]
    if bars.empty:
        payload = MarketScanPayload(
            universe_id=universe_id,
            primary_metric=metrics[0],
            comparison=comparison,
            direction=direction,
            assets_ranked=0,
            observations=[],
        )
        return Output(
            payload, rows_scanned=0, assets=0, empty=True, warnings=("No prices matched the universe and window.",)
        )

    # A return runs from the close before the window's first session (data.py's return_base) to its last close.
    summary = bars.groupby("asset_id").agg(
        start_price=("return_base", "first"),
        end_price=("adjusted_close", "last"),
        total_volume=("volume", "sum"),
        volatility=("adjusted_return_1d", "std"),
        observation_count=("adjusted_close", "count"),
    )
    # One row per asset is little work, so it is ranked on the host, in NumPy, as pandas computes it (common.py).
    # On the GPU each pandas call on a frame this small cost more than its arithmetic (0.5 to 1.5 ms: 52 calls made
    # a 50-stock scan 70 ms against the CPU's 26), and a column divisor made cudf compile CuPy reductions for each
    # new number of assets (about 9 s on the first call of a new universe size).
    asset_ids = host(summary.index)
    count = host(summary["observation_count"])
    volatility = host(summary["volatility"])
    with np.errstate(divide="ignore", invalid="ignore"):  # as pandas: a zero start price gives an infinite return
        total_return = host(summary["end_price"]) / host(summary["start_price"]) - 1
        columns = {
            "total_return": total_return,
            "total_volume": host(summary["total_volume"]),
            "volatility": np.where(np.isnan(volatility), 0.0, volatility),
            "peer_relative_return": total_return - mean(total_return),
        }
        coverage_ratio = count / count.max()
        # Every requested metric's z-score among the universe's assets, so a metric that does not rank still says
        # how unusual a value is (a volume next to a return ranking).
        zscores = {metric: _zscores(columns[COLUMNS[metric]]) for metric in metrics}
    primary = columns[COLUMNS[metrics[0]]]
    if comparison == "magnitude":
        score = np.abs(primary)
    elif comparison == "zscore":
        score = zscores[metrics[0]]
    else:
        score = primary
    ranked = rank(score, asset_ids, ascending=direction == "lowest")[:limit]
    values = {metric: columns[COLUMNS[metric]][ranked].tolist() for metric in metrics}
    scores = {metric: zscores[metric][ranked].tolist() for metric in metrics}
    observations = [
        RankedAsset(
            rank=position + 1,
            asset_id=asset_id,
            score=row_score,
            values={metric: values[metric][position] for metric in metrics},
            zscores={metric: scores[metric][position] for metric in metrics},
            observation_count=observation_count,
            coverage_ratio=row_coverage,
        )
        for position, (asset_id, row_score, observation_count, row_coverage) in enumerate(
            zip(
                asset_ids[ranked].tolist(),
                score[ranked].tolist(),
                count[ranked].tolist(),
                coverage_ratio[ranked].tolist(),
                strict=True,
            )
        )
    ]
    payload = MarketScanPayload(
        universe_id=universe_id,
        primary_metric=metrics[0],
        comparison=comparison,
        direction=direction,
        assets_ranked=len(asset_ids),
        observations=observations,
    )
    return Output(payload, rows_scanned=len(bars), assets=len(asset_ids))


def _zscores(values: np.ndarray) -> np.ndarray:
    """(value - mean) / standard deviation over the assets (population, ddof=0); 0 when every value is the same."""
    spread = population_std(values)
    if spread > 0:
        return (values - mean(values)) / spread
    return np.zeros(len(values))

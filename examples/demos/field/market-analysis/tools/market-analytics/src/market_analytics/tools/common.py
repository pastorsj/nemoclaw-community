# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""What every market tool returns, plus the small helpers they share.

`host`, `mean`, `population_std` and `rank` finish a tool on the host, in NumPy, once its rows are reduced to a
few: on the GPU, each pandas call on a small frame costs more in fixed overhead (0.5 to 1.5 ms on an A100) than its
arithmetic. They compute exactly what pandas computes (pandas.core.nanops, without bottleneck), so a CPU result is
the same to the last bit as with the pandas calls they replace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

import numpy as np
import pandas as pd
from pydantic import BaseModel

from ..models import Frequency
from ..models import InvalidRequest


@dataclass(frozen=True)
class Output:
    payload: BaseModel
    rows_scanned: int
    assets: int  # distinct assets in the rows scanned
    empty: bool = False
    warnings: tuple[str, ...] = ()


# market_anomaly_scan, shared by its host path (anomaly.py) and its device path (anomaly_gpu.py)
MIN_TRAINING_ROWS = 8
FLAG_QUANTILE = 0.95

NANOSECONDS_PER_DAY = 86_400_000_000_000


def check_window(start: datetime, end: datetime) -> None:
    """Both ends are naive UTC here (tools/__init__.py converts the arguments); the message shows them as UTC."""
    if end < start:
        end_text, start_text = (moment.replace(tzinfo=UTC).isoformat() for moment in (end, start))
        raise InvalidRequest(f"the window ends ({end_text}) before it starts ({start_text})")


def period_start(timestamps: pd.Series, frequency: Frequency) -> pd.Series:
    """Midnight UTC of the day, the Monday of the week, or the first of the month that holds each timestamp."""
    day = timestamps.dt.floor("D")
    if frequency == "weekly":
        return day - _days(day.dt.dayofweek)
    if frequency == "monthly":
        return day - _days(day.dt.day - 1)
    return day


def host(values: pd.Series | pd.Index) -> np.ndarray:
    """A column as a plain NumPy array on the host: one copy from the GPU under cudf.pandas, whose own arrays would
    send the NumPy work after it back through its proxy."""
    return np.asarray(values.to_numpy())


def mean(values: np.ndarray) -> float:
    """Series.mean(): NaN skipped, the sum in float64 (pandas' nanmean)."""
    missing = np.isnan(values) if values.dtype.kind == "f" else np.zeros(len(values), dtype=bool)
    count = len(values) - int(missing.sum())
    if count == 0:
        return np.nan
    total = np.where(missing, 0, values).sum(dtype=np.float64) if missing.any() else values.sum(dtype=np.float64)
    return total / np.float64(count)


def population_std(values: np.ndarray) -> float:
    """Series.std(ddof=0): NaN skipped, two passes in float64 (pandas' nanvar, then its square root)."""
    values = values.astype(np.float64)
    missing = np.isnan(values)
    count = np.float64(len(values) - int(missing.sum()))
    if count <= 0:
        return np.nan
    if missing.any():
        values = np.where(missing, 0.0, values)
    average = values.sum(dtype=np.float64) / count
    squares = (average - values) ** 2
    if missing.any():
        squares[missing] = 0.0
    return np.sqrt(squares.sum(dtype=np.float64) / count)


def rank(scores: np.ndarray, ids: np.ndarray, *, ascending: bool) -> np.ndarray:
    """Positions ordered by score, then id, as DataFrame.sort_values([score, id], ascending=[ascending, True]) orders
    rows: a missing score last either way."""
    by_id = np.argsort(ids, kind="stable")
    keys = scores[by_id] if ascending else -scores[by_id]
    return by_id[np.argsort(keys, kind="stable")]


def _days(counts: pd.Series) -> pd.Series:
    # Integer nanoseconds cast to a timedelta: runs on the GPU, where pd.to_timedelta falls back to pandas.
    return (counts.astype("int64") * NANOSECONDS_PER_DAY).astype("timedelta64[ns]")


@dataclass(frozen=True)
class Scored:
    """What either engine's scan decides: the counts, and the `limit` highest-scoring observations in rank order."""

    training: int
    scoring: int
    assets: int
    flagged: int
    asset_ids: np.ndarray  # of the ranked observations
    timestamps: np.ndarray  # datetime64, naive UTC
    scores: np.ndarray  # reconstruction errors
    decisions: np.ndarray  # the training window's 95th percentile error, less the score
    deviations: np.ndarray  # (ranked, FEATURES) robust z-scores against the training window

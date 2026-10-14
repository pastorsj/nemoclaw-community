# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""market_anomaly_scan on the device: the same scan as anomaly.py, without the trips between host and GPU.

Through cudf.pandas and cuml.accel the scan spent most of its time moving data: filtering the feature frame by
universe and window (27 proxied pandas calls), copying both windows to the host, standardizing and taking medians in
NumPy there, and converting every PCA input and output between host and device. Here each universe's features are
kept once as CuPy arrays (`Resident`), a window is two comparisons on a timestamp column, and standardizing, the
PCA fit and its four transforms, the 95th percentile, the highest scores and the robust z-scores all stay on the
device. Only the ranked rows come back. cuML fits the same PCA (full SVD solver) and the arithmetic is the one
anomaly.py does, so the answers match the host path to float64 rounding (about 1e-15 relative).

CuPy and cuML are imported when a scan runs: the CPU engine never imports this module's GPU dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from ..data import FEATURES
from ..data import MarketData
from ..models import InvalidRequest
from .common import FLAG_QUANTILE
from .common import MIN_TRAINING_ROWS
from .common import Scored

MAD_SCALE = 1.4826


@dataclass(frozen=True)
class Resident:
    """One universe's anomaly features, kept on the device in the feature frame's row order."""

    values: Any  # CuPy (rows, FEATURES) float32
    nanos: Any  # CuPy int64: each row's timestamp in nanoseconds, naive UTC
    codes: Any  # CuPy int32: a code for each row's asset
    asset_ids: np.ndarray  # the host's asset id of each row, for the rows that are ranked
    timestamps: np.ndarray  # the host's datetime64[ns] of each row


def available(data: MarketData) -> bool:
    """The features are a cudf frame (the worker runs under cudf.pandas), so their columns can stay on the device."""
    return getattr(data.features, "_fsproxy_fast", None) is not None


def prepare(data: MarketData) -> None:
    """Keep every universe's features on the device now, so no scan pays for it."""
    if available(data):
        for universe_id in data.universes:
            resident(data, universe_id)


def resident(data: MarketData, universe_id: str) -> Resident:
    cached = data.resident.get(("anomaly", universe_id))
    if cached is None:
        fast = data.features._fsproxy_fast  # the cudf frame behind the cudf.pandas proxy
        scoped = fast[fast["asset_id"].isin(list(data.universe(universe_id)))]
        cached = Resident(
            values=scoped[list(FEATURES)].to_cupy(),
            nanos=scoped["timestamp"].astype("int64").to_cupy(),
            codes=scoped["asset_id"].astype("category").cat.codes.to_cupy(),
            asset_ids=scoped["asset_id"].to_pandas().to_numpy(),
            timestamps=scoped["timestamp"].to_pandas().to_numpy(),
        )
        data.resident[("anomaly", universe_id)] = cached
    return cached


def score(
    data: MarketData,
    universe_id: str,
    *,
    training_start: datetime,
    training_end: datetime,
    scoring_start: datetime,
    scoring_end: datetime,
    limit: int,
) -> Scored:
    import cuml
    import cupy as cp
    from cuml.decomposition import PCA

    rows = resident(data, universe_id)
    nanos = rows.nanos
    in_training = (nanos >= _nanos(training_start)) & (nanos <= _nanos(training_end))
    in_scoring = (nanos >= _nanos(scoring_start)) & (nanos <= _nanos(scoring_end))
    training, scoring = int(in_training.sum()), int(in_scoring.sum())
    if training < MIN_TRAINING_ROWS:
        raise InvalidRequest(f"the training window must contain at least {MIN_TRAINING_ROWS} observations")
    if scoring == 0:
        raise InvalidRequest("the scoring window contains no observations")

    train = rows.values[in_training].astype(cp.float64)
    score_rows = rows.values[in_scoring].astype(cp.float64)
    mean, scale = train.mean(axis=0), train.std(axis=0)
    scale = cp.where(scale <= 1e-12, 1.0, scale)
    train_z, score_z = (train - mean) / scale, (score_rows - mean) / scale
    if not (bool(cp.isfinite(train_z).all()) and bool(cp.isfinite(score_z).all())):
        raise ValueError("the anomaly features contain NaN or infinity")
    with cuml.using_output_type("cupy"):
        model = PCA(n_components=min(3, len(FEATURES), training - 1), svd_solver="full").fit(train_z)
        train_error = cp.mean((train_z - model.inverse_transform(model.transform(train_z))) ** 2, axis=1)
        score_error = cp.mean((score_z - model.inverse_transform(model.transform(score_z))) ** 2, axis=1)
    threshold = cp.quantile(train_error, FLAG_QUANTILE)

    # The `limit` highest scores, ties included, ordered as anomaly._highest orders them: score descending, then
    # asset id, then time. Only those candidate rows go to the host, for their ids and timestamps.
    limit = min(limit, scoring)
    if limit > 0:
        cutoff = cp.partition(score_error, scoring - limit)[scoring - limit]
        candidates = cp.flatnonzero(score_error >= cutoff)
        positions = cp.flatnonzero(in_scoring)[candidates].get()
        candidate_scores = score_error[candidates].get()
        asset_ids, timestamps = rows.asset_ids[positions], rows.timestamps[positions]
        order = np.lexsort((timestamps, asset_ids, -candidate_scores))[:limit]
        top = candidates[cp.asarray(order)]
        asset_ids, timestamps = asset_ids[order], timestamps[order]
    else:
        top = cp.asarray([], dtype=cp.int64)
        asset_ids, timestamps = np.array([], dtype=object), np.array([], dtype="datetime64[ns]")

    median = cp.median(train, axis=0)
    mad = cp.median(cp.abs(train - median), axis=0) * MAD_SCALE
    deviations = (score_rows[top] - median) / cp.where(mad > 1e-12, mad, 1.0)
    scores = score_error[top]
    return Scored(
        training=training,
        scoring=scoring,
        assets=int(cp.unique(cp.concatenate([rows.codes[in_training], rows.codes[in_scoring]])).size),
        flagged=int((score_error > threshold).sum()),
        asset_ids=asset_ids,
        timestamps=timestamps,
        scores=scores.get(),
        decisions=(threshold - scores).get(),
        deviations=deviations.get(),
    )


def _nanos(moment: datetime) -> int:
    """A naive UTC datetime (tools/__init__.py converts the arguments) as the int64 nanoseconds the columns hold."""
    return int(np.datetime64(moment, "ns").astype("int64"))

# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""market_anomaly_scan: score later sessions against a PCA model of an earlier baseline window.

The detector standardizes four price/volume features on the training window, fits a PCA with up to three
components, and scores each session by its reconstruction error. Sessions above the training window's 95th
percentile error are flagged. On GPU the scan stays on the device (anomaly_gpu.py): the features of each universe are
kept as CuPy arrays and cuML fits the same PCA; the host path below runs on the CPU engine and gives the same answers.
"""

from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn import config_context
from sklearn.decomposition import PCA

from ..data import FEATURES
from ..data import MarketData
from ..models import AnomalyObservation
from ..models import InvalidRequest
from ..models import MarketAnomalyPayload
from . import anomaly_gpu
from .common import FLAG_QUANTILE
from .common import MIN_TRAINING_ROWS
from .common import Output
from .common import Scored
from .common import check_window
from .common import host

LIMITATIONS = (
    "Anomaly scores describe unusual observed feature combinations; they are not forecasts or probabilities.",
    "Observed deviations are robust z-scores against the training window, not returns or percentages; they are "
    "descriptive reason codes and do not establish a cause or adverse event.",
)


def run(
    data: MarketData,
    *,
    universe_id: str,
    training_start: datetime,
    training_end: datetime,
    scoring_start: datetime,
    scoring_end: datetime,
    limit: int = 10,
    minimum_percentile: float | None = None,
) -> Output:
    check_window(training_start, training_end)
    check_window(scoring_start, scoring_end)
    if scoring_start <= training_end:
        raise InvalidRequest("the scoring window must begin after the training window ends")
    windows = {
        "training_start": training_start,
        "training_end": training_end,
        "scoring_start": scoring_start,
        "scoring_end": scoring_end,
    }
    if os.environ.get("MARKET_ANALYTICS_ENGINE", "cpu") == "gpu" and anomaly_gpu.available(data):
        scored = anomaly_gpu.score(data, universe_id, limit=limit, **windows)
    else:
        scored = _score(data, universe_id, limit=limit, **windows)

    # Ranks and percentiles count every scored row, though only the `limit` highest are kept
    count = scored.scoring
    position = np.arange(len(scored.asset_ids))
    cohort_percentile = 100 * (count - position) / count
    keep = np.ones(len(position), dtype=bool)
    if minimum_percentile is not None:
        keep = cohort_percentile >= minimum_percentile  # it falls with the position, so the kept rows lead
    observations = [
        AnomalyObservation(
            rank=rank,
            asset_id=asset_id,
            timestamp=timestamp,
            anomaly_score=anomaly_score,
            decision_score=decision,
            cohort_percentile=percentile,
            is_anomaly=decision < 0,
            observed_deviations=dict(zip(FEATURES, row_deviations, strict=True)),
        )
        for rank, asset_id, timestamp, anomaly_score, decision, percentile, row_deviations in zip(
            (position[keep] + 1).tolist(),
            scored.asset_ids[keep].tolist(),
            scored.timestamps[keep].astype("datetime64[us]").tolist(),
            scored.scores[keep].tolist(),
            scored.decisions[keep].tolist(),
            cohort_percentile[keep].tolist(),
            scored.deviations[keep].tolist(),
            strict=True,
        )
    ]
    payload = MarketAnomalyPayload(
        universe_id=universe_id,
        feature_names=list(FEATURES),
        training_observations=scored.training,
        scoring_observations=scored.scoring,
        flagged_observations=scored.flagged,
        observations=observations,
    )
    return Output(payload, rows_scanned=scored.training + scored.scoring, assets=scored.assets, empty=not observations)


def _score(
    data: MarketData,
    universe_id: str,
    *,
    training_start: datetime,
    training_end: datetime,
    scoring_start: datetime,
    scoring_end: datetime,
    limit: int,
) -> Scored:
    """The scan on host arrays: scikit-learn's PCA, or cuml.accel's when the GPU engine has no resident features."""
    features = data.features
    scoped = features[features["asset_id"].isin(data.universe(universe_id))]
    training = scoped[scoped["timestamp"].between(training_start, training_end)]
    scoring = scoped[scoped["timestamp"].between(scoring_start, scoring_end)]
    if len(training) < MIN_TRAINING_ROWS:
        raise InvalidRequest(f"the training window must contain at least {MIN_TRAINING_ROWS} observations")
    if scoring.empty:
        raise InvalidRequest("the scoring window contains no observations")

    # Plain host arrays: PCA runs on them through cuml.accel, and the arithmetic below is small NumPy work that
    # cudf.pandas would otherwise route through its array proxy.
    train = np.asarray(training[list(FEATURES)].to_numpy(np.float64))
    score = np.asarray(scoring[list(FEATURES)].to_numpy(np.float64))
    mean, scale = train.mean(axis=0), train.std(axis=0)
    scale[scale <= 1e-12] = 1.0
    train_z, score_z = (train - mean) / scale, (score - mean) / scale
    # Checked once here, on the host, and not again inside PCA: cuml.accel's own check compiles a CUDA kernel for
    # each new input size (about 5 s each on an A100), which no warm-up can cover. The error is scikit-learn's.
    if not (np.isfinite(train_z).all() and np.isfinite(score_z).all()):
        raise ValueError("the anomaly features contain NaN or infinity")
    with config_context(assume_finite=True):
        model = PCA(n_components=min(3, len(FEATURES), len(train) - 1), svd_solver="full").fit(train_z)
        train_error = np.mean((train_z - model.inverse_transform(model.transform(train_z))) ** 2, axis=1)
        score_error = np.mean((score_z - model.inverse_transform(model.transform(score_z))) ** 2, axis=1)
    threshold = np.quantile(train_error, FLAG_QUANTILE)

    decision_score = threshold - score_error

    # Ranked on the host too: only the `limit` highest scores are needed, and on the GPU sorting the whole scoring
    # frame by three keys, then reading its top rows back, took more pandas calls than the PCA itself. The order is
    # the one that sort gave (score descending, then asset id, then time), and a percentile still counts every row.
    top, asset_ids, timestamps = _highest(scoring, score_error, min(limit, len(score_error)))

    # Robust z-scores (median / MAD) against the training window explain which features moved.
    median = np.median(train, axis=0)
    mad = np.median(np.abs(train - median), axis=0) * 1.4826
    deviations = (score[top] - median) / np.where(mad > 1e-12, mad, 1.0)
    # Concatenated, then counted: both windows' assets, on the GPU under cudf.pandas
    assets = pd.concat([training["asset_id"], scoring["asset_id"]]).nunique()
    return Scored(
        training=len(training),
        scoring=len(scoring),
        assets=int(assets),
        flagged=int((score_error > threshold).sum()),
        asset_ids=asset_ids,
        timestamps=timestamps,
        scores=score_error[top],
        decisions=decision_score[top],
        deviations=deviations,
    )


def _highest(scoring: pd.DataFrame, scores: np.ndarray, limit: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The positions of the `limit` highest scores, in rank order, with their asset ids and timestamps.

    Only the rows that can rank are read back: those scoring at least the `limit`-th highest score, ties included,
    which the asset id and time then order exactly as a sort of every row would.
    """
    if limit <= 0:
        return np.array([], dtype=np.int64), np.array([], dtype=object), np.array([], dtype="datetime64[ns]")
    cutoff = np.partition(scores, len(scores) - limit)[len(scores) - limit]
    candidates = np.flatnonzero(scores >= cutoff)
    rows = scoring.iloc[candidates]
    asset_ids, timestamps = host(rows["asset_id"]), host(rows["timestamp"])
    order = np.lexsort((timestamps, asset_ids, -scores[candidates]))[:limit]
    return candidates[order], asset_ids[order], timestamps[order]

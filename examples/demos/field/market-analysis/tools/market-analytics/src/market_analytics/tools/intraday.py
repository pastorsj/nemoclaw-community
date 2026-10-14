# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""intraday_scan: rank asset sessions by how they traded minute by minute, from the pack's minute bars.

The only tool that reads minute bars. They are never loaded whole: bars.py reads the chosen symbols' files batch
by batch, and `session_profile` reduces each batch to one row per symbol and session before the next is read, so
memory holds one batch whatever the window. The raw dataset's symbols are the pack's asset ids.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time
from functools import partial

import pandas as pd

from ..data import MarketData
from ..models import Direction
from ..models import IntradayMetric
from ..models import IntradayScanPayload
from ..models import IntradaySession
from ..models import InvalidRequest
from .common import Output
from .common import check_window

LIMITATIONS = (
    "Metrics describe the regular session's minute bars as observed; they are not forecasts or investment advice.",
)
US_SESSION = (time(9, 30), time(16, 0))  # when the pack declares no regular session
WINDOW_MINUTES = 30  # the opening and closing windows whose volume share is reported


def run(
    data: MarketData,
    *,
    start: datetime,
    end: datetime,
    universe_id: str | None = None,
    asset_ids: list[str] | None = None,
    rank_by: IntradayMetric = "intraday_range",
    direction: Direction = "highest",
    limit: int = 10,
) -> Output:
    check_window(start, end)
    if asset_ids:
        symbols = data.resolve_assets(asset_ids)
    elif universe_id:
        symbols = list(data.universe(universe_id))
    else:
        raise InvalidRequest("name the assets (asset_ids) or a universe (universe_id)")
    bars = data.pack.minute_bars
    opens, closes = (moment.hour * 60 + moment.minute for moment in bars.regular_session or US_SESSION)
    reduce = partial(session_profile, opening_end=opens + WINDOW_MINUTES, closing_start=closes - WINDOW_MINUTES)
    scan = bars.scan(symbols, start, end, reduce)
    if scan.result.empty:
        payload = IntradayScanPayload(
            rank_by=rank_by,
            direction=direction,
            assets_scanned=0,
            sessions_scanned=0,
            files_read=scan.files,
            batches=scan.batches,
            observations=[],
        )
        return Output(
            payload, rows_scanned=0, assets=0, empty=True, warnings=("No minute bars matched the assets and window.",)
        )

    sessions = with_metrics(scan.result)
    ranked = sessions.sort_values(
        [rank_by, "symbol", "session"], ascending=[direction == "lowest", True, True], ignore_index=True
    ).head(limit)
    fields = [name for name in IntradaySession.model_fields if name != "rank"]
    rows = ranked.rename(columns={"symbol": "asset_id"})[fields].to_dict("records")
    payload = IntradayScanPayload(
        rank_by=rank_by,
        direction=direction,
        assets_scanned=sessions["symbol"].nunique(),
        sessions_scanned=len(sessions),
        files_read=scan.files,
        batches=scan.batches,
        observations=[
            IntradaySession(rank=rank, **(row | {"session": row["session"].date()}))
            for rank, row in enumerate(rows, start=1)
        ],
    )
    return Output(payload, rows_scanned=scan.rows, assets=payload.assets_scanned)


def session_profile(frame: pd.DataFrame, *, opening_end: int, closing_start: int) -> pd.DataFrame:
    """One row per symbol and session: its bar, and the sums the intraday metrics are made from.

    Each symbol's session arrives as a contiguous run of bars in time order (bars.py reads files whole, in order),
    so a minute return is the change from the previous row when that row is the same symbol and session.
    """
    close = frame["close"].astype("float64")
    session = frame["time"].dt.floor("D")
    minute = frame["time"].dt.hour * 60 + frame["time"].dt.minute
    same = (frame["symbol"] == frame["symbol"].shift()) & (session == session.shift())
    frame = frame.assign(
        session=session,
        close=close,
        value=close * frame["volume"],
        squared_return=((close / close.shift() - 1) ** 2).where(same, 0.0),
        opening_volume=frame["volume"].where(minute < opening_end, 0.0),
        closing_volume=frame["volume"].where(minute > closing_start, 0.0),
    )
    frame["drawdown"] = ratio(frame["close"], frame.groupby(["symbol", "session"])["close"].cummax()) - 1
    return (
        frame.groupby(["symbol", "session"])
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            value=("value", "sum"),
            bar_count=("close", "count"),
            realized_variance=("squared_return", "sum"),
            max_drawdown=("drawdown", "min"),
            opening_volume=("opening_volume", "sum"),
            closing_volume=("closing_volume", "sum"),
        )
        .reset_index()
    )


def with_metrics(sessions: pd.DataFrame) -> pd.DataFrame:
    """The metrics of each reduced session. A session with no volume (or a zero low) gets neutral values."""
    traded = sessions["volume"] > 0
    return sessions.assign(
        vwap=ratio(sessions["value"], sessions["volume"]).where(traded, sessions["close"]),
        open_to_close_return=ratio(sessions["close"], sessions["open"]) - 1,
        intraday_range=(ratio(sessions["high"], sessions["low"]) - 1).where(sessions["low"] > 0, 0.0),
        realized_volatility=sessions["realized_variance"] ** 0.5,
        opening_volume_share=ratio(sessions["opening_volume"], sessions["volume"]).where(traded, 0.0),
        closing_volume_share=ratio(sessions["closing_volume"], sessions["volume"]).where(traded, 0.0),
    )


def ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """numerator / denominator, as a product with a power. On the GPU, cudf checks a column divisor with CuPy
    reductions, and CuPy compiles those once per array size class: about 9 s each on an A100, on whichever question
    first brings a new number of rows. A power has no such check."""
    return numerator * denominator**-1.0

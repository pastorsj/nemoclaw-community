# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Minute bars, read in place from the pack's raw dataset: partition-scoped, in batches, reduced batch by batch.

The daily tables fit in memory, so the worker loads them once (data.py). Minute bars are hundreds of times larger
and are never loaded whole. A scan names symbols and a window, and:

1. picks the files: by symbol when each file holds one symbol (such as `<SYMBOL>_full_1min_adjsplit.parquet`), or
   by window when files sit in month partitions (`month=YYYY-MM/`, the canonical layout), and skips any file
   whose Parquet footer shows no row group in the window (or, in a file of many symbols, none of the symbols);
2. estimates from the footers what reading each file takes: the columns read, in the row groups read;
3. reads the files in batches whose estimate stays under a byte budget (MARKET_ANALYTICS_BATCH_BYTES), one
   read_parquet call per batch;
4. reduces each batch with the caller's function, such as `session_profile` (tools/intraday.py), before it reads
   the next.

Memory holds one batch plus the reduced results, whatever the dataset's size, which is how a GPU scan goes past an
A100's 40 GB. The reads are plain pandas calls, which cudf.pandas runs on cudf's Parquet reader. As with Polars'
`scan_parquet`, a batch is one multi-file read: cudf pays about 25 ms per call, so reading
file by file was 16 to 46 times slower on the GPU.

In month partitions the window and the symbols go into the reader, which skips row groups by their statistics. A
file of one symbol has no symbol column, so a batch of them is read whole: each row's symbol follows from its
file's row count in the footer, and the window is applied after the read. That layout suits bounded requests;
the canonical one suits long windows over many symbols.

A reduction must be complete within one file's bars for a symbol and session: in one-symbol files a symbol never
spans two files, and in month partitions each part holds whole symbols, so grouping by symbol and session is safe.

Times become the dataset's wall-clock times in its zone (`timezone`): bars are exchange-local, and so is the
regular session. The window, naive UTC like every other timestamp in the worker, is converted to them. A tz-aware
time column (TIMESTAMP WITH TIME ZONE) is converted on read; its footer statistics and the reader's filter take the
column's own zone. cudf cannot filter a tz-aware column, so on the GPU such month partitions are read by pandas:
for GPU speed, store naive wall-clock times.
"""

from __future__ import annotations

import bisect
import os
import re
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import time
from functools import cached_property
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

FIELDS = ("time", "open", "high", "low", "close", "volume")
# The default batch budget per engine. A batch peaks at about 5 to 7 times its estimate on the GPU and at 8 to 14
# times on the CPU, where 256 MiB batches keep a whole-market scan near 2 GB and are no slower than larger ones.
GPU_BATCH_BYTES = 1 << 30
CPU_BATCH_BYTES = 1 << 28
UTC_ZONE = ZoneInfo("UTC")


def batch_bytes() -> int:
    """MARKET_ANALYTICS_BATCH_BYTES, or the engine's default."""
    gpu = os.environ.get("MARKET_ANALYTICS_ENGINE", "cpu") == "gpu"
    return int(os.environ.get("MARKET_ANALYTICS_BATCH_BYTES") or (GPU_BATCH_BYTES if gpu else CPU_BATCH_BYTES))


@dataclass(frozen=True)
class Part:
    path: Path
    symbol: str | None  # from the file name; None when the file has a symbol column
    rows: int  # the file's rows, from its footer
    bytes: int  # estimated uncompressed bytes of the columns and row groups a scan reads


@dataclass(frozen=True)
class Scan:
    result: pd.DataFrame  # the reduced batches, concatenated in batch order; empty when no bar matched
    files: int
    batches: int
    rows: int  # bars read, after the window and session filters
    bytes: int  # the estimate the batches were planned with


@dataclass(frozen=True)
class MinuteBars:
    root: Path
    files: str  # glob under root
    columns: dict[str, str]  # each of FIELDS -> the dataset's column
    timezone: ZoneInfo  # of the time column's naive wall-clock values
    symbol_from_path: str | None = None  # a regular expression whose first group is the symbol
    symbol_column: str | None = None
    regular_session: tuple[time, time] | None = None  # both ends included

    @classmethod
    def from_pack(cls, manifest: dict[str, Any]) -> MinuteBars | None:
        """pack.json's `market.bars`, with `root` resolved by the data build; None for a pack without minute bars."""
        bars = (manifest.get("market") or {}).get("bars")
        if not bars or bars.get("frequency") != "1min":
            return None
        session = bars.get("regular_session")
        return cls(
            root=Path(bars["root"]),
            files=bars["files"],
            columns=bars["columns"],
            timezone=ZoneInfo(bars["timezone"]),
            symbol_from_path=bars.get("symbol_from_path"),
            symbol_column=bars.get("symbol_column"),
            regular_session=(time.fromisoformat(session[0]), time.fromisoformat(session[1])) if session else None,
        )

    def scan(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        reduce: Callable[[pd.DataFrame], pd.DataFrame],
        *,
        budget: int | None = None,
    ) -> Scan:
        """Read the symbols' bars in the window (naive UTC, inclusive) batch by batch, and reduce each batch."""
        parts = self.plan(symbols, self.local(start), self.local(end))
        results, rows = [], 0
        for batch in _batches(parts, budget or batch_bytes()):
            frame = self._read(batch, symbols, start, end)
            rows += len(frame)
            results.append(reduce(frame))
        result = pd.concat(results, ignore_index=True) if rows else pd.DataFrame()
        return Scan(result, files=len(parts), batches=len(results), rows=rows, bytes=sum(p.bytes for p in parts))

    def plan(self, symbols: list[str], start: datetime, end: datetime) -> list[Part]:
        """The files a scan reads, with their estimates; `start` and `end` are local wall-clock times."""
        if self.symbol_from_path:
            index = self._symbol_files
            files = [(index[symbol], symbol) for symbol in dict.fromkeys(symbols) if symbol in index]
        else:
            files = [(path, None) for path in sorted(self.root.glob(self.files)) if _month_overlaps(path, start, end)]
        ordered = sorted(set(symbols))
        parts = [self._part(path, symbol, ordered, start, end) for path, symbol in files]
        return [part for part in parts if part.bytes]

    def local(self, moment: datetime) -> datetime:
        """A naive UTC moment as the dataset's naive wall-clock time."""
        return moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(self.timezone).replace(tzinfo=None)

    def _wall_clock(self, value: Any) -> Any:
        """A time column's value (a footer statistic) as naive wall-clock time: tz-aware values are converted."""
        return value if value.tzinfo is None else value.astimezone(self.timezone).replace(tzinfo=None)

    @cached_property
    def _symbol_files(self) -> dict[str, Path]:
        pattern = re.compile(self.symbol_from_path or "")
        index = {match[1]: path for path in self.root.glob(self.files) if (match := pattern.search(path.as_posix()))}
        if not index:  # raised, so not cached: a dataset fetched or made readable later is found on the next call
            raise FileNotFoundError(f"no minute bars match {self.root / self.files}: missing or unreadable")
        return index

    def _read_columns(self) -> list[str]:
        return [self.columns[field] for field in FIELDS] + ([self.symbol_column] if self.symbol_column else [])

    def _part(self, path: Path, symbol: str | None, symbols: list[str], start: datetime, end: datetime) -> Part:
        """The file's row count and the bytes a scan reads from it: 0 when no row group can hold a matching bar."""
        metadata = pq.read_metadata(path)
        read = set(self._read_columns())
        whole = matching = 0
        for index in range(metadata.num_row_groups):
            group = metadata.row_group(index)
            chunks = {group.column(i).path_in_schema: group.column(i) for i in range(group.num_columns)}
            size = sum(chunk.total_uncompressed_size for name, chunk in chunks.items() if name in read)
            whole += size
            if self._may_match(chunks, symbols, start, end):
                matching += size
        # The reader skips a partition's other row groups; a one-symbol file is read whole if any of it is needed.
        needed = matching if self.symbol_column or not matching else whole
        return Part(path, symbol, rows=metadata.num_rows, bytes=needed)

    def _may_match(self, chunks: dict[str, Any], symbols: list[str], start: datetime, end: datetime) -> bool:
        """Whether a row group's statistics allow a bar in the window (and, with a symbol column, of the symbols)."""
        low, high = _bounds(chunks[self.columns["time"]])
        if low is not None and (self._wall_clock(high) < start or self._wall_clock(low) > end):
            return False
        if self.symbol_column:
            low, high = _bounds(chunks[self.symbol_column])
            # False when no requested symbol sorts between the group's smallest and largest.
            return low is None or bisect.bisect_left(symbols, low) < bisect.bisect_right(symbols, high)
        return True

    def _read(self, batch: list[Part], symbols: list[str], start: datetime, end: datetime) -> pd.DataFrame:
        """The batch's bars in the window (naive UTC) and the regular session, as symbol, time (wall-clock), open,
        high, low, close, volume."""
        paths = [str(part.path) for part in batch]
        names = {source: field for field, source in self.columns.items()}
        time_column = self.columns["time"]
        # The reader compares a tz-aware column only with bounds in the column's own zone.
        zone = getattr(pq.read_schema(paths[0]).field(time_column).type, "tz", None)
        if self.symbol_column:
            low, high = (_in_zone(moment, zone) if zone else self.local(moment) for moment in (start, end))
            filters = [(time_column, ">=", low), (time_column, "<=", high), (self.symbol_column, "in", symbols)]
            frame = pd.read_parquet(paths, columns=self._read_columns(), filters=filters)
            frame = frame.rename(columns={**names, self.symbol_column: "symbol"})
        else:
            frame = pd.read_parquet(paths, columns=self._read_columns()).rename(columns=names)
            # Row i came from file files[i]; a gather turns that into the file's symbol.
            files = np.repeat(np.arange(len(batch)), [part.rows for part in batch])
            frame["symbol"] = pd.Series([part.symbol for part in batch]).iloc[files].reset_index(drop=True)
        if zone:  # pandas reads the column tz-aware, cudf as naive UTC: both become naive wall-clock time
            times = frame["time"]
            if isinstance(times.dtype, pd.DatetimeTZDtype):
                times = times.dt.tz_convert(None)
            # ZoneInfo objects, not names: cudf.pandas runs tz_localize and tz_convert on the GPU only with those.
            frame["time"] = times.dt.tz_localize(UTC_ZONE).dt.tz_convert(self.timezone).dt.tz_localize(None)
        # The reader applied the window to month partitions; one-symbol files were read whole.
        keep = None if self.symbol_column else frame["time"].between(self.local(start), self.local(end))
        if self.regular_session:
            opens, closes = (moment.hour * 60 + moment.minute for moment in self.regular_session)
            in_session = (frame["time"].dt.hour * 60 + frame["time"].dt.minute).between(opens, closes)
            keep = in_session if keep is None else keep & in_session
        frame = frame if keep is None else frame[keep]  # one filtered copy of the batch
        return frame[["symbol", *FIELDS]]


def _batches(parts: list[Part], budget: int) -> Iterator[list[Part]]:
    """Consecutive parts whose estimates add up to at most `budget`; a larger part is a batch of its own."""
    batch: list[Part] = []
    size = 0
    for part in parts:
        if batch and size + part.bytes > budget:
            yield batch
            batch, size = [], 0
        batch.append(part)
        size += part.bytes
    if batch:
        yield batch


def _in_zone(moment: datetime, zone: str) -> datetime:
    """A naive UTC moment as a tz-aware datetime in `zone`, a tz database name or a fixed offset such as +05:30."""
    aware = moment.replace(tzinfo=moment.tzinfo or UTC)
    try:
        return aware.astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        return pd.Timestamp(aware).tz_convert(zone).to_pydatetime()


def _bounds(chunk: pq.ColumnChunkMetaData) -> tuple[Any, Any]:
    statistics = chunk.statistics
    if statistics is None or not statistics.has_min_max:
        return None, None
    return statistics.min, statistics.max


def _month_overlaps(path: Path, start: datetime, end: datetime) -> bool:
    """False only for a file under a month=YYYY-MM directory that lies wholly outside the window."""
    for directory in path.parts:
        if match := re.fullmatch(r"month=(\d{4})-(\d{2})", directory):
            month = (int(match[1]), int(match[2]))
            return (start.year, start.month) <= month <= (end.year, end.month)
    return True

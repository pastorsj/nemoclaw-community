# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The active data pack (/data/active): its settings, the contract check, and the frames the tools read.

`Pack` is the cheap part (pack.json only) and is what the server needs to describe its tools. `MarketData` loads
and derives the tables once, in the worker process, so every tool call runs on warm in-memory frames.

Every timestamp inside the worker is tz-naive UTC datetime64[ns]. cudf.pandas cannot hold a tz-aware column on
the GPU: one such column sends every operation on its frame back to pandas, which made the GPU engine slower than
the CPU. So `utc_naive` normalizes the tables once here, tools/__init__.py converts the arguments to naive UTC,
and models.UtcDatetime restores the UTC offset ("Z") when a result is serialized.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import networkx as nx
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .bars import MinuteBars
from .models import InvalidRequest

# The contract ships next to the sources (tools/market-analytics/contract/) so demo-data validates packs
# against the same file. The image keeps that layout.
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contract" / "market-analytics.v1.json"

FEATURES = ("adjusted_return_1d", "adjusted_return_5d", "realized_volatility_20d", "log_volume_deviation_20d")
SENTIMENT_SCORES = {"negative": -1, "neutral": 0, "positive": 1}
# Bytes the loaded frames take per table row, measured on a 1.36-million-row pack, plus 8 for the price frame's
# return base: a daily price row counts its prices and anomaly-feature rows. cudf keeps strings in Arrow columns,
# so the GPU needs less than pandas.
BYTES_PER_ROW = {"cpu": {"price": 233, "news": 310}, "gpu": {"price": 143, "news": 130}}


class ContractError(Exception):
    """The pack does not satisfy the market-analytics contract; `problems` lists every violation."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("the active data pack does not satisfy market-analytics/v1: " + "; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Universe:
    description: str
    where: str  # SQL predicate over the assets table, from the pack (trusted configuration)


@dataclass(frozen=True)
class Pack:
    root: Path
    source_id: str
    database_name: str
    contract_id: str
    news_table: str | None  # None: the pack has no ticker-linked news, and the news tools are unavailable
    session_close: timedelta  # added to trading_date to timestamp a daily bar (UTC)
    universes: dict[str, Universe]
    graph_window: tuple[date, date]
    graph_mode: str  # full_correlation | sparse_declared_peers
    prediction: dict[str, Any] | None
    minute_bars: MinuteBars | None  # None: the pack has no minute bars, and intraday_scan is unavailable

    @classmethod
    def load(cls, root: Path) -> Pack:
        manifest = json.loads((root / "pack.json").read_text(encoding="utf-8"))
        analytics = manifest["analytics"]
        graph = analytics["relationship_graph"]
        hours, minutes = analytics["session_close_utc"].split(":")
        return cls(
            root=root,
            source_id=manifest["structured"]["source"],
            database_name=manifest["structured"]["database_name"],
            contract_id=analytics["contract"],
            news_table=analytics["news_table"],
            session_close=timedelta(hours=int(hours), minutes=int(minutes)),
            universes={
                name: Universe(description=universe["description"], where=universe["where"])
                for name, universe in analytics["universes"].items()
            },
            graph_window=(date.fromisoformat(graph["window_start"]), date.fromisoformat(graph["window_end"])),
            graph_mode=graph["mode"],
            prediction=manifest.get("prediction"),
            minute_bars=MinuteBars.from_pack(manifest),
        )

    def table(self, name: str) -> Path:
        return self.root / "tables" / f"{name}.parquet"

    @property
    def database(self) -> Path:
        return self.root / "structured" / f"{self.database_name}.duckdb"


def validate(pack: Pack) -> None:
    """Check the pack's tables against the contract; raise ContractError listing every problem."""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if pack.contract_id != contract["id"]:
        raise ContractError([f"pack declares contract {pack.contract_id!r}, this service implements {contract['id']}"])
    types = contract["logical_types"]
    problems: list[str] = []
    with duckdb.connect() as db:
        for name, spec in contract["tables"].items():
            table = pack.news_table if name == "$news_table" else name
            if table is None:  # no news table: the news tools report news_unavailable
                continue
            path = pack.table(table)
            if not path.is_file():
                problems.append(f"table {table} is missing ({path})")
                continue
            relation = f"read_parquet('{path}')"
            columns = {row[0]: row[1] for row in db.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()}
            column_problems = [
                f"{table}.{column} is missing"
                if column not in columns
                else f"{table}.{column} is {columns[column]}, not {logical}"
                for column, logical in spec["columns"].items()
                if columns.get(column) not in types[logical]
            ]
            if column_problems:
                problems += column_problems
                continue
            for column, allowed in spec.get("enums", {}).items():
                literals = ", ".join(f"'{value}'" for value in allowed)
                (outside,) = db.execute(
                    f"SELECT count(*) FROM {relation} WHERE {column} NOT IN ({literals})"
                ).fetchone()
                if outside:
                    problems.append(f"{table}.{column} has {outside} rows outside {allowed}")
            for key in spec.get("unique", []) + [spec.get("primary_key", [])]:
                if key:
                    keys = ", ".join(key)
                    (duplicates,) = db.execute(f"SELECT count(*) - count(DISTINCT ({keys})) FROM {relation}").fetchone()
                    if duplicates:
                        problems.append(f"{table} has {duplicates} duplicate ({keys}) keys")
    if problems:
        raise ContractError(problems)


@dataclass(frozen=True)
class MarketData:
    """Everything the daily market tools read, derived once from the pack tables."""

    pack: Pack
    prices: pd.DataFrame  # asset_id, trading_date, timestamp, session, adjusted_close, volume, returns
    features: pd.DataFrame  # asset_id, timestamp and the four anomaly FEATURES
    news: pd.DataFrame  # news_id, asset_id, published_at, source_name, sentiment_label, session
    edges: pd.DataFrame  # source, target, correlation, weight (both directions)
    graph: nx.DiGraph
    universes: dict[str, tuple[str, ...]]
    aliases: dict[str, tuple[str, ...]]  # normalized asset id, ticker, name or name prefix -> asset ids
    # What the GPU engine keeps on the device between calls (tools/anomaly_gpu.py): the data never changes
    resident: dict[Any, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def load(cls, pack: Pack) -> MarketData:
        prices = _prices(pack)
        edges = _edges(pack, prices)
        return cls(
            pack=pack,
            prices=prices,
            features=_features(prices),
            news=_news(pack, prices),
            edges=edges,
            graph=nx.from_pandas_edgelist(edges, "source", "target", ["weight"], create_using=nx.DiGraph),
            universes=_universes(pack),
            aliases=_aliases(pack),
        )

    def universe(self, universe_id: str) -> tuple[str, ...]:
        try:
            return self.universes[universe_id]
        except KeyError:
            raise InvalidRequest(f"unknown universe {universe_id!r}; use one of {sorted(self.universes)}") from None

    def resolve_assets(self, references: list[str]) -> list[str]:
        """Map asset ids, tickers, company names or unique leading words of a name to asset ids, in order."""
        resolved: list[str] = []
        for reference in references:
            matches = self.aliases.get(_normalized(reference), ())
            if not matches:
                raise InvalidRequest(f"unknown asset {reference!r}")
            if len(matches) > 1:  # a shared leading word can match thousands of assets: name only a few
                examples = list(matches[:5])
                raise InvalidRequest(f"ambiguous asset {reference!r}: matches {len(matches)} assets, e.g. {examples}")
            if matches[0] not in resolved:
                resolved.append(matches[0])
        return resolved


def footprint(pack: Pack, device: str) -> str:
    """Row counts from the Parquet footers, and about how much memory the frames will take once loaded."""
    tables = {"price": "daily_prices", "news": pack.news_table}  # a pack may have no news table
    rows = {kind: pq.read_metadata(pack.table(table)).num_rows for kind, table in tables.items() if table}
    size = sum(count * BYTES_PER_ROW[device][kind] for kind, count in rows.items())
    counts = " and ".join(f"{count:,} {kind} rows" for kind, count in rows.items())
    return f"{counts}: about {size / 1e9:.1f} GB on the {device}"


def utc_naive(values: pd.Series) -> pd.Series:
    """Dates or timestamps as tz-naive UTC datetime64[ns], the worker's one timestamp model.

    pandas reads a parquet DATE as Python dates and a TIMESTAMP WITH TIME ZONE as tz-aware; cudf reads them as
    naive datetime64 (UTC). Both end up the same here.
    """
    if isinstance(values.dtype, pd.DatetimeTZDtype):
        values = values.dt.tz_convert(None)
    return values.astype("datetime64[ns]")


def _prices(pack: Pack) -> pd.DataFrame:
    columns = ["asset_id", "trading_date", "adjusted_close", "volume", "total_return_1d"]
    prices = pd.read_parquet(pack.table("daily_prices"), columns=columns)
    prices["trading_date"] = utc_naive(prices["trading_date"])
    prices = prices.sort_values(["asset_id", "trading_date"], ignore_index=True)
    prices["timestamp"] = prices["trading_date"] + pack.session_close
    prices["session"] = prices.groupby("asset_id").cumcount() + 1
    # Rows are sorted by asset, then date, so a shift or rolling window stays inside one asset exactly when the
    # asset has enough earlier sessions. `.where(session > n)` blanks the rows where it would not.
    previous_close = prices["adjusted_close"].shift(1).where(prices["session"] > 1)
    prices["adjusted_return_1d"] = prices["adjusted_close"] / previous_close - 1
    # The close a return over a window starting at this session is measured from, so "the N sessions ending D"
    # are N daily returns: the previous session's close, or this session's own for an asset's first session.
    prices["return_base"] = previous_close.fillna(prices["adjusted_close"])
    # A copy consolidates the columns added one by one: pandas selects rows from it about 3x faster.
    return prices.copy()


def _features(prices: pd.DataFrame) -> pd.DataFrame:
    """Point-in-time price/volume features for the anomaly detector (price_volume_v1)."""
    session = prices["session"]
    # A session with no volume has no log volume (not -inf): it, and the 20 sessions it is a baseline for, drop out.
    volume = prices["volume"].astype("float64")
    log_volume = np.log(volume.where(volume > 0))
    prior_volume = log_volume.shift(1).rolling(20)  # the 20 sessions before this one
    prior_std = prior_volume.std().where(session > 20)
    features = pd.DataFrame(
        {
            "asset_id": prices["asset_id"],
            "timestamp": prices["timestamp"],
            "adjusted_return_1d": prices["total_return_1d"],
            "adjusted_return_5d": prices["adjusted_close"] / prices["adjusted_close"].shift(5).where(session > 5) - 1,
            "realized_volatility_20d": prices["total_return_1d"].rolling(20).std().where(session >= 20),
            "log_volume_deviation_20d": (log_volume - prior_volume.mean()) / prior_std,
        }
    )
    features = features[(prior_std > 0) & features[list(FEATURES)].notna().all(axis=1)]
    return features.astype(dict.fromkeys(FEATURES, "float32")).reset_index(drop=True)


def _news(pack: Pack, prices: pd.DataFrame) -> pd.DataFrame:
    if pack.news_table is None:  # the news tools report news_unavailable and never read it
        types = {"news_id": str, "asset_id": str, "published_at": "datetime64[ns]", "source_name": str}
        types |= {"sentiment_label": str, "session": "int64"}
        return pd.DataFrame({column: pd.Series(dtype=dtype) for column, dtype in types.items()})
    columns = ["news_id", "primary_asset_id", "published_at", "source_name", "sentiment_label"]
    news = pd.read_parquet(pack.table(pack.news_table), columns=columns).rename(
        columns={"primary_asset_id": "asset_id"}
    )
    news["published_at"] = utc_naive(news["published_at"])
    # Align each article with its asset's first session at or after publication.
    news = pd.merge_asof(
        news.sort_values("published_at"),
        prices[["asset_id", "timestamp", "session"]].sort_values("timestamp"),
        left_on="published_at",
        right_on="timestamp",
        by="asset_id",
        direction="forward",
    )
    # 0 when no session follows the article (sessions start at 1). Not a nullable Int64: cudf.pandas cannot merge
    # it with the int64 price sessions.
    news["session"] = news["session"].fillna(0).astype("int64")
    return news.drop(columns="timestamp").sort_values(["published_at", "news_id"], ignore_index=True)


def _edges(pack: Pack, prices: pd.DataFrame) -> pd.DataFrame:
    """The return-correlation graph over the pack's window: every pair of assets, or only declared peers."""
    start, end = (datetime.combine(day, time()) for day in pack.graph_window)
    window = prices.loc[prices["trading_date"].between(start, end), ["trading_date", "asset_id", "total_return_1d"]]
    if pack.graph_mode == "sparse_declared_peers":
        edges = _peer_correlations(pack, window)
    else:
        edges = _all_correlations(window)
    edges = edges.assign(weight=edges["correlation"].abs().clip(lower=1e-6))
    return edges.sort_values(["source", "target"], ignore_index=True)


def _all_correlations(window: pd.DataFrame) -> pd.DataFrame:
    """Every pair of assets, from a dates x assets matrix: memory grows with the square of the assets."""
    returns = window.pivot(index="trading_date", columns="asset_id", values="total_return_1d")
    matrix = returns.corr().rename_axis(index="source")
    # var_name explicitly: pandas names the melted column after the columns axis, cudf.pandas does not.
    edges = matrix.reset_index().melt(id_vars="source", var_name="target", value_name="correlation")
    edges = edges.dropna(subset=["correlation"])
    return edges[edges["source"] != edges["target"]]


def _peer_correlations(pack: Pack, window: pd.DataFrame) -> pd.DataFrame:
    """Only the declared peers (both directions): memory grows with the pairs, not the square of the assets.

    The same Pearson correlation as DataFrame.corr, over the sessions where both assets have a return: each pair's
    returns are joined on the date, centered on the pair's own means, and summed.
    """
    declared = pd.read_parquet(pack.table("asset_relationships"), columns=["source_asset_id", "target_asset_id"])
    declared.columns = ["source", "target"]
    pairs = pd.concat([declared, declared.rename(columns={"source": "target", "target": "source"})]).drop_duplicates()
    pairs = pairs[pairs["source"] != pairs["target"]]
    returns = window.dropna(subset=["total_return_1d"])
    x = returns.rename(columns={"asset_id": "source", "total_return_1d": "x"})
    y = returns.rename(columns={"asset_id": "target", "total_return_1d": "y"})
    joined = pairs.merge(x, on="source").merge(y, on=["target", "trading_date"])
    keys = ["source", "target"]
    by_pair = joined.groupby(keys)
    dx = joined["x"] - by_pair["x"].transform("mean")
    dy = joined["y"] - by_pair["y"].transform("mean")
    sums = joined[keys].assign(xy=dx * dy, xx=dx * dx, yy=dy * dy).groupby(keys).sum()
    sums = sums[(sums["xx"] > 0) & (sums["yy"] > 0)]  # a single shared session or a flat series has no correlation
    correlation = sums["xy"] / np.sqrt(sums["xx"] * sums["yy"])
    return correlation.rename("correlation").reset_index()


def _universes(pack: Pack) -> dict[str, tuple[str, ...]]:
    assets = pack.table("assets")
    with duckdb.connect() as db:
        return {
            name: tuple(
                row[0]
                for row in db.execute(
                    f"SELECT asset_id FROM read_parquet(?) WHERE {universe.where} ORDER BY asset_id", [str(assets)]
                ).fetchall()
            )
            for name, universe in pack.universes.items()
        }


def _aliases(pack: Pack) -> dict[str, tuple[str, ...]]:
    # A Python loop over every asset: DuckDB hands it the rows (iterating a cudf.pandas frame falls back to pandas).
    with duckdb.connect() as db:
        rows = db.execute(
            """
            SELECT a.asset_id, a.company_name, t.ticker
            FROM read_parquet(?) AS a
            LEFT JOIN (SELECT asset_id, ticker FROM read_parquet(?) WHERE effective_to IS NULL) AS t USING (asset_id)
            """,
            [str(pack.table("assets")), str(pack.table("ticker_history"))],
        ).fetchall()
    exact: dict[str, set[str]] = {}
    prefixes: dict[str, set[str]] = {}
    for asset_id, company_name, ticker in rows:
        for name in [asset_id, company_name, *([ticker] if isinstance(ticker, str) else [])]:
            exact.setdefault(_normalized(name), set()).add(asset_id)
        words = _normalized(company_name).split()
        for count in range(1, len(words)):
            prefixes.setdefault(" ".join(words[:count]), set()).add(asset_id)
    # An id, ticker or full name wins over name prefixes: ticker ACI is Albertsons, not also "ACI Worldwide".
    aliases = prefixes | exact
    return {alias: tuple(sorted(asset_ids)) for alias, asset_ids in aliases.items()}


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())

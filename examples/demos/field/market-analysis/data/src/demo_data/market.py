# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Import market bars into the pack's market tables (the `market` section of pack.yaml).

    bars, read in place ──rollup──▶ <cache>/rollups/<key16>/daily_bars.parquet ──import──▶ tables/*.parquet

The rollup is the only step that reads every bar: one streaming DuckDB pass that keeps the regular session and
spills to disk past its memory limit. It is cached by the dataset's fingerprint and the bar settings, so builds
that differ only in questions, exclusions or company data reuse it. The import then works on daily rows only:

    trading_sessions     dates on which at least half the symbols trading at the time have a bar (drops holidays)
    assets               the symbols left after the exclusions, with company data and a liquidity rank
    ticker_history       one row per asset: its ticker, from its first session
    daily_prices         the rollup on sessions: OHLC, adjusted_close (= close: the bars are split-adjusted),
                         volume, dollar volume, bar count and the one-session return
    asset_relationships  declared peers: the most liquid assets with the same four-digit SIC code
    <news table>         the dataset's ticker-linked news, only when `market.news` names it

Company data comes from SEC (`companies: sec`, see sec.py) or from a table in the dataset. SEC filings are a
separate document source, built by the corpus part; nothing here reads or writes news from them.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from demo_data import external
from demo_data import sec
from demo_data.external import Dataset
from demo_data.pack import Pack
from demo_data.pack import sql_literal

ROLLUP_VERSION = "1"  # bump when the rollup SQL changes, so cached rollups are rebuilt
ROW_GROUP_ROWS = 122_880
PARQUET = f"FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {ROW_GROUP_ROWS}"
# For a symbol SEC does not list: the suffixes that mark a warrant, unit or right of another symbol in the data.
UNLISTED_SUFFIXES = {"W": "warrants", "WS": "warrants", "U": "units", "R": "rights", "RT": "rights"}


class MarketError(Exception):
    """The market data could not be imported."""


@dataclass(frozen=True)
class Listing:
    """A symbol's company data, before SIC codes are resolved."""

    company_name: str
    cik: int | None
    exchange: str
    primary: bool = False  # the first of its issuer's tickers in SEC's list
    sic_code: str = ""
    sector: str = ""
    industry: str = ""
    profile: str | None = None
    is_synthetic: bool = False


def import_tables(pack: Pack, dataset: Dataset, tables_dir: Path, cache_dir: Path) -> dict[str, Any]:
    """Write the market tables from `dataset` to `tables_dir`; returns what the import did, for pack.json."""
    config = pack.manifest["market"]
    files = external.require_verified(dataset)
    rollup, rollup_info = daily_rollup(dataset, files, config["bars"], cache_dir)
    with duckdb.connect() as db:
        configure(db, cache_dir)
        db.execute(f"CREATE VIEW rollup AS SELECT * FROM read_parquet({sql_literal(str(rollup))})")
        db.execute(SESSIONS)
        counts = dict(db.execute(SESSION_COUNTS).fetchall())
        listings = company_listings(config["companies"], dataset, files, cache_dir)
        dropped, kept = exclude(sorted(counts), counts, listings, config["exclude"], config["min_sessions"])
        if not kept:
            raise MarketError("no symbols are left after the exclusions")
        companies = resolve_companies({symbol: listings[symbol] for symbol in kept}, config["companies"], cache_dir)
        db.register("companies", companies)
        news = dataset_file(dataset, files, config["news"]) if config["news"] else None
        write_tables(db, pack, tables_dir, news)
        sessions = db.execute("SELECT count(*) FROM sessions").fetchone()[0]
    return {
        "dataset": dataset.id,
        "fingerprint": dataset.fingerprint,
        "rollup": rollup_info,
        "symbols": len(counts),
        "sessions": sessions,
        "dropped": {reason: len(symbols) for reason, symbols in dropped.items()},
        "assets": len(kept),
    }


def configure(db: duckdb.DuckDBPyConnection, cache_dir: Path) -> None:
    """Spill to the cache volume, not to memory; DATA_DUCKDB_MEMORY caps DuckDB's memory (e.g. 8GB)."""
    (cache_dir / "tmp").mkdir(parents=True, exist_ok=True)
    db.execute(f"SET temp_directory = {sql_literal(str(cache_dir / 'tmp'))}")
    db.execute("SET preserve_insertion_order = false")
    if memory := os.environ.get("DATA_DUCKDB_MEMORY"):
        db.execute(f"SET memory_limit = {sql_literal(memory)}")


# --------------------------------------------------------------------------------------------------- rollup


def daily_rollup(
    dataset: Dataset, files: list[dict[str, Any]], bars: dict[str, Any], cache_dir: Path
) -> tuple[Path, dict[str, Any]]:
    """The daily bars of every symbol (cached): (path, what pack.json records about it)."""
    inputs = json.dumps([dataset.fingerprint, bars, ROLLUP_VERSION], sort_keys=True)
    key = hashlib.sha256(inputs.encode()).hexdigest()[:16]
    directory = cache_dir / "rollups" / key
    target, info_path = directory / "daily_bars.parquet", directory / "rollup.json"
    if target.is_file() and info_path.is_file():
        return target, json.loads(info_path.read_text(encoding="utf-8")) | {"cached": True}

    matched = sorted(path.relative_to(dataset.root).as_posix() for path in dataset.root.glob(bars["files"]))
    unlisted = sorted(set(matched) - {entry["path"] for entry in files})
    if unlisted:
        raise MarketError(f"{dataset.id}: {len(unlisted)} files match {bars['files']} but are not in the manifest")
    if not matched:
        raise MarketError(f"{dataset.id}: no files match {bars['files']}")

    started = time.monotonic()
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / ".daily_bars.parquet.tmp"
    with duckdb.connect() as db:
        configure(db, cache_dir)
        source = f"read_parquet({sql_literal(str(dataset.root / bars['files']))}, filename = true)"
        db.execute(f"CREATE VIEW bars AS {bars_select(db, source, bars)}")
        db.execute(f"COPY ({ROLLUP.format(session=session_filter(bars))}) TO {sql_literal(str(partial))} ({PARQUET})")
        rows, symbols, first, last = db.execute(
            f"SELECT count(*), count(DISTINCT symbol), min(trading_date), max(trading_date) "
            f"FROM read_parquet({sql_literal(str(partial))})"
        ).fetchone()
    os.replace(partial, target)
    info = {
        "key": key,
        "files": len(matched),
        "symbols": symbols,
        "rows": rows,
        "first_date": first.isoformat(),
        "last_date": last.isoformat(),
        "seconds": round(time.monotonic() - started, 1),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    info_path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return target, info | {"cached": False}


def bars_select(db: duckdb.DuckDBPyConnection, source: str, bars: dict[str, Any]) -> str:
    """The bars with the pack's column mapping applied: symbol, time (wall-clock in bars.timezone), OHLCV."""
    columns = bars["columns"]
    types = dict(db.execute(f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM {source})").fetchall())
    missing = sorted({*columns.values(), bars.get("symbol_column", "filename")} - set(types))
    if missing:
        raise MarketError(f"the bars have no column {missing}; columns are {sorted(types)}")
    time_column = quote(columns["time"])
    if types[columns["time"]] == "TIMESTAMP WITH TIME ZONE":
        time_column = f"timezone({sql_literal(bars['timezone'])}, {time_column})"
    elif types[columns["time"]] == "DATE":
        time_column = f"CAST({time_column} AS TIMESTAMP)"
    if "symbol_column" in bars:
        symbol = quote(bars["symbol_column"])
    else:
        symbol = f"regexp_extract(filename, {sql_literal(bars['symbol_from_path'])}, 1)"
    values = ", ".join(f"{quote(columns[name])} AS {name}" for name in ("open", "high", "low", "close", "volume"))
    return f"SELECT {symbol} AS symbol, {time_column} AS time, {values} FROM {source}"


def session_filter(bars: dict[str, Any]) -> str:
    """Minute bars count from the open through the close, both included (the close carries the auction)."""
    if bars["frequency"] != "1min":
        return "true"
    start, end = bars["regular_session"]
    return f"CAST(time AS TIME) BETWEEN TIME '{start}' AND TIME '{end}'"


ROLLUP = """
SELECT symbol, CAST(time AS DATE) AS trading_date,
  arg_min(open, time) AS open, max(high) AS high, min(low) AS low, arg_max(close, time) AS close,
  sum(volume) AS volume, sum(close * volume) AS dollar_volume, count(*) AS bar_count
FROM bars
WHERE {session} AND symbol <> ''
GROUP BY ALL
ORDER BY symbol, trading_date
"""


# --------------------------------------------------------------------------------------------------- import

# A session is a date on which at least half the symbols trading at the time (between their first and last bar)
# have a bar. That keeps every exchange session and drops holidays, where only a few symbols have stray bars.
SESSIONS = """
CREATE TABLE sessions AS
WITH spans AS (SELECT symbol, min(trading_date) AS first_date, max(trading_date) AS last_date FROM rollup GROUP BY 1),
     dates AS (SELECT trading_date, count(*) AS symbols FROM rollup GROUP BY 1)
SELECT d.trading_date, d.symbols
FROM dates d JOIN spans s ON d.trading_date BETWEEN s.first_date AND s.last_date
GROUP BY d.trading_date, d.symbols
HAVING 2 * d.symbols >= count(*)
"""
SESSION_COUNTS = "SELECT symbol, count(*) FROM rollup JOIN sessions USING (trading_date) GROUP BY symbol"


def company_listings(
    companies: str, dataset: Dataset, files: list[dict[str, Any]], cache_dir: Path
) -> dict[str, Listing]:
    """Data symbol -> company data, from SEC's ticker list (BRK-B there is BRK.B in the data) or a dataset table."""
    if companies == "sec":
        snapshot = sec.snapshot(cache_dir)
        return {
            ticker.replace("-", "."): Listing(company.name, company.cik, company.exchange, company.primary)
            for ticker, company in sec.companies(snapshot).items()
        }
    path = dataset_file(dataset, files, companies)
    with duckdb.connect() as db:
        rows = db.execute(f"SELECT * FROM read_parquet({sql_literal(str(path))})").to_arrow_table().to_pylist()
    return {
        row["symbol"]: Listing(
            company_name=row["company_name"],
            cik=None,
            exchange=row["exchange"],
            sic_code=row["sic_code"] or "",
            sector=row["sector"],
            industry=row["industry"],
            profile=row.get("profile"),
            is_synthetic=bool(row.get("is_synthetic", False)),
        )
        for row in rows
    }


def exclude(
    symbols: list[str], sessions: dict[str, int], listings: dict[str, Listing], rules: list[str], min_sessions: int
) -> tuple[dict[str, list[str]], list[str]]:
    """Apply the exclusions in order; returns ({reason: symbols}, the symbols kept).

    1. warrants, units, rights, preferred: another security of an issuer (see `secondary`).
    2. preferred: any other symbol with a dot that is not listed (BRK.B is listed, as BRK-B; ABR.D is not).
    3. not_listed: the company list does not have it (ETFs, funds, delisted issuers).
    4. min_sessions: it has fewer daily bars on sessions than `min_sessions`.
    """
    present = set(symbols)
    issuers: dict[int, set[str]] = {}
    for symbol, listing in listings.items():
        if listing.cik is not None:
            issuers.setdefault(listing.cik, set()).add(symbol)
    dropped: dict[str, list[str]] = {reason: [] for reason in [*rules, "not_listed", "min_sessions"]}
    kept = []
    for symbol in symbols:
        reason = secondary(symbol, present, listings, issuers)
        if reason not in rules:
            reason = None
        if reason is None and "preferred" in rules and "." in symbol and symbol not in listings:
            reason = "preferred"
        if reason is None and symbol not in listings:
            reason = "not_listed"
        if reason is None and sessions[symbol] < min_sessions:
            reason = "min_sessions"
        if reason is None:
            kept.append(symbol)
        else:
            dropped[reason].append(symbol)
    return dropped, kept


def secondary(symbol: str, present: set[str], listings: dict[str, Listing], issuers: dict[int, set[str]]) -> str | None:
    """Why `symbol` is another security of an issuer whose stock ticker is its prefix, or None.

    A listed symbol's issuer is its SEC CIK: AGNC and AGNCL, AUR and AUROW, ALF and ALFUU share one. Its suffix then
    says what it is: ...U units, ...R or ...RT rights, a W warrants, and anything else preferred (preferred and
    depositary shares, exchange-traded notes). A dotted symbol SEC lists (BRK.B) and the issuer's primary ticker
    (GOOGL, next to GOOG) are share classes and stay. A symbol SEC does not list goes by the data alone: another
    symbol in it plus a derivative suffix (ABCW, ABC.WS, ABCU, ABCR).
    """
    listing = listings.get(symbol)
    for end in range(len(symbol) - 1, 0, -1):  # the longest base first
        base, suffix = symbol[:end], symbol[end:].lstrip(".")
        if base.endswith(".") or not suffix:
            continue
        if listing is None:
            if base in present and suffix in UNLISTED_SUFFIXES:
                return UNLISTED_SUFFIXES[suffix]
        elif listing.cik is not None and base in issuers.get(listing.cik, ()):
            if suffix.endswith("U"):
                return "units"
            if suffix.endswith(("R", "RT")):
                return "rights"
            if "W" in suffix:
                return "warrants"
            return None if "." in symbol or listing.primary else "preferred"
    return None


def resolve_companies(listings: dict[str, Listing], companies: str, cache_dir: Path) -> pa.Table:
    """The kept symbols' company rows, with SEC SIC codes looked up for `companies: sec`."""
    if companies == "sec":
        snapshot = sec.snapshot(cache_dir)
        codes = sec.sic_codes(snapshot, {listing.cik for listing in listings.values()})
        listings = {
            symbol: Listing(
                company_name=listing.company_name,
                cik=listing.cik,
                exchange=listing.exchange,
                sic_code=codes[listing.cik][0],
                sector=sec.sector(codes[listing.cik][0]),
                industry=sec.industry(codes[listing.cik][1]),
            )
            for symbol, listing in listings.items()
        }
    return pa.Table.from_pylist(
        [
            {
                "asset_id": symbol,
                "company_name": listing.company_name,
                "cik": f"{listing.cik:010d}" if listing.cik is not None else None,
                "sic_code": listing.sic_code,
                "sector": listing.sector,
                "industry": listing.industry,
                "exchange": listing.exchange,
                "profile": listing.profile,
                "is_synthetic": listing.is_synthetic,
            }
            for symbol, listing in sorted(listings.items())
        ],
        schema=pa.schema(
            [
                ("asset_id", pa.string()),
                ("company_name", pa.string()),
                ("cik", pa.string()),
                ("sic_code", pa.string()),
                ("sector", pa.string()),
                ("industry", pa.string()),
                ("exchange", pa.string()),
                ("profile", pa.string()),
                ("is_synthetic", pa.bool_()),
            ]
        ),
    )


def write_tables(db: duckdb.DuckDBPyConnection, pack: Pack, out: Path, news: Path | None) -> None:
    """The market tables, from the rollup and sessions (and `news`, a ticker-linked news table) to `out`."""
    config = pack.manifest["market"]
    bars = config["bars"]
    anchor = pack.manifest.get("prediction", {}).get("anchor")
    # Liquidity is ranked on what was observable at the prediction anchor, so the ranked population leaks nothing.
    cutoff = f"DATE '{datetime.fromisoformat(anchor).astimezone(UTC).date()}'" if anchor else "DATE '9999-12-31'"
    close = sql_literal(bars["regular_session"][1])
    sessions = TRADING_SESSIONS.format(timezone=sql_literal(bars["timezone"]), close=close)
    statements = {  # table: (query, sort order of its file)
        "daily_prices": (DAILY_PRICES, "asset_id, trading_date"),
        "assets": (ASSETS.format(cutoff=cutoff), "asset_id"),
        "ticker_history": (TICKER_HISTORY, "asset_id"),
        "trading_sessions": (sessions, "trading_date"),
        "asset_relationships": (RELATIONSHIPS.format(peers=config["peers"]), "source_asset_id, peer_rank"),
    }
    db.execute(PRICES)
    for table, (select, order) in statements.items():
        db.execute(f"CREATE TABLE {table} AS {select}")
        db.execute(f"COPY (FROM {table} ORDER BY {order}) TO {sql_literal(str(out / f'{table}.parquet'))} ({PARQUET})")
    if news is not None:
        news_table = pack.manifest["analytics"]["news_table"]
        select = NEWS.format(path=sql_literal(str(news)))
        db.execute(f"COPY ({select}) TO {sql_literal(str(out / f'{news_table}.parquet'))} ({PARQUET})")


PRICES = """
CREATE TABLE prices AS
SELECT r.symbol AS asset_id, r.trading_date,
  round(CAST(r.open AS DOUBLE), 4) AS open, round(CAST(r.high AS DOUBLE), 4) AS high,
  round(CAST(r.low AS DOUBLE), 4) AS low, round(CAST(r.close AS DOUBLE), 4) AS close,
  CAST(round(r.volume) AS BIGINT) AS volume, CAST(r.dollar_volume AS DOUBLE) AS dollar_volume,
  CAST(r.bar_count AS INTEGER) AS bar_count
FROM rollup r JOIN sessions USING (trading_date) JOIN companies c ON c.asset_id = r.symbol
"""
DAILY_PRICES = """
SELECT asset_id || ':' || strftime(trading_date, '%Y-%m-%d') AS price_id, asset_id, trading_date,
  open, high, low, close, close AS adjusted_close, volume, dollar_volume, bar_count,
  coalesce(close / lag(close) OVER (PARTITION BY asset_id ORDER BY trading_date) - 1, 0.0) AS total_return_1d
FROM prices
"""
ASSETS = """
SELECT c.asset_id, c.company_name, c.cik, c.sic_code, c.sector, c.industry, c.exchange, c.profile,
  s.first_session, s.last_session, CAST(s.sessions AS INTEGER) AS sessions, s.median_dollar_volume,
  CAST(row_number() OVER (ORDER BY s.median_dollar_volume DESC NULLS LAST, c.asset_id) AS INTEGER) AS liquidity_rank,
  c.is_synthetic
FROM companies c JOIN (
  SELECT asset_id, min(trading_date) AS first_session, max(trading_date) AS last_session, count(*) AS sessions,
    median(dollar_volume) FILTER (WHERE trading_date <= {cutoff}) AS median_dollar_volume
  FROM prices GROUP BY asset_id
) s USING (asset_id)
"""
TICKER_HISTORY = """
SELECT asset_id AS ticker_history_id, asset_id, asset_id AS ticker, first_session AS effective_from,
  CAST(NULL AS DATE) AS effective_to
FROM assets
"""
TRADING_SESSIONS = """
SELECT strftime(trading_date, '%Y-%m-%d') AS session_id, trading_date,
  timezone({timezone}, trading_date + CAST({close} AS TIME)) AS close_at, CAST(symbols AS INTEGER) AS symbols
FROM sessions
"""
RELATIONSHIPS = """
WITH ranked AS (
  SELECT a.asset_id AS source_asset_id, b.asset_id AS target_asset_id,
    row_number() OVER (PARTITION BY a.asset_id ORDER BY b.liquidity_rank) AS peer_rank
  FROM assets a JOIN assets b ON a.sic_code = b.sic_code AND a.asset_id <> b.asset_id
  WHERE a.sic_code <> ''
)
SELECT source_asset_id || '>' || target_asset_id AS relationship_id, source_asset_id, target_asset_id,
  'same_sic_code' AS relationship_type, CAST(peer_rank AS INTEGER) AS peer_rank
FROM ranked WHERE peer_rank <= {peers}
"""
NEWS = """
SELECT n.news_id, n.symbol AS primary_asset_id, n.* EXCLUDE (news_id, symbol)
FROM read_parquet({path}) n JOIN assets a ON a.asset_id = n.symbol
ORDER BY n.published_at, n.news_id
"""


def dataset_file(dataset: Dataset, files: list[dict[str, Any]], relative: str) -> Path:
    """A file of the dataset that the manifest lists, and so was verified."""
    if relative not in {entry["path"] for entry in files}:
        raise MarketError(f"{dataset.id}: {relative} is not in the dataset's manifest")
    return dataset.root / relative


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'

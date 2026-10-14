#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Generate the synthetic market's raw dataset, in the layout a real market dataset uses.

    python build.py --profile standard --out <dir>

    <dir>/bars/month=YYYY-MM/part-NNN.parquet   symbol, time, open, high, low, close, volume
    <dir>/companies.parquet                     symbol, company_name, sector, industry, sic_code, exchange,
                                                profile, is_synthetic
    <dir>/news.parquet                          news_id, symbol, published_at, source_name, headline, summary,
                                                event_type, sentiment_label, is_story
    <dir>/manifest.json                         every file's size and SHA-256, and the dataset fingerprint

demo-data then imports it exactly as it imports a real dataset: the daily rollup, sessions, assets, peers and
news. The numbers come from seeded numpy streams with model.yaml's constants. The names and the news text come
from text/, which `demo.sh data generate` writes with NeMo Data Designer and Nemotron. This step needs no key and
no network, and the same inputs always give the same bytes.

The market is simulated over model.yaml's whole calendar and each profile keeps its own window, so an issuer
has the same prices in every profile that includes it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zlib
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

HERE = Path(__file__).resolve().parent
PACK_DIR = HERE.parent
ROW_GROUP_ROWS = 122_880
PART_ROWS = 5_000_000  # bars per file at most, about 100 MB
BARS = pa.schema(
    [
        ("symbol", pa.string()),
        ("time", pa.timestamp("us")),  # wall-clock time in America/New_York
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
    ]
)
NEWS = pa.schema(
    [
        ("news_id", pa.string()),
        ("symbol", pa.string()),
        ("published_at", pa.timestamp("us", tz="UTC")),
        ("source_name", pa.string()),
        ("headline", pa.string()),
        ("summary", pa.string()),
        ("event_type", pa.string()),
        ("sentiment_label", pa.string()),
        ("is_story", pa.bool_()),
    ]
)
Text = dict[str, list[dict[str, Any]]]


def rng(seed: int, stream: str, *keys: int) -> np.random.Generator:
    """The random stream for one concern and key, e.g. rng(seed, "issuer", slot)."""
    return np.random.default_rng([seed, zlib.crc32(stream.encode()), *keys])


def main() -> None:
    args = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    args.add_argument("--profile", required=True)
    args.add_argument("--out", type=Path, required=True)
    options = args.parse_args()
    pack = yaml.safe_load((PACK_DIR / "pack.yaml").read_text(encoding="utf-8"))
    model = yaml.safe_load((HERE / "model.yaml").read_text(encoding="utf-8"))
    params = pack["generator"]["profiles"][options.profile]["params"]
    generate(model, params, read_text(PACK_DIR / "text", model, params["issuers"]), options.out)


def read_text(directory: Path, model: dict[str, Any], issuers: int) -> Text:
    """The Nemotron text: the first `issuers` companies, the headline templates and the story items."""
    text = {
        name: [json.loads(line) for line in (directory / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]
        for name in ("companies", "headlines", "stories")
    }
    if len(text["companies"]) < issuers:
        raise SystemExit(
            f"{directory} has text for {len(text['companies'])} issuers and this profile needs {issuers}: run "
            "`demo.sh data generate --profile <profile>`, which extends it"
        )
    text["companies"] = text["companies"][:issuers]
    for story, event in zip(text["stories"], model["story"], strict=True):
        if [story["date"], story["event_type"], story["sentiment_label"]] != [
            event["date"],
            event["event_type"],
            event["sentiment"],
        ]:
            raise SystemExit(f"text/stories.jsonl no longer matches model.yaml's story {story['slot']}: run generate")
    return text


def generate(model: dict[str, Any], params: dict[str, Any], text: Text, out: Path) -> None:
    calendar = sessions(model)
    window = (calendar >= np.datetime64(params["start"])) & (calendar <= np.datetime64(params["end"]))
    companies = text["companies"]
    news = news_events(model, companies, calendar)
    daily = simulate(model, companies, len(calendar), news, window)
    out.mkdir(parents=True, exist_ok=True)
    write_bars(model, companies, calendar[window], daily, out, frequency=params["frequency"])
    write_companies(companies, out / "companies.parquet")
    write_news(model, text, news[window[news["session"]]], calendar, out / "news.parquet")
    write_manifest(out)


# ------------------------------------------------------------------------------------------------ calendar


def sessions(model: dict[str, Any]) -> np.ndarray:
    """US exchange sessions over model.yaml's calendar: weekdays without the holidays and closures."""
    first, last = (date.fromisoformat(day) for day in model["calendar"]["range"])
    closed = {date.fromisoformat(day) for day in model["calendar"]["closures"]}
    for year in range(first.year, last.year + 1):
        closed |= holidays(year)
    days = (first + timedelta(days=offset) for offset in range((last - first).days + 1))
    return np.array([day for day in days if day.weekday() < 5 and day not in closed], dtype="datetime64[D]")


def holidays(year: int) -> set[date]:
    """The NYSE's full-day holidays in a year, on the days they are observed."""

    def nth_weekday(month: int, weekday: int, n: int) -> date:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))

    def observed(day: date) -> date:  # Saturday -> Friday, Sunday -> Monday
        return day + timedelta(days={5: -1, 6: 1}.get(day.weekday(), 0))

    may_31 = date(year, 5, 31)
    days = {
        nth_weekday(1, 0, 3),  # Martin Luther King Jr. Day
        nth_weekday(2, 0, 3),  # Washington's Birthday
        easter(year) - timedelta(days=2),  # Good Friday
        may_31 - timedelta(days=may_31.weekday()),  # Memorial Day
        observed(date(year, 7, 4)),
        nth_weekday(9, 0, 1),  # Labor Day
        nth_weekday(11, 3, 4),  # Thanksgiving
        observed(date(year, 12, 25)),
    }
    if date(year, 1, 1).weekday() != 5:  # a Saturday New Year's Day is not moved back into December
        days.add(observed(date(year, 1, 1)))
    if year >= 2022:
        days.add(observed(date(year, 6, 19)))  # Juneteenth
    return days


def easter(year: int) -> date:
    """Easter Sunday, by the anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday) // 451
    month, day = divmod(h + weekday - 7 * m + 114, 31)
    return date(year, month, day + 1)


def month_bounds(calendar: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The first session of each month and the one after its last."""
    months = calendar.astype("datetime64[M]")
    starts = np.flatnonzero(np.r_[True, months[1:] != months[:-1]])
    return starts, np.r_[starts[1:], len(calendar)]


# --------------------------------------------------------------------------------------------------- news


def news_events(model: dict[str, Any], companies: list[dict], calendar: np.ndarray) -> np.ndarray:
    """Every news item: its issuer slot, session, type, sentiment, headline variant, minute (UTC) and story flag.

    Background items arrive per issuer as a Poisson process over the months; each story issuer also has its one
    planted story item. Sorted by (slot, session, minute).
    """
    seed, config = model["seed"], model["news"]
    types, sentiments = list(model["event_types"]), list(model["sentiments"])
    type_p = normalized([model["event_types"][name]["weight"] for name in types])
    sentiment_p = normalized(list(model["sentiments"].values()))
    first, last = (_minute(clock) for clock in config["publish_utc"])
    starts, ends = month_bounds(calendar)
    parts = []
    for company in companies:
        draw = rng(seed, "news", company["slot"])
        rate = config["story_per_issuer_month" if company["is_story"] else "per_issuer_month"]
        counts = draw.poisson(rate, len(starts))
        n = int(counts.sum())
        month_start, month_length = np.repeat(starts, counts), np.repeat(ends - starts, counts)
        parts.append(
            {
                "slot": np.full(n, company["slot"]),
                "session": month_start + (draw.random(n) * month_length).astype(int),
                "type": draw.choice(len(types), n, p=type_p),
                "sentiment": draw.choice(len(sentiments), n, p=sentiment_p),
                "variant": draw.integers(0, model["headline_variants"], n),
                "minute": draw.integers(first, last + 1, n),
                "story": np.zeros(n, dtype=bool),
            }
        )
    for slot, story in enumerate(model["story"][: len(companies)]):
        session = int(np.searchsorted(calendar, np.datetime64(story["date"])))
        if session >= len(calendar) or calendar[session] != np.datetime64(story["date"]):
            raise SystemExit(f"story {slot}: {story['date']} is not a session")
        values = [
            slot,
            session,
            types.index(story["event_type"]),
            sentiments.index(story["sentiment"]),
            0,
            _minute(config["story_utc"]),
            True,
        ]
        parts.append({key: np.array([value]) for key, value in zip(parts[0], values, strict=True)})
    events = {key: np.concatenate([part[key] for part in parts]) for key in parts[0]}
    order = np.lexsort((events["minute"], events["session"], events["slot"]))
    table = np.empty(len(order), dtype=[(key, values.dtype) for key, values in events.items()])
    for key, values in events.items():
        table[key] = values[order]
    return table


def normalized(weights: list[float]) -> np.ndarray:
    values = np.array(weights, dtype=float)
    return values / values.sum()


def _minute(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


# ------------------------------------------------------------------------------------------------- prices


def simulate(model: dict[str, Any], companies: list[dict], length: int, news: np.ndarray, window: np.ndarray):
    """Daily open, high, low, close and volume, as issuers x sessions arrays over the profile's window.

    r = beta * market + gamma * industry + drift + volatility * noise + news, where the market, industry and
    noise series are unit-variance Student-t draws. A background item tilts the next session's return by
    `tilt` volatilities with its sentiment. A story item adds its shock to its own session, and a share of it
    over the sessions after. Volume starts from a daily dollar amount and rises with the size of the move and
    on news sessions.
    """
    seed, prices, volumes = model["seed"], model["prices"], model["volumes"]
    df = prices["tail_df"]
    industries = {entry["sic"]: entry for entry in model["industries"]}
    market = student_t(rng(seed, "market"), df, length) * prices["market_volatility"]
    industry_factor = {
        sic: student_t(rng(seed, "industry", sic), df, length) * prices["industry_volatility"] for sic in industries
    }
    direction = np.array([{"positive": 1.0, "negative": -1.0}.get(name, 0.0) for name in model["sentiments"]])
    follow, follow_share = model["news"]["story_follow_through"]
    out = {key: np.empty((len(companies), int(window.sum()))) for key in ("open", "high", "low", "close", "volume")}
    for index, company in enumerate(companies):
        slot, industry = company["slot"], industries[int(company["sic_code"])]
        draw = rng(seed, "issuer", slot)
        volatility = draw.uniform(*prices["issuer_volatility"]) * industry["volatility"]
        beta, gamma = draw.uniform(*prices["beta"]), draw.uniform(*prices["gamma"])
        drift = draw.uniform(*prices["annual_drift"]) / 252
        start = industry["price"] * np.exp(draw.uniform(*np.log(prices["start_price_spread"])))
        dollars = np.exp(
            draw.uniform(*np.log(volumes["story_dollar_volume" if company["is_story"] else "dollar_volume"]))
        )

        mine = news[news["slot"] == slot]
        effect = np.zeros(length + follow)  # room past the calendar's end, cut below
        np.add.at(effect, mine["session"] + 1, direction[mine["sentiment"]] * model["news"]["tilt"] * volatility)
        for event in mine[mine["story"]]:
            shock = model["story"][slot]["shock"]
            effect[event["session"]] += shock
            effect[event["session"] + 1 : event["session"] + 1 + follow] += shock * follow_share / follow
        effect = effect[:length]
        on_news = np.zeros(length, dtype=bool)
        on_news[mine["session"]] = True

        noise = rng(seed, "issuer-noise", slot)
        returns = beta * market + gamma * industry_factor[industry["sic"]] + drift + effect
        returns = np.clip(returns + volatility * student_t(noise, df, length), -0.6, 1.5)
        close = start * np.cumprod(1 + returns)
        open_ = np.r_[start, close[:-1]] * (1 + noise.normal(0, prices["gap_volatility"] * volatility, length))
        spread = np.abs(noise.normal(0, prices["range_volatility"] * volatility, (2, length)))
        total_volatility = np.sqrt(
            (beta * prices["market_volatility"]) ** 2 + (gamma * prices["industry_volatility"]) ** 2 + volatility**2
        )
        activity = volumes["move_sensitivity"] * np.abs(returns) / total_volatility
        activity = activity + noise.normal(0, volumes["daily_noise"], length)
        multiplier = np.where(on_news, noise.uniform(*volumes["news_multiplier"], length), 1.0)
        bars = {
            "open": open_,
            "high": np.maximum(open_, close) * (1 + spread[0]),
            "low": np.minimum(open_, close) * (1 - spread[1]),
            "close": close,
            "volume": np.maximum(np.round(dollars * np.exp(activity) * multiplier / close), 100),
        }
        for key, values in bars.items():
            out[key][index] = values[window]
    return {key: values.astype(np.int64) if key == "volume" else np.round(values, 4) for key, values in out.items()}


def student_t(draw: np.random.Generator, df: float, size: int) -> np.ndarray:
    """Student-t draws scaled to unit variance: fat tails with a standard normal's spread."""
    return draw.standard_t(df, size) / np.sqrt(df / (df - 2))


# -------------------------------------------------------------------------------------------------- write


def write_bars(
    model: dict[str, Any], companies: list[dict], days: np.ndarray, daily: dict, out: Path, *, frequency: str
) -> None:
    """One directory per month. In it, parts of issuers in ticker order, each sorted by (symbol, time)."""
    order = sorted(range(len(companies)), key=lambda index: companies[index]["ticker"])
    per_session = minute_count(model) if frequency == "1min" else 1
    months = days.astype("datetime64[M]")
    for month in np.unique(months):
        columns = np.flatnonzero(months == month)
        chunk = max(1, PART_ROWS // (len(columns) * per_session))
        directory = out / "bars" / f"month={month}"
        directory.mkdir(parents=True, exist_ok=True)
        for part, first in enumerate(range(0, len(order), chunk)):
            rows = [
                (companies[index]["ticker"], index, companies[index]["slot"]) for index in order[first : first + chunk]
            ]
            if frequency == "1min":
                table = minute_bars(
                    model, rows, days[columns], {key: value[:, columns] for key, value in daily.items()}
                )
            else:
                table = daily_bars(rows, days[columns], {key: value[:, columns] for key, value in daily.items()})
            path = directory / f"part-{part:03d}.parquet"
            pq.write_table(table, path, row_group_size=ROW_GROUP_ROWS, compression="zstd")


def daily_bars(rows: list[tuple], days: np.ndarray, daily: dict) -> pa.Table:
    """One bar per session, stamped at the 16:00 close, so the rollup's regular session keeps it unchanged."""
    indexes = [index for _, index, _ in rows]
    arrays = {
        "symbol": np.repeat([ticker for ticker, _, _ in rows], len(days)),
        "time": np.tile(days.astype("datetime64[us]") + np.timedelta64(16, "h"), len(rows)),
    }
    arrays |= {key: daily[key][indexes].ravel() for key in ("open", "high", "low", "close", "volume")}
    return pa.Table.from_pydict(arrays, schema=BARS)


def minute_count(model: dict[str, Any]) -> int:
    start, end = (_minute(clock) for clock in model["minute_bars"]["session"])
    return end - start + 1


def minute_bars(model: dict[str, Any], rows: list[tuple], days: np.ndarray, daily: dict) -> pa.Table:
    """1-minute bars that roll up exactly to the daily bar: first open, max high, min low, last close, sum volume.

    A session's path is a Brownian bridge from its open to its close, clipped to its range, and the bars at the
    path's highest and lowest points reach the day's high and low. Volume is U-shaped over the session, and the
    16:00 bar carries the closing auction.
    """
    seed, config = model["seed"], model["minute_bars"]
    count, sessions_ = minute_count(model), len(days)
    offsets = np.timedelta64(_minute(config["session"][0]), "m") + np.arange(count).astype("timedelta64[m]")
    times = (days.astype("datetime64[m]")[:, None] + offsets).astype("datetime64[us]").ravel()
    steps = np.linspace(0, 1, count + 1)
    u_shape = 1 + 2 * np.linspace(-1, 1, count - 1) ** 2
    auction = config["closing_auction_share"]
    frames = []
    for ticker, index, slot in rows:
        draw = rng(seed, "minutes", slot)
        o, h, low, c, v = (daily[key][index][:, None] for key in ("open", "high", "low", "close", "volume"))
        walk = np.cumsum(draw.normal(0, 1, (sessions_, count + 1)), axis=1)
        bridge = walk - steps * walk[:, -1:]  # 0 at both ends
        bridge *= (h - low) / 4 / np.maximum(np.abs(bridge).max(axis=1, keepdims=True), 1e-9)
        path = np.clip(o + (c - o) * steps + bridge, low, h)
        path[:, :1], path[:, -1:] = o, c
        opens, closes = path[:, :-1], path[:, 1:]
        highs, lows = np.maximum(opens, closes), np.minimum(opens, closes)
        rows_ = np.arange(sessions_)
        highs[rows_, highs.argmax(axis=1)] = h[:, 0]
        lows[rows_, lows.argmin(axis=1)] = low[:, 0]
        weights = u_shape * np.exp(draw.normal(0, 0.3, (sessions_, count - 1)))
        weights = np.c_[weights / weights.sum(axis=1, keepdims=True) * (1 - auction), np.full(sessions_, auction)]
        volume = np.floor(weights * v).astype(np.int64)
        volume[:, -1] += v[:, 0] - volume.sum(axis=1)
        frames.append(
            {
                "symbol": np.full(sessions_ * count, ticker),
                "time": times,
                "open": np.round(opens, 4).ravel(),
                "high": np.round(highs, 4).ravel(),
                "low": np.round(lows, 4).ravel(),
                "close": np.round(closes, 4).ravel(),
                "volume": volume.ravel(),
            }
        )
    return pa.Table.from_pydict({key: np.concatenate([frame[key] for frame in frames]) for key in BARS.names}, BARS)


def write_companies(companies: list[dict], path: Path) -> None:
    rows = [
        {
            "symbol": company["ticker"],
            "company_name": company["company_name"],
            "sector": company["sector"],
            "industry": company["industry"],
            "sic_code": str(company["sic_code"]),
            "exchange": company["exchange"],
            "profile": company["profile"],
            "is_synthetic": True,
        }
        for company in sorted(companies, key=lambda company: company["ticker"])
    ]
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")


def write_news(model: dict[str, Any], text: Text, news: np.ndarray, calendar: np.ndarray, path: Path) -> None:
    """Background items fill a seeded headline template with the company's name; story items have their own text."""
    types, sentiments = list(model["event_types"]), list(model["sentiments"])
    by_slot = {company["slot"]: company for company in text["companies"]}
    templates = {
        (row["event_type"], row["sentiment_label"], row["variant"]): row["template"] for row in text["headlines"]
    }
    stories = {row["slot"]: row for row in text["stories"]}
    seen: dict[str, int] = {}
    rows = []
    for event in news:
        company = by_slot[int(event["slot"])]
        day: date = calendar[event["session"]].item()
        event_type, sentiment = types[event["type"]], sentiments[event["sentiment"]]
        prefix = f"{company['ticker']}-{day:%Y%m%d}"
        seen[prefix] = seen.get(prefix, 0) + 1
        if event["story"]:
            headline, summary = stories[company["slot"]]["headline"], stories[company["slot"]]["summary"]
        else:
            template = templates[(event_type, sentiment, int(event["variant"]))]
            headline, summary = template.replace("{company}", company["company_name"]), None
        rows.append(
            {
                "news_id": f"{prefix}-{seen[prefix]}",
                "symbol": company["ticker"],
                "published_at": datetime.combine(day, datetime.min.time()) + timedelta(minutes=int(event["minute"])),
                "source_name": model["news"]["source_name"],
                "headline": headline,
                "summary": summary,
                "event_type": event_type,
                "sentiment_label": sentiment,
                "is_story": bool(event["story"]),
            }
        )
    pq.write_table(pa.Table.from_pylist(rows, schema=NEWS), path, compression="zstd")


def write_manifest(out: Path) -> None:
    """The dataset manifest: every file's path, size and SHA-256, sorted by path, and the fingerprint over them."""
    files = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files.append({"path": path.relative_to(out).as_posix(), "bytes": path.stat().st_size, "sha256": digest})
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {
        "format": "market-demo-dataset",
        "version": 1,
        "dataset_fingerprint": fingerprint,
        "file_count": len(files),
        "total_bytes": sum(entry["bytes"] for entry in files),
        "files": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

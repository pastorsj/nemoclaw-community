#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""SEC company-facts helper for the NemoClaw financial assistant."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
from functools import lru_cache
from typing import Any

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
MAX_TICKER_LENGTH = 16
MAX_USER_AGENT_LENGTH = 200
TICKER_PATTERN = re.compile(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*", re.ASCII)
CONTACT_EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.ASCII | re.IGNORECASE
)
METRICS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "NetIncomeLoss",
    "OperatingIncomeLoss",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
]


def user_agent() -> str:
    value = os.environ.get("SEC_USER_AGENT", "").strip()
    email = CONTACT_EMAIL_PATTERN.search(value)
    identity = ""
    if email is not None:
        identity = f"{value[: email.start()]} {value[email.end() :]}".strip()
    if (
        not value
        or len(value) > MAX_USER_AGENT_LENGTH
        or not value.isascii()
        or not value.isprintable()
        or email is None
        or not any(character.isalnum() for character in identity)
    ):
        raise ValueError(
            "SEC_USER_AGENT must identify the caller and include a contact email"
        )
    return value


def normalize_ticker(symbol: str) -> str:
    stripped = symbol.strip()
    if not stripped:
        raise ValueError("empty ticker")
    if not stripped.isascii():
        raise ValueError(f"invalid SEC ticker: {symbol!r}")
    normalized = stripped.upper()
    if len(normalized) > MAX_TICKER_LENGTH or not TICKER_PATTERN.fullmatch(normalized):
        raise ValueError(f"invalid SEC ticker: {symbol!r}")
    return normalized


def fetch_json(url: str, timeout: int) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": user_agent(),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


@lru_cache(maxsize=1)
def ticker_index(timeout: int) -> dict[str, dict[str, Any]]:
    raw = fetch_json(SEC_TICKERS_URL, timeout=timeout)
    index: dict[str, dict[str, Any]] = {}
    for item in raw.values():
        try:
            ticker = normalize_ticker(str(item["ticker"]))
        except (KeyError, ValueError):
            # The upstream map occasionally contains placeholders that are not
            # valid exchange tickers (for example, a trailing separator). Do
            # not let one provider-side record prevent valid lookups.
            continue
        cik = str(item["cik_str"]).zfill(10)
        index[ticker] = {
            "ticker": ticker,
            "cik": cik,
            "title": item.get("title"),
        }
    return index


def resolve_ticker(symbol: str, timeout: int) -> dict[str, Any]:
    symbol = normalize_ticker(symbol)
    index = ticker_index(timeout)
    if symbol not in index:
        raise ValueError(f"ticker not found in SEC company_tickers.json: {symbol}")
    return index[symbol]


def latest_fact(metric: dict[str, Any]) -> dict[str, Any] | None:
    units = metric.get("units", {})
    candidates: list[dict[str, Any]] = []
    for unit, facts in units.items():
        for fact in facts:
            if fact.get("form") not in {"10-K", "10-Q"}:
                continue
            if "val" not in fact:
                continue
            enriched = dict(fact)
            enriched["unit"] = unit
            candidates.append(enriched)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.get("filed", ""),
            item.get("end", ""),
            item.get("fy") or 0,
        ),
    )


def summarize_facts(symbol: str, timeout: int) -> dict[str, Any]:
    company = resolve_ticker(symbol, timeout)
    facts = fetch_json(SEC_FACTS_URL.format(cik=company["cik"]), timeout=timeout)
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    summary: dict[str, Any] = {}
    for metric_name in METRICS:
        metric = us_gaap.get(metric_name)
        if not metric:
            continue
        latest = latest_fact(metric)
        if latest:
            summary[metric_name] = latest
    return {
        "company": {
            "ticker": company["ticker"],
            "cik": company["cik"],
            "title": company["title"],
            "entity_name": facts.get("entityName"),
        },
        "metrics": summary,
    }


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=int, default=20, help="request timeout in seconds"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    lookup = subparsers.add_parser("lookup", help="resolve ticker to SEC CIK")
    lookup.add_argument("symbol")

    facts = subparsers.add_parser("facts", help="summarize selected SEC company facts")
    facts.add_argument("symbol")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "lookup":
            result = resolve_ticker(args.symbol, args.timeout)
        else:
            result = summarize_facts(args.symbol, args.timeout)
        emit(
            {
                "ok": True,
                "source": "sec-companyfacts-json",
                "retrieved_at_unix": int(time.time()),
                "result": result,
                "caveat": "Public SEC data; review original filings before relying on it.",
            }
        )
        return 0
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        emit({"ok": False, "error": type(exc).__name__, "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The real names the synthetic issuers must not collide with: SEC's public ticker files.

company_tickers.json and company_tickers_exchange.json list every SEC-registered operating company's ticker and
title; company_tickers_mf.json lists fund symbols. SEC requires a User-Agent with a name and an email
(SEC_USER_AGENT). Tests pass a directory holding stub copies instead.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

FILES = ("company_tickers.json", "company_tickers_exchange.json", "company_tickers_mf.json")
BASE_URL = "https://www.sec.gov/files/"
LEGAL_WORDS = {"inc", "corp", "corporation", "co", "company", "ltd", "plc", "holdings", "group", "llc", "lp", "sa",
               "nv", "ag", "trust", "the"}  # fmt: skip


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def normalized_name(name: str) -> str:
    """Casefolded, without punctuation or legal words: 'The Velbrook Group, Inc.' -> 'velbrook'."""
    return " ".join(word for word in words(name) if word not in LEGAL_WORDS)


@dataclass
class SecNames:
    tickers: set[str] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)  # normalized
    title_words: set[str] = field(default_factory=set)
    record: dict[str, Any] = field(default_factory=dict)

    def root_taken(self, root: str) -> bool:
        return root.casefold() in self.title_words

    def ticker_taken(self, ticker: str) -> bool:
        return ticker.upper() in self.tickers

    def name_taken(self, name: str) -> bool:
        return normalized_name(name) in self.titles


def load(user_agent: str | None, directory: Path | None = None) -> SecNames:
    """Fetch the three files from SEC (or read them from `directory`) and index them."""
    names = SecNames()
    files = []
    for name in FILES:
        if directory is not None:
            body, url = (directory / name).read_bytes(), str(directory / name)
        else:
            if not user_agent:
                raise SystemExit("set SEC_USER_AGENT (a name and an email) to fetch SEC's ticker files")
            request = urllib.request.Request(BASE_URL + name, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(request, timeout=60) as response:
                body, url = response.read(), BASE_URL + name
        files.append({"url": url, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
        _index(names, name, json.loads(body))
    names.record = {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": files,
        "tickers": len(names.tickers),
        "titles": len(names.titles),
    }
    return names


def _index(names: SecNames, file: str, document: Any) -> None:
    if file == "company_tickers.json":
        rows = [(entry["ticker"], entry["title"]) for entry in document.values()]
    else:
        columns = document["fields"]
        ticker = columns.index("symbol" if "symbol" in columns else "ticker")
        title = columns.index("name") if "name" in columns else None
        rows = [(row[ticker], row[title] if title is not None else None) for row in document["data"]]
    for ticker, title in rows:
        if ticker:
            names.tickers.add(str(ticker).upper())
            names.tickers.add(re.split(r"[-.]", str(ticker).upper())[0])  # BRK-B also blocks BRK
        if title:
            names.titles.add(normalized_name(title))
            names.title_words.update(words(title))

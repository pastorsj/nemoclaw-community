# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""SEC company metadata for real tickers: company metadata only, never filings or news.

    company_tickers_exchange.json      CIK, name, ticker and exchange of every listed SEC registrant
    submissions/CIK##########.json     per CIK: its SIC code and description (one request per CIK)

Both are cached in <cache>/sec/<YYYY-MM-DD>/, a snapshot: company_tickers_exchange.json as fetched, and sic.json
with the SIC code of every CIK looked up so far. `prepare` uses the newest snapshot (`--refresh-sec` starts a new
one), and the build key includes the ticker file's digest. SEC requires a descriptive User-Agent (SEC_USER_AGENT)
and at most 10 requests a second.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path

from demo_data.corpus.common import sha256_file

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
TICKERS = "company_tickers_exchange.json"
SIC = "sic.json"
REQUESTS_PER_SECOND = 9  # SEC's fair-access limit is 10
# SIC divisions (the first two digits), the sector a company is filed under.
DIVISIONS = [
    (1, 9, "Agriculture, Forestry and Fishing"),
    (10, 14, "Mining"),
    (15, 17, "Construction"),
    (20, 39, "Manufacturing"),
    (40, 49, "Transportation, Communications and Utilities"),
    (50, 51, "Wholesale Trade"),
    (52, 59, "Retail Trade"),
    (60, 67, "Finance, Insurance and Real Estate"),
    (70, 89, "Services"),
    (91, 97, "Public Administration"),
]


class SecError(Exception):
    """SEC data could not be fetched."""


class NotFound(SecError):
    """SEC has no such file (for example, submissions for a CIK it no longer serves)."""


@dataclass(frozen=True)
class Company:
    cik: int
    name: str
    exchange: str
    primary: bool  # SEC lists an issuer's primary ticker first (GOOGL before GOOG)


def snapshot(cache: Path, *, refresh: bool = False) -> Path:
    """The newest snapshot directory, fetching today's ticker file if there is none (or `refresh`)."""
    root = cache / "sec"
    existing = sorted(path.parent for path in root.glob(f"*/{TICKERS}"))
    if existing and not refresh:
        return existing[-1]
    directory = root / datetime.now(UTC).date().isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    body = get(TICKERS_URL)
    json.loads(body)  # fail on a truncated or error response before caching it
    (directory / f".{TICKERS}.tmp").write_bytes(body)
    os.replace(directory / f".{TICKERS}.tmp", directory / TICKERS)
    return directory


def digest(directory: Path) -> str:
    """Identifies a snapshot for the build key."""
    return sha256_file(directory / TICKERS)


def companies(directory: Path) -> dict[str, Company]:
    """SEC ticker (as SEC spells it, e.g. BRK-B) -> company. A ticker listed twice keeps its first row."""
    table = json.loads((directory / TICKERS).read_text(encoding="utf-8"))
    columns = table["fields"]
    result: dict[str, Company] = {}
    seen: set[int] = set()
    for row in table["data"]:
        values = dict(zip(columns, row, strict=True))
        if values["ticker"] and values["ticker"].upper() not in result:
            company = Company(values["cik"], values["name"], values["exchange"] or "", values["cik"] not in seen)
            result[values["ticker"].upper()] = company
            seen.add(values["cik"])
    return result


def sic_codes(directory: Path, ciks: set[int]) -> dict[int, tuple[str, str]]:
    """CIK -> (SIC code, SIC description) for `ciks`, fetching any the snapshot does not have yet.

    Empty strings mean SEC has no SIC code for the CIK. Progress is saved as it goes, so a rerun resumes.
    """
    path = directory / SIC
    cached: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    missing = sorted(cik for cik in ciks if str(cik) not in cached)
    lock, pace = threading.Lock(), Pace(REQUESTS_PER_SECOND)

    def lookup(cik: int) -> tuple[int, list[str]]:
        pace.wait()
        try:
            submissions = json.loads(get(SUBMISSIONS_URL.format(cik=cik)))
        except NotFound:
            return cik, ["", ""]
        return cik, [submissions.get("sic") or "", submissions.get("sicDescription") or ""]

    if missing:
        print(f"sec: looking up SIC codes for {len(missing):,} companies (about {len(missing) // 9 + 1} s)")
    with ThreadPoolExecutor(max_workers=4) as pool:
        for count, (cik, sic) in enumerate(pool.map(lookup, missing), start=1):
            with lock:
                cached[str(cik)] = sic
            if count % 200 == 0 or count == len(missing):
                _save(path, cached)
    return {cik: tuple(cached[str(cik)]) for cik in ciks}


def sector(sic_code: str) -> str:
    """The SIC division of a four-digit SIC code."""
    if sic_code[:2].isdigit():
        major = int(sic_code[:2])
        for low, high, name in DIVISIONS:
            if low <= major <= high:
                return name
    return "Nonclassifiable"


def industry(description: str) -> str:
    """SEC's upper-case SIC description in title case: "SEMICONDUCTORS & RELATED DEVICES" -> "Semiconductors & ..."."""
    return description.title().replace("'S", "'s") if description else "Unclassified"


class Pace:
    """Spaces request starts at least 1/rate seconds apart, across threads."""

    def __init__(self, rate: float) -> None:
        self.interval, self.next, self.lock = 1.0 / rate, 0.0, threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next)
            self.next = start + self.interval
        time.sleep(max(0.0, start - now))


def get(url: str, attempts: int = 4) -> bytes:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        raise SecError("set SEC_USER_AGENT (e.g. 'Example Co admin@example.com') to fetch SEC data")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise NotFound(f"{url}: {error}") from error
            failure, retry = error, error.code in (429, 500, 502, 503, 504)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            failure, retry = error, True
        if not retry or attempt == attempts:
            raise SecError(f"{url}: {failure}") from failure
        time.sleep(2**attempt)
    raise SecError(f"{url}: no attempts")


def _save(path: Path, cached: dict[str, list[str]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(cached, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)

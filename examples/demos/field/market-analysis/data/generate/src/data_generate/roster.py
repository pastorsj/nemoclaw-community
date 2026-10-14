# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The seeded roster: one row per issuer slot, handed to Data Designer as its seed dataset.

A slot's industry, exchange and name root come from its own random stream, so a roster of N issuers is the first
N rows of any larger one. Name roots are invented words; tickers are built from them. Both are checked against
the real SEC lists (sec.py) and for unfit words, and a slot whose root or ticker fails draws the next candidate from
its stream.
"""

from __future__ import annotations

import itertools
import zlib
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np

ONSETS = ["b", "br", "c", "cl", "d", "dr", "f", "fl", "g", "gr", "h", "k", "l", "m", "n", "p", "pr", "qu", "r",
          "s", "st", "t", "tr", "v", "z", "th", "sh", "ch"]  # fmt: skip
VOWELS = ["a", "e", "i", "o", "u", "a", "e", "o", "ae", "ia", "io", "ou", "ea"]
CODAS = ["", "", "", "n", "r", "l", "x", "s", "m", "nt", "rk", "st", "nd"]
ENDINGS = ["a", "ex", "on", "is", "ia", "ara", "ion", "ora", "ix", "en", "ar", "yn", "ant", "ent", "um", "o", "et"]
# Random syllables can spell words unfit for a public demo, in a root or in a ticker built from it.
UNFIT = ["anal", "anus", "ass", "boob", "butt", "clit", "cock", "coon", "crap", "cum", "cunt", "damn", "dick",
         "fag", "fart", "fuc", "fuk", "fux", "hell", "homo", "jiz", "kike", "kkk", "nazi", "nig", "nude", "peni",
         "piss", "poo", "porn", "rape", "sex", "shit", "slut", "spic", "sux", "tard", "tit", "turd", "twat", "vag",
         "wank", "whor"]  # fmt: skip
MAX_ROUNDS = 5  # candidate roots a slot may reject before generation stops


def rng(seed: int, stream: str, *keys: int) -> np.random.Generator:
    """The random stream for one concern and key, e.g. rng(seed, "roster", slot)."""
    return np.random.default_rng([seed, zlib.crc32(stream.encode()), *keys])


@dataclass(frozen=True)
class Slot:
    slot: int
    sic_code: int
    industry: str
    sector: str
    exchange: str
    is_story: bool
    name_root: str
    ticker: str

    def row(self) -> dict[str, Any]:
        return self.__dict__.copy()


def industries(model: dict[str, Any]) -> dict[int, str]:
    return {entry["sic"]: entry["industry"] for entry in model["industries"]}


def sector(model: dict[str, Any], sic_code: int) -> str:
    """SEC's SIC division for a code, e.g. 3674 -> Manufacturing."""
    major = sic_code // 100
    for division in model["sectors"]:
        if division["from"] <= major <= division["to"]:
            return division["sector"]
    raise ValueError(f"SIC code {sic_code} is in no division")


def unfit(word: str) -> bool:
    return any(fragment in word.casefold() for fragment in UNFIT)


def name_roots(seed: int, slot: int) -> Iterator[str]:
    """An endless, seeded sequence of pronounceable invented words, 5 to 10 letters long."""
    draw = rng(seed, "name-root", slot)
    while True:
        syllables = int(draw.integers(1, 3))
        word = "".join(
            str(draw.choice(ONSETS)) + str(draw.choice(VOWELS)) + str(draw.choice(CODAS)) for _ in range(syllables)
        )
        word += str(draw.choice(ENDINGS))
        if 5 <= len(word) <= 10:
            yield word.capitalize()


def ticker_candidates(root: str, seed: int, slot: int) -> Iterator[str]:
    """Four-letter tickers spelled from the root's letters in order, then seeded random ones."""
    letters = [letter for letter in root.upper() if letter.isalpha()]
    seen: set[str] = set()
    for rest in itertools.combinations(letters[1:], 3):
        ticker = letters[0] + "".join(rest)
        if ticker not in seen:
            seen.add(ticker)
            yield ticker
    draw = rng(seed, "ticker", slot)
    while True:
        yield "".join(chr(ord("A") + int(code)) for code in draw.integers(0, 26, 4))


def build(
    model: dict[str, Any],
    count: int,
    root_taken: Callable[[str], bool],
    ticker_taken: Callable[[str], bool],
    rejected: dict[int, set[str]] | None = None,
) -> list[Slot]:
    """The first `count` slots. `rejected` lists roots already refused per slot (e.g. by a name check)."""
    seed = model["seed"]
    names = industries(model)
    story = model["story"]
    sics = list(names)
    weights = np.array([entry["weight"] for entry in model["industries"]], dtype=float)
    exchanges = list(model["exchanges"])
    exchange_weights = np.array(list(model["exchanges"].values()), dtype=float)
    used_roots: set[str] = set()
    used_tickers: set[str] = set()
    slots = []
    for slot in range(count):
        draw = rng(seed, "roster", slot)
        sic = story[slot]["sic"] if slot < len(story) else sics[int(draw.choice(len(sics), p=weights / weights.sum()))]
        exchange = exchanges[int(draw.choice(len(exchanges), p=exchange_weights / exchange_weights.sum()))]
        refused = (rejected or {}).get(slot, set())
        root = None
        for attempt, candidate in enumerate(name_roots(seed, slot)):
            if attempt > 200 + len(refused):
                raise RuntimeError(f"slot {slot}: no free name root")
            key = candidate.casefold()
            if candidate in refused or key in used_roots or unfit(candidate) or root_taken(candidate):
                continue
            root = candidate
            break
        ticker = next(
            t
            for t in ticker_candidates(root, seed, slot)
            if t not in used_tickers and not unfit(t) and not ticker_taken(t)
        )
        used_roots.add(root.casefold())
        used_tickers.add(ticker)
        slots.append(Slot(slot, sic, names[sic], sector(model, sic), exchange, slot < len(story), root, ticker))
    return slots

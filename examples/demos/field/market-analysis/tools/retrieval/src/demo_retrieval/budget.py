# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep every retrieve_evidence result short enough for the agent to read whole.

Hermes saves an MCP tool result longer than 50,000 characters to a file the sandboxed agent cannot read and shows
the model only a 1,500-character preview, and it does the same to the largest results of one turn past 200,000
characters together (docs/architecture.md, "Tool result size"). So a call returns at most `MAX_HITS` passages of at
most search.MAX_SNIPPET_CHARS each, and drops its lowest-ranked passages while it is longer than `MAX_RESULT_CHARS`
as the agent reads it. Every passage it keeps is whole: its rank, document, chunk, title, URL, date and metadata.
"""

from __future__ import annotations

import json

import pydantic_core

from .search import RetrievalResult
from .search import clip

# 60% of Hermes' 50,000-character limit for one MCP result, as the agent reads it. Six such results, the most one
# recorded turn made at once, also stay within its 200,000-character budget for a turn.
MAX_RESULT_CHARS = 30_000
MAX_HITS = 8  # eight whole passages come to about 27,000 characters as the agent reads them
# What the execution-receipts plugin puts first in every result: {"evidence_id": "hermes-receipt:<sha256>"}.
EVIDENCE_ID = "hermes-receipt:" + "0" * 64
# Only a pathological passage needs these: it is cut to fit when it alone is too long.
MAX_FIELD_CHARS = 300


def agent_chars(result: RetrievalResult) -> int:
    """The result's length as the agent reads it: the MCP server sends it as indented JSON text, Hermes puts that
    text in a JSON string under "result", and the execution-receipts plugin adds the evidence id."""
    text = pydantic_core.to_json(result, fallback=str, indent=2).decode()
    return len(json.dumps({"evidence_id": EVIDENCE_ID, "result": text}, ensure_ascii=False))


def fit(result: RetrievalResult) -> RetrievalResult:
    """The result without its lowest-ranked passages while it is too long; a lone passage too long is cut."""
    hits = list(result.hits)
    current = result
    while agent_chars(current) > MAX_RESULT_CHARS and len(hits) > 1:
        hits.pop()
        current = result.model_copy(update={"hits": hits})
    if agent_chars(current) > MAX_RESULT_CHARS and hits:
        hit = hits[0]
        metadata = {key: clip(v, MAX_FIELD_CHARS) if isinstance(v, str) else v for key, v in hit.metadata.items()}
        shortened = hit.model_copy(update={"title": clip(hit.title, MAX_FIELD_CHARS), "metadata": metadata})
        current = result.model_copy(update={"hits": [shortened]})
        while agent_chars(current) > MAX_RESULT_CHARS and len(shortened.snippet) > 100:
            shortened = shortened.model_copy(update={"snippet": clip(shortened.snippet, len(shortened.snippet) // 2)})
            current = result.model_copy(update={"hits": [shortened]})
    return current

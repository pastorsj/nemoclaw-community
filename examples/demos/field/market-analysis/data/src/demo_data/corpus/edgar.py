# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""SEC EDGAR current reports -> one document per primary filing document and EX-99 exhibit.

Manifest (`format: edgar-filings`):
    {"source_id", "filings": [{"filing_id": "<cik>:<accession>", "company_name", "form", "filed_on",
                               "source_url", "sha256", "ticker"?, "items"?}, ...]}

Each filing is pinned with the index fields it needs, so a build never depends on SEC's indexes, which SEC
regenerates. An optional `ticker` (the filer's stock in the pack) and `items` (the 8-K items it reports, such as
"1.05,9.01") become metadata for citations; the filings stay documents, never news. SEC requires a descriptive
User-Agent (SEC_USER_AGENT, e.g. "Example Co admin@example.com") and at most 10 requests a second.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from lxml import html

from demo_data.corpus.common import CorpusError
from demo_data.corpus.common import Document
from demo_data.corpus.common import Downloads
from demo_data.corpus.common import normalize_text

REQUEST_PAUSE_SECONDS = 0.12
MAX_BYTES = 128 * 1024 * 1024
MIN_TEXT_CHARS = 80
DOCUMENT_BLOCK = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.IGNORECASE | re.DOTALL)
DOCUMENT_FIELD = re.compile(r"^<(TYPE|FILENAME|DESCRIPTION)>(.*)$", re.IGNORECASE | re.MULTILINE)
TEXT_BLOCK = re.compile(r"<TEXT>(.*)</TEXT>", re.IGNORECASE | re.DOTALL)


def documents(source_id: str, manifest_path: Path, downloads: Downloads) -> list[Document]:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        raise CorpusError(f"{source_id}: set SEC_USER_AGENT (e.g. 'Example Co admin@example.com') to fetch SEC EDGAR")
    filings = json.loads(manifest_path.read_text(encoding="utf-8"))["filings"]
    results = []
    for filing in filings:
        body = downloads.fetch(
            filing["source_url"],
            filing["sha256"],
            user_agent=user_agent,
            max_bytes=MAX_BYTES,
            pause=REQUEST_PAUSE_SECONDS,
        )
        results += filing_documents(filing, body.read_text(encoding="latin-1"), source_id)
    return results


def filing_documents(filing: dict[str, Any], submission: str, source_id: str) -> list[Document]:
    """The primary document and EX-99 exhibits of one full-text submission, as plain text."""
    cik, accession = filing["filing_id"].split(":")
    company, form, filed_on = filing["company_name"], filing["form"], filing["filed_on"]
    results = []
    for position, (fields, body) in enumerate(_selected_blocks(form, submission), start=1):
        text = plain_text(body)
        if len(text) < MIN_TEXT_CHARS:
            continue
        filename = fields.get("FILENAME", "document.txt")
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", filename).strip("-") or f"document-{position}"
        document_type = fields["TYPE"].upper()
        results.append(
            Document(
                document_id=f"edgar:{cik}:{accession}:{safe_name.lower()}",
                source_id=source_id,
                title=normalize_text(f"{company} {form}: {fields.get('DESCRIPTION', document_type)}"),
                text=text,
                url=filing["source_url"],
                published_at=f"{filed_on}T00:00:00Z",
                metadata={
                    "citation": f"SEC {form}, {company}, filed {filed_on}, accession {accession}",
                    "cik": cik,
                    "company_name": company,
                    "form": form,
                    "filed_on": filed_on,
                    "accession": accession,
                    "document_type": document_type,
                    "filename": filename,
                    **{key: filing[key] for key in ("ticker", "items") if filing.get(key)},
                },
            )
        )
    return results


def _selected_blocks(form: str, submission: str) -> list[tuple[dict[str, str], str]]:
    """(header fields, body) of each <DOCUMENT> that is the filing's own form or an EX-99 exhibit."""
    blocks = []
    for block in DOCUMENT_BLOCK.findall(submission):
        fields = {key.upper(): normalize_text(value) for key, value in DOCUMENT_FIELD.findall(block)}
        text = TEXT_BLOCK.search(block)
        document_type = fields.get("TYPE", "").upper()
        if text is not None and document_type and (document_type == form or document_type.startswith("EX-99")):
            blocks.append((fields, text.group(1)))
    return blocks


def plain_text(content: str) -> str:
    """Filing HTML (or plain text) as normalized text, without scripts and styles."""
    if "<" not in content or ">" not in content:
        return normalize_text(content)
    try:
        tree = html.fromstring(content)
    except (ValueError, TypeError):
        return normalize_text(re.sub(r"<[^>]+>", " ", content))
    for element in tree.xpath("//script|//style|//noscript"):
        element.drop_tree()
    return normalize_text(tree.text_content())

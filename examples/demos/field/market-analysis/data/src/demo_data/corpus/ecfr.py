# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""eCFR title XML (the versioned full-title API) -> one document per section.

Manifest (`format: ecfr-xml`):
    {"source_id", "as_of", "document": {"url", "sha256", "title_number", "title"}}

Each section's text starts with its title, chapter, part and subpart headings, so a chunk keeps its context.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

from lxml import etree

from demo_data.corpus.common import CorpusError
from demo_data.corpus.common import Document
from demo_data.corpus.common import Downloads
from demo_data.corpus.common import normalize_text

USER_AGENT = "demo-data (nemoclaw-community market-analysis)"
MAX_BYTES = 256 * 1024 * 1024
CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


def documents(source_id: str, manifest_path: Path, downloads: Downloads) -> list[Document]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pin = manifest["document"]
    xml = downloads.fetch(pin["url"], pin["sha256"], user_agent=USER_AGENT, max_bytes=MAX_BYTES)
    return parse_title(
        xml,
        source_id=source_id,
        title_number=pin["title_number"],
        title_heading=pin["title"],
        as_of=manifest["as_of"],
        snapshot_url=pin["url"],
    )


def parse_title(
    path: Path, *, source_id: str, title_number: str, title_heading: str, as_of: str, snapshot_url: str
) -> list[Document]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    try:
        root = etree.parse(str(path), parser=parser).getroot()
    except etree.XMLSyntaxError as error:
        raise CorpusError(f"{path.name} is not valid XML: {error}") from error
    title = next(iter(root.xpath(".//DIV1[@TYPE='TITLE']")), None)
    if title is None or title.get("N") != title_number:
        raise CorpusError(f"the eCFR snapshot is not title {title_number}")
    dated_title_url = f"https://www.ecfr.gov/on/{quote(as_of, safe='-')}/title-{title_number}"
    results = []
    for section in root.xpath(".//DIV8[@TYPE='SECTION']"):
        number = normalize_text(section.get("N", "")).removeprefix("§ ")
        if not number or len(number) > 160 or CONTROL_CHARACTER.search(number):
            raise CorpusError(f"an eCFR section has an invalid number: {number!r}")
        heading = _head(section) or f"§ {number}"
        body = normalize_text(" ".join(section.itertext()))
        chapter, part = _ancestor(section, "CHAPTER"), _ancestor(section, "PART")
        subpart = _ancestor(section, "SUBPART")
        if subpart is None:
            subpart = _ancestor(section, "SUBJGRP")
        context = [title_heading, _head(chapter), _head(part), _head(subpart), heading]
        text = "\n\n".join(item for item in context if item)
        if body and body != heading:
            text = f"{text}\n\n{body}"
        # A reserved range of sections (" - " in its number) has no page of its own; link its part instead.
        if " - " in number and part is not None:
            url = f"{dated_title_url}/part-{quote(part.get('N', ''), safe='.-')}"
        else:
            url = f"{dated_title_url}/section-{quote(number, safe='.-()')}"
        readable = re.sub(r"[^a-z0-9.-]+", "-", number.lower()).strip("-")[:80]
        stable = hashlib.sha256(number.encode()).hexdigest()[:12]
        results.append(
            Document(
                document_id=f"ecfr-title{title_number}:{readable}:{stable}",
                source_id=source_id,
                title=normalize_text(heading),
                text=text,
                url=url,
                published_at=None,
                metadata={
                    "citation": f"{title_number} CFR § {number}",
                    "title_number": title_number,
                    "chapter": chapter.get("N") if chapter is not None else None,
                    "chapter_heading": _head(chapter),
                    "part": part.get("N") if part is not None else None,
                    "part_heading": _head(part),
                    "subpart_heading": _head(subpart),
                    "section": number,
                    "as_of": as_of,
                    "snapshot_url": snapshot_url,
                },
            )
        )
    if not results:
        raise CorpusError("the eCFR snapshot contains no sections")
    return results


def _head(element: etree._Element | None) -> str:
    head = element.find("HEAD") if element is not None else None
    return normalize_text(" ".join(head.itertext())) if head is not None else ""


def _ancestor(section: etree._Element, element_type: str) -> etree._Element | None:
    parent = section.getparent()
    while parent is not None and parent.get("TYPE") != element_type:
        parent = parent.getparent()
    return parent

# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""A final rule's Federal Register XML (federalregister.gov full text) -> its summary and its amended form text.

Manifest (`format: federal-register-xml`):
    {"source_id", "document": {"url", "sha256", "html_url", "document_number", "citation", "published_on",
     "title", "release"}}

The CFR holds a rule's regulations but not the text of the SEC forms it amends: those instructions, such as Form
8-K's filing deadline for Item 1.05, appear only in the rule's appendices. So the builder keeps one document for
the rule's summary and dates, and one per appendix of amended form text. The preamble's discussion and economic
analysis are left out.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date
from pathlib import Path

from lxml import etree

from demo_data.corpus.common import CorpusError
from demo_data.corpus.common import Document
from demo_data.corpus.common import Downloads
from demo_data.corpus.common import normalize_text

USER_AGENT = "demo-data (nemoclaw-community market-analysis)"
MAX_BYTES = 16 * 1024 * 1024
OMITTED = "* * *"  # how the Federal Register marks unchanged text it leaves out (STARS)
SKIPPED = {"FTREF", "FTNT", "PRTPAGE"}  # footnote references, footnotes and page breaks


def documents(source_id: str, manifest_path: Path, downloads: Downloads) -> list[Document]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pin = manifest["document"]
    xml = downloads.fetch(pin["url"], pin["sha256"], user_agent=USER_AGENT, max_bytes=MAX_BYTES)
    return parse_rule(xml, source_id=source_id, pin=pin)


def parse_rule(path: Path, *, source_id: str, pin: dict[str, str]) -> list[Document]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    try:
        root = etree.parse(str(path), parser=parser).getroot()
    except etree.XMLSyntaxError as error:
        raise CorpusError(f"{path.name} is not valid XML: {error}") from error
    preamble, supplement = root.find("PREAMB"), root.find("SUPLINF")
    if root.tag != "RULE" or preamble is None or supplement is None:
        raise CorpusError(f"{path.name} is not a Federal Register rule")
    if normalize_text(preamble.findtext("SUBJECT") or "") != pin["title"]:
        raise CorpusError(f"{path.name} is not the rule the manifest names: {pin['title']!r}")

    parts = [("summary", "Summary and dates", [preamble.find("SUM"), preamble.find("EFFDATE")])]
    appendix: list[etree._Element] | None = None
    for element in supplement:
        heading = _text(element) if element.tag == "HD" else ""
        if heading.startswith("Appendix "):
            appendix = []
            parts.append((_slug(heading), heading, appendix))
        elif appendix is not None:
            appendix.append(element)
    if len(parts) == 1:
        raise CorpusError(f"{path.name} has no appendix of amended form text")

    published = date.fromisoformat(pin["published_on"])
    cited = f"{pin['citation']} ({published:%B} {published.day}, {published.year})"
    results = []
    for slug, heading, elements in parts:
        paragraphs = [paragraph for element in elements if element is not None for paragraph in _paragraphs(element)]
        if not paragraphs:
            raise CorpusError(f"{path.name}: {heading} has no text")
        intro = f"{pin['title']}. Final rule, {pin['release']}, {cited}. {heading}."
        results.append(
            Document(
                document_id=f"fr-{pin['document_number']}:{slug}",
                source_id=source_id,
                title=f"{pin['title']}: {heading}",
                text="\n\n".join([intro, *paragraphs]),
                url=pin["html_url"],
                published_at=f"{pin['published_on']}T00:00:00Z",
                metadata={
                    "citation": f"{cited}, {heading}",
                    "document_number": pin["document_number"],
                    "release": pin["release"],
                    "part": heading,
                    "published_on": pin["published_on"],
                    "snapshot_url": pin["url"],
                },
            )
        )
    return results


def _paragraphs(element: etree._Element) -> list[str]:
    """One paragraph per heading, paragraph or omission mark, in order."""
    if element.tag in SKIPPED:
        return []
    if element.tag == "STARS":
        return [OMITTED]
    if element.tag in {"HD", "P", "FP", "AMDPAR"} or not len(element):
        text = _text(element)
        return [text] if text else []
    return [paragraph for child in element for paragraph in _paragraphs(child)]


def _text(element: etree._Element) -> str:
    """The element's text without footnote markers or page breaks."""
    copy = deepcopy(element)
    # A footnote reference is its number in superscript, <SU>1</SU>, then an <FTREF/>.
    for node in copy.xpath(".//SU[following-sibling::*[1][self::FTREF]]"):
        _drop(node)
    for node in copy.xpath(" | ".join(f".//{tag}" for tag in sorted(SKIPPED))):
        _drop(node)
    return normalize_text("".join(copy.itertext()))


def _drop(node: etree._Element) -> None:
    """Remove `node`, keeping the text that follows it."""
    parent, previous = node.getparent(), node.getprevious()
    if node.tail:
        if previous is not None:
            previous.tail = (previous.tail or "") + node.tail
        else:
            parent.text = (parent.text or "") + node.tail
    parent.remove(node)


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")[:80]

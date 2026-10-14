# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Publish the agent's draft: resolve ``[evidence:<id>]`` tokens into numbered citations.

The agent cites each claim with the ``evidence_id`` of a tool result, which is the receipt id the
plugin stored. Publication checks every token against this job's completed receipts, numbers the
ones that resolve in order of first use, removes the rest, and appends one Sources list. It does
not judge whether the evidence supports the claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from demo_api.registry import ToolRegistry

from .markdown import normalize_web_markdown

# A token or reference takes the spaces before it along when it is removed, so "claim [x]." reads "claim.".
# Some models write the token in lenticular brackets, 【evidence:<id>】, as they learned to cite, bracket a
# receipt id without the "evidence:" prefix, or name the receipt id in code or bold in a prose source note
# ("evidence `hermes-receipt:<id>`"); all count too.
_EVIDENCE_TOKEN = re.compile(
    r"(?P<lead>[ \t]*)(?:(?:\[\[?|【)\s*"
    r"(?:(?:evidence|source)\s*:\s*(?P<id>[A-Za-z0-9][A-Za-z0-9._:/-]{0,255})|(?P<receipt>hermes-receipt:[0-9a-f]{64}))"
    r"\s*(?:\]?\]|】)|(?:`|\*\*)(?P<code>hermes-receipt:[0-9a-f]{64})(?:`|\*\*))",
    re.IGNORECASE,
)
# A receipt id the model wrote any other way: with a rank or note inside the brackets ("[evidence:<id>, rank 4]"),
# in parentheses, as its bare digest, or loose in the prose ("evidence ID `<id>`"). Each becomes a numbered
# marker too, so no raw id reaches the reader (or the next turn, which sees this answer).
_RECEIPT = r"(?:(?:evidence|source)\s*:\s*)?(?:hermes-receipt:[0-9a-f]{16,64}|[0-9a-f]{64})(?![0-9a-z])"
_CITATION_GROUP = re.compile(r"(?P<lead>[ \t]*)(?:\[\[?|【|\()(?P<body>[^\[\]【】()\n]{1,600}?)(?:\]?\]|】|\))")
_GROUP_REFERENCE = re.compile(rf"(?:`|\*\*)?(?P<ref>{_RECEIPT})(?:`|\*\*)?", re.IGNORECASE)
# What may sit beside the ids in a citation group: separators, the words a citation uses, and hit ranks or pages.
_GROUP_FILLER = re.compile(
    r"^(?:[\s,;:&/*`'\"-]|\u2013|\u2014|\b(?:and|see|evidence|ids?|receipts?|sources?|ranks?|hits?|passages?"
    r"|chunks?|pp?\.?|page|nos?\.?)\b|\d)*$",
    re.IGNORECASE,
)
_LOOSE_RECEIPT = re.compile(
    r"(?P<lead>[ \t]*)(?:\b(?:evidence|receipt)(?:[^\S\n]+id)?[^\S\n]*:?[^\S\n]*)?(?:`|\*\*)?"
    r"(?P<ref>(?:(?:evidence|source)\s*:\s*)?hermes-receipt:[0-9a-f]{16,64})(?![0-9a-z])(?:`|\*\*)?",
    re.IGNORECASE,
)
_BARE_DIGEST = re.compile(r"(?P<lead>[ \t]*)(?:`|\*\*)?(?P<ref>\b[0-9a-f]{64}\b)(?:`|\*\*)?", re.IGNORECASE)
# 64 hex digits with no receipt or evidence prefix: a citation only when they are a receipt digest of this run
_BARE_HASH = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
# Resolved citations are marked with control characters (the draft has none left) until the end, so code or bold
# around them can be unwrapped and each becomes a [n] the UI renders as a citation: not after a letter or digit.
_MARK = "\x00{}\x01"
_MARKS = re.compile(r"\x00(\d+)\x01")
_WRAPPED_MARKS = re.compile(r"(`|\*\*)(?P<marks>(?:\x00\d+\x01[ \t,;]*)+)\1")
_REPEATED_MARK = re.compile(r"(\x00(\d+)\x01)(?:[ \t]*\x00\2\x01)+")
_NUMBERED_REFERENCE = re.compile(r"[ \t]*\[[1-9][0-9]{0,3}\]")
_RECEIPT_DIGEST = re.compile(r"(?i)(?:hermes-receipt:)?(?P<digest>[0-9a-f]{16,64})")
_UNIQUE_PREFIX = 32  # hex digits of a cut-short receipt digest that still name one receipt
_RECEIPT_SHA256 = re.compile(r"hermes-receipt:(?P<digest>[0-9a-fA-F]{64})")
_SOURCE_SECTION = re.compile(
    r"(?ims)^(?:#{1,6}\s+(?:sources|references)\s*:?\s*|\*{1,2}(?:sources|references)\s*:?\s*\*{1,2})$"
    r".*?(?=^#{1,6}\s+\S|^\*{1,2}[^*\n]+\*{1,2}\s*$|\Z)"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_CREDENTIAL = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|authorization|password|passwd|secret|token|cookie)"
    r"(\s*[:=]\s*)(?:['\"])?[^\s,;'\"]+(?:['\"])?"
)
_EVIDENCE_LIMITATION = (
    "## Evidence limitation\n\n"
    "One or more citations did not match evidence recorded for this run and were removed. "
    "Treat the claims they supported as unsupported."
)
_LABELS = {
    "retrieval_evidence": "Unstructured Retrieval evidence",
    "analytics_result": "Market analytics result",
    "structured_query": "Structured retrieval result",
    "structured_prediction": "NVIDIA Kumo prediction result",
}


@dataclass(frozen=True, slots=True)
class Citation:
    """A receipt the answer may cite; ``evidence_id`` is the receipt id."""

    evidence_id: str
    label: str
    capability_id: str
    invocation_id: str

    def wire(self, number: int) -> dict[str, Any]:
        return {
            "number": number,
            "evidenceId": self.evidence_id,
            "label": self.label,
            "capabilityId": self.capability_id,
            "invocationId": self.invocation_id,
        }


@dataclass(frozen=True, slots=True)
class PublishedReport:
    markdown: str
    citations: list[dict[str, Any]]  # Citation.wire(), numbered in order of first use
    invalid_evidence_ids: list[str]
    uncited_evidence_ids: list[str]  # evidence the answer could have cited but did not

    @property
    def status(self) -> str:
        """``reference_ids_resolved``, ``partial`` (some citations did not resolve), ``evidence_uncited`` or
        ``no_evidence``, as the original demo reported publication."""
        if self.invalid_evidence_ids:
            return "partial"
        if self.citations:
            return "reference_ids_resolved"
        return "evidence_uncited" if self.uncited_evidence_ids else "no_evidence"

    def resolution(self) -> dict[str, Any]:
        """The citation resolution, as the ``report.reference_resolution`` event reports it."""
        return {
            "status": self.status,
            "total_citations": len(self.citations),
            "uncited_evidence_count": len(self.uncited_evidence_ids),
            "invalid_evidence_count": len(self.invalid_evidence_ids),
        }


def citations_from_receipts(receipts: list[dict[str, Any]], registry: ToolRegistry) -> list[Citation]:
    """Every completed receipt of the job can be cited."""
    citations = []
    for receipt in receipts:
        if receipt.get("status") != "completed" or not isinstance(receipt.get("content"), dict):
            continue
        kind = receipt["artifactKind"]
        tool = registry.by_hermes_name(receipt["toolName"])
        citations.append(
            Citation(
                evidence_id=receipt["receiptId"],
                label=_label(kind, receipt["content"]),
                capability_id=tool.family if tool is not None else kind,
                invocation_id=receipt["invocationId"],
            )
        )
    return citations


def publish_report(draft: str, evidence: list[Citation]) -> PublishedReport:
    markdown = _BEARER.sub("Bearer [redacted]", _CONTROL.sub(" ", draft))
    markdown = _CREDENTIAL.sub(lambda match: f"{match[1]}{match[2]}[redacted]", markdown)
    markdown = normalize_web_markdown(markdown)
    # The application owns the one Sources list, and numbers written by the model are not evidence.
    markdown = _SOURCE_SECTION.sub("", markdown).strip()
    markdown = _NUMBERED_REFERENCE.sub("", markdown)

    by_id = {item.evidence_id.casefold(): item for item in evidence}
    by_digest = {
        m["digest"].casefold(): item for item in evidence if (m := _RECEIPT_SHA256.fullmatch(item.evidence_id))
    }
    cited: list[Citation] = []
    invalid: list[str] = []

    def mark(reference: str) -> str:
        """The marker of the evidence ``reference`` names, or "" (and an invalid id) when this job has none."""
        evidence_id = re.sub(r"(?i)^(?:evidence|source)\s*:\s*", "", reference.strip())
        item = by_id.get(evidence_id.casefold())
        if item is None and (receipt := _RECEIPT_DIGEST.fullmatch(evidence_id)):
            # A bare digest, the id in another case, or a digest the model cut short but still unique to one receipt
            digest = receipt["digest"].casefold()
            matches = [found for key, found in by_digest.items() if key.startswith(digest)]
            item = matches[0] if len(matches) == 1 and len(digest) >= _UNIQUE_PREFIX else by_digest.get(digest)
        if item is None:
            if evidence_id not in invalid:
                invalid.append(evidence_id)
            return ""
        if item not in cited:
            cited.append(item)
        return _MARK.format(cited.index(item))

    def group(match: re.Match[str]) -> str:
        references = [found["ref"] for found in _GROUP_REFERENCE.finditer(match["body"])]
        if not references or not _GROUP_FILLER.match(_GROUP_REFERENCE.sub(" ", match["body"])):
            return match[0]
        if any(_BARE_HASH.fullmatch(ref) and ref.casefold() not in by_digest for ref in references):
            return match[0]  # a hash the answer quotes, which digest() leaves as written; it marks any receipt
        marks = "".join(mark(reference) for reference in references)
        return f"{match['lead']}{marks}" if marks else ""

    def token(match: re.Match[str]) -> str:
        marked = mark(match["id"] or match["receipt"] or match["code"])
        return f"{match['lead']}{marked}" if marked else ""

    def loose(match: re.Match[str]) -> str:
        marked = mark(match["ref"])
        return f"{match['lead']}{marked}" if marked else ""

    def digest(match: re.Match[str]) -> str:
        if match["ref"].casefold() not in by_digest:
            return match[0]  # a hash the answer quotes, not a citation
        return f"{match['lead']}{mark(match['ref'])}"

    markdown = _CITATION_GROUP.sub(group, markdown)
    markdown = _LOOSE_RECEIPT.sub(loose, markdown)
    markdown = _EVIDENCE_TOKEN.sub(token, markdown)
    markdown = _BARE_DIGEST.sub(digest, markdown)
    markdown = _WRAPPED_MARKS.sub(lambda match: match["marks"].rstrip(" \t,;"), markdown)
    markdown = _REPEATED_MARK.sub(r"\1", markdown)
    markdown = re.sub(r"(?<=\w)\x00", " \x00", markdown)
    markdown = _MARKS.sub(lambda match: f"[{int(match[1]) + 1}]", markdown)
    markdown = re.sub(r"[ \t]+(?=\n)", "", markdown).strip()
    # The limitation goes before the Sources list: the UI and the next turn's history drop everything from
    # the Sources heading on.
    if invalid:
        markdown = f"{markdown}\n\n{_EVIDENCE_LIMITATION}"
    if cited:
        lines = [f"- [{number}] {item.label} — evidence `{item.evidence_id}`" for number, item in enumerate(cited, 1)]
        markdown = f"{markdown}\n\n## Sources\n\n" + "\n".join(lines)
    return PublishedReport(
        markdown=markdown,
        citations=[item.wire(number) for number, item in enumerate(cited, 1)],
        invalid_evidence_ids=invalid,
        uncited_evidence_ids=[item.evidence_id for item in evidence if item not in cited],
    )


def _label(kind: str, content: dict[str, Any]) -> str:
    """A short Sources-list label from the receipt content (camelCase, as stored)."""
    label = _LABELS.get(kind, "Tool evidence")
    detail: str | None = None
    if kind == "analytics_result":
        detail = str(content.get("operationId", "")).replace("_", " ")
    elif kind == "structured_query":
        detail = content.get("databaseName")
    elif kind == "structured_prediction":
        detail = content.get("templateId")
    elif kind == "retrieval_evidence":
        titles = {hit.get("documentId"): hit.get("title") for hit in content.get("hits", []) if isinstance(hit, dict)}
        detail = f"{len(titles)} documents" if len(titles) > 1 else next(iter(titles.values()), None)
    detail = " ".join(str(detail or "").split())[:160]
    return f"{label} — {detail}" if detail else label

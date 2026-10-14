# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Mechanical Markdown clean-up for the web UI; it never rewrites what the answer says."""

from __future__ import annotations

import re

_OUTER_MARKDOWN_OPEN = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})[ \t]*(?:markdown|md)[ \t]*$", re.IGNORECASE)
_FENCE_OPEN = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
_TABLE_DELIMITER = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$")


def normalize_web_markdown(value: str) -> str:
    """Normalize line endings, unwrap a whole-document ```markdown fence, trim trailing spaces and
    extra blank lines outside code fences, and put blank lines around tables so they render.
    Fenced code stays exactly as written.
    """
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _outer_fence_body(text) or text
    output: list[str] = []
    fence: tuple[str, int] | None = None
    for raw_line in text.split("\n"):
        if fence is not None:
            output.append(raw_line)
            if _closes(raw_line, fence):
                fence = None
            continue
        line = raw_line.rstrip()
        if opening := _FENCE_OPEN.match(line):
            fence = (opening["marker"][0], len(opening["marker"]))
        elif not line:
            if output and output[-1]:
                output.append("")
            continue
        output.append(line)
    while output and not output[-1]:
        output.pop()
    return "\n".join(_space_tables(output))


def _closes(line: str, fence: tuple[str, int]) -> bool:
    character, length = fence
    return re.fullmatch(rf" {{0,3}}{re.escape(character)}{{{length},}}[ \t]*", line) is not None


def _outer_fence_body(text: str) -> str | None:
    lines = text.strip("\n").split("\n")
    if len(lines) < 3 or not (opening := _OUTER_MARKDOWN_OPEN.fullmatch(lines[0])):
        return None
    fence = (opening["marker"][0], len(opening["marker"]))
    if not _closes(lines[-1], fence) or any(_closes(line, fence) for line in lines[1:-1]):
        return None
    return "\n".join(lines[1:-1])


def _space_tables(lines: list[str]) -> list[str]:
    output: list[str] = []
    fence: tuple[str, int] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if fence is not None or (opening := _FENCE_OPEN.match(line)):
            if fence is None:
                fence = (opening["marker"][0], len(opening["marker"]))
            elif _closes(line, fence):
                fence = None
            output.append(line)
            index += 1
            continue
        starts_table = "|" in line and index + 1 < len(lines) and _TABLE_DELIMITER.fullmatch(lines[index + 1])
        if not starts_table:
            output.append(line)
            index += 1
            continue
        if output and output[-1]:
            output.append("")
        output.extend(lines[index : index + 2])
        index += 2
        while index < len(lines) and lines[index] and "|" in lines[index]:
            output.append(lines[index])
            index += 1
        if index < len(lines) and lines[index]:
            output.append("")
    return output

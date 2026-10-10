# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Path templates, OpenShell path globs, and host and method matching.

Three notations appear in this package:

* **Template**: OpenAPI style, for example ``/repos/{owner}/{repo}/contents/{path}``.
  Agent Spec placeholders (``{{path}}``) are normalized to ``{path}`` first.
  A parameter matches exactly one segment unless it is listed as multi-segment.
* **Glob**: an OpenShell L7 rule path. ``*`` matches within one segment and
  ``**`` written as a whole segment matches one or more segments. This follows
  the OpenShell network-rules documentation: ``/repos/o/r/**`` covers every path
  under the repository but not ``/repos/o/r`` itself.
* **Concrete path**: a real request path such as ``/repos/NVIDIA/OpenShell``.

All three are parsed into the same token sequence so that overlap ("could one
request match both?") and coverage ("does every request matched by B also match
A?") can be computed with one recursive matcher.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from functools import lru_cache

LIT = "lit"
"""A literal segment."""
ONE = "one"
"""Any single segment: an unbound template parameter or a ``*`` glob segment."""
PART = "part"
"""A single segment constrained by an fnmatch pattern, such as ``bot*`` or ``v{n}``."""
MANY = "many"
"""One or more segments: a multi-segment parameter or a ``**`` glob segment."""

_AGENTSPEC_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_PARAM = re.compile(r"^\{([^{}/]+)\}$")
_INNER_PARAM = re.compile(r"\{[^{}/]+\}")


@dataclass(frozen=True)
class Tok:
    kind: str
    text: str = ""
    """Literal text, fnmatch pattern, or parameter name, depending on ``kind``."""


def normalize_agentspec_template(value: str) -> str:
    """Turn Agent Spec ``{{name}}`` placeholders into template ``{name}`` parameters."""
    return _AGENTSPEC_PLACEHOLDER.sub(lambda m: "{" + m.group(1).strip() + "}", value)


def has_placeholder(value: str) -> bool:
    """Return True when an Agent Spec value contains a ``{{name}}`` placeholder."""
    return bool(_AGENTSPEC_PLACEHOLDER.search(value))


def split_segments(path: str) -> list[str]:
    stripped = path.split("?", 1)[0].strip("/")
    return stripped.split("/") if stripped else []


def parse_template(path: str, multi_segment: frozenset[str] | set[str] = frozenset()) -> tuple[Tok, ...]:
    tokens = []
    for segment in split_segments(path):
        whole = _PARAM.match(segment)
        if whole:
            name = whole.group(1)
            tokens.append(Tok(MANY if name in multi_segment else ONE, name))
        elif "{" in segment:
            tokens.append(Tok(PART, _INNER_PARAM.sub("*", segment)))
        else:
            tokens.append(Tok(LIT, segment))
    return tuple(tokens)


def parse_glob(glob: str) -> tuple[Tok, ...]:
    tokens = []
    for segment in split_segments(glob):
        if segment == "**":
            tokens.append(Tok(MANY))
        elif segment == "*":
            tokens.append(Tok(ONE))
        elif "*" in segment or "?" in segment:
            tokens.append(Tok(PART, segment))
        else:
            tokens.append(Tok(LIT, segment))
    return tuple(tokens)


def parse_concrete(path: str) -> tuple[Tok, ...]:
    return tuple(Tok(LIT, segment) for segment in split_segments(path))


def _single_overlap(a: Tok, b: Tok) -> bool:
    if a.kind == ONE or b.kind == ONE:
        return True
    if a.kind == LIT and b.kind == LIT:
        return a.text == b.text
    if a.kind == LIT and b.kind == PART:
        return fnmatch.fnmatchcase(a.text, b.text)
    if a.kind == PART and b.kind == LIT:
        return fnmatch.fnmatchcase(b.text, a.text)
    return True  # two partial patterns: conservatively assume they can overlap


def _single_covers(a: Tok, b: Tok) -> bool:
    if a.kind == ONE:
        return True
    if b.kind == ONE:
        return False
    if a.kind == LIT:
        return b.kind == LIT and a.text == b.text
    # a is PART
    if b.kind == LIT:
        return fnmatch.fnmatchcase(b.text, a.text)
    return a.text == b.text


def overlaps(p: tuple[Tok, ...], q: tuple[Tok, ...]) -> bool:
    """Return True when at least one concrete path matches both sequences."""

    @lru_cache(maxsize=None)
    def go(i: int, j: int) -> bool:
        if i == len(p) and j == len(q):
            return True
        if i == len(p) or j == len(q):
            return False
        a, b = p[i], q[j]
        if a.kind == MANY and b.kind == MANY:
            return go(i + 1, j + 1) or go(i, j + 1) or go(i + 1, j)
        if a.kind == MANY:
            return go(i + 1, j + 1) or go(i, j + 1)
        if b.kind == MANY:
            return go(i + 1, j + 1) or go(i + 1, j)
        return _single_overlap(a, b) and go(i + 1, j + 1)

    return go(0, 0)


def covers(p: tuple[Tok, ...], q: tuple[Tok, ...]) -> bool:
    """Return True when every concrete path matched by ``q`` is also matched by ``p``."""

    @lru_cache(maxsize=None)
    def go(i: int, j: int) -> bool:
        if i == len(p) and j == len(q):
            return True
        if i == len(p) or j == len(q):
            return False
        a, b = p[i], q[j]
        if b.kind == MANY:
            if a.kind != MANY:
                return False
            return go(i + 1, j + 1) or go(i, j + 1)
        if a.kind == MANY:
            return go(i + 1, j + 1) or go(i, j + 1)
        return _single_covers(a, b) and go(i + 1, j + 1)

    return go(0, 0)


def glob_matches(glob: str, path: str) -> bool:
    """Return True when an OpenShell rule path glob matches a concrete request path."""
    return covers(parse_glob(glob), parse_concrete(path))


def glob_covers(broad: str, narrow: str) -> bool:
    """Return True when glob ``broad`` matches every path glob ``narrow`` matches."""
    return covers(parse_glob(broad), parse_glob(narrow))


def glob_overlaps_template(glob: str, template: str, multi_segment: frozenset[str] | set[str] = frozenset()) -> bool:
    return overlaps(parse_glob(glob), parse_template(template, multi_segment))


def glob_covers_template(glob: str, template: str, multi_segment: frozenset[str] | set[str] = frozenset()) -> bool:
    return covers(parse_glob(glob), parse_template(template, multi_segment))


def instantiate(
    template: str,
    bindings: dict[str, str] | None = None,
    multi_segment: frozenset[str] | set[str] = frozenset(),
) -> str:
    """Render a template as an OpenShell glob.

    Bound parameters become literal text. Unbound single-segment parameters
    become ``*`` and unbound multi-segment parameters become ``**``.
    """
    bindings = bindings or {}
    out = []
    for segment in split_segments(template):
        whole = _PARAM.match(segment)
        if whole:
            name = whole.group(1)
            if name in bindings:
                out.append(bindings[name].strip("/"))
            else:
                out.append("**" if name in multi_segment else "*")
        elif "{" in segment:
            out.append(
                _INNER_PARAM.sub(
                    lambda m: bindings.get(m.group(0)[1:-1], "*"),
                    segment,
                )
            )
        else:
            out.append(segment)
    return "/" + "/".join(out)


def align(declared: str, template: str, multi_segment: frozenset[str] | set[str] = frozenset()) -> str | None:
    """Narrow a graph operation template with a declared call shape.

    ``declared`` is the path an agent declares, as a template (for example an
    Agent Spec URL path with ``{{path}}`` normalized to ``{path}``). ``template``
    is the graph operation's path template. When they can describe the same
    request, the result is a glob that keeps the declared literal segments and
    uses the operation's parameter shape elsewhere. Returns ``None`` when the
    declared path cannot match the operation.
    """
    d = parse_template(declared)
    t = parse_template(template, multi_segment)

    @lru_cache(maxsize=None)
    def go(i: int, j: int) -> tuple[tuple[int, ...], ...] | None:
        """Return, for each remaining template token, the declared indexes it consumes."""
        if i == len(d) and j == len(t):
            return ()
        if i == len(d) or j == len(t):
            return None
        a, b = d[i], t[j]
        if b.kind == MANY:
            for end in range(i + 1, len(d) + 1):
                rest = go(end, j + 1)
                if rest is not None:
                    return (tuple(range(i, end)),) + rest
            return None
        if a.kind == MANY:
            return None  # a declared multi-segment value cannot fit one template segment
        if not _single_overlap(a, b):
            return None
        rest = go(i + 1, j + 1)
        if rest is None:
            return None
        return ((i,),) + rest

    plan = go(0, 0)
    if plan is None:
        return None
    out: list[str] = []
    for tok, consumed in zip(t, plan):
        declared_toks = [d[k] for k in consumed]
        if tok.kind == LIT:
            out.append(tok.text)
        elif tok.kind == MANY:
            pieces = []
            for dt in declared_toks:
                if dt.kind == LIT:
                    pieces.append(dt.text)
                else:
                    pieces.append("**")
                    break
            out.append("/".join(pieces))
        else:
            dt = declared_toks[0]
            if dt.kind == LIT:
                out.append(dt.text)
            elif dt.kind == PART:
                out.append(dt.text)
            elif tok.kind == PART:
                out.append(tok.text)
            else:
                out.append("*")
    return "/" + "/".join(out)


def template_to_glob(template: str) -> str:
    """Render a declared template as a glob when no graph operation is known.

    Every placeholder becomes ``*`` (one segment). This is the narrow choice:
    a value containing ``/`` is then denied, which the report states.
    """
    return instantiate(template)


def host_matches(pattern: str, host: str) -> bool:
    """Match an OpenShell host pattern against a concrete host name.

    ``*`` matches within one label. ``**`` is allowed only as the whole first
    label and matches one or more labels.
    """
    pattern_labels = pattern.lower().rstrip(".").split(".")
    host_labels = host.lower().rstrip(".").split(".")
    if pattern_labels and pattern_labels[0] == "**":
        suffix = pattern_labels[1:]
        if len(host_labels) <= len(suffix):
            return False
        return all(
            fnmatch.fnmatchcase(h, p) for h, p in zip(host_labels[-len(suffix):], suffix)
        )
    if len(pattern_labels) != len(host_labels):
        return False
    return all(fnmatch.fnmatchcase(h, p) for h, p in zip(host_labels, pattern_labels))


def method_covers(rule_method: str, method: str) -> bool:
    """Return True when an allow rule's method admits a request method.

    ``*`` admits every method, and OpenShell treats an allowed ``GET`` as also
    allowing ``HEAD``.
    """
    rule = rule_method.upper()
    request = method.upper()
    if rule == "*" or rule == request:
        return True
    return rule == "GET" and request == "HEAD"

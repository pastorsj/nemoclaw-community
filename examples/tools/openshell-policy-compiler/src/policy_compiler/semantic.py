# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The semantic layer: deterministic, explainable matching from text to concepts.

Free text, such as a use-case statement or a tool description, is matched
against the graph's concept vocabulary with keyword and IDF weighting instead of
embeddings, so every result is reproducible offline and lists the terms that
produced it. The layer also finds the providers a text names and the write verbs
it uses. It only proposes: front ends decide what becomes a need, and
suggestions attached to unknowns are never granted automatically.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from policy_compiler import paths
from policy_compiler.graph.store import Graph
from policy_compiler.ir.contract import Suggestion

STOPWORDS: frozenset[str] = frozenset(
    """
    a about after all also am an and any are as at be been being but by can could
    did do does for from had has have he her here him his how i if in into is it
    its just me might must my of on one only onto or our she should so some than
    that the their them then there these they this those to too us very via was we
    were what when where which while who why will with would you your
    access need needs please want wants
    com http https www
    """.split()
)
"""Words that carry no concept. Besides common English words, the list holds the
phrasing of requests ("I need access to ...") and URL noise."""

KEYWORD_WEIGHT = 2.0
"""Bag weight of a concept keyword: words a curator chose to describe the concept."""
TEXT_WEIGHT = 1.0
"""Bag weight of the words in names, descriptions, operation summaries, and tags."""

_WORD = re.compile(r"[^\W_]+")
_HOST = re.compile(r"(?<![\w.-])[a-z0-9-]+(?:\.[a-z0-9-]+)+")


def stem(token: str) -> str:
    """Light stemming: strip a plural ``s``, then an ``ing`` or ``ed`` ending.

    The ``s`` goes when the token is longer than 3 characters and does not end
    in ``ss``; then ``ing`` or ``ed`` goes when the token is longer than 5. This
    is deliberately crude ("settings" becomes "sett"); it only needs to treat
    queries and keywords the same way.
    """
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        token = token[:-1]
    if len(token) > 5:
        if token.endswith("ing"):
            token = token[:-3]
        elif token.endswith("ed"):
            token = token[:-2]
    return token


def _words(text: str) -> list[str]:
    """Lower-case alphanumeric runs, without stopword removal or stemming."""
    return _WORD.findall(text.lower())


def tokenize(text: str) -> list[str]:
    """Lower-case, split on non-alphanumerics, drop stopwords, and stem.

    Single-character tokens are dropped as well. Order and duplicates are kept.
    """
    tokens = []
    for word in _words(text):
        if len(word) < 2 or word in STOPWORDS:
            continue
        token = stem(word)
        if token not in STOPWORDS:
            tokens.append(token)
    return tokens


@dataclass
class ConceptMatch:
    concept: str
    score: float
    matched_terms: list[str] = field(default_factory=list)


class SemanticIndex:
    """A keyword and IDF index over the graph's concepts.

    Each concept has a *term bag* that maps a stemmed term to a weight:

    * its keywords, weight ``KEYWORD_WEIGHT`` (2);
    * the tokens of its name and description, and of the summaries and tags of
      the operations its capabilities enable
      (``graph.operations_enabled_by(graph.capabilities_for_concept(c))``),
      weight ``TEXT_WEIGHT`` (1).

    A term found in several sources keeps the highest weight. Provider IDs are
    not taken from weight-1 text (an operation summary that says "GitHub" does
    not make a concept about GitHub), but a curator may still list one as a
    keyword.
    """

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        provider_terms = {term for provider_id in graph.providers for term in tokenize(provider_id)}
        self._bags: dict[str, dict[str, float]] = {}
        for concept in graph.concepts.values():
            operations = graph.operations_enabled_by(graph.capabilities_for_concept(concept.id))
            texts = [concept.name, concept.description]
            texts += [text for operation in operations for text in (operation.summary, *operation.tags)]
            bag: dict[str, float] = {}
            for term in (term for text in texts for term in tokenize(text)):
                if term not in provider_terms:
                    bag[term] = TEXT_WEIGHT
            for term in (term for keyword in concept.keywords for term in tokenize(keyword)):
                bag[term] = KEYWORD_WEIGHT
            self._bags[concept.id] = bag

        document_frequency = Counter(term for bag in self._bags.values() for term in bag)
        concept_count = len(self._bags)
        self._idf = {term: math.log(1 + concept_count / df) for term, df in document_frequency.items()}
        self._max_weight: dict[str, float] = {}
        for bag in self._bags.values():
            for term, weight in bag.items():
                self._max_weight[term] = max(self._max_weight.get(term, 0.0), weight)

        self._provider_phrases = {
            provider.id: [phrase for phrase in (tuple(_words(provider.id)), tuple(_words(provider.name))) if phrase]
            for provider in graph.providers.values()
        }
        self._provider_hosts: dict[str, list[str]] = {provider_id: [] for provider_id in graph.providers}
        for service in graph.services.values():
            hosts = self._provider_hosts.setdefault(service.provider, [])
            for origin in service.origins:
                if origin.host.lower() not in hosts:
                    hosts.append(origin.host.lower())

    def terms(self, concept_id: str) -> dict[str, float]:
        """The term bag of a concept (a copy), for explanations and debugging."""
        return dict(self._bags.get(concept_id, {}))

    def match(self, text: str, *, min_score: float = 0.15, top_k: int = 8) -> list[ConceptMatch]:
        """Rank the concepts that ``text`` describes.

        Let ``Q`` be the distinct tokens of ``text`` that occur in at least one
        bag (other tokens are ignored, so unknown words never lower a score),
        ``w_c(t)`` the weight of term ``t`` in concept ``c``'s bag,
        ``W(t) = max_c w_c(t)``, ``N`` the number of concepts, ``df(t)`` the
        number of bags that contain ``t``, and ``idf(t) = ln(1 + N / df(t))``.
        For each concept, with ``M = Q ∩ bag(c)``::

            weight   = Σ_{t∈M} w_c(t)·idf(t)  /  Σ_{t∈Q} W(t)·idf(t)
            coverage = (1 + |M| / |Q|) / 2
            score    = round(weight × coverage, 4)

        ``weight`` is 1 when the concept has every query term at the highest
        weight any concept gives it, and ``coverage`` (between 0.5 and 1) favours
        concepts that explain more of the distinct terms. The score is in
        ``[0, 1]``. Results with ``score >= min_score`` are sorted by descending
        score, ties by concept ID, and cut to ``top_k``. ``matched_terms`` is
        ``M``, sorted.
        """
        query = sorted({token for token in tokenize(text) if token in self._idf})
        if not query or top_k <= 0:
            return []
        total = sum(self._max_weight[term] * self._idf[term] for term in query)
        results = []
        for concept_id, bag in self._bags.items():
            matched = [term for term in query if term in bag]
            if not matched:
                continue
            weight = sum(bag[term] * self._idf[term] for term in matched) / total
            coverage = (1 + len(matched) / len(query)) / 2
            score = round(weight * coverage, 4)
            if score >= min_score:
                results.append(ConceptMatch(concept_id, score, matched))
        results.sort(key=lambda result: (-result.score, result.concept))
        return results[:top_k]

    def providers_mentioned(self, text: str) -> list[str]:
        """Providers that ``text`` names, in graph order.

        A provider is named by its ID or name as whole words ("GitHub", "NVIDIA
        API Catalog"), or by one of its services' origin hosts ("github.com",
        "raw.githubusercontent.com"). Words inside a host name do not count, so
        "github.example.com" does not name GitHub.
        """
        lowered = text.lower()
        hosts = _HOST.findall(lowered)
        words = _words(_HOST.sub(" ", lowered))
        found = []
        for provider_id in self.graph.providers:
            named = any(_contains(words, phrase) for phrase in self._provider_phrases[provider_id])
            hosted = any(_host_matches(origin, host) for origin in self._provider_hosts[provider_id] for host in hosts)
            if named or hosted:
                found.append(provider_id)
        return found

    def concepts_for_operation(self, operation_id: str) -> list[str]:
        """Concepts realized by the capabilities an operation requires.

        Concepts that list one of the capabilities in ``realized_by`` come first,
        then concepts that reach one only through ``implies`` (for example
        ``issues.write`` through GitLab's coarse ``api`` scope); each group in
        graph order. An unknown operation, or one that requires nothing, gives
        an empty list.
        """
        operation = self.graph.operations.get(operation_id)
        if operation is None:
            return []
        found: set[str] = set()
        direct: set[str] = set()
        for capability_id in dict.fromkeys(cap for group in operation.requires for cap in group):
            for concept_id in self.graph.concepts_for_capability(capability_id):
                found.add(concept_id)
                if capability_id in self.graph.concepts[concept_id].realized_by:
                    direct.add(concept_id)
        order = list(self.graph.concepts)
        return [c for c in order if c in direct] + [c for c in order if c in found - direct]

    def suggest(self, text: str) -> list[Suggestion]:
        """``match`` results as contract suggestions, for a person to review."""
        return [Suggestion(m.concept, m.score, list(m.matched_terms)) for m in self.match(text)]


def _contains(words: list[str], phrase: tuple[str, ...]) -> bool:
    size = len(phrase)
    return any(tuple(words[i : i + size]) == phrase for i in range(len(words) - size + 1))


def _host_matches(origin: str, host: str) -> bool:
    return paths.host_matches(origin, host) if "*" in origin else origin == host


# --- Write verbs ---------------------------------------------------------------

WRITE_VERBS: frozenset[str] = frozenset(
    """
    write edit modify change update fix push commit
    create open comment reply merge delete submit file
    """.split()
)
"""Verbs that ask for write access, in their base form."""

_PLURAL_NOUNS = frozenset({"change", "comment", "commit", "edit", "file", "fix", "reply", "update"})
"""Verbs whose ``-s`` form is usually a plural noun ("read the comments")."""
_IRREGULAR = {"write": ("wrote", "written")}
_DETERMINERS = frozenset("a an the this these those each every any some its their my your our his her".split())
"""A write verb right after one of these is a noun or an adjective ("the open issues").
"that" is left out because it usually starts a clause ("an agent that opens ...")."""
_NOT_A_VERB_BEFORE = {"open": frozenset({"source", "sourced"})}
_NEEDS_OBJECT = {"file": frozenset({"issue", "bug", "ticket", "report"})}
"""Verbs that count only with one of these objects ("file a bug", not "read file contents")."""
_OBJECT_FILLERS = _DETERMINERS | {"new", "more"}
_NEGATIONS = frozenset({"not", "never", "no", "without", "nor", "cannot", "t"})
"""``t`` is what remains of ``n't`` ("don't" splits into "don" and "t")."""
_NEGATION_WINDOW = 4


def _forms(verb: str) -> set[str]:
    forms = {verb, *_IRREGULAR.get(verb, ())}
    if verb.endswith("e"):
        forms |= {verb + "d", verb[:-1] + "ing"}
    elif verb.endswith("y") and verb[-2] not in "aeiou":
        forms |= {verb[:-1] + "ied", verb + "ing"}
    else:
        forms |= {verb + "ed", verb + "ing"}
        if len(verb) > 2 and verb[-1] not in "aeiouwxy" and verb[-2] in "aeiou" and verb[-3] not in "aeiou":
            forms |= {verb + verb[-1] + "ed", verb + verb[-1] + "ing"}
    if verb not in _PLURAL_NOUNS:
        if verb.endswith(("s", "sh", "ch", "x", "z")):
            forms.add(verb + "es")
        elif verb.endswith("y") and verb[-2] not in "aeiou":
            forms.add(verb[:-1] + "ies")
        else:
            forms.add(verb + "s")
    return forms


_VERB_FORMS = {form: verb for verb in sorted(WRITE_VERBS) for form in _forms(verb)}


def find_write_verbs(text: str) -> tuple[list[str], list[str]]:
    """Find the write verbs in ``text``: ``(asked, negated)``, base forms, sorted.

    Base, past, and ``-ing`` forms count ("update", "updated", "updating"), and
    third-person forms count for verbs whose plural is rarely a noun ("opens",
    "pushes"; not "comments" or "files"). These do not count:

    * a form right after a determiner ("the open issues", "a commit");
    * ``open`` before ``source``;
    * ``file`` without an issue-like object ("read file contents").

    A verb with a negation among the four words before it ("never push", "do not
    open or comment") is reported as negated instead of asked. A negation after
    the verb ("read/write is not needed") is not detected.
    """
    words = _words(text)
    asked: set[str] = set()
    negated: set[str] = set()
    for i, word in enumerate(words):
        verb = _VERB_FORMS.get(word)
        if verb is None or (i and words[i - 1] in _DETERMINERS):
            continue
        following = words[i + 1] if i + 1 < len(words) else ""
        if following in _NOT_A_VERB_BEFORE.get(verb, ()):
            continue
        objects = _NEEDS_OBJECT.get(verb)
        if objects is not None:
            noun = next((w for w in words[i + 1 :] if w not in _OBJECT_FILLERS), "")
            if stem(noun) not in objects:
                continue
        before = words[max(0, i - _NEGATION_WINDOW) : i]
        (negated if any(w in _NEGATIONS for w in before) else asked).add(verb)
    return sorted(asked), sorted(negated)

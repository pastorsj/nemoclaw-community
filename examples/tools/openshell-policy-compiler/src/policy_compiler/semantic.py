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

    def keywords(self, concept_id: str) -> frozenset[str]:
        """The stemmed keyword terms of a concept: the words a curator chose for it."""
        return frozenset(term for term, weight in self._bags.get(concept_id, {}).items() if weight >= KEYWORD_WEIGHT)

    @property
    def keyword_vocabulary(self) -> frozenset[str]:
        """Every concept's keyword terms."""
        return frozenset(term for concept_id in self._bags for term in self.keywords(concept_id))

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


READ_VERBS: frozenset[str] = frozenset(
    """
    list show summarize summarise find read get fetch review count display search
    check see view explain understand study inspect explore analyze analyse
    investigate browse describe
    """.split()
)
"""Verbs that ask to look, not to change. A write verb right after one is an
adjective ("list open issues"), and a concept keyword that is one of these says
how something is accessed, not what."""

_OTHER_VERBS = frozenset("triage clone answer help monitor track label test run build use".split())
"""Further verbs that can start a request that a write verb then joins ("triage bugs and comment")."""
_PLURAL_NOUNS = frozenset({"change", "comment", "commit", "edit", "file", "fix", "reply", "update"})
"""Verbs whose ``-s`` form is usually a plural noun ("read the comments")."""
_DETERMINERS = frozenset("a an the this these those each every any some its their my your our his her".split())
"""A write verb right after one of these is a noun or an adjective ("the open issues")."""
_NOT_A_VERB_BEFORE = {
    "open": frozenset({"source", "sourced"}),
    # "commit history", "commit message": the noun, not the verb.
    "commit": frozenset({"history", "histories", "log", "logs", "message", "messages", "hash", "sha", "id", "ids"}),
    "change": frozenset({"history", "log", "logs", "list", "summary"}),
    "update": frozenset({"history", "log", "logs"}),
}
_NEEDS_OBJECT = {"file": frozenset({"issue", "bug", "ticket", "report"})}
"""Verbs that count only with one of these objects ("file a bug", not "read file contents")."""
_OBJECT_FILLERS = _DETERMINERS | {"new", "more"}

_MODALS = frozenset("to can could should must may might will would shall please do does don doesn".split())
"""A base form after one of these is a request ("to open", "please merge", "can push")."""
_SUBJECTS = frozenset("i you we".split())
"""A base form after one of these states what the agent does ("you open issues")."""
_RELATIVES = frozenset("that which who it".split())
"""A third-person form after one of these describes the agent ("an agent that opens ...")."""
_JOINERS = frozenset("and or then nor".split())
"""A base or third-person form after one of these joins an earlier requested verb."""
_ADVERBS = frozenset("also always just only first then automatically directly".split())
"""Skipped when looking for the word before a verb ("you can also open")."""
_CLAUSE_BREAKS = frozenset("but however although though while whereas because unless except".split())
_NEGATIONS = frozenset({"not", "never", "no", "without", "nor", "cannot", "t"})
"""Negations before a verb. ``t`` is what remains of ``n't`` ("don't" splits into "don" and "t")."""
_NEGATIONS_AFTER = frozenset({"not", "never", "cannot", "t", "forbidden", "prohibited", "disallowed"})
"""Negations later in the verb's clause ("pushing to main is not allowed"). "no" and
"without" are left out: after a verb they usually qualify the object."""
_NEGATION_WINDOW = 4
_SKIPPED = _ADVERBS | _NEGATIONS
_SENTENCE = re.compile(r"[.!?;]+(?=\s|$)|\n\s*\n")
"""Sentence ends and blank lines. A single newline is line wrapping, not a sentence break."""
_CLAUSE_TOKEN = re.compile(r"[^\W_]+|[,:()]")


def _verb_forms(verb: str) -> tuple[str, str | None]:
    """The base form and, unless the plural is usually a noun, the third-person form."""
    if verb in _PLURAL_NOUNS:
        return verb, None
    if verb.endswith(("s", "sh", "ch", "x", "z")):
        return verb, verb + "es"
    if verb.endswith("y") and verb[-2] not in "aeiou":
        return verb, verb[:-1] + "ies"
    return verb, verb + "s"


_BASE_FORMS = {verb: verb for verb in WRITE_VERBS}
_THIRD_PERSON_FORMS = {third: verb for verb in WRITE_VERBS if (third := _verb_forms(verb)[1]) is not None}


@dataclass(frozen=True)
class WriteVerbUse:
    """One occurrence of a write verb used as a request, or negated."""

    verb: str
    """Base form."""
    negated: bool
    objects: tuple[str, ...]
    """Tokens (see :func:`tokenize`) after the verb in its clause, up to the next verb: what it acts on."""


def _clauses(sentence: str) -> list[tuple[str, int]]:
    """Words of a sentence, lower-cased, each with the index of its clause."""
    words: list[tuple[str, int]] = []
    clause = 0
    for token in _CLAUSE_TOKEN.findall(sentence.lower()):
        if token in ",:()" or token in _CLAUSE_BREAKS:
            clause += 1
        else:
            words.append((token, clause))
    return words


def find_write_verb_uses(text: str) -> list[WriteVerbUse]:
    """Find the write verbs that ``text`` uses as requests, one entry per occurrence.

    Only base forms ("open") and third-person forms ("opens", but not
    "comments" or "files", which are usually nouns) count; past and ``-ing``
    forms describe state ("merged pull requests", "opening issues"). A form is
    a request when the word before it (skipping adverbs and negations, within
    its clause) is:

    * nothing: the verb starts the sentence ("Open issues for bugs");
    * for a base form, ``to``, a modal (``can``, ``must``, ``please``, ...), or a
      subject (``I``, ``you``, ``we``);
    * for a third-person form, ``that``, ``which``, ``who``, or ``it``;
    * ``and``, ``or``, ``then``, or ``nor``, when an earlier verb in the same
      sentence was requested ("read the code and open issues").

    These never count: a form right after a determiner or a read verb ("the open
    issues", "list open issues"); ``open`` before ``source`` and similar noun
    uses ("commit history"); ``file`` without an issue-like object.

    A request is negated when a negation is among the four words before it, or
    anywhere after it, in the same clause ("never push", "push to main is not
    allowed"). Negations never cross a sentence or a clause break (a comma,
    "but", ...), and each occurrence is judged on its own. Negated occurrences
    are reported even outside a request position ("do not open or comment").
    """
    uses: list[WriteVerbUse] = []
    for sentence in _SENTENCE.split(text):
        words = _clauses(sentence)
        requested_earlier = False
        for i, (word, clause) in enumerate(words):
            before = [w for w, c in words[:i] if c == clause]
            after = [w for w, c in words[i + 1 :] if c == clause]
            context = next((w for w in reversed(before) if w not in _SKIPPED), None)
            at_start = all(w in _SKIPPED for w, _ in words[:i])
            third_person = word not in _BASE_FORMS
            verb = _THIRD_PERSON_FORMS.get(word) if third_person else _BASE_FORMS[word]
            if verb is None:
                if _is_read_verb(word) or word in _OTHER_VERBS:
                    requested_earlier |= _in_request_position(context, at_start, None, requested_earlier)
                continue
            if context in _DETERMINERS or (context is not None and _is_read_verb(context)):
                continue
            if after[:1] and after[0] in _NOT_A_VERB_BEFORE.get(verb, ()):
                continue
            objects_needed = _NEEDS_OBJECT.get(verb)
            if objects_needed is not None:
                noun = next((w for w in after if w not in _OBJECT_FILLERS), "")
                if stem(noun) not in objects_needed:
                    continue
            negated = any(w in _NEGATIONS for w in before[-_NEGATION_WINDOW:]) or any(
                w in _NEGATIONS_AFTER for w in after
            )
            requested = _in_request_position(context, at_start, third_person, requested_earlier)
            if not (negated or requested):
                continue
            span: list[str] = []
            for w in after:
                is_verb = w in _BASE_FORMS or w in _THIRD_PERSON_FORMS or _is_read_verb(w)
                if is_verb and not (span and span[-1] in _DETERMINERS):  # "commit the fix": a noun
                    break
                span.append(w)
            uses.append(WriteVerbUse(verb, negated, tuple(tokenize(" ".join(span)))))
            requested_earlier |= requested and not negated
    return uses


def _is_read_verb(word: str) -> bool:
    return word in READ_VERBS or stem(word) in READ_VERBS


def _in_request_position(context: str | None, at_start: bool, third_person: bool | None, joined: bool) -> bool:
    """Whether a verb is requested, given the word before it. ``third_person=None`` accepts either form."""
    if at_start or (context in _JOINERS and joined):
        return True
    if third_person is not False and context in _RELATIVES:
        return True
    return third_person is not True and (context in _MODALS or context in _SUBJECTS)


def find_write_verbs(text: str) -> tuple[list[str], list[str]]:
    """The write verbs that ``text`` requests and negates: ``(asked, negated)``, base forms, sorted.

    A summary of :func:`find_write_verb_uses`. A verb can be in both lists when
    one occurrence asks for it and another is negated ("open issues but never
    open pull requests").
    """
    uses = find_write_verb_uses(text)
    asked = sorted({use.verb for use in uses if not use.negated})
    negated = sorted({use.verb for use in uses if use.negated})
    return asked, negated

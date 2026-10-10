# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Use-case front end: turn a plain statement of intent into a contract.

"I need access to code in GitHub so that I can understand NVIDIA/OpenShell"
becomes a need for ``source-code.read`` on ``github``, scoped to
``{owner: NVIDIA, repo: OpenShell}``, plus a need for model inference.

The front end fails safe: when it is unsure whether the text asks for write
access, it grants none and says so in the contract notes, so the user can
rephrase. The steps, in order:

1. **Scope.** Repository references are taken out of the text first: web URLs on
   a provider's repository hosts (``github.com/<owner>/<repo>``,
   ``gitlab.com/<group>/<project>``) and bare ``<owner>/<repo>`` tokens. They
   name resources, not intent or providers, so "NVIDIA/OpenShell" does not name
   the NVIDIA or OpenShell providers. A URL with only an owner scopes the grant
   to that owner, with a note.
2. **Concepts.** The rest of the text is matched with
   :class:`~policy_compiler.semantic.SemanticIndex`.
3. **Inference.** Concepts that the inference need already covers (such as
   ``llm.inference``) are left to it.
4. **Evidence.** A concept is kept only when it matched one of its keywords that
   names what to access ("code", "issue"), not only a read verb ("review") or the
   requested write verb, and not only words from operation summaries.
5. **Privilege.** A concept at write level or above is kept only when an
   occurrence of a write verb requests it
   (:func:`~policy_compiler.semantic.find_write_verb_uses`): either the verb is
   one of the concept's write-verb keywords and the words that follow it in its
   clause include another of the concept's keywords, or the verb leads the
   summary of one of the concept's write-level operations and the words that
   follow it include a word of that summary ("comment on pull requests" fits
   "Comment on an issue or pull request"; "delete the repository" does not fit
   "Delete a file"). "Open issues" keeps ``issues.write``; "open the code" and
   "list open issues" keep no write concept.
6. **Unmet requests.** A write request that no kept concept covers becomes a
   ``NOT_IN_CATALOG`` unknown naming the request ("delete the repository"), and
   read-level concepts that matched only the words of such a request are
   dropped: asking to delete a repository is not asking to read it.
7. **Redundancy.** A concept is dropped only when another kept concept at the
   same or a lower privilege level scored higher, matched every term it matched,
   and covers all of its capabilities (through ``implies``) for every provider
   that realizes it.
8. **Provider.** One need per named provider that realizes the concept. The
   compiler never picks a provider the text does not name, even when only one
   realizes the concept: that becomes an ``AMBIGUOUS`` unknown, or a
   ``NOT_IN_CATALOG`` unknown when the text names services that are not in the
   catalog ("tickets in Jira"). A concept whose only named provider is a rival
   without a realization (``code-review.read`` "on GitLab") becomes a
   ``NOT_IN_CATALOG`` unknown rather than silently switching provider.
9. **Repository.** When a provider's operations for the concept are scoped to a
   repository (``{owner}/{repo}``, ``{namespace}/{project}``) and the text names
   no repository or owner, the compiler asks for one (an ``AMBIGUOUS`` unknown)
   instead of granting every repository.

A request for "full access" (or admin rights, all permissions, ...) is never
granted: it becomes an ``AMBIGUOUS`` unknown that asks for the actions to be
named. Every drop is recorded in the contract notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from policy_compiler.graph.model import RestSelector
from policy_compiler.graph.store import Graph
from policy_compiler.ir.common import Evidence, EvidenceKind, Privilege
from policy_compiler.ir.contract import Contract, Need, Suggestion, Target, Unknown, UnknownKind
from policy_compiler.semantic import (
    READ_VERBS,
    STOPWORDS,
    WRITE_VERBS,
    ConceptMatch,
    SemanticIndex,
    WriteVerbUse,
    find_write_verb_uses,
    stem,
    tokenize,
)

INFERENCE_CAPABILITIES: dict[str, str | None] = {
    "local": "openshell.inference:invoke",
    "nvidia": "nvidia.inference:invoke",
    "openai": "openai.model:request",
    "none": None,
}
"""The ``inference`` choices and the capability each one needs."""

_INFERENCE_LABELS = {
    "local": "OpenShell managed inference (inference.local)",
    "nvidia": "NVIDIA hosted models",
    "openai": "the OpenAI API",
}

SOURCE = "use-case"

# Scope conventions, recognized from the template parameters of a provider's
# REST operations so that an org layer (for example GitHub Enterprise) works too.
_OWNER_REPO = "owner-repo"
"""GitHub style: ``/{owner}/{repo}/...``; scope ``{owner, repo}``."""
_NAMESPACE_PROJECT = "namespace-project"
"""GitLab style: ``/{namespace}/{project}.git`` and ``/projects/{id}``; scope ``{id, namespace, project}``."""

_SCOPE_PARAMETERS = {
    _OWNER_REPO: frozenset({"owner", "repo"}),
    _NAMESPACE_PROJECT: frozenset({"id", "namespace", "project"}),
}
_SCOPE_EXAMPLES = {_OWNER_REPO: "<owner>/<repo>, such as NVIDIA/OpenShell", _NAMESPACE_PROJECT: "<group>/<project>"}

_FULL_ACCESS = re.compile(
    r"\b(?:(?:full|complete|unrestricted|unlimited|total|blanket|admin|administrator|administrative|root|owner"
    r"|superuser)\s+(?:access|control|rights|permissions?|privileges?)"
    r"|(?:all|every)\s+(?:the\s+)?(?:permissions?|privileges?|rights|scopes))\b",
    re.IGNORECASE,
)
"""Requests for blanket access, which the compiler never grants."""
_NAME_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+|\n+")
_COMMON_ACRONYMS = frozenset("api apis url urls http https rest json yaml sdk cli pr prs ci cd llm ai id ui ux os".split())
"""Capitalized words that are not service names."""

_SUMMARY_WORD = re.compile(r"[a-z0-9]+")
_PLACE_PREPOSITIONS = frozenset("from in into to at within under inside across for of".split())
"""Prepositions that end the object of an operation summary ("Delete a file from a repository")."""

_PARAMETER = re.compile(r"\{([^{}]+)\}")
_SCHEME = re.compile(r"^(?:https?://)?(?:www\.)?", re.IGNORECASE)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{2,}")
_LETTER = re.compile(r"[A-Za-z]")
_TOP_LEVEL_DOMAIN = re.compile(r"\.[A-Za-z]{2,}$")
_FILE_EXTENSION = re.compile(
    r"\.(?:py|md|txt|json|ya?ml|toml|cfg|ini|rs|go|c|h|cc|cpp|hpp|java|kt|ts|tsx|jsx|sh|html|css|lock|xml|csv|rst"
    r"|ipynb|sql|rb|php|swift|pdf|png|jpe?g|svg)$",
    re.IGNORECASE,
)
"""File names, not repositories ("lib/utils.py"). ``.js`` and ``.io`` are left out:
repositories such as ``vercel/next.js`` use them."""
_LEADING_PUNCTUATION = "([{<\"'`"
_TRAILING_PUNCTUATION = ")]}>\"'`.,;:!?"

_NOT_REPOSITORY_WORDS = STOPWORDS | frozenset(
    """
    and or nor either neither both yes no true false on off he she him hers they
    read write input output client server request response allow deny pass fail
    open close start stop before after src dst tcp ip udp http ui ux ci cd
    dev prod frontend backend
    doc docs lib libs test tests bin script scripts example examples pkg cmd include
    build dist config configs etc usr var tmp home opt assets static public vendor
    readme license makefile dockerfile changelog
    """.split()
)
"""Words that make ``x/y`` a phrase ("and/or", "read/write") or a path ("docs/README")
rather than a repository."""

_OWNER_ROOTS = frozenset({"orgs", "users", "groups"})
"""URL roots followed by an owner (``github.com/orgs/NVIDIA``)."""
_RESERVED_ROOTS = frozenset(
    "orgs users groups settings api marketplace apps sponsors topics explore - login logout new notifications".split()
)
"""URL roots that are site pages, not owners."""


@dataclass(frozen=True)
class _Scope:
    provider: str | None
    """The provider whose URL named the repository, or ``None`` for a bare ``x/y`` token."""
    namespace: str
    """Owner, or GitLab group path (may contain ``/`` for subgroups)."""
    name: str | None
    """Repository or project, or ``None`` when only the owner is known."""


def contract_from_use_case(
    text: str,
    graph: Graph,
    *,
    default_clients: list[str],
    inference: str = "local",
    agent_name: str = "use-case",
) -> Contract:
    """Build a contract from a use-case statement (see the module docstring).

    ``inference`` is ``local``, ``nvidia``, ``openai``, or ``none``; the first
    three add a capability need for the matching model endpoint.
    """
    if inference not in INFERENCE_CAPABILITIES:
        choices = ", ".join(INFERENCE_CAPABILITIES)
        raise ValueError(f"unknown inference choice {inference!r}; expected one of: {choices}")
    contract = Contract(
        agent=agent_name,
        sources=[SOURCE],
        default_clients=list(default_clients),
        use_case=text,
    )
    _UseCaseCompiler(text, graph, inference, contract).run()
    return contract


class _UseCaseCompiler:
    def __init__(self, text: str, graph: Graph, inference: str, contract: Contract) -> None:
        self.text = text
        self.graph = graph
        self.inference = inference
        self.inference_capability = INFERENCE_CAPABILITIES[inference]
        self.contract = contract
        self.index = SemanticIndex(graph)
        self.evidence = Evidence(EvidenceKind.USER, SOURCE, note=text)
        self.styles = _scope_styles(graph)
        self.repository_hosts = _repository_hosts(graph, self.styles)
        self.concepts_by_provider = _concepts_by_provider(graph)

    def run(self) -> None:
        scopes, rest = _extract_scopes(self.text, self.repository_hosts, self.styles, self.index.keyword_vocabulary)
        uses = find_write_verb_uses(rest)
        asked = sorted({use.verb for use in uses if not use.negated})
        # Base forms let "pushes" match the keyword "push", which stemming misses.
        matches = self.index.match(" ".join([rest, *asked]))
        mentioned = self.index.providers_mentioned(rest)

        self.foreign_names = self._foreign_names(rest)
        self._flag_full_access(rest, mentioned)
        kept = self._leave_inference_concepts(matches)
        kept = self._require_evidence(kept, set(asked))
        kept = self._apply_privilege(kept, uses)
        kept = self._flag_unmet_requests(kept, uses, set(asked))
        kept = self._drop_redundant(kept)
        for match in kept:
            self._add_concept(match, mentioned, scopes)
        self._add_inference()
        if not matches:
            self.contract.notes.append("No concept in the catalog matched the use case.")

    # Filters

    def _leave_inference_concepts(self, matches: list[ConceptMatch]) -> list[ConceptMatch]:
        if self.inference_capability is None or self.inference_capability not in self.graph.capabilities:
            return matches
        covered = set(self.graph.concepts_for_capability(self.inference_capability))
        for match in matches:
            if match.concept in covered:
                self.contract.notes.append(
                    f"Concept {match.concept} is covered by the inference need (inference: {self.inference})."
                )
        return [match for match in matches if match.concept not in covered]

    def _require_evidence(self, matches: list[ConceptMatch], asked: set[str]) -> list[ConceptMatch]:
        """Keep concepts that matched a keyword naming what to access."""
        kept, dropped = [], []
        for match in matches:
            (kept if self._subjects(match, asked) else dropped).append(match)
        if dropped:
            names = ", ".join(match.concept for match in dropped)
            self.contract.notes.append(
                f"Dropped concepts that matched no keyword naming what to access (only verbs or operation "
                f"descriptions): {names}."
            )
        return kept

    def _subjects(self, match: ConceptMatch, asked: set[str]) -> set[str]:
        """The matched keywords that name what to access: not the requested write verbs, not read verbs."""
        subjects = set(self.index.keywords(match.concept) & set(match.matched_terms)) - asked
        return {term for term in subjects if term not in READ_VERBS}

    def _apply_privilege(self, matches: list[ConceptMatch], uses: list[WriteVerbUse]) -> list[ConceptMatch]:
        kept, dropped = [], []
        requests = [use for use in uses if not use.negated]
        for match in matches:
            if not self._privilege(match).at_least(Privilege.WRITE) or any(
                self._requests(use, match.concept) for use in requests
            ):
                kept.append(match)
            else:
                dropped.append(match.concept)
        if dropped:
            self.contract.notes.append(
                f"Dropped write-level concepts that no write request in the use case asks for: {', '.join(dropped)}. "
                "Say what the agent should change, as a request, to grant them (for example 'open issues for bugs' "
                "or 'push commits to <owner>/<repo>')."
            )
        ignored = [f"{use.verb} {' '.join(use.objects)}".strip() for use in uses if use.negated]
        if ignored:
            self.contract.notes.append(f"Ignored negated write requests: {'; '.join(dict.fromkeys(ignored))}.")
        return kept

    def _requests(self, use: WriteVerbUse, concept_id: str) -> bool:
        """Whether one requested write verb asks for a concept: its verb and its object both fit.

        A write-verb keyword of the concept needs another of its keywords as the
        object. A verb that only leads a write operation's summary ("Delete a
        file from a repository") needs that summary's object ("file"), so
        "delete the repository" does not ask for the concept that deletes files.
        """
        objects = set(use.objects)
        keywords = self.index.keywords(concept_id)
        if use.verb in keywords and use.verb in WRITE_VERBS and (keywords - {use.verb}) & objects:
            return True
        return any(found & objects for found in self._summary_objects(concept_id, use.verb))

    def _summary_objects(self, concept_id: str, verb: str) -> list[set[str]]:
        """What each of the concept's write-level operations led by ``verb`` acts on, from its summary.

        The object is the words after the verb up to a preposition that names a
        place: "Delete a file from a repository" acts on a file, not on the repository.
        """
        capabilities = self.graph.capabilities_for_concept(concept_id)
        found = []
        for operation in self.graph.operations_enabled_by(capabilities):
            words = _SUMMARY_WORD.findall(operation.summary.lower())
            if not operation.privilege.at_least(Privilege.WRITE) or words[:1] != [verb]:
                continue
            phrase = []
            for word in words[1:]:
                if word in _PLACE_PREPOSITIONS:
                    break
                phrase.append(word)
            found.append(set(tokenize(" ".join(phrase))) - {verb})
        return found

    def _flag_unmet_requests(
        self, matches: list[ConceptMatch], uses: list[WriteVerbUse], asked: set[str]
    ) -> list[ConceptMatch]:
        """Report write requests that no kept concept covers, and drop read concepts that only they named.

        Only verbs that are no concept's keyword ("delete") are reported: a
        keyword verb that asks for nothing ("open the code", "fix bugs") is
        either a read phrasing or part of a kept concept's work, and the write
        concepts it did not request are already noted as dropped.
        """
        writes = [match for match in matches if self._privilege(match).at_least(Privilege.WRITE)]
        unmet: dict[str, WriteVerbUse] = {}
        for use in uses:
            if use.negated or use.verb in self.index.keyword_vocabulary:
                continue
            if any(self._requests(use, match.concept) for match in writes):
                continue
            unmet.setdefault(f"{use.verb} {' '.join(use.objects)}".strip(), use)
        if not unmet:
            return matches
        for phrase in unmet:
            self._add_plain_unknown(
                UnknownKind.NOT_IN_CATALOG,
                phrase,
                f"The use case asks to {phrase!r}, but no concept in the catalog covers that request, so no access "
                "was granted for it. The compiler grants only the catalog's named actions; if the agent needs this, "
                "add a concept for it to the catalog.",
            )
        named_only_there = set().union(*(set(use.objects) for use in unmet.values()))
        kept, dropped = [], []
        for match in matches:
            is_read = not self._privilege(match).at_least(Privilege.WRITE)
            (dropped if is_read and self._subjects(match, asked) <= named_only_there else kept).append(match)
        if dropped:
            self.contract.notes.append(
                "Dropped concepts that matched only the words of a request the catalog cannot satisfy: "
                f"{', '.join(match.concept for match in dropped)}."
            )
        return kept

    def _flag_full_access(self, text: str, mentioned: list[str]) -> None:
        """Turn a request for blanket access into an unknown: the compiler grants only named actions."""
        phrases = list(dict.fromkeys(found.group().lower() for found in _FULL_ACCESS.finditer(text)))
        if not phrases:
            return
        where = f" on {', '.join(mentioned)}" if mentioned else ""
        self._add_plain_unknown(
            UnknownKind.AMBIGUOUS,
            phrases[0],
            f"The use case asks for {' and '.join(repr(p) for p in phrases)}{where}. Full access is never granted: "
            "the compiler grants only the actions that a use case names. Name each action and the repository, for "
            "example 'read the code of <owner>/<repo> on GitHub' or 'open issues in <owner>/<repo> on GitHub'.",
        )

    def _foreign_names(self, text: str) -> list[str]:
        """Capitalized words that look like names of services outside the catalog ("Jira", "Slack").

        The first word of a sentence, stopwords, concept keywords, verbs, common
        acronyms, and catalog provider names are not counted.
        """
        vocabulary = self.index.keyword_vocabulary
        names = []
        for sentence in _SENTENCE_BREAK.split(text):
            for position, found in enumerate(_NAME_WORD.finditer(sentence)):
                word = found.group()
                lowered = word.lower()
                if position == 0 or word == lowered:
                    continue
                if lowered in STOPWORDS or lowered in _COMMON_ACRONYMS or lowered in READ_VERBS:
                    continue
                if lowered in WRITE_VERBS or lowered in vocabulary or stem(lowered) in vocabulary:
                    continue
                if not self.index.providers_mentioned(word):
                    names.append(word)
        return list(dict.fromkeys(names))

    def _drop_redundant(self, matches: list[ConceptMatch]) -> list[ConceptMatch]:
        kept, dropped = [], []
        for match in matches:
            stronger = next((other for other in matches if self._covers(other, match)), None)
            if stronger is None:
                kept.append(match)
            else:
                dropped.append(f"{match.concept} (covered by {stronger.concept})")
        if dropped:
            self.contract.notes.append(
                f"Dropped concepts that a stronger match covers for every provider: {', '.join(dropped)}."
            )
        return kept

    def _covers(self, other: ConceptMatch, match: ConceptMatch) -> bool:
        if other.concept == match.concept or other.score <= match.score:
            return False
        if self._privilege(other).rank > self._privilege(match).rank:
            return False  # never trade a lower-privilege concept for a higher one
        if not set(match.matched_terms) <= set(other.matched_terms):
            return False
        for provider in _providers_realizing(self.graph, match.concept):
            needed = self.graph.capabilities_for_concept(match.concept, provider)
            granted = self.graph.capability_closure(self.graph.capabilities_for_concept(other.concept, provider))
            if not set(needed) <= granted:
                return False
        return True

    def _privilege(self, match: ConceptMatch) -> Privilege:
        return self.graph.concepts[match.concept].privilege

    # Needs and unknowns

    def _add_concept(self, match: ConceptMatch, mentioned: list[str], scopes: list[_Scope]) -> None:
        concept = self.graph.concepts[match.concept]
        realizers = _providers_realizing(self.graph, concept.id)
        named = [provider for provider in mentioned if provider in realizers]
        if named:
            providers = named
        elif not realizers:
            self._add_unknown(
                UnknownKind.NOT_IN_CATALOG,
                match,
                f"The use case matches {concept.id} ({concept.name}), but no capability in the catalog "
                "realizes it, so no access was granted.",
            )
            return
        else:
            rivals = [provider for provider in mentioned if self._is_rival(provider, realizers)]
            if rivals:
                self._add_unknown(
                    UnknownKind.NOT_IN_CATALOG,
                    match,
                    f"The use case matches {concept.id} ({concept.name}) and names {', '.join(rivals)}, but the "
                    f"catalog has no realization of it there; it is realized by {', '.join(realizers)}. "
                    "No access was granted.",
                )
                return
            if self.foreign_names:
                self._add_unknown(
                    UnknownKind.NOT_IN_CATALOG,
                    match,
                    f"The use case matches {concept.id} ({concept.name}), which the catalog realizes on "
                    f"{', '.join(realizers)}, but the use case names no catalog provider; it mentions {', '.join(self.foreign_names)}, "
                    "which are not providers in the catalog. No access was granted; add the service to the catalog "
                    "or name a catalog provider.",
                )
                return
            example = self.graph.providers[realizers[0]].name
            which = "several providers realize" if len(realizers) > 1 else "only one provider in the catalog realizes"
            ask = f"name the provider, for example 'on {example}'"
            if not scopes and any(self._needs_repository(concept.id, provider) for provider in realizers):
                ask += ", and the repository"
            self._add_unknown(
                UnknownKind.AMBIGUOUS,
                match,
                f"The use case matches {concept.id} ({concept.name}), which {which}: {', '.join(realizers)}. It names "
                f"none of them, and the compiler never picks a provider on its own, so no access was granted; {ask}.",
            )
            return
        for provider in providers:
            bindings = self._scopes_for(provider, scopes)
            if bindings == [{}] and self._needs_repository(concept.id, provider):
                self._add_unknown(
                    UnknownKind.AMBIGUOUS,
                    match,
                    f"The use case asks for {concept.id} ({concept.name}) on {provider} but names no repository, so "
                    "the grant would cover every repository that the credentials reach; no access was granted. Name "
                    f"the repository, for example {_SCOPE_EXAMPLES[self.styles[provider]]}.",
                )
                continue
            for scope in bindings:
                self._add_need(
                    Target(concept=concept.id, provider=provider, scope=scope),
                    concept.id,
                    f"use case: matched {', '.join(match.matched_terms)}",
                    self.evidence,
                )

    def _is_rival(self, provider: str, realizers: list[str]) -> bool:
        """A named provider that shares a concept with a realizer, such as GitLab for GitHub."""
        offered = self.concepts_by_provider.get(provider, set())
        return any(offered & self.concepts_by_provider.get(realizer, set()) for realizer in realizers)

    def _needs_repository(self, concept_id: str, provider: str) -> bool:
        """Whether any of the provider's operations for the concept is scoped to a repository or an owner."""
        style = self.styles.get(provider)
        if style is None:
            return False
        names = _SCOPE_PARAMETERS[style]
        capabilities = self.graph.capabilities_for_concept(concept_id, provider)
        return any(
            isinstance(operation.selector, RestSelector) and names & set(_PARAMETER.findall(operation.selector.path))
            for operation in self.graph.operations_enabled_by(capabilities)
        )

    def _scopes_for(self, provider: str, scopes: list[_Scope]) -> list[dict[str, str]]:
        style = self.styles.get(provider)
        if style is None:
            return [{}]
        found: list[dict[str, str]] = []
        for scope in scopes:
            if scope.provider not in (None, provider):
                continue
            if style == _OWNER_REPO:
                if "/" in scope.namespace:
                    continue
                binding = {"owner": scope.namespace}
                if scope.name is not None:
                    binding["repo"] = scope.name
            elif scope.name is None:
                binding = {"namespace": scope.namespace}
            else:
                full_path = f"{scope.namespace}/{scope.name}"
                binding = {"id": full_path.replace("/", "%2F"), "namespace": scope.namespace, "project": scope.name}
            if scope.name is None:
                note = (
                    f"The use case names {scope.namespace} on {provider} without a repository, so the grant is "
                    f"not limited to one repository: it covers every repository of {scope.namespace}."
                )
                if note not in self.contract.notes:
                    self.contract.notes.append(note)
            if binding not in found:
                found.append(binding)
        return found or [{}]

    def _add_inference(self) -> None:
        capability = self.inference_capability
        if capability is None:
            return
        if capability not in self.graph.capabilities:
            self.contract.unknowns.append(
                Unknown(
                    id=self._next_id("unknown", self.contract.unknowns, "inference"),
                    kind=UnknownKind.NOT_IN_CATALOG,
                    subject=capability,
                    description=f"The inference choice {self.inference!r} needs capability {capability}, "
                    "which is not in the catalog.",
                    evidence=[Evidence(EvidenceKind.USER, SOURCE, pointer="inference", note=self.inference)],
                )
            )
            return
        self._add_need(
            Target(capability=capability),
            "inference",
            f"the agent needs a language model to act on the use case; inference: {self.inference} "
            f"uses {_INFERENCE_LABELS[self.inference]}",
            Evidence(EvidenceKind.USER, SOURCE, pointer="inference", note=self.inference),
        )

    def _add_need(self, target: Target, slug_source: str, rationale: str, evidence: Evidence) -> None:
        self.contract.needs.append(
            Need(
                id=self._next_id("need", self.contract.needs, slug_source),
                target=target,
                rationale=rationale,
                evidence=[evidence],
            )
        )

    def _add_plain_unknown(self, kind: UnknownKind, subject: str, description: str) -> None:
        self.contract.unknowns.append(
            Unknown(
                id=self._next_id("unknown", self.contract.unknowns, subject),
                kind=kind,
                subject=subject,
                description=description,
                evidence=[self.evidence],
            )
        )

    def _add_unknown(self, kind: UnknownKind, match: ConceptMatch, description: str) -> None:
        self.contract.unknowns.append(
            Unknown(
                id=self._next_id("unknown", self.contract.unknowns, match.concept),
                kind=kind,
                subject=match.concept,
                description=description,
                evidence=[self.evidence],
                suggestions=[Suggestion(match.concept, match.score, list(match.matched_terms))],
            )
        )

    @staticmethod
    def _next_id(prefix: str, existing: list, slug_source: str) -> str:
        return f"{prefix}-{len(existing) + 1:02d}-{_slug(slug_source)}"


# --- Graph-derived conventions ---------------------------------------------------


def _scope_styles(graph: Graph) -> dict[str, str]:
    """Each provider's repository scope convention, from its REST path parameters."""
    parameters: dict[str, set[str]] = {}
    for operation in graph.operations.values():
        if isinstance(operation.selector, RestSelector):
            provider = graph.services[operation.service].provider
            parameters.setdefault(provider, set()).update(_PARAMETER.findall(operation.selector.path))
    styles = {}
    for provider in graph.providers:
        names = parameters.get(provider, set())
        if {"owner", "repo"} <= names:
            styles[provider] = _OWNER_REPO
        elif {"id", "namespace", "project"} <= names:
            styles[provider] = _NAMESPACE_PROJECT
    return styles


def _repository_hosts(graph: Graph, styles: dict[str, str]) -> dict[str, str]:
    """Hosts whose paths start with the repository (``github.com/<owner>/<repo>``), mapped to their provider.

    These are the hosts of services with an operation whose path template begins
    with a parameter. API hosts such as ``api.github.com`` are not included,
    because their paths start with a fixed prefix (``/repos/...``).
    """
    hosts: dict[str, str] = {}
    for operation in graph.operations.values():
        selector = operation.selector
        if not isinstance(selector, RestSelector) or not selector.path.startswith("/{"):
            continue
        service = graph.services[operation.service]
        if service.provider not in styles:
            continue
        for origin in service.origins:
            host = origin.host.lower()
            if "*" not in host:
                hosts.setdefault(host, service.provider)
    return hosts


def _concepts_by_provider(graph: Graph) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for concept in graph.concepts.values():
        for capability_id in concept.realized_by:
            result.setdefault(graph.capabilities[capability_id].provider, set()).add(concept.id)
    return result


def _providers_realizing(graph: Graph, concept_id: str) -> list[str]:
    owners = {graph.capabilities[cap].provider for cap in graph.capabilities_for_concept(concept_id)}
    return [provider for provider in graph.providers if provider in owners]


# --- Scope extraction ------------------------------------------------------------


def _extract_scopes(
    text: str, repository_hosts: dict[str, str], styles: dict[str, str], vocabulary: frozenset[str]
) -> tuple[list[_Scope], str]:
    """Find repository references and return them with the text they are blanked out of.

    Only the ``<owner>/<repo>`` part is blanked: the host still names the
    provider, and a trailing path such as ``/issues`` still describes intent.
    ``vocabulary`` holds concept keyword terms: ``issues/PRs`` is not a repository.
    """
    scopes: list[_Scope] = []
    blanked = list(text)
    for found in re.finditer(r"\S+", text):
        token, start = found.group(), found.start()
        lead = len(token) - len(token.lstrip(_LEADING_PUNCTUATION))
        token, start = token[lead:], start + lead
        token = token.rstrip(_TRAILING_PUNCTUATION)
        for possessive in ("'s", "’s"):
            token = token.removesuffix(possessive)
        token = token.rstrip(_TRAILING_PUNCTUATION)
        if not token:
            continue
        span = _parse_url(token, repository_hosts, styles, scopes)
        if span is None and "://" not in token:
            span = _parse_bare(token, vocabulary, scopes)
        if span is not None:
            first, last = span
            blanked[start + first : start + last] = " " * (last - first)
    unique = list(dict.fromkeys(scopes))
    return unique, "".join(blanked)


def _parse_url(
    token: str, repository_hosts: dict[str, str], styles: dict[str, str], scopes: list[_Scope]
) -> tuple[int, int] | None:
    """Parse ``[https://]host/<path>`` on a repository host; return the span of the owner and repository part.

    A path under a reserved root (``settings``, ``marketplace``, ``-``, ...)
    names no repository; ``orgs/<owner>`` and ``users/<owner>`` name an owner.
    A path with only an owner gives an owner-only scope.
    """
    prefix = _SCHEME.match(token).end()
    slash = token.find("/", prefix)
    if slash <= prefix:
        return None
    host = token[prefix:slash].lower().split(":")[0]
    provider = repository_hosts.get(host)
    if provider is None:
        return None
    nothing = (0, 0)  # A repository host without a repository: nothing to scope, nothing to blank.
    path_start = slash + 1
    segments = re.split(r"[?#]", token[path_start:])[0].split("/")
    if segments[0].lower() in _OWNER_ROOTS and len(segments) > 1:
        path_start += len(segments[0]) + 1
        segments = segments[1:2]
    elif segments[0].lower() in _RESERVED_ROOTS:
        return nothing
    if styles[provider] == _OWNER_REPO:
        used = segments[:2]
    else:
        used = []
        for segment in segments:
            if segment == "-":
                break
            used.append(segment)
    while used and not used[-1]:
        used.pop()
    if not used or not all(_IDENTIFIER.fullmatch(segment) for segment in used):
        return nothing
    if len(used) == 1:
        scopes.append(_Scope(provider, used[0], None))
    else:
        name = _strip_git(used[-1])
        if not _IDENTIFIER.fullmatch(name):
            return nothing
        scopes.append(_Scope(provider, "/".join(used[:-1]), name))
    return path_start, path_start + len("/".join(used))


def _parse_bare(token: str, vocabulary: frozenset[str], scopes: list[_Scope]) -> tuple[int, int] | None:
    """Parse a bare ``<owner>/<repo>`` token; return its span."""
    if token.count("/") != 1:
        return None
    owner, name = token.split("/")
    name = _strip_git(name)
    if _TOP_LEVEL_DOMAIN.search(owner) or not all(_looks_like_name(part, vocabulary) for part in (owner, name)):
        return None
    scopes.append(_Scope(None, owner, name))
    return 0, len(token)


def _looks_like_name(part: str, vocabulary: frozenset[str]) -> bool:
    lowered = part.lower()
    return (
        _IDENTIFIER.fullmatch(part) is not None
        and _LETTER.search(part) is not None
        and lowered not in _NOT_REPOSITORY_WORDS
        and lowered not in vocabulary
        and stem(lowered) not in vocabulary
        and _FILE_EXTENSION.search(part) is None
    )


def _strip_git(name: str) -> str:
    return name[: -len(".git")] if name.endswith(".git") and len(name) > len(".git") else name


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")

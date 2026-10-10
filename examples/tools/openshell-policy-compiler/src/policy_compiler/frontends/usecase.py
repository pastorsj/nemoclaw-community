# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Use-case front end: turn a plain statement of intent into a contract.

"I need access to code in GitHub so that I can understand NVIDIA/OpenShell"
becomes a need for ``source-code.read`` on ``github``, scoped to
``{owner: NVIDIA, repo: OpenShell}``, plus a need for model inference.

The steps, in order:

1. **Scope.** Repository references are taken out of the text first: web URLs on
   a provider's repository hosts (``github.com/<owner>/<repo>``,
   ``gitlab.com/<group>/<project>``) and bare ``<owner>/<repo>`` tokens. They
   name resources, not intent or providers, so "NVIDIA/OpenShell" does not name
   the NVIDIA or OpenShell providers.
2. **Concepts.** The rest of the text is matched with
   :class:`~policy_compiler.semantic.SemanticIndex`.
3. **Inference.** Concepts that the inference need already covers (such as
   ``llm.inference``) are left to it.
4. **Privilege.** A concept at write level or above is kept only when the text
   asks for it with a write verb that the concept itself matched: "open issues"
   keeps ``issues.write`` but not ``source-code.write``, and "read the source
   files" keeps neither. Negated verbs ("never push") ask for nothing.
5. **Redundancy.** A concept is dropped when another kept concept matched every
   term it matched, scored higher, and is at least as privileged ("open issues"
   matches ``code-review.write`` only through "open" and "issue", which
   ``issues.write`` explains better).
6. **Provider.** One need per named provider that realizes the concept. With no
   provider named, a concept that a single provider realizes uses it, and one
   that several realize becomes an ``AMBIGUOUS`` unknown: the compiler asks
   instead of granting the union. A concept whose only named provider is a rival
   without a realization (``code-review.read`` "on GitLab") becomes a
   ``NOT_IN_CATALOG`` unknown rather than silently switching provider.

Every drop is recorded in the contract notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from policy_compiler.graph.model import RestSelector
from policy_compiler.graph.store import Graph
from policy_compiler.ir.common import Evidence, EvidenceKind, Privilege
from policy_compiler.ir.contract import Contract, Need, Suggestion, Target, Unknown, UnknownKind
from policy_compiler.semantic import STOPWORDS, ConceptMatch, SemanticIndex, find_write_verbs

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

_PARAMETER = re.compile(r"\{([^{}]+)\}")
_SCHEME = re.compile(r"^(?:https?://)?(?:www\.)?", re.IGNORECASE)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{2,}")
_LETTER = re.compile(r"[A-Za-z]")
_TOP_LEVEL_DOMAIN = re.compile(r"\.[A-Za-z]{2,}$")
_LEADING_PUNCTUATION = "([{<\"'`"
_TRAILING_PUNCTUATION = ")]}>\"'`.,;:!?"

_NOT_REPOSITORY_WORDS = STOPWORDS | frozenset(
    """
    and or nor either neither both yes no true false on off he she him hers they
    read write input output client server request response allow deny pass fail
    open close start stop before after src dst tcp ip udp http ui ux ci cd
    dev prod frontend backend
    """.split()
)
"""Words that make ``x/y`` a phrase ("and/or", "read/write") rather than a repository."""


@dataclass(frozen=True)
class _Scope:
    provider: str | None
    """The provider whose URL named the repository, or ``None`` for a bare ``x/y`` token."""
    namespace: str
    """Owner, or GitLab group path (may contain ``/`` for subgroups)."""
    name: str
    """Repository or project."""


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
        scopes, rest = _extract_scopes(self.text, self.repository_hosts, self.styles)
        asked, negated = find_write_verbs(rest)
        # Base forms let "created" match the keyword "create", which stemming misses.
        matches = self.index.match(" ".join([rest, *asked]))
        mentioned = self.index.providers_mentioned(rest)

        kept = self._leave_inference_concepts(matches)
        kept = self._apply_privilege(kept, asked, negated)
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

    def _apply_privilege(self, matches: list[ConceptMatch], asked: list[str], negated: list[str]) -> list[ConceptMatch]:
        kept, dropped = [], []
        for match in matches:
            if self._privilege(match).at_least(Privilege.WRITE) and not set(asked) & set(match.matched_terms):
                dropped.append(match.concept)
            else:
                kept.append(match)
        if dropped:
            self.contract.notes.append(
                f"Dropped write-level concepts that no write verb in the use case asks for: {', '.join(dropped)}. "
                "Say what the agent should change (for example 'open issues' or 'push commits') to grant them."
            )
        ignored = [verb for verb in negated if verb not in asked]
        if ignored:
            self.contract.notes.append(f"Ignored negated write verbs: {', '.join(ignored)}.")
        return kept

    def _drop_redundant(self, matches: list[ConceptMatch]) -> list[ConceptMatch]:
        kept, dropped = [], []
        for match in matches:
            terms = set(match.matched_terms)
            stronger = next(
                (
                    other
                    for other in matches
                    if other.score > match.score
                    and terms <= set(other.matched_terms)
                    and self._privilege(other).rank >= self._privilege(match).rank
                ),
                None,
            )
            if stronger is None:
                kept.append(match)
            else:
                dropped.append(f"{match.concept} (explained by {stronger.concept})")
        if dropped:
            self.contract.notes.append(
                f"Dropped concepts whose matched terms a stronger match explains: {', '.join(dropped)}."
            )
        return kept

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
            if len(realizers) > 1:
                example = self.graph.providers[realizers[0]].name
                self._add_unknown(
                    UnknownKind.AMBIGUOUS,
                    match,
                    f"The use case matches {concept.id} ({concept.name}), which several providers realize: "
                    f"{', '.join(realizers)}. It names none of them, so no access was granted; name the provider, "
                    f"for example 'on {example}'.",
                )
                return
            providers = realizers
        for provider in providers:
            for scope in self._scopes_for(provider, scopes):
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
                binding = {"owner": scope.namespace, "repo": scope.name}
            else:
                full_path = f"{scope.namespace}/{scope.name}"
                binding = {"id": full_path.replace("/", "%2F"), "namespace": scope.namespace, "project": scope.name}
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


def _extract_scopes(text: str, repository_hosts: dict[str, str], styles: dict[str, str]) -> tuple[list[_Scope], str]:
    """Find repository references and return them with the text they are blanked out of.

    Only the ``<owner>/<repo>`` part is blanked: the host still names the
    provider, and a trailing path such as ``/issues`` still describes intent.
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
            span = _parse_bare(token, scopes)
        if span is not None:
            first, last = span
            blanked[start + first : start + last] = " " * (last - first)
    unique = list(dict.fromkeys(scopes))
    return unique, "".join(blanked)


def _parse_url(
    token: str, repository_hosts: dict[str, str], styles: dict[str, str], scopes: list[_Scope]
) -> tuple[int, int] | None:
    """Parse ``[https://]host/<path>`` on a repository host; return the span of the repository part."""
    prefix = _SCHEME.match(token).end()
    slash = token.find("/", prefix)
    if slash <= prefix:
        return None
    host = token[prefix:slash].lower().split(":")[0]
    provider = repository_hosts.get(host)
    if provider is None:
        return None
    path = re.split(r"[?#]", token[slash + 1 :])[0]
    segments = path.split("/")
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
    if len(used) < 2 or not all(_IDENTIFIER.fullmatch(segment) for segment in used):
        return (0, 0)  # A repository host without a repository: nothing to scope, nothing to blank.
    name = _strip_git(used[-1])
    if not _IDENTIFIER.fullmatch(name):
        return (0, 0)
    scopes.append(_Scope(provider, "/".join(used[:-1]), name))
    return slash + 1, slash + 1 + len("/".join(used))


def _parse_bare(token: str, scopes: list[_Scope]) -> tuple[int, int] | None:
    """Parse a bare ``<owner>/<repo>`` token; return its span."""
    if token.count("/") != 1:
        return None
    owner, name = token.split("/")
    name = _strip_git(name)
    if not (_looks_like_name(owner) and _looks_like_name(name)) or _TOP_LEVEL_DOMAIN.search(owner):
        return None
    scopes.append(_Scope(None, owner, name))
    return 0, len(token)


def _looks_like_name(part: str) -> bool:
    return (
        _IDENTIFIER.fullmatch(part) is not None
        and _LETTER.search(part) is not None
        and part.lower() not in _NOT_REPOSITORY_WORDS
    )


def _strip_git(name: str) -> str:
    return name[: -len(".git")] if name.endswith(".git") and len(name) > len(".git") else name


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")

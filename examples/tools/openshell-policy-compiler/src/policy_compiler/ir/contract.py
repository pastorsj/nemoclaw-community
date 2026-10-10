# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The contract: what an agent needs, what it must never do, and what is unknown.

The contract is internal. Front ends produce it from agent definitions; the
resolver turns it into an access set using the capability graph. A need may
name its target at any level of detail, from a cross-provider concept down to a
raw endpoint the graph does not know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from policy_compiler.ir.common import Evidence, Privilege, Protocol


class Necessity(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class Phase(StrEnum):
    INSTALL = "install"
    BUILD = "build"
    STARTUP = "startup"
    AUTH = "auth"
    RUNTIME = "runtime"
    TELEMETRY = "telemetry"


COMPILED_PHASES = frozenset({Phase.STARTUP, Phase.AUTH, Phase.RUNTIME, Phase.TELEMETRY})


class Locus(StrEnum):
    """Where a call originates. Only calls from inside the sandbox compile to rules."""

    SANDBOX = "sandbox"
    CLIENT = "client"
    """The client application runs the tool (Agent Spec ClientTool)."""
    REMOTE = "remote"
    """A remote provider performs the call (for example, hosted web search)."""
    DATABASE = "database"
    """A database performs the call (for example, an in-database LLM call)."""


class Approval(StrEnum):
    NONE = "none"
    CONFIRM = "confirm"
    """The definition asks for confirmation before the call. OpenShell cannot prompt mid-request."""


class UnknownKind(StrEnum):
    OPAQUE_IMPL = "opaque_impl"
    """Code the compiler cannot see, such as an Agent Spec ServerTool."""
    OPEN_TOOLSET = "open_toolset"
    """A toolbox that exposes every tool, such as MCP without a tool filter."""
    OPEN_DESTINATION = "open_destination"
    """A destination chosen at run time, such as a templated host without an allow list."""
    UNRESOLVED_REFERENCE = "unresolved_reference"
    UNSUPPORTED_COMPONENT = "unsupported_component"
    AMBIGUOUS = "ambiguous"
    """The input fits several providers; a person must choose."""
    NOT_IN_CATALOG = "not_in_catalog"


class DefaultEffect(StrEnum):
    DENY = "deny"
    ALLOW = "allow"


@dataclass(frozen=True)
class RawEndpoint:
    """A destination declared by a source, possibly unknown to the graph."""

    host: str
    port: int
    scheme: str = "https"
    protocol: Protocol = Protocol.REST
    method: str | None = None
    """Upper-case method, ``*`` when templated, or ``None`` for endpoint-level needs."""
    path: str | None = None
    """Template path with ``{name}`` parameters, or ``None`` for endpoint-level needs."""
    mcp_tools: tuple[str, ...] = ()
    path_is_glob: bool = False
    """True only when ``path`` is an OpenShell glob the compiler built (for example from a
    ``url_allow_list`` prefix). Glob characters in a declared literal path are never trusted."""


@dataclass
class Target:
    """What a need or prohibition is about. Exactly one of the four fields is set."""

    concept: str | None = None
    capability: str | None = None
    operation: str | None = None
    raw: RawEndpoint | None = None
    provider: str | None = None
    """Restrict a concept to one provider's realizations."""
    scope: dict[str, str] = field(default_factory=dict)
    """Resource scope bindings for template parameters, such as ``{"owner": "NVIDIA"}``."""

    def __post_init__(self) -> None:
        chosen = [x for x in (self.concept, self.capability, self.operation, self.raw) if x is not None]
        if len(chosen) != 1:
            raise ValueError("a Target needs exactly one of concept, capability, operation, raw")

    @property
    def level(self) -> str:
        if self.concept is not None:
            return "concept"
        if self.capability is not None:
            return "capability"
        if self.operation is not None:
            return "operation"
        return "raw"

    def describe(self) -> str:
        if self.raw is not None:
            raw = self.raw
            shape = f"{raw.method or '*'} {raw.path or '/**'}" if raw.protocol != Protocol.L4 else "L4"
            return f"{raw.scheme}://{raw.host}:{raw.port} {shape}"
        name = self.concept or self.capability or self.operation or ""
        extra = f" on {self.provider}" if self.provider else ""
        scope = ", ".join(f"{k}={v}" for k, v in sorted(self.scope.items()))
        return f"{self.level} {name}{extra}" + (f" [{scope}]" if scope else "")


@dataclass
class Credential:
    """A hint that the call carries a secret. The compiler never sees the value."""

    placement: str
    """For example ``header:Authorization``, ``query:key``, or ``mtls``."""
    reference: str = ""
    """Environment variable, provider name, or Agent Spec field holding the value."""


@dataclass
class Need:
    id: str
    target: Target
    rationale: str
    evidence: list[Evidence] = field(default_factory=list)
    necessity: Necessity = Necessity.REQUIRED
    phase: Phase = Phase.RUNTIME
    locus: Locus = Locus.SANDBOX
    clients: list[str] = field(default_factory=list)
    """Binary paths that make the call. Empty means the contract's default clients."""
    approval: Approval = Approval.NONE
    credential: Credential | None = None
    path_prefixes: list[str] = field(default_factory=list)
    """Allowed path prefixes from the definition itself (Agent Spec ``url_allow_list``)."""
    subject: str = ""
    """The component that declared the need, such as a tool name."""


@dataclass
class Prohibition:
    id: str
    rationale: str
    target: Target | None = None
    provider: str | None = None
    min_privilege: Privilege | None = None
    """With ``provider``: forbid every operation of that provider at or above this level."""
    evidence: list[Evidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.target is None and self.provider is None:
            raise ValueError("a Prohibition needs a target or a provider")


@dataclass
class Suggestion:
    concept: str
    score: float
    matched_terms: list[str] = field(default_factory=list)


@dataclass
class Unknown:
    id: str
    kind: UnknownKind
    subject: str
    description: str
    evidence: list[Evidence] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    """Semantic suggestions for a person to review. Never granted automatically."""


@dataclass
class Contract:
    agent: str
    sources: list[str] = field(default_factory=list)
    default_clients: list[str] = field(default_factory=list)
    default_effect: DefaultEffect = DefaultEffect.DENY
    use_case: str = ""
    """Free text that describes the intended use, for semantic justification."""
    needs: list[Need] = field(default_factory=list)
    prohibitions: list[Prohibition] = field(default_factory=list)
    unknowns: list[Unknown] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    referenced_specs: list[str] = field(default_factory=list)
    """API specs the definition points to (Agent Spec ``api_spec_uri``), resolved paths or URLs."""

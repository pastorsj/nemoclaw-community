# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Capability graph nodes.

The graph is the compiler's static knowledge about APIs: which services exist,
which operations they serve, which provider-defined capabilities those
operations require, which cross-provider concepts the capabilities realize, and
which client binaries perform operations implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from policy_compiler.ir.common import Evidence, Inspection, Layer, Privilege, Protocol


@dataclass(frozen=True)
class Origin:
    """A network location that serves a service."""

    host: str
    port: int = 443
    scheme: str = "https"


@dataclass(frozen=True)
class RestSelector:
    method: str
    """Upper-case HTTP method."""
    path: str
    """Path template relative to the host, including any service base path."""


@dataclass(frozen=True)
class McpSelector:
    tool: str


@dataclass(frozen=True)
class GraphQLSelector:
    operation_type: str
    """``query``, ``mutation``, or ``subscription``."""
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class JsonRpcSelector:
    method: str


Selector = Union[RestSelector, McpSelector, GraphQLSelector, JsonRpcSelector]


@dataclass
class Provider:
    id: str
    name: str
    description: str = ""
    layer: Layer = Layer.BASE
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Service:
    id: str
    provider: str
    name: str
    origins: list[Origin]
    protocol: Protocol = Protocol.REST
    inspection: Inspection = Inspection.L7
    allowed_ips: list[str] = field(default_factory=list)
    """CIDR ranges for services on private networks (emitted as ``allowed_ips``)."""
    description: str = ""
    layer: Layer = Layer.BASE
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Operation:
    id: str
    service: str
    selector: Selector
    privilege: Privilege
    requires: list[list[str]] = field(default_factory=list)
    """Capability requirement in disjunctive normal form: any inner list, all of its members."""
    multi_segment: list[str] = field(default_factory=list)
    """Template parameters whose values may contain ``/``."""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    layer: Layer = Layer.BASE
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Capability:
    id: str
    provider: str
    name: str
    level: Privilege
    implies: list[str] = field(default_factory=list)
    description: str = ""
    layer: Layer = Layer.BASE
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Concept:
    id: str
    name: str
    privilege: Privilege
    keywords: list[str] = field(default_factory=list)
    realized_by: list[str] = field(default_factory=list)
    """Capability IDs. Org layers may append to this list through concept links."""
    description: str = ""
    layer: Layer = Layer.BASE
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class ClientUse:
    """Operations a client binary performs implicitly when it is used for a capability."""

    operations: list[str]
    when_capability: list[str] = field(default_factory=list)
    """The uses apply when any of these capabilities is granted. Empty means always."""


@dataclass
class Client:
    id: str
    paths: list[str]
    """Real executable paths (symlinks resolved), as OpenShell matches them."""
    description: str = ""
    bypasses_l7: bool = False
    """The binary speaks a protocol OpenShell cannot inspect (for example ssh)."""
    can_exfiltrate: bool = True
    uses: list[ClientUse] = field(default_factory=list)
    layer: Layer = Layer.BASE
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class ConceptLink:
    """An org or agent layer attaching its capabilities to an existing concept."""

    concept: str
    realized_by: list[str]
    layer: Layer = Layer.BASE
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class CatalogDocument:
    """The contents of one catalog file before it is merged into a graph."""

    providers: list[Provider] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    capabilities: list[Capability] = field(default_factory=list)
    operations: list[Operation] = field(default_factory=list)
    concepts: list[Concept] = field(default_factory=list)
    concept_links: list[ConceptLink] = field(default_factory=list)
    clients: list[Client] = field(default_factory=list)
    source: str = ""

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Vocabulary shared by the capability graph, the contract, and the access set."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Privilege(StrEnum):
    """How much an operation or capability can change.

    ``UNKNOWN`` ranks above ``ADMIN`` so that unclassified operations are treated
    as the most privileged when the compiler compares levels.
    """

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        return _PRIVILEGE_RANK[self]

    def at_least(self, other: Privilege) -> bool:
        return self.rank >= other.rank


_PRIVILEGE_RANK = {
    Privilege.READ: 1,
    Privilege.WRITE: 2,
    Privilege.ADMIN: 3,
    Privilege.UNKNOWN: 4,
}


class Protocol(StrEnum):
    """Wire protocol of a service, using OpenShell's protocol names."""

    REST = "rest"
    GRAPHQL = "graphql"
    MCP = "mcp"
    JSON_RPC = "json-rpc"
    WEBSOCKET = "websocket"
    SQL = "sql"
    TCP = "tcp"
    L4 = "l4"
    """Connection-level only: no L7 protocol inspection."""


class Inspection(StrEnum):
    """Whether OpenShell can terminate TLS and inspect requests."""

    L7 = "l7"
    OPAQUE = "opaque"
    """mTLS, certificate pinning, or a non-HTTP protocol: ``tls: skip``."""


class EvidenceKind(StrEnum):
    """Why the compiler believes a fact."""

    DECLARED = "declared"
    """Stated by the agent definition (for example, an Agent Spec RemoteTool)."""
    DOCUMENTED = "documented"
    """Stated by the API owner (OpenAPI security scopes, permission data)."""
    CURATED = "curated"
    """Written by hand in a catalog file."""
    INFERRED = "inferred"
    """Guessed by a heuristic, such as privilege from the HTTP method."""
    OBSERVED = "observed"
    """Seen in traffic logs. Reserved for the observation phase."""
    USER = "user"
    """Supplied by a person, for example a use-case statement or flag."""
    LIFTED = "lifted"
    """Read back from an existing OpenShell policy."""


class Layer(StrEnum):
    """Where a graph node came from. Later layers may add, never redefine."""

    BASE = "base"
    ORG = "org"
    AGENT = "agent"


@dataclass(frozen=True)
class Evidence:
    """One piece of provenance for a node, need, or rule."""

    kind: EvidenceKind
    source: str
    """Short source label, such as ``agentspec:agent.yaml`` or ``catalog:base/github.yaml``."""
    pointer: str = ""
    """Location inside the source, such as ``$.tools[0]`` or ``network_policies.github``."""
    note: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict:
        data = {"kind": self.kind.value, "source": self.source}
        if self.pointer:
            data["pointer"] = self.pointer
        if self.note:
            data["note"] = self.note
        if self.confidence != 1.0:
            data["confidence"] = self.confidence
        return data


def method_privilege(method: str) -> Privilege:
    """Guess a privilege level from an HTTP method. Callers must label it inferred."""
    upper = method.upper()
    if upper in {"GET", "HEAD", "OPTIONS"}:
        return Privilege.READ
    if upper in {"POST", "PUT", "PATCH", "DELETE"}:
        return Privilege.WRITE
    return Privilege.UNKNOWN

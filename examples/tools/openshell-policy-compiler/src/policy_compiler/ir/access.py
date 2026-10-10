# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The access set: concrete allow and deny rules, independent of output format.

Matcher strings use OpenShell's glob dialect (see ``policy_compiler.paths``) so
that an existing OpenShell policy can be lifted into an access set and lowered
back without loss. Non-network policy sections are carried in ``extras``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from policy_compiler.ir.common import Inspection, Privilege, Protocol


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Enforcement(StrEnum):
    ENFORCE = "enforce"
    AUDIT = "audit"


@dataclass(frozen=True)
class ClientSet:
    mode: str
    """``list``, ``any``, or ``none``. OpenShell needs ``list``; the others exist for lifting."""
    paths: tuple[str, ...] = ()

    @staticmethod
    def of(paths: list[str] | tuple[str, ...]) -> ClientSet:
        unique = tuple(dict.fromkeys(paths))
        return ClientSet("list", unique) if unique else ClientSet("none")


@dataclass(frozen=True)
class EndpointKey:
    host: str
    """Exact host or OpenShell host pattern. Empty only for hostless ``allowed_ips`` endpoints."""
    ports: tuple[int | str, ...]
    """Ports. Lifted templates may carry placeholder strings such as ``__VSS_PORT__``."""
    protocol: Protocol
    inspection: Inspection = Inspection.L7
    enforcement: Enforcement = Enforcement.ENFORCE
    allowed_ips: tuple[str, ...] = ()
    path: str = ""
    """OpenShell endpoint route selector. Empty in generated policies."""


@dataclass(frozen=True)
class Matcher:
    """One L7 allow or deny matcher. Unused fields stay ``None``."""

    method: str | None = None
    path: str | None = None
    query: tuple[tuple[str, str], ...] = ()
    tool: str | None = None
    """MCP tool name, used with ``method == "tools/call"``."""
    operation_type: str | None = None
    operation_name: str | None = None
    fields: tuple[str, ...] = ()
    command: str | None = None
    """SQL command."""


@dataclass
class AccessRule:
    effect: Effect
    group: str
    """Suggested policy key. Rules with the same group and client set share one policy entry."""
    clients: ClientSet
    endpoint: EndpointKey
    matcher: Matcher | None = None
    """``None`` means the whole endpoint: L4, or an ``access`` preset when ``access_preset`` is set."""
    access_preset: str | None = None
    """``read-only``, ``read-write``, or ``full`` when the authored form used a preset."""
    operations: list[str] = field(default_factory=list)
    """Graph operation IDs this rule was generated for."""
    privilege: Privilege | None = None
    reasons: list[str] = field(default_factory=list)
    """Need IDs, prohibition IDs, or lifted-source pointers that justify the rule."""
    credential_binding: str | None = None
    name: str | None = None
    """Authored ``name`` of the policy entry when lifted and different from ``group``."""
    endpoint_extras: dict[str, Any] = field(default_factory=dict)
    """Lifted endpoint fields the access set does not model, kept for round trips."""


@dataclass
class Widening:
    """A place where the compiler could not be as narrow as the input asked."""

    need: str
    reason: str
    detail: str = ""


@dataclass
class Diagnostic:
    level: str
    """``info``, ``warning``, or ``error``."""
    code: str
    message: str
    pointer: str = ""


@dataclass
class AccessSet:
    rules: list[AccessRule] = field(default_factory=list)
    widenings: list[Widening] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    """Non-network sections (``version``, ``filesystem_policy``, ``landlock``, ``process``, ...)."""
    group_order: list[str] = field(default_factory=list)
    """Policy key order as authored, for stable round trips."""

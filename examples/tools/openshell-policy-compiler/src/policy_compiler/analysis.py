# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""What an access set actually permits, measured against the capability graph.

This works for generated policies and for existing policies lifted from YAML.
It answers "which catalogued operations can this policy reach?" and computes
breadth metrics that make an over-broad policy visible.

The breadth score is a heuristic, not a security rating. Its weights are listed
in ``SCORE_WEIGHTS`` so that the number can be reproduced and challenged.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from policy_compiler import paths
from policy_compiler.graph.model import McpSelector, Operation, RestSelector
from policy_compiler.graph.store import Graph
from policy_compiler.ir.access import AccessRule, AccessSet, Effect, Enforcement
from policy_compiler.ir.common import Inspection, Privilege, Protocol

PRESET_METHODS: dict[str, frozenset[str] | None] = {
    "read-only": frozenset({"GET", "HEAD", "OPTIONS"}),
    "read-write": frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH"}),
    "full": None,
}

SCORE_WEIGHTS = {
    "operation.read": 1,
    "operation.write": 3,
    "operation.admin": 10,
    "operation.unknown": 3,
    "connection_level_endpoint": 5,
    "connection_level_uncatalogued_host": 20,
    "unenforced_endpoint": 10,
    "match_all_path": 5,
    "any_method": 3,
    "binary_glob": 5,
    "opaque_endpoint": 5,
    "wildcard_host": 5,
}


@dataclass
class PermittedOperation:
    operation: str
    privilege: Privilege
    rules: list[int] = field(default_factory=list)
    """Indexes into ``AccessSet.rules`` of the allow rules that reach it."""


@dataclass
class Breadth:
    allow_rules: int = 0
    deny_rules: int = 0
    endpoints: int = 0
    hosts: list[str] = field(default_factory=list)
    uncatalogued_hosts: list[str] = field(default_factory=list)
    permitted: dict[str, int] = field(default_factory=dict)
    """Count of catalogued operations reachable, by privilege."""
    connection_level_endpoints: int = 0
    unenforced_endpoints: int = 0
    opaque_endpoints: int = 0
    match_all_paths: int = 0
    any_method_rules: int = 0
    binary_globs: int = 0
    wildcard_hosts: int = 0
    score: int = 0


def _matching_operations(rule: AccessRule, graph: Graph) -> list[Operation]:
    ports = {p for p in rule.endpoint.ports if isinstance(p, int)}
    found: list[Operation] = []
    for service in graph.services.values():
        if not any(
            (not rule.endpoint.host or paths.host_matches(rule.endpoint.host, origin.host))
            and (not ports or origin.port in ports)
            for origin in service.origins
        ):
            continue
        found.extend(graph.operations_of(service.id))
    return found


def _rule_is_endpoint_wide(rule: AccessRule) -> bool:
    """True when the rule's L7 restrictions do not bind (no matcher, L4, opaque, or audit)."""
    endpoint = rule.endpoint
    if rule.matcher is None and rule.access_preset is None:
        return True
    if endpoint.protocol in (Protocol.L4, Protocol.TCP) or endpoint.inspection == Inspection.OPAQUE:
        return True
    return endpoint.enforcement == Enforcement.AUDIT


def _rule_permits(rule: AccessRule, op: Operation) -> bool:
    if _rule_is_endpoint_wide(rule):
        return True
    selector = op.selector
    if rule.matcher is None:
        methods = PRESET_METHODS.get(rule.access_preset or "full")
        if methods is None:
            return True
        return isinstance(selector, RestSelector) and selector.method in methods
    matcher = rule.matcher
    if isinstance(selector, RestSelector):
        if matcher.method is None or not paths.method_covers(matcher.method, selector.method):
            return False
        if matcher.path is None:
            return True
        return paths.glob_overlaps_template(matcher.path, selector.path, set(op.multi_segment))
    if isinstance(selector, McpSelector):
        if matcher.method != "tools/call":
            return False
        return matcher.tool is None or fnmatch.fnmatchcase(selector.tool, matcher.tool)
    return False


def _deny_covers(rule: AccessRule, op: Operation) -> bool:
    matcher = rule.matcher
    selector = op.selector
    if matcher is None or not isinstance(selector, RestSelector):
        return False
    if matcher.method is None or not paths.method_covers(matcher.method, selector.method):
        return False
    if matcher.path is None:
        return True
    return paths.glob_covers_template(matcher.path, selector.path, set(op.multi_segment))


def permitted_operations(access: AccessSet, graph: Graph) -> dict[str, PermittedOperation]:
    """Catalogued operations that at least one allow rule reaches and no deny rule fully blocks."""
    result: dict[str, PermittedOperation] = {}
    denies = [r for r in access.rules if r.effect == Effect.DENY and r.endpoint.enforcement == Enforcement.ENFORCE]
    for index, rule in enumerate(access.rules):
        if rule.effect != Effect.ALLOW:
            continue
        for op in _matching_operations(rule, graph):
            if not _rule_permits(rule, op):
                continue
            if any(
                d.endpoint.host == rule.endpoint.host and _deny_covers(d, op) for d in denies
            ) and not _rule_is_endpoint_wide(rule):
                continue
            entry = result.setdefault(op.id, PermittedOperation(op.id, op.privilege))
            entry.rules.append(index)
    return result


def breadth(access: AccessSet, graph: Graph, permitted: dict[str, PermittedOperation] | None = None) -> Breadth:
    permitted = permitted if permitted is not None else permitted_operations(access, graph)
    b = Breadth()
    endpoints = {}
    for rule in access.rules:
        if rule.effect == Effect.DENY:
            b.deny_rules += 1
            continue
        b.allow_rules += 1
        endpoints.setdefault((rule.group, rule.endpoint), rule)
        if rule.matcher is not None and rule.matcher.path in ("/**", "**"):
            b.match_all_paths += 1
        if rule.matcher is not None and rule.matcher.method == "*":
            b.any_method_rules += 1
    globbed: set[str] = set()
    for rule in access.rules:
        for path in rule.clients.paths:
            if any(ch in path for ch in "*?["):
                globbed.add(path)
    b.binary_globs = len(globbed)

    hosts: list[str] = []
    uncatalogued: list[str] = []
    for (_, endpoint), rule in endpoints.items():
        label = endpoint.host or ",".join(endpoint.allowed_ips) or "(any host)"
        if label not in hosts:
            hosts.append(label)
        known = bool(_matching_operations(rule, graph)) or bool(
            endpoint.host and graph.services_for(endpoint.host, _first_int(endpoint.ports))
        )
        if not known and label not in uncatalogued:
            uncatalogued.append(label)
        if "*" in endpoint.host:
            b.wildcard_hosts += 1
        if endpoint.inspection == Inspection.OPAQUE:
            b.opaque_endpoints += 1
        if endpoint.protocol in (Protocol.L4, Protocol.TCP) or endpoint.inspection == Inspection.OPAQUE:
            b.connection_level_endpoints += 1
            b.score += SCORE_WEIGHTS[
                "connection_level_endpoint" if known else "connection_level_uncatalogued_host"
            ]
        elif endpoint.enforcement == Enforcement.AUDIT:
            b.unenforced_endpoints += 1
            b.score += SCORE_WEIGHTS["unenforced_endpoint"]
    b.endpoints = len(endpoints)
    b.hosts = hosts
    b.uncatalogued_hosts = uncatalogued

    counts = {p.value: 0 for p in Privilege}
    for entry in permitted.values():
        counts[entry.privilege.value] += 1
        b.score += SCORE_WEIGHTS[f"operation.{entry.privilege.value}"]
    b.permitted = counts
    b.score += (
        SCORE_WEIGHTS["match_all_path"] * b.match_all_paths
        + SCORE_WEIGHTS["any_method"] * b.any_method_rules
        + SCORE_WEIGHTS["binary_glob"] * b.binary_globs
        + SCORE_WEIGHTS["opaque_endpoint"] * b.opaque_endpoints
        + SCORE_WEIGHTS["wildcard_host"] * b.wildcard_hosts
    )
    return b


def _first_int(ports: tuple) -> int:
    for port in ports:
        if isinstance(port, int):
            return port
    return 443

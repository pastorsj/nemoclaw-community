# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve a contract against the capability graph into an access set.

Resolution turns each need into *grants*: one allowed matcher on one endpoint
for one set of client binaries, tagged with the graph operations it serves. The
grants then pass through the prohibitions, optional use-case justification, and
coverage pruning before they become access rules. The same grants, widened to
whole capabilities without resource scope, form the boundary (ceiling) policy.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol as TypingProtocol

from policy_compiler import paths
from policy_compiler.graph.model import McpSelector, Operation, RestSelector, Service
from policy_compiler.graph.store import Graph
from policy_compiler.ir.access import (
    AccessRule,
    AccessSet,
    ClientSet,
    Diagnostic,
    Effect,
    EndpointKey,
    Enforcement,
    Matcher,
    Widening,
)
from policy_compiler.ir.common import Inspection, Privilege, Protocol, method_privilege
from policy_compiler.ir.contract import COMPILED_PHASES, Contract, Locus, Need, Prohibition, Target

MCP_LIFECYCLE_METHODS = ("initialize", "notifications/initialized", "tools/list")
ALWAYS_JUSTIFIED_CONCEPTS = frozenset({"llm.inference"})
"""An agent needs its model even when the use case never mentions one."""

_PRIVATE_LABELS = frozenset({"internal", "local", "corp", "lan", "intranet", "private", "localdomain"})


class ConceptMatcher(TypingProtocol):
    """The part of ``semantic.SemanticIndex`` the resolver uses."""

    def match(self, text: str, *, min_score: float = ..., top_k: int = ...) -> list: ...

    def concepts_for_operation(self, operation_id: str) -> list[str]: ...


@dataclass
class ResolveOptions:
    pin_scope: bool = True
    """Bind resource scope (owner, repo, ...) into paths. Off for the boundary."""
    use_case: str | None = None
    """Text used to justify needs. Defaults to the contract's ``use_case``."""
    strict_use_case: bool = False
    """Drop grants the use case does not justify instead of only reporting them."""
    min_justification_score: float = 0.15
    route_inference_local: bool = False
    """Send model traffic to OpenShell's ``inference.local`` route instead of the provider."""


@dataclass
class Grant:
    need: str
    service: Service | None
    host: str
    port: int
    protocol: Protocol
    clients: tuple[str, ...]
    matcher: Matcher | None
    operations: list[str] = field(default_factory=list)
    privilege: Privilege = Privilege.UNKNOWN
    inspection: Inspection = Inspection.L7
    allowed_ips: tuple[str, ...] = ()
    justified_by: list[str] = field(default_factory=list)


@dataclass
class NeedResolution:
    need: str
    status: str
    """``resolved``, ``uncatalogued``, ``skipped``, ``unresolved``, ``conflict``, or ``excluded``."""
    operations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    justified_by: list[str] = field(default_factory=list)


@dataclass
class Resolution:
    contract: Contract
    access: AccessSet
    boundary: AccessSet
    needs: list[NeedResolution]
    forbidden_operations: list[str] = field(default_factory=list)
    use_case_concepts: list[str] = field(default_factory=list)


def resolve(
    contract: Contract,
    graph: Graph,
    options: ResolveOptions | None = None,
    *,
    concept_index: ConceptMatcher | None = None,
) -> Resolution:
    """Resolve ``contract`` against ``graph``.

    ``concept_index`` enables use-case justification. Without it, every grant is
    reported as unjustified-but-kept only when a use case is present.
    """
    options = options or ResolveOptions()
    resolver = _Resolver(contract, graph, options, concept_index)
    return resolver.run()


class _Resolver:
    def __init__(
        self,
        contract: Contract,
        graph: Graph,
        options: ResolveOptions,
        index: ConceptMatcher | None,
    ) -> None:
        self.contract = contract
        self.graph = graph
        self.options = options
        self.index = index
        self.diagnostics: list[Diagnostic] = []
        self.widenings: list[Widening] = []
        self.results: dict[str, NeedResolution] = {}
        self.client_specific: dict[str, set[str]] = {}
        """Operation ID -> client paths that perform it implicitly (git-upload-pack -> git)."""
        for client in graph.clients.values():
            for use in client.uses:
                for op_id in use.operations:
                    self.client_specific.setdefault(op_id, set()).update(client.paths)

    # ------------------------------------------------------------------ driver

    def run(self) -> Resolution:
        grants: list[Grant] = []
        for need in self.contract.needs:
            grants.extend(self._resolve_need(need))

        if self.options.route_inference_local:
            grants = self._route_inference_local(grants)

        use_case_concepts = self._justify(grants)

        forbidden = self._forbidden_operations()
        grants = self._apply_prohibitions(grants, forbidden)

        access = self._build_access(grants, forbidden)
        boundary_grants = self._boundary_grants(grants, forbidden)
        boundary = self._build_access(boundary_grants, forbidden)

        access.diagnostics = list(self.diagnostics)
        access.widenings = list(self.widenings)
        ordered = [self.results[n.id] for n in self.contract.needs if n.id in self.results]
        return Resolution(
            contract=self.contract,
            access=access,
            boundary=boundary,
            needs=ordered,
            forbidden_operations=sorted(forbidden),
            use_case_concepts=use_case_concepts,
        )

    def _result(self, need: Need) -> NeedResolution:
        return self.results.setdefault(need.id, NeedResolution(need=need.id, status="resolved"))

    # ------------------------------------------------------------ need → grant

    def _resolve_need(self, need: Need) -> list[Grant]:
        result = self._result(need)
        if need.locus != Locus.SANDBOX:
            result.status = "skipped"
            result.notes.append(f"The call originates outside the sandbox ({need.locus.value}).")
            return []
        if need.phase not in COMPILED_PHASES:
            result.status = "skipped"
            result.notes.append(
                f"{need.phase.value.capitalize()}-phase traffic is not part of the runtime policy."
            )
            return []
        clients = tuple(need.clients or self.contract.default_clients)
        if not clients:
            result.status = "unresolved"
            self._diag("error", "no-clients", f"{need.id}: no client binaries; pass --client.", need.id)
            return []

        target = need.target
        if target.raw is not None:
            grants = self._resolve_raw(need, clients)
        else:
            operations, note = self._operations_for_target(target)
            if note:
                result.notes.append(note)
            if not operations:
                if result.status == "resolved":
                    result.status = "unresolved"
                return []
            grants = self._grants_for_operations(need, operations, clients, target.scope)

        grants.extend(self._client_use_grants(need, grants, clients, target.scope))
        result.operations = _unique([op for g in grants for op in g.operations])
        if not grants and result.status == "resolved":
            result.status = "unresolved"
        return grants

    def _operations_for_target(self, target: Target) -> tuple[list[Operation], str]:
        graph = self.graph
        if target.operation is not None:
            op = graph.operations.get(target.operation)
            if op is None:
                return [], f"Operation {target.operation} is not in the catalog."
            return [op], ""
        if target.capability is not None:
            if target.capability not in graph.capabilities:
                return [], f"Capability {target.capability} is not in the catalog."
            return graph.operations_enabled_by([target.capability]), ""
        concept = target.concept or ""
        if concept not in graph.concepts:
            return [], f"Concept {concept} is not in the catalog."
        capabilities = graph.capabilities_for_concept(concept, target.provider)
        if not capabilities:
            where = f" for provider {target.provider}" if target.provider else ""
            return [], f"No capability realizes {concept}{where}."
        note = f"Concept {concept} expanded to {', '.join(capabilities)}."
        return graph.operations_enabled_by(capabilities), note

    def _grants_for_operations(
        self,
        need: Need,
        operations: list[Operation],
        clients: tuple[str, ...],
        scope: dict[str, str],
    ) -> list[Grant]:
        result = self._result(need)
        grants: list[Grant] = []
        excluded: list[str] = []
        special, generic = self._split_clients(clients)
        for op in operations:
            if self.options.pin_scope and scope and isinstance(op.selector, RestSelector):
                params = set(re.findall(r"\{([^{}/]+)\}", op.selector.path))
                if not params & set(scope):
                    excluded.append(op.id)
                    continue
            # Special-purpose clients (such as git) only receive their own implicit uses, and
            # operations only such clients perform are not granted to general-purpose ones.
            op_clients = generic
            if not op_clients or op.id in self.client_specific:
                continue
            grants.extend(self._grants_for_operation(need, op, op_clients, scope))
        if excluded:
            result.notes.append(
                "Left out because they cannot be limited to the requested scope: "
                + ", ".join(excluded)
                + "."
            )
        if special and not generic:
            result.notes.append(
                "Only special-purpose clients were given; they receive their implicit operations only."
            )
        return grants

    def _grants_for_operation(
        self,
        need: Need,
        op: Operation,
        clients: tuple[str, ...],
        scope: dict[str, str],
        glob: str | None = None,
    ) -> list[Grant]:
        service = self.graph.services[op.service]
        grants = []
        for origin in service.origins:
            matcher = self._matcher_for_operation(op, scope, glob)
            grants.append(
                Grant(
                    need=need.id,
                    service=service,
                    host=origin.host,
                    port=origin.port,
                    protocol=service.protocol,
                    clients=clients,
                    matcher=matcher,
                    operations=[op.id],
                    privilege=op.privilege,
                    inspection=service.inspection,
                    allowed_ips=tuple(service.allowed_ips),
                )
            )
        return grants

    def _matcher_for_operation(self, op: Operation, scope: dict[str, str], glob: str | None) -> Matcher:
        selector = op.selector
        if isinstance(selector, RestSelector):
            if glob is None:
                bindings = scope if self.options.pin_scope else {}
                glob = paths.instantiate(selector.path, bindings, set(op.multi_segment))
            return Matcher(method=selector.method, path=glob)
        if isinstance(selector, McpSelector):
            return Matcher(method="tools/call", tool=selector.tool)
        raise ValueError(f"unsupported selector for {op.id}: {type(selector).__name__}")

    def _resolve_raw(self, need: Need, clients: tuple[str, ...]) -> list[Grant]:
        raw = need.target.raw
        assert raw is not None
        result = self._result(need)
        services = self.graph.services_for(raw.host, raw.port)
        service = services[0] if services else None
        inspection = service.inspection if service else Inspection.L7
        allowed_ips = tuple(service.allowed_ips) if service else ()

        def grant(matcher: Matcher | None, ops: list[str], privilege: Privilege, protocol: Protocol) -> Grant:
            return Grant(
                need=need.id,
                service=service,
                host=raw.host,
                port=raw.port,
                protocol=protocol,
                clients=clients,
                matcher=matcher,
                operations=ops,
                privilege=privilege,
                inspection=inspection,
                allowed_ips=allowed_ips,
            )

        if service is None and _looks_private(raw.host):
            self._diag(
                "warning",
                "private-destination",
                f"{need.id}: {raw.host} looks like a private address. OpenShell needs allowed_ips "
                "for private ranges; describe the API in a catalog with allowed_ips.",
                need.id,
            )

        if raw.protocol in (Protocol.TCP, Protocol.L4, Protocol.SQL):
            self._widen(need.id, "connection-level", f"{raw.host}:{raw.port} is allowed at the connection level.")
            return [grant(None, [], Privilege.UNKNOWN, Protocol.L4)]

        if raw.protocol == Protocol.MCP:
            matchers = [Matcher(method=m) for m in MCP_LIFECYCLE_METHODS]
            if raw.mcp_tools:
                matchers += [Matcher(method="tools/call", tool=t) for t in raw.mcp_tools]
            else:
                self._widen(need.id, "open-toolset", "No tool filter: tools/call is not allowed for any tool.")
            ops = [
                op.id
                for svc in services
                for op in self.graph.operations_of(svc.id)
                if isinstance(op.selector, McpSelector) and op.selector.tool in raw.mcp_tools
            ]
            if not ops:
                result.status = "uncatalogued"
            return [grant(m, ops, Privilege.UNKNOWN, Protocol.MCP) for m in matchers]

        if raw.method is None or raw.path is None:
            self._widen(
                need.id,
                "endpoint-level",
                f"No method or path is declared for {raw.host}; every request to it is allowed.",
            )
            if service is None:
                result.status = "uncatalogued"
            return [grant(Matcher(method="*", path="/**"), [], Privilege.UNKNOWN, Protocol.REST)]

        if inspection == Inspection.OPAQUE:
            self._widen(need.id, "opaque", f"{raw.host} cannot be inspected; only the host and port are limited.")
            return [grant(None, [], Privilege.UNKNOWN, Protocol.L4)]

        prefixes = [p.rstrip("/") for p in need.path_prefixes]
        if "*" in raw.path:
            matches = self._glob_matches(raw.host, raw.port, raw.method, raw.path)
            if not matches:
                result.status = "uncatalogued"
            privilege = _max_privilege([op.privilege for op in matches]) if matches else method_privilege(raw.method)
            return [grant(Matcher(method=raw.method, path=raw.path), [op.id for op in matches], privilege, Protocol.REST)]

        matches = self.graph.match_rest(raw.host, raw.port, raw.method, raw.path)
        if prefixes:
            kept = [(op, glob) for op, glob in matches if _within_prefixes(glob, prefixes)]
            if len(kept) < len(matches):
                result.notes.append("Operations outside the declared url_allow_list were left out.")
            matches = kept
        if matches:
            grants = []
            for op, glob in matches:
                # Never widen the declared method: a catalogued ``*`` operation (common in policies
                # mined from hand-written files) still yields a rule for the method the agent declared.
                method = raw.method if raw.method != "*" else op.selector.method  # type: ignore[union-attr]
                grants.append(grant(Matcher(method=method, path=glob), [op.id], op.privilege, Protocol.REST))
            return grants

        result.status = "uncatalogued"
        glob = paths.template_to_glob(raw.path)
        if prefixes and not _within_prefixes(glob, prefixes):
            result.status = "unresolved"
            result.notes.append(f"The declared path {raw.path} is outside its own url_allow_list.")
            return []
        if raw.method == "*":
            self._widen(need.id, "templated-method", "The HTTP method is chosen at run time; every method is allowed.")
        result.notes.append(
            f"{raw.host} {raw.method} {raw.path} is not in the catalog; the rule follows the declared shape "
            "and its privilege is guessed from the method."
        )
        return [grant(Matcher(method=raw.method, path=glob), [], method_privilege(raw.method), Protocol.REST)]

    def _glob_matches(self, host: str, port: int, method: str, glob: str) -> list[Operation]:
        found = []
        for service in self.graph.services_for(host, port):
            for op in self.graph.operations_of(service.id):
                selector = op.selector
                if not isinstance(selector, RestSelector):
                    continue
                if not paths.method_covers(method, selector.method):
                    continue
                if paths.glob_overlaps_template(glob, selector.path, set(op.multi_segment)):
                    found.append(op)
        return found

    def _client_may_use(self, op: Operation, clients: tuple[str, ...]) -> bool:
        owners = self.client_specific.get(op.id)
        return owners is None or bool(owners & set(clients))

    def _split_clients(self, clients: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Separate clients with implicit uses (git) from general-purpose ones (curl, python)."""
        special = tuple(c for c in clients if any(cl.uses for cl in self.graph.clients_for_path(c)))
        generic = tuple(c for c in clients if c not in special)
        return special, generic

    def _client_use_grants(
        self,
        need: Need,
        grants: list[Grant],
        clients: tuple[str, ...],
        scope: dict[str, str],
    ) -> list[Grant]:
        """Add the operations special-purpose clients perform implicitly, such as git-upload-pack."""
        target = need.target
        granted_caps: set[str] = set()
        if target.capability:
            granted_caps.add(target.capability)
        elif target.concept:
            granted_caps.update(self.graph.capabilities_for_concept(target.concept, target.provider))
        else:
            for g in grants:
                for op_id in g.operations:
                    granted_caps.update(_cheapest_requirement(self.graph, self.graph.operations[op_id]))
        if not granted_caps:
            return []
        closure = self.graph.capability_closure(granted_caps)
        extra: list[Grant] = []
        for path in clients:
            for client in self.graph.clients_for_path(path):
                for use in client.uses:
                    if use.when_capability and not set(use.when_capability) & closure:
                        continue
                    for op_id in use.operations:
                        op = self.graph.operations[op_id]
                        if target.provider and self.graph.services[op.service].provider != target.provider:
                            continue
                        extra.extend(self._grants_for_operation(need, op, (path,), scope))
        if extra:
            self._result(need).notes.append(
                "Added the implicit operations of " + ", ".join(sorted({c for g in extra for c in g.clients})) + "."
            )
        return extra

    # ------------------------------------------------------------ inference.local

    def _route_inference_local(self, grants: list[Grant]) -> list[Grant]:
        local_service = self.graph.services.get("openshell.inference-local")
        if local_service is None:
            self._diag("warning", "no-inference-local", "The catalog has no openshell.inference-local service.")
            return grants
        local_ops = {
            (op.selector.method, op.selector.path): op
            for op in self.graph.operations_of(local_service.id)
            if isinstance(op.selector, RestSelector)
        }
        routed: list[Grant] = []
        for g in grants:
            concepts = {c for op in g.operations for c in self._concepts_for(op)}
            if "llm.inference" not in concepts or g.service is local_service or g.matcher is None:
                routed.append(g)
                continue
            replacement = local_ops.get((g.matcher.method, g.matcher.path))
            result = self.results.get(g.need)
            if replacement is None:
                if result:
                    result.notes.append(
                        f"{g.matcher.method} {g.matcher.path} has no inference.local equivalent and was dropped."
                    )
                continue
            routed.extend(
                self._grants_for_operation(
                    _need_stub(g.need), replacement, g.clients, {}, None
                )
            )
            if result:
                result.notes.append(
                    f"Routed {g.host} to inference.local; point the agent's base URL at https://inference.local/v1."
                )
        return routed

    # ------------------------------------------------------------ justification

    def _concepts_for(self, op_id: str) -> list[str]:
        if self.index is not None:
            return self.index.concepts_for_operation(op_id)
        op = self.graph.operations.get(op_id)
        if op is None:
            return []
        concepts: list[str] = []
        for group in op.requires:
            for cap in group:
                concepts.extend(self.graph.concepts_for_capability(cap))
        return _unique(concepts)

    def _justify(self, grants: list[Grant]) -> list[str]:
        text = self.options.use_case if self.options.use_case is not None else self.contract.use_case
        if not text or self.index is None:
            return []
        matches = self.index.match(text, min_score=self.options.min_justification_score)
        concepts = [m.concept for m in matches]
        allowed = set(concepts) | ALWAYS_JUSTIFIED_CONCEPTS
        unjustified_needs: dict[str, list[str]] = {}
        for g in grants:
            op_concepts = {c for op in g.operations for c in self._concepts_for(op)}
            g.justified_by = sorted(op_concepts & allowed)
            if not g.justified_by:
                unjustified_needs.setdefault(g.need, []).extend(g.operations or [f"{g.host}"])
        for need_id, result in self.results.items():
            justified = sorted({c for g in grants if g.need == need_id for c in g.justified_by})
            result.justified_by = justified
            if need_id in unjustified_needs:
                what = ", ".join(_unique(unjustified_needs[need_id]))
                if self.options.strict_use_case:
                    result.notes.append(f"Excluded because the use case does not justify: {what}.")
                else:
                    result.notes.append(f"Not justified by the use case (kept): {what}.")
        if self.options.strict_use_case:
            kept = [g for g in grants if g.justified_by]
            for need_id in unjustified_needs:
                if not any(g.need == need_id for g in kept):
                    self.results[need_id].status = "excluded"
            grants[:] = kept
        return concepts

    # ------------------------------------------------------------ prohibitions

    def _forbidden_operations(self) -> set[str]:
        forbidden: set[str] = set()
        for prohibition in self.contract.prohibitions:
            forbidden.update(self._operations_for_prohibition(prohibition))
        return forbidden

    def _operations_for_prohibition(self, prohibition: Prohibition) -> set[str]:
        graph = self.graph
        found: set[str] = set()
        if prohibition.provider is not None:
            floor = prohibition.min_privilege or Privilege.READ
            for op in graph.operations.values():
                if graph.services[op.service].provider == prohibition.provider and op.privilege.at_least(floor):
                    found.add(op.id)
        if prohibition.target is not None:
            target = prohibition.target
            if target.raw is not None:
                raw = target.raw
                for service in graph.services_for(raw.host, raw.port):
                    for op in graph.operations_of(service.id):
                        found.add(op.id)
            else:
                ops, _ = self._operations_for_target(target)
                found.update(op.id for op in ops)
        return found

    def _apply_prohibitions(self, grants: list[Grant], forbidden: set[str]) -> list[Grant]:
        raw_hosts = {
            (p.target.raw.host.lower(), p.target.raw.port)
            for p in self.contract.prohibitions
            if p.target is not None and p.target.raw is not None
        }
        kept: list[Grant] = []
        for g in grants:
            blocked_ops = [op for op in g.operations if op in forbidden]
            if (g.host.lower(), g.port) in raw_hosts or (g.operations and len(blocked_ops) == len(g.operations)):
                result = self.results[g.need]
                result.status = "conflict"
                what = ", ".join(blocked_ops) or f"{g.host}:{g.port}"
                result.notes.append(f"Removed because a prohibition forbids: {what}.")
                self._diag("warning", "prohibited", f"{g.need}: a prohibition removed {what}.", g.need)
                continue
            kept.append(g)
        return kept

    # ------------------------------------------------------------ boundary

    def _boundary_grants(self, grants: list[Grant], forbidden: set[str]) -> list[Grant]:
        """Widen each catalogued grant to every operation its capabilities enable, without scope."""
        boundary: list[Grant] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for g in grants:
            if not g.operations:
                boundary.append(g)
                continue
            caps: set[str] = set()
            for op_id in g.operations:
                caps.update(_cheapest_requirement(self.graph, self.graph.operations[op_id]))
            ops = list(self.graph.operations_enabled_by(caps)) if caps else []
            ops += [self.graph.operations[o] for o in g.operations]
            for op in ops:
                if op.id in forbidden or (op.id, g.clients) in seen:
                    continue
                if not self._client_may_use(op, g.clients):
                    continue
                seen.add((op.id, g.clients))
                stub = _need_stub(g.need)
                boundary.extend(self._grants_for_operation(stub, op, g.clients, {}, None))
        return boundary

    # ------------------------------------------------------------ access set

    def _build_access(self, grants: list[Grant], forbidden: set[str]) -> AccessSet:
        rules = self._rules_from_grants(grants)
        rules.extend(self._deny_rules(rules, forbidden))
        return AccessSet(rules=rules, extras={"version": 1})

    def _rules_from_grants(self, grants: list[Grant]) -> list[AccessRule]:
        buckets: dict[tuple, list[Grant]] = {}
        for g in grants:
            key = self._endpoint_key(g)
            buckets.setdefault((key, g.clients, _group_name(g)), []).append(g)
        rules: list[AccessRule] = []
        for (key, clients, group), bucket in buckets.items():
            matchers: list[tuple[Matcher | None, Grant]] = []
            for g in bucket:
                matchers.append((g.matcher, g))
            merged = _prune_covered(matchers)
            for matcher, members in merged:
                rules.append(
                    AccessRule(
                        effect=Effect.ALLOW,
                        group=group,
                        clients=ClientSet.of(clients),
                        endpoint=key,
                        matcher=matcher,
                        operations=_unique([op for m in members for op in m.operations]),
                        privilege=_max_privilege([m.privilege for m in members]),
                        reasons=_unique([m.need for m in members]),
                    )
                )
        return rules

    def _endpoint_key(self, g: Grant) -> EndpointKey:
        protocol = g.protocol
        if g.matcher is None and protocol not in (Protocol.TCP,):
            protocol = Protocol.L4
        return EndpointKey(
            host=g.host,
            ports=(g.port,),
            protocol=protocol,
            inspection=g.inspection,
            enforcement=Enforcement.ENFORCE,
            allowed_ips=g.allowed_ips,
        )

    def _deny_rules(self, rules: list[AccessRule], forbidden: set[str]) -> list[AccessRule]:
        """Deny forbidden operations that a broader allow on the same endpoint would admit."""
        denies: list[AccessRule] = []
        seen: set[tuple] = set()
        for rule in rules:
            if rule.matcher is None or rule.matcher.path is None or rule.matcher.method is None:
                continue
            for service in self.graph.services_for(rule.endpoint.host, rule.endpoint.ports[0]):
                for op in self.graph.operations_of(service.id):
                    if op.id not in forbidden or not isinstance(op.selector, RestSelector):
                        continue
                    if not paths.method_covers(rule.matcher.method, op.selector.method):
                        continue
                    if not paths.glob_overlaps_template(rule.matcher.path, op.selector.path, set(op.multi_segment)):
                        continue
                    deny_path = paths.instantiate(op.selector.path, {}, set(op.multi_segment))
                    key = (rule.group, rule.clients, rule.endpoint, op.selector.method, deny_path)
                    if key in seen:
                        continue
                    seen.add(key)
                    denies.append(
                        AccessRule(
                            effect=Effect.DENY,
                            group=rule.group,
                            clients=rule.clients,
                            endpoint=rule.endpoint,
                            matcher=Matcher(method=op.selector.method, path=deny_path),
                            operations=[op.id],
                            privilege=op.privilege,
                            reasons=["prohibition"],
                        )
                    )
        return denies

    # ------------------------------------------------------------ helpers

    def _diag(self, level: str, code: str, message: str, pointer: str = "") -> None:
        self.diagnostics.append(Diagnostic(level=level, code=code, message=message, pointer=pointer))

    def _widen(self, need: str, reason: str, detail: str) -> None:
        self.widenings.append(Widening(need=need, reason=reason, detail=detail))


def _need_stub(need_id: str) -> Need:
    return Need(id=need_id, target=Target(operation="-"), rationale="")


def _cheapest_requirement(graph: Graph, op: Operation) -> list[str]:
    """The AND-list with the lowest highest-level capability (ties: the shortest, then first)."""
    if not op.requires:
        return []

    def cost(group: list[str]) -> tuple[int, int]:
        levels = [graph.capabilities[c].level.rank for c in group if c in graph.capabilities]
        return (max(levels) if levels else Privilege.UNKNOWN.rank, len(group))

    return list(min(op.requires, key=cost))


def _prune_covered(items: list[tuple[Matcher | None, Grant]]) -> list[tuple[Matcher | None, list[Grant]]]:
    """Merge duplicate matchers and fold matchers that another matcher already covers into it."""
    merged: dict[Matcher | None, list[Grant]] = {}
    for matcher, g in items:
        merged.setdefault(matcher, []).append(g)
    keys = list(merged)

    def covers(a: Matcher | None, b: Matcher | None) -> bool:
        if a is None or b is None or a == b:
            return False
        if a.path is None or b.path is None or a.method is None or b.method is None:
            return False
        if a.tool != b.tool:
            return False
        return paths.method_covers(a.method, b.method) and paths.glob_covers(a.path, b.path)

    def dominated(m: Matcher | None) -> bool:
        for other in keys:
            if covers(other, m) and not (covers(m, other) and keys.index(m) < keys.index(other)):
                return True
        return False

    maximal = [m for m in keys if not dominated(m)]
    result: dict[Matcher | None, list[Grant]] = {m: list(merged[m]) for m in maximal}
    for m in keys:
        if m in result:
            continue
        cover = next(o for o in maximal if covers(o, m))
        result[cover].extend(merged[m])
    return list(result.items())


def _group_name(g: Grant) -> str:
    if g.service is not None:
        base = g.service.id
    else:
        base = g.host
    return re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")


def _max_privilege(levels: Iterable[Privilege]) -> Privilege:
    levels = list(levels)
    return max(levels, key=lambda p: p.rank) if levels else Privilege.UNKNOWN


def _within_prefixes(glob: str, prefixes: list[str]) -> bool:
    for prefix in prefixes:
        if paths.glob_covers(prefix + "/**", glob) or paths.glob_covers(prefix, glob):
            return True
    return False


def _looks_private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return bool(set(host.lower().split(".")) & _PRIVATE_LABELS)


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))

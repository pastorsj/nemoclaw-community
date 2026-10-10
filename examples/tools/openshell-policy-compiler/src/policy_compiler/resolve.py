# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve a contract against the capability graph into an access set.

Resolution turns each need into *grants*: one allowed matcher on one endpoint
for one set of client binaries, tagged with the graph operations it serves. The
grants then pass through the prohibitions, optional use-case justification, and
coverage pruning before they become access rules. The same grants, plus the
operations their capabilities enable without resource scope, form the boundary
(ceiling) policy, so the policy always lies within its boundary.

When the compiler cannot tell what a grant reaches (an uncatalogued path, a
glob, a whole endpoint, or a connection), prohibitions and use-case
justification fail closed: the grant is narrowed or removed, and the need's
notes say why.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field, replace
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
from policy_compiler.ir.common import Inspection, Layer, Privilege, Protocol, method_privilege
from policy_compiler.ir.contract import COMPILED_PHASES, Contract, Locus, Need, Prohibition, RawEndpoint, Target

MCP_LIFECYCLE_METHODS = ("initialize", "notifications/initialized", "tools/list")
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
"""Methods a declared REST call may use, besides ``*`` for a templated method. CONNECT and TRACE are refused."""
GLOB_CHARACTERS = frozenset("*?[]")
"""Characters that make an OpenShell path or tool matcher a pattern instead of a literal."""
MTLS_DETAIL = "mtls: OpenShell TLS termination would break client certificates"
LLM_CONFIG_JUSTIFICATION = "llm_config"
"""``justified_by`` entry for needs that an agent's model configuration declares."""
BROAD_PREFIX = 8
"""``allowed_ips`` ranges with a prefix shorter than this are reported as broad."""
ALWAYS_JUSTIFIED_CONCEPTS = frozenset({"llm.inference"})
"""An agent needs its model even when the use case never mentions one."""

_SCOPE_VALUE = re.compile(r"^[A-Za-z0-9_.%-]+$")
"""A scope value must be one plain path segment: no glob characters, slashes, or dot segments."""

_PRIVATE_LABELS = frozenset({"internal", "local", "corp", "lan", "intranet", "private", "localdomain"})


class ConceptMatcher(TypingProtocol):
    """The part of ``semantic.SemanticIndex`` the resolver uses."""

    def match(self, text: str, *, min_score: float = ..., top_k: int = ...) -> list: ...

    def concepts_for_operation(self, operation_id: str) -> list[str]: ...

    def keywords(self, concept_id: str) -> frozenset[str]: ...


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
    declared: bool = False
    """The matcher follows the declared call shape (a glob, an uncatalogued path, or a whole endpoint), so it
    can admit requests no catalogued operation describes."""
    lifecycle: bool = False
    """An MCP lifecycle method (initialize, tools/list) that only serves the need's tools/call grants."""
    endpoint_path: str = ""
    """OpenShell endpoint route selector (``EndpointKey.path``): the declared MCP server path. Empty for
    every other grant, and for catalogued MCP grants, which then cover the whole host."""


@dataclass
class NeedResolution:
    need: str
    status: str
    """``resolved``, ``uncatalogued``, ``skipped``, ``unresolved``, ``conflict``, or ``excluded``."""
    operations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    justified_by: list[str] = field(default_factory=list)
    unjustified: list[str] = field(default_factory=list)
    """Operations (or hosts, for uncatalogued grants) that the use case does not justify."""
    unjustified_action: str = ""
    """``kept`` or ``excluded`` (with ``strict_use_case``) when ``unjustified`` is not empty."""


@dataclass
class Resolution:
    contract: Contract
    access: AccessSet
    boundary: AccessSet
    needs: list[NeedResolution]
    forbidden_operations: list[str] = field(default_factory=list)
    forbidden_by: dict[str, list[str]] = field(default_factory=dict)
    """Prohibition ID -> the catalogued operations it forbids."""
    use_case_concepts: list[str] = field(default_factory=list)
    justified: bool = False
    """Use-case justification ran: there was use-case text and a concept index."""


def resolve(
    contract: Contract,
    graph: Graph,
    options: ResolveOptions | None = None,
    *,
    concept_index: ConceptMatcher | None = None,
) -> Resolution:
    """Resolve ``contract`` against ``graph``.

    ``concept_index`` enables use-case justification, together with use-case text
    (``options.use_case`` or the contract's). Without either, no grant is judged
    and ``Resolution.justified`` is False.
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
        self.justified = False
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
        self._note_uncatalogued_models()

        use_case_concepts = self._justify(grants)

        forbidden_by = {p.id: sorted(self._operations_for_prohibition(p)) for p in self.contract.prohibitions}
        forbidden = {op for ops in forbidden_by.values() for op in ops}
        grants = self._apply_prohibitions(grants, forbidden)

        access = self._build_access(grants, forbidden)
        boundary_grants = self._boundary_grants(grants, forbidden)
        boundary = self._build_access(boundary_grants, forbidden)
        self._check_allowed_ips(access, boundary)

        access.diagnostics = list(self.diagnostics)
        access.widenings = list(self.widenings)
        ordered = [self.results[n.id] for n in self.contract.needs if n.id in self.results]
        return Resolution(
            contract=self.contract,
            access=access,
            boundary=boundary,
            needs=ordered,
            forbidden_operations=sorted(forbidden),
            forbidden_by=forbidden_by,
            use_case_concepts=use_case_concepts,
            justified=self.justified,
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
        invalid = {k: v for k, v in target.scope.items() if not _valid_scope_value(v)}
        if invalid:
            result.status = "unresolved"
            what = ", ".join(f"{k}={v!r}" for k, v in invalid.items())
            result.notes.append(
                f"Invalid scope value ({what}): a scope value must be one path segment made of letters, digits, "
                "and _.%- characters, and not . or ..; nothing was granted."
            )
            self._diag("warning", "invalid-scope", f"{need.id}: invalid scope value {what}.", need.id)
            return []

        if target.raw is not None:
            grants, bindings = self._resolve_raw(need, clients)
            uses = self._client_use_grants(need, grants, clients, bindings, require_bound=True)
            # Special-purpose clients (such as git) only receive their implicit uses, never the declared call.
            special, generic = self._split_clients(clients)
            if special:
                grants = [replace(g, clients=generic) for g in grants] if generic else []
                if not generic:
                    result.notes.append(
                        "Only special-purpose clients were given; they receive their implicit operations only."
                    )
            grants.extend(uses)
        else:
            if self._ambiguous_concept(need):
                return []
            operations, note = self._operations_for_target(target)
            if note:
                result.notes.append(note)
            if not operations:
                if result.status == "resolved":
                    result.status = "unresolved"
                return []
            grants = self._grants_for_operations(need, operations, clients, target.scope)
            if target.concept is not None and grants:
                self._widen(
                    need.id,
                    "concept-level",
                    f"{note} Every operation those capabilities enable is allowed"
                    + (" within the scope." if target.scope else "."),
                )
            grants.extend(
                self._client_use_grants(
                    need,
                    grants,
                    clients,
                    target.scope,
                    require_bound=bool(target.scope) and self.options.pin_scope,
                )
            )
            if _uses_mtls(need) and grants:
                grants = self._connection_level(need, grants)

        result.operations = _unique([op for g in grants for op in g.operations])
        if not grants and result.status == "resolved":
            result.status = "unresolved"
        return grants

    def _ambiguous_concept(self, need: Need) -> bool:
        """A concept without a provider that several providers realize grants nothing: the agent must choose."""
        target = need.target
        if target.concept is None or target.provider is not None or target.concept not in self.graph.concepts:
            return False
        capabilities = self.graph.capabilities_for_concept(target.concept)
        providers = sorted({self.graph.capabilities[c].provider for c in capabilities})
        if len(providers) < 2:
            return False
        result = self._result(need)
        result.status = "unresolved"
        message = (
            f"Concept {target.concept} is realized by several providers ({', '.join(providers)}); "
            "ambiguous: name a provider."
        )
        result.notes.append(message)
        self._diag("warning", "ambiguous-concept", f"{need.id}: {message}", need.id)
        return True

    def _connection_level(self, need: Need, grants: list[Grant]) -> list[Grant]:
        """Turn a need's grants into one opaque connection-level grant per endpoint and client set (mTLS)."""
        self._widen(need.id, "mtls", MTLS_DETAIL)
        merged: dict[tuple, Grant] = {}
        for g in grants:
            key = (g.host, g.port, g.clients)
            if key not in merged:
                merged[key] = replace(
                    g,
                    protocol=Protocol.L4,
                    matcher=None,
                    operations=list(g.operations),
                    privilege=Privilege.UNKNOWN,
                    inspection=Inspection.OPAQUE,
                    declared=True,
                    lifecycle=False,
                    endpoint_path="",
                )
                continue
            kept = merged[key]
            kept.operations = _unique([*kept.operations, *g.operations])
        return list(merged.values())

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
        return self._with_mcp_lifecycle(need.id, grants)

    def _with_mcp_lifecycle(self, need_id: str, grants: list[Grant]) -> list[Grant]:
        """Add the MCP lifecycle methods once per service endpoint and client set that has tools/call grants."""
        seen: set[tuple] = set()
        lifecycle: list[Grant] = []
        for g in grants:
            if g.protocol != Protocol.MCP or g.matcher is None or g.lifecycle:
                continue
            key = (g.host, g.port, g.endpoint_path, g.clients)
            if key in seen:
                continue
            seen.add(key)
            lifecycle.extend(
                Grant(
                    need=need_id,
                    service=g.service,
                    host=g.host,
                    port=g.port,
                    protocol=Protocol.MCP,
                    clients=g.clients,
                    matcher=Matcher(method=method),
                    privilege=Privilege.READ,
                    inspection=g.inspection,
                    allowed_ips=g.allowed_ips,
                    lifecycle=True,
                    endpoint_path=g.endpoint_path,
                )
                for method in MCP_LIFECYCLE_METHODS
            )
        return lifecycle + grants

    def _grants_for_operation(
        self,
        need: Need,
        op: Operation,
        clients: tuple[str, ...],
        scope: dict[str, str],
        glob: str | None = None,
    ) -> list[Grant]:
        service = self.graph.services[op.service]
        opaque = service.inspection == Inspection.OPAQUE
        if opaque:
            self._widen(
                need.id,
                "opaque",
                f"{service.id} cannot be inspected (tls: skip); {op.id} is allowed at the connection level, "
                "so every request to the host is allowed.",
            )
        grants = []
        for origin in service.origins:
            matcher = None if opaque else self._matcher_for_operation(op, scope, glob)
            grants.append(
                Grant(
                    need=need.id,
                    service=service,
                    host=origin.host,
                    port=origin.port,
                    protocol=Protocol.L4 if opaque else service.protocol,
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

    def _resolve_raw(self, need: Need, clients: tuple[str, ...]) -> tuple[list[Grant], dict[str, str]]:
        """Grants for a declared endpoint, plus the template parameters its literal segments fix."""
        declared = need.target.raw
        assert declared is not None
        raw = replace(
            declared,
            host=_normal_host(declared.host),
            method=declared.method.upper() if declared.method is not None else None,
        )
        result = self._result(need)
        connection = raw.protocol in (Protocol.TCP, Protocol.L4, Protocol.SQL)
        if not connection and not self._valid_raw_shape(need, raw):
            return [], {}

        services = self.graph.services_for(raw.host, raw.port)
        opaque_services = [s for s in services if s.inspection == Inspection.OPAQUE]
        service = opaque_services[0] if opaque_services else (services[0] if services else None)
        inspection = service.inspection if service else Inspection.L7
        allowed_ips = tuple(service.allowed_ips) if service else ()
        self._check_origin(need, raw)

        def grant(
            matcher: Matcher | None,
            ops: list[str],
            privilege: Privilege,
            protocol: Protocol,
            *,
            declared: bool,
            lifecycle: bool = False,
            endpoint_path: str = "",
            grant_inspection: Inspection | None = None,
        ) -> Grant:
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
                inspection=grant_inspection or inspection,
                allowed_ips=allowed_ips,
                declared=declared,
                lifecycle=lifecycle,
                endpoint_path=endpoint_path,
            )

        if service is None and _looks_private(raw.host):
            self._diag(
                "warning",
                "private-destination",
                f"{need.id}: {raw.host} looks like a private address. OpenShell needs allowed_ips "
                "for private ranges; describe the API in a catalog with allowed_ips.",
                need.id,
            )

        if _uses_mtls(need):
            # OpenShell terminates TLS to inspect requests, which would replace the client certificate.
            self._widen(need.id, "mtls", MTLS_DETAIL)
            if service is None:
                result.status = "uncatalogued"
            return [grant(None, [], Privilege.UNKNOWN, Protocol.L4, declared=True, grant_inspection=Inspection.OPAQUE)], {}

        if connection:
            self._widen(need.id, "connection-level", f"{raw.host}:{raw.port} is allowed at the connection level.")
            return [grant(None, [], Privilege.UNKNOWN, Protocol.L4, declared=True)], {}

        if inspection == Inspection.OPAQUE:
            # Before the MCP and endpoint-level branches: L7 rules under tls: skip would never be enforced.
            self._widen(need.id, "opaque", f"{raw.host} cannot be inspected; only the host and port are limited.")
            return [grant(None, [], Privilege.UNKNOWN, Protocol.L4, declared=True)], {}

        if raw.protocol == Protocol.MCP:
            endpoint_path = self._mcp_endpoint_path(need, raw.path)
            grants = [
                grant(
                    Matcher(method=m),
                    [],
                    Privilege.READ,
                    Protocol.MCP,
                    declared=False,
                    lifecycle=True,
                    endpoint_path=endpoint_path,
                )
                for m in MCP_LIFECYCLE_METHODS
            ]
            if not raw.mcp_tools:
                self._widen(need.id, "open-toolset", "No tool filter: tools/call is not allowed for any tool.")
            tools = [t for t in raw.mcp_tools if not GLOB_CHARACTERS & set(t)]
            patterns = [t for t in raw.mcp_tools if t not in tools]
            if patterns:
                shown = ", ".join(repr(t) for t in patterns)
                result.notes.append(
                    f"MCP tool names with glob characters ({shown}) are not trusted as literal names; "
                    "tools/call was not allowed for them."
                )
                self._diag("warning", "glob-in-literal", f"{need.id}: MCP tool name {shown} has glob characters.", need.id)
            by_tool: dict[str, Operation] = {}
            for svc in services:
                for op in self.graph.operations_of(svc.id):
                    if isinstance(op.selector, McpSelector):
                        by_tool.setdefault(op.selector.tool, op)
            for tool in tools:
                op = by_tool.get(tool)
                grants.append(
                    grant(
                        Matcher(method="tools/call", tool=tool),
                        [op.id] if op else [],
                        op.privilege if op else Privilege.UNKNOWN,
                        Protocol.MCP,
                        declared=op is None,
                        endpoint_path=endpoint_path,
                    )
                )
            if not any(g.operations for g in grants):
                result.status = "uncatalogued"
            return grants, {}

        if raw.method is None or raw.path is None:
            self._widen(
                need.id,
                "endpoint-level",
                f"No method or path is declared for {raw.host}; every request to it is allowed.",
            )
            if service is None:
                result.status = "uncatalogued"
            return [grant(Matcher(method="*", path="/**"), [], Privilege.UNKNOWN, Protocol.REST, declared=True)], {}

        prefixes = [p.rstrip("/") for p in need.path_prefixes]
        if raw.path_is_glob:
            if prefixes and not _within_prefixes(raw.path, prefixes):
                result.status = "unresolved"
                result.notes.append(
                    f"The declared path {raw.path} reaches beyond its url_allow_list ({', '.join(prefixes)}) "
                    "and was left out."
                )
                return [], {}
            if raw.method == "*":
                self._widen(
                    need.id, "templated-method", "The HTTP method is chosen at run time; every method is allowed."
                )
            matches = self._glob_matches(raw.host, raw.port, raw.method, raw.path)
            if not matches:
                result.status = "uncatalogued"
            privilege = _max_privilege([op.privilege for op in matches]) if matches else method_privilege(raw.method)
            bindings = _merge_bindings(
                paths.align_bindings(raw.path, op.selector.path, set(op.multi_segment), glob=True)  # type: ignore[union-attr]
                for op in matches
            )
            matcher = Matcher(method=raw.method, path=raw.path)
            return [grant(matcher, [op.id for op in matches], privilege, Protocol.REST, declared=True)], bindings

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
                grants.append(grant(Matcher(method=method, path=glob), [op.id], op.privilege, Protocol.REST, declared=False))
            bindings = _merge_bindings(
                paths.align_bindings(raw.path, op.selector.path, set(op.multi_segment))  # type: ignore[union-attr]
                for op, _ in matches
            )
            return grants, bindings

        result.status = "uncatalogued"
        glob = paths.template_to_glob(raw.path)
        if prefixes and not _within_prefixes(glob, prefixes):
            result.status = "unresolved"
            result.notes.append(f"The declared path {raw.path} is outside its own url_allow_list.")
            return [], {}
        if raw.method == "*":
            self._widen(need.id, "templated-method", "The HTTP method is chosen at run time; every method is allowed.")
        result.notes.append(
            f"{raw.host} {raw.method} {raw.path} is not in the catalog; the rule follows the declared shape "
            "and its privilege is guessed from the method."
        )
        return [grant(Matcher(method=raw.method, path=glob), [], method_privilege(raw.method), Protocol.REST, declared=True)], {}

    def _valid_raw_shape(self, need: Need, raw: RawEndpoint) -> bool:
        """Refuse a declared method or literal path that could match more than the agent declared."""
        result = self._result(need)
        problems: list[str] = []
        if raw.protocol != Protocol.MCP and raw.method is not None and raw.method != "*" and raw.method not in HTTP_METHODS:
            problems.append(
                f"The declared method {raw.method!r} is not a standard HTTP method "
                f"({', '.join(sorted(HTTP_METHODS))}); nothing was granted."
            )
            self._diag(
                "warning", "invalid-method", f"{need.id}: method {raw.method!r} is not a standard HTTP method.", need.id
            )
        if raw.path is not None and not raw.path_is_glob and GLOB_CHARACTERS & set(raw.path):
            problems.append(
                f"The declared path {raw.path} contains glob characters (* ? [ ]). Glob characters in a "
                "declared literal path are not trusted, so nothing was granted."
            )
            self._diag(
                "warning", "glob-in-literal", f"{need.id}: the declared path {raw.path} has glob characters.", need.id
            )
        if not problems:
            return True
        result.status = "unresolved"
        result.notes.extend(problems)
        return False

    def _mcp_endpoint_path(self, need: Need, path: str | None) -> str:
        """The OpenShell endpoint path for a declared MCP server URL path. Empty covers the whole host."""
        if not path:
            return ""
        if path.endswith("/sse"):
            covered = path[: -len("/sse")] + "/**"
            self._widen(
                need.id,
                "mcp-sse-path",
                f"With the SSE transport the server names its message path at run time, so the MCP endpoint "
                f"covers {covered} instead of {path}.",
            )
            return covered
        return path

    def _check_origin(self, need: Need, raw: RawEndpoint) -> None:
        """Warn when a need reaches a catalogued host on a port or scheme the catalog does not list."""
        host_services = self._services_for_host(raw.host)
        if not host_services:
            return
        if any(
            origin.port == raw.port and origin.scheme == raw.scheme and paths.host_matches(origin.host, raw.host)
            for service in host_services
            for origin in service.origins
        ):
            return
        ids = ", ".join(s.id for s in host_services)
        providers = ", ".join(sorted({s.provider for s in host_services}))
        self._diag(
            "warning",
            "uncatalogued-port",
            f"{need.id}: {raw.scheme}://{raw.host}:{raw.port} is not a catalogued origin of {ids}. The catalog's "
            f"operations do not describe it, and prohibitions on {providers} still apply to it.",
            need.id,
        )

    def _services_for_host(self, host: str) -> list[Service]:
        """Services with an origin on ``host`` at any port. Prohibitions follow the host, not the port."""
        return [
            service
            for service in self.graph.services.values()
            if any(paths.host_matches(o.host, host) or paths.host_matches(host, o.host) for o in service.origins)
        ]

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
        *,
        require_bound: bool,
    ) -> list[Grant]:
        """Add the operations special-purpose clients perform implicitly, such as git-upload-pack.

        With ``require_bound``, an implicit operation is added only when ``scope`` fixes every parameter of
        its path, so that a need for one repository never grants git access to every repository.
        """
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
        skipped: dict[str, list[str]] = {}
        for path in clients:
            for client in self.graph.clients_for_path(path):
                for use in client.uses:
                    if use.when_capability and not set(use.when_capability) & closure:
                        continue
                    for op_id in use.operations:
                        op = self.graph.operations[op_id]
                        if target.provider and self.graph.services[op.service].provider != target.provider:
                            continue
                        missing = _template_params(op) - set(scope)
                        if require_bound and missing:
                            skipped.setdefault(path, []).append(f"{op.id} (needs {', '.join(sorted(missing))})")
                            continue
                        extra.extend(self._grants_for_operation(need, op, (path,), scope))
        if extra:
            self._result(need).notes.append(
                "Added the implicit operations of " + ", ".join(sorted({c for g in extra for c in g.clients})) + "."
            )
        for path, ops in skipped.items():
            self._result(need).notes.append(
                f"Left out the implicit operations of {path} because the need does not fix their parameters: "
                + ", ".join(_unique(ops))
                + "."
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
        dropped: list[str] = []
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
                dropped.append(g.need)
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
        for need_id in _unique(dropped):
            result = self.results.get(need_id)
            if result is None or any(g.need == need_id for g in routed):
                continue
            if result.status in ("resolved", "uncatalogued"):
                result.status = "unresolved"
            result.notes.append(
                "No model endpoint of this need has an inference.local equivalent, so nothing was granted; "
                "use a model API that inference.local serves, or compile without --route-inference-local."
            )
        return routed

    def _note_uncatalogued_models(self) -> None:
        """Point uncatalogued model endpoints at OpenShell's managed inference route."""
        for need in self.contract.needs:
            result = self.results.get(need.id)
            if result is None or not _declared_by_llm_config(need) or not result.status.endswith("uncatalogued"):
                continue
            host = _normal_host(need.target.raw.host) if need.target.raw is not None else "the model endpoint"
            if self.options.route_inference_local:
                result.notes.append(
                    f"The model endpoint {host} is not in the catalog, so it was not routed to inference.local; "
                    "its host is allowed directly."
                )
            else:
                result.notes.append(
                    f"The model endpoint {host} is not in the catalog. Consider --route-inference-local to send "
                    f"model traffic through OpenShell's inference.local route instead of allowing {host} directly."
                )

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
        """Tag grants with the use-case concepts that justify them.

        A grant is justified only when every operation it serves is. MCP lifecycle grants follow the
        tools/call grants of their need. With ``strict_use_case`` an unjustified grant is excluded.
        """
        text = self.options.use_case if self.options.use_case is not None else self.contract.use_case
        if not text or self.index is None:
            return []
        self.justified = True
        from policy_compiler.semantic import find_write_verb_uses

        uses = find_write_verb_uses(text)
        asked = sorted({use.verb for use in uses if not use.negated})
        # Base forms let "pushes" match the keyword "push", as in the use-case front end.
        matches = self.index.match(" ".join([text, *asked]), min_score=self.options.min_justification_score)
        concepts = self._requested_concepts(matches, uses)
        allowed = set(concepts) | ALWAYS_JUSTIFIED_CONCEPTS
        unjustified: dict[str, list[str]] = {}
        failed: set[int] = set()
        # The agent needs the model its own configuration names, whatever the use case says.
        model_needs = {n.id for n in self.contract.needs if _declared_by_llm_config(n)}
        for g in grants:
            if g.lifecycle:
                continue
            per_op = {op: set(self._concepts_for(op)) & allowed for op in g.operations}
            g.justified_by = sorted({c for cs in per_op.values() for c in cs})
            missing = [op for op, cs in per_op.items() if not cs] if per_op else [g.host]
            if missing and g.need in model_needs:
                g.justified_by = sorted({*g.justified_by, LLM_CONFIG_JUSTIFICATION})
                missing = []
            if missing:
                failed.add(id(g))
                unjustified.setdefault(g.need, []).extend(missing)
        action = "excluded" if self.options.strict_use_case else "kept"
        for need_id, result in self.results.items():
            result.justified_by = sorted({c for g in grants if g.need == need_id for c in g.justified_by})
            if need_id in unjustified:
                result.unjustified = _unique(unjustified[need_id])
                result.unjustified_action = action
                what = ", ".join(result.unjustified)
                if self.options.strict_use_case:
                    result.notes.append(f"Excluded because the use case does not justify: {what}.")
                else:
                    result.notes.append(f"Not justified by the use case (kept): {what}.")
        if self.options.strict_use_case:
            kept = _drop_orphan_lifecycle([g for g in grants if id(g) not in failed], grants)
            for need_id in unjustified:
                if not any(g.need == need_id for g in kept):
                    self.results[need_id].status = "excluded"
            grants[:] = kept
        return concepts

    def _requested_concepts(self, matches: list, uses: list) -> list[str]:
        """Keep a write-level concept only when a requested (not negated) write verb asks for it.

        This is the use-case front end's rule: the verb is one of the concept's action verbs and the words
        after it in its clause include another of the concept's keywords. "Never change the repository"
        mentions a write verb but asks for the opposite; "open the code" opens nothing an issue concept owns.
        """
        requests = [use for use in uses if not use.negated]
        kept: list[str] = []
        dropped: list[str] = []
        for m in matches:
            concept = self.graph.concepts.get(m.concept)
            write_level = concept is not None and concept.privilege.at_least(Privilege.WRITE)
            if write_level and not any(self._requests(use, m.concept) for use in requests):
                dropped.append(m.concept)
                continue
            kept.append(m.concept)
        if dropped:
            note = "The use case does not request write access for these concepts, so they justify nothing: " + ", ".join(
                dropped
            )
            negated = [f"{use.verb} {' '.join(use.objects)}".strip() for use in uses if use.negated]
            if negated:
                note += f" (negated: {'; '.join(dict.fromkeys(negated))})"
            self._diag("info", "write-concept-not-requested", note + ".")
        return kept

    def _requests(self, use, concept_id: str) -> bool:
        """Whether one requested write verb asks for a concept: its verb and its object both fit."""
        assert self.index is not None
        keywords = self.index.keywords(concept_id)
        return use.verb in self._action_verbs(concept_id) and bool((keywords - {use.verb}) & set(use.objects))

    def _action_verbs(self, concept_id: str) -> set[str]:
        """Write verbs that ask for a concept: its write-verb keywords and its write operations' leading verbs."""
        from policy_compiler.semantic import WRITE_VERBS

        assert self.index is not None
        verbs = {term for term in self.index.keywords(concept_id) if term in WRITE_VERBS}
        capabilities = self.graph.capabilities_for_concept(concept_id)
        for operation in self.graph.operations_enabled_by(capabilities):
            leading = operation.summary.split(maxsplit=1)[:1]
            if operation.privilege.at_least(Privilege.WRITE) and leading and leading[0].lower() in WRITE_VERBS:
                verbs.add(leading[0].lower())
        return verbs

    # ------------------------------------------------------------ prohibitions

    def _operations_for_prohibition(self, prohibition: Prohibition) -> set[str]:
        """Catalogued operations a prohibition forbids.

        A capability or concept prohibition forbids every operation that has a requirement group with a
        capability whose closure reaches a banned capability: forbidding ``contents:read`` also forbids
        writes (``contents:write`` implies it), and forbidding ``contents:write`` leaves reads alone.
        """
        graph = self.graph
        found: set[str] = set()
        if prohibition.provider is not None:
            floor = prohibition.min_privilege or Privilege.READ
            for op in graph.operations.values():
                if graph.services[op.service].provider == prohibition.provider and _floor_privilege(op).at_least(floor):
                    found.add(op.id)
        target = prohibition.target
        if target is None:
            return found
        if target.raw is not None:
            # A host prohibition holds on every port the host serves.
            for service in self._services_for_host(_normal_host(target.raw.host)):
                for op in graph.operations_of(service.id):
                    found.add(op.id)
            return found
        if target.operation is not None:
            banned_ops = {target.operation} & set(graph.operations)
            if not banned_ops:
                self._unknown_prohibition(prohibition, f"operation {target.operation}")
            return found | banned_ops
        if target.capability is not None:
            if target.capability not in graph.capabilities:
                self._unknown_prohibition(prohibition, f"capability {target.capability}")
            banned = {target.capability}
        else:
            if (target.concept or "") not in graph.concepts:
                self._unknown_prohibition(prohibition, f"concept {target.concept}")
            banned = set(graph.capabilities_for_concept(target.concept or "", target.provider))
        closures: dict[str, set[str]] = {}
        for op in graph.operations.values():
            for group in op.requires:
                if any(closures.setdefault(c, graph.capability_closure([c])) & banned for c in group):
                    found.add(op.id)
                    break
        return found

    def _unknown_prohibition(self, prohibition: Prohibition, what: str) -> None:
        self._diag(
            "warning",
            "unknown-prohibition-target",
            f"{prohibition.id}: {what} is not in the catalog, so the prohibition forbids nothing.",
            prohibition.id,
        )

    def _apply_prohibitions(self, grants: list[Grant], forbidden: set[str]) -> list[Grant]:
        raw_hosts = {
            _normal_host(p.target.raw.host)
            for p in self.contract.prohibitions
            if p.target is not None and p.target.raw is not None
        }
        floors: dict[str, tuple[Privilege, str]] = {}
        """Provider -> its lowest prohibited level and the prohibition that sets it."""
        for p in self.contract.prohibitions:
            if p.provider is None:
                continue
            floor = p.min_privilege or Privilege.READ
            if p.provider not in floors or floor.rank < floors[p.provider][0].rank:
                floors[p.provider] = (floor, p.id)
        kept: list[Grant] = []
        for g in grants:
            blocked_ops = [op for op in g.operations if op in forbidden]
            host = _normal_host(g.host)
            on_raw_host = any(paths.host_matches(h, host) or paths.host_matches(host, h) for h in raw_hosts)
            if on_raw_host or (g.operations and len(blocked_ops) == len(g.operations)):
                self._conflict(g, ", ".join(blocked_ops) or f"{g.host}:{g.port}")
                continue
            floor = self._provider_floor(g, floors)
            if floor is not None and not g.lifecycle:
                narrowed = self._apply_provider_floor(g, *floor)
                if narrowed is None:
                    continue
                g = narrowed
            kept.append(g)
        return _drop_orphan_lifecycle(kept, grants)

    def _provider_floor(self, g: Grant, floors: dict[str, tuple[Privilege, str]]) -> tuple[Privilege, str] | None:
        """The lowest prohibited level among the providers that serve the grant's host, on any port."""
        services = self._services_for_host(_normal_host(g.host))
        found = [floors[s.provider] for s in services if s.provider in floors]
        return min(found, key=lambda f: f[0].rank) if found else None

    def _apply_provider_floor(self, g: Grant, floor: Privilege, prohibition: str) -> Grant | None:
        """Keep a grant on a prohibited provider's host only when it cannot reach the prohibited level.

        Catalogued operations are checked by name (and denied under broader rules). What the catalog cannot
        name fails closed: a connection-level grant is removed, an any-method rule is narrowed to GET (or
        removed when reads are prohibited too), and a declared or uncatalogued rule is removed when its
        method's guessed privilege reaches the prohibited level.
        """
        where = f"{g.host}:{g.port}"
        rule = f"{prohibition} forbids this provider at {floor.value} level or above"
        if g.matcher is None or g.protocol in (Protocol.L4, Protocol.TCP) or g.inspection == Inspection.OPAQUE:
            self._conflict(g, f"connection-level access to {where} ({rule})")
            return None
        matcher = g.matcher
        if matcher.method == "*" and g.protocol == Protocol.REST:
            if not floor.rank > Privilege.READ.rank:
                self._conflict(g, f"every method on {where} {matcher.path} ({rule})")
                return None
            operations = [op for op in g.operations if _get_reaches(self.graph.operations[op])]
            g = replace(
                g,
                matcher=Matcher(method="GET", path=matcher.path),
                operations=operations,
                privilege=_max_privilege(self.graph.operations[op].privilege for op in operations)
                if operations
                else Privilege.READ,
            )
            detail = f"* {matcher.path} on {where} narrowed to GET by prohibition {prohibition}."
            self._widen(g.need, "narrowed-by-prohibition", detail)
            self.results[g.need].notes.append(f"The rule {detail[:-1]}: other methods are prohibited there.")
            matcher = g.matcher
        what = f"{matcher.method} {matcher.path or matcher.tool or ''}".strip()
        if g.protocol == Protocol.REST and matcher.method:
            effective = self._effective_privilege(g, matcher.method)
            if effective is not None and effective.at_least(floor):
                self._conflict(
                    g, f"{what} on {where}, whose privilege ({effective.value}) is at least the method's ({rule})"
                )
                return None
        elif (g.declared or not g.operations) and method_privilege(matcher.method or "").at_least(floor):
            self._conflict(g, f"{what} on {where}, whose privilege is unknown or guessed from the method ({rule})")
            return None
        return g

    def _effective_privilege(self, g: Grant, method: str) -> Privilege | None:
        """A REST grant's privilege under a provider floor: never below what its HTTP method suggests.

        The method counts when the grant has no catalogued operation or follows the declared shape, and it
        raises the stated privilege of an operation whose label is untrusted (agent layer) or describes a
        different method (a ``*`` selector). Curated base and org operations keep their privilege, such as
        GitHub's read-level POST git-upload-pack: they are matched by name in the forbidden set and denied
        under broader rules. ``None`` means nothing here depends on the method.
        """
        guessed = method_privilege(method)
        # Requests the catalog does not describe: only the method tells their privilege.
        levels: list[Privilege] = [guessed] if g.declared or not g.operations else []
        for op_id in g.operations:
            op = self.graph.operations[op_id]
            selector = op.selector
            untrusted = op.layer is Layer.AGENT
            other_method = not isinstance(selector, RestSelector) or not paths.method_covers(selector.method, method)
            if untrusted or other_method or selector.method == "*":
                levels += [op.privilege, guessed]
        return _max_privilege(levels) if levels else None

    def _conflict(self, g: Grant, what: str) -> None:
        result = self.results[g.need]
        result.status = "conflict"
        result.notes.append(f"Removed because a prohibition forbids: {what}.")
        self._diag("warning", "prohibited", f"{g.need}: a prohibition removed {what}.", g.need)

    # ------------------------------------------------------------ boundary

    def _boundary_grants(self, grants: list[Grant], forbidden: set[str]) -> list[Grant]:
        """The policy's own grants, plus every operation their capabilities enable, without scope.

        Every grant stays in the boundary, so the policy always lies within it. Special-purpose clients
        (git) widen only to their own implicit operations; general-purpose clients never receive those.
        """
        boundary: list[Grant] = list(grants)
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for g in grants:
            if not g.operations:
                continue
            caps: set[str] = set()
            for op_id in g.operations:
                caps.update(_cheapest_requirement(self.graph, self.graph.operations[op_id]))
            ops = list(self.graph.operations_enabled_by(caps)) if caps else []
            ops += [self.graph.operations[o] for o in g.operations]
            special, generic = self._split_clients(g.clients)
            for clients, implicit_only in ((generic, False), (special, True)):
                if not clients:
                    continue
                for op in ops:
                    if op.id in forbidden or (op.id, clients) in seen:
                        continue
                    owners = self.client_specific.get(op.id)
                    if implicit_only and not (owners and owners & set(clients)):
                        continue
                    if not implicit_only and owners:
                        continue
                    seen.add((op.id, clients))
                    stub = _need_stub(g.need)
                    boundary.extend(self._grants_for_operation(stub, op, clients, {}, None))
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
            path=g.endpoint_path if g.matcher is not None else "",
        )

    def _deny_rules(self, rules: list[AccessRule], forbidden: set[str]) -> list[AccessRule]:
        """Deny forbidden operations that a broader allow on the same endpoint would admit."""
        denies: list[AccessRule] = []
        seen: set[tuple] = set()
        for rule in rules:
            if rule.matcher is None or rule.matcher.path is None or rule.matcher.method is None:
                continue
            for service in self._services_for_host(_normal_host(rule.endpoint.host)):
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

    def _check_allowed_ips(self, access: AccessSet, boundary: AccessSet) -> None:
        """Warn about allowed_ips ranges so broad that the endpoint can reach almost any address."""
        where: dict[tuple[str, str], list[str]] = {}
        for label, rules in (("policy", access.rules), ("boundary", boundary.rules)):
            for rule in rules:
                if rule.effect != Effect.ALLOW:
                    continue
                for cidr in rule.endpoint.allowed_ips:
                    labels = where.setdefault((rule.endpoint.host, cidr), [])
                    if label not in labels:
                        labels.append(label)
        for (host, cidr), labels in where.items():
            problem = _broad_cidr(cidr)
            if problem is None:
                continue
            self._diag(
                "warning",
                "broad-allowed-ips",
                f"{host or 'A hostless endpoint'}: allowed_ips {cidr} {problem} (in the {' and '.join(labels)}). "
                "Such a range lets the endpoint reach almost any address; describe the private range exactly.",
            )

    # ------------------------------------------------------------ helpers

    def _diag(self, level: str, code: str, message: str, pointer: str = "") -> None:
        self.diagnostics.append(Diagnostic(level=level, code=code, message=message, pointer=pointer))

    def _widen(self, need: str, reason: str, detail: str) -> None:
        self.widenings.append(Widening(need=need, reason=reason, detail=detail))


def _normal_host(host: str) -> str:
    """Lower-case a host name and drop a trailing dot, so ``API.github.com.`` matches ``api.github.com``."""
    return host.rstrip(".").lower()


def _uses_mtls(need: Need) -> bool:
    return need.credential is not None and need.credential.placement == "mtls"


def _declared_by_llm_config(need: Need) -> bool:
    """Whether an agent's model configuration (``llm_config``) declared the need."""
    return any("llm_config" in evidence.pointer for evidence in need.evidence)


def _floor_privilege(op: Operation) -> Privilege:
    """An operation's privilege for a provider prohibition.

    An agent-layer label is untrusted, so a concrete HTTP method raises it (a ``DELETE`` labelled read counts
    as a write). ``*`` selectors are left to the grant-level check, which knows the declared method.
    """
    selector = op.selector
    if op.layer is Layer.AGENT and isinstance(selector, RestSelector) and selector.method != "*":
        return _max_privilege([op.privilege, method_privilege(selector.method)])
    return op.privilege


def _broad_cidr(cidr: str) -> str | None:
    """Why an allowed_ips entry is too broad, or ``None``."""
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return "is not a valid CIDR range"
    if network.prefixlen < BROAD_PREFIX:
        return f"has a /{network.prefixlen} prefix, shorter than /{BROAD_PREFIX}"
    return None


def _need_stub(need_id: str) -> Need:
    return Need(id=need_id, target=Target(operation="-"), rationale="")


def _valid_scope_value(value: object) -> bool:
    return isinstance(value, str) and bool(_SCOPE_VALUE.match(value)) and value not in (".", "..")


def _template_params(op: Operation) -> set[str]:
    if isinstance(op.selector, RestSelector):
        return set(re.findall(r"\{([^{}/]+)\}", op.selector.path))
    return set()


def _merge_bindings(items: Iterable[dict[str, str] | None]) -> dict[str, str]:
    """Combine the parameters each matched operation binds; drop parameters bound inconsistently or unsafely."""
    merged: dict[str, str] = {}
    conflicting: set[str] = set()
    for bindings in items:
        for key, value in (bindings or {}).items():
            if key in merged and merged[key] != value:
                conflicting.add(key)
            merged.setdefault(key, value)
    return {k: v for k, v in merged.items() if k not in conflicting and _valid_scope_value(v)}


def _get_reaches(op: Operation) -> bool:
    """Whether a GET rule can reach the operation."""
    selector = op.selector
    return isinstance(selector, RestSelector) and (selector.method == "*" or paths.method_covers("GET", selector.method))


def _drop_orphan_lifecycle(kept: list[Grant], original: list[Grant]) -> list[Grant]:
    """Drop MCP lifecycle grants whose need lost every tools/call grant on that endpoint and client set."""

    def key(g: Grant) -> tuple:
        return (g.need, g.host, g.port, g.endpoint_path, g.clients)

    had = {key(g) for g in original if g.protocol == Protocol.MCP and not g.lifecycle}
    has = {key(g) for g in kept if g.protocol == Protocol.MCP and not g.lifecycle}
    return [g for g in kept if not g.lifecycle or key(g) in has or key(g) not in had]


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

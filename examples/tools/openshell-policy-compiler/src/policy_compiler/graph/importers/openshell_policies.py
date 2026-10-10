# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Mine existing OpenShell policies into the capability graph.

Hand-written policies record which hosts, paths, and binaries real agents use,
but they are often broader than necessary. Mining therefore seeds *discovery*,
not sizing:

* A host the catalog already serves produces only usage statistics.
* An unknown host becomes a ``mined.*`` service with one operation per allow
  rule. Paths become templates (``*`` -> ``{pN}``, ``**`` -> ``{rest}``), the
  privilege is guessed from the method and labelled inferred, and ``requires``
  is empty, so these operations are reachable only by a direct match, never by
  a capability or concept.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from policy_compiler.backends.openshell import lift
from policy_compiler.graph.model import (
    CatalogDocument,
    Client,
    McpSelector,
    Operation,
    Origin,
    Provider,
    RestSelector,
    Service,
)
from policy_compiler.graph.store import Graph
from policy_compiler.ir.access import Effect
from policy_compiler.ir.common import Evidence, EvidenceKind, Layer, Privilege, Protocol, method_privilege

PROVIDER_ID = "mined"
_PRESET_PRIVILEGE = {"read-only": Privilege.READ, "read-write": Privilege.WRITE, "full": Privilege.UNKNOWN}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "root"


def glob_to_template(glob: str) -> tuple[str, list[str]]:
    """Convert an OpenShell path glob into a template plus its multi-segment parameters."""
    segments = glob.split("?", 1)[0].strip("/").split("/") if glob.strip("/") else []
    out: list[str] = []
    multi: list[str] = []
    counter = 0
    for segment in segments:
        if segment == "**":
            name = "rest" if "rest" not in multi else f"rest{len(multi) + 1}"
            multi.append(name)
            out.append("{" + name + "}")
            continue

        def replace(_: re.Match) -> str:
            nonlocal counter
            counter += 1
            return "{p" + str(counter) + "}"

        out.append(re.sub(r"\*+", replace, segment))
    return "/" + "/".join(out), multi


def import_openshell_policies(
    policies: list[tuple[str, dict]],
    *,
    known: Graph | None,
    layer: Layer = Layer.ORG,
) -> tuple[CatalogDocument, dict[str, dict[str, Any]]]:
    """Return a catalog document for hosts the graph does not know, plus a usage index for every host."""
    doc = CatalogDocument(source="mined")
    services: dict[str, Service] = {}
    service_ids: dict[str, str] = {}
    """Lower-cased host -> service ID. Different hosts never share an ID, even when their slugs collide."""
    operations: dict[tuple[str, str, str], Operation] = {}
    used_ids: set[str] = set()
    clients: dict[str, Client] = {}
    usage: dict[str, dict[str, Any]] = {}

    for source, policy in policies:
        access = lift(policy, source=source)
        for rule in access.rules:
            if rule.effect != Effect.ALLOW:
                continue
            endpoint = rule.endpoint
            host = endpoint.host or ",".join(endpoint.allowed_ips) or "(any host)"
            ports = [p for p in endpoint.ports if isinstance(p, int)]
            catalogued = bool(
                known is not None and endpoint.host and any(known.services_for(endpoint.host, p) for p in ports or [443])
            )
            entry = usage.setdefault(
                host, {"policies": [], "methods": [], "binaries": [], "catalogued": catalogued}
            )
            if source not in entry["policies"]:
                entry["policies"].append(source)
            method = rule.matcher.method if rule.matcher and rule.matcher.method else None
            if method is None and rule.access_preset:
                method = f"access:{rule.access_preset}"
            if method and method not in entry["methods"]:
                entry["methods"].append(method)
            for path in rule.clients.paths:
                if path not in entry["binaries"]:
                    entry["binaries"].append(path)
                if known is not None and known.clients_for_path(path):
                    continue
                if any(ch in path for ch in "*?[") or path in clients:
                    continue
                base = f"{PROVIDER_ID}.client.{slug(os.path.basename(path))}"
                client_id = base
                n = 2
                while any(c.id == client_id for c in clients.values()):
                    client_id = f"{base}-{n}"
                    n += 1
                clients[path] = Client(
                    id=client_id,
                    paths=[path],
                    description=f"Binary seen in {source}.",
                    layer=layer,
                    evidence=[Evidence(EvidenceKind.LIFTED, source, rule.reasons[0] if rule.reasons else "")],
                )

            if catalogued or not endpoint.host or "*" in endpoint.host:
                continue
            service_id = _service_id(endpoint.host, service_ids)
            evidence = Evidence(
                EvidenceKind.LIFTED, source, rule.reasons[0].split("#", 1)[-1] if rule.reasons else ""
            )
            service = services.get(service_id)
            if service is None:
                protocol = endpoint.protocol if endpoint.protocol != Protocol.TCP else Protocol.L4
                service = Service(
                    id=service_id,
                    provider=PROVIDER_ID,
                    name=endpoint.host,
                    origins=[],
                    protocol=protocol,
                    inspection=endpoint.inspection,
                    allowed_ips=list(endpoint.allowed_ips),
                    description="Mined from existing OpenShell policies.",
                    layer=layer,
                    evidence=[evidence],
                )
                services[service_id] = service
            elif evidence not in service.evidence:
                service.evidence.append(evidence)
            for port in ports or [443]:
                origin = Origin(host=endpoint.host, port=port)
                if origin not in service.origins:
                    service.origins.append(origin)

            op = _mined_operation(rule, service_id, evidence, layer, used_ids)
            if op is None:
                continue
            key = (service_id, *(_selector_key(op)))
            existing = operations.get(key)
            if existing is None:
                operations[key] = op
                used_ids.add(op.id)
            elif evidence not in existing.evidence:
                existing.evidence.append(evidence)

    if services:
        doc.providers.append(
            Provider(
                id=PROVIDER_ID,
                name="Mined from existing OpenShell policies",
                description="Hosts and rules found in hand-written policies. Privileges are guessed.",
                layer=layer,
            )
        )
    doc.services = list(services.values())
    doc.operations = list(operations.values())
    doc.clients = list(clients.values())
    return doc, usage


def _service_id(host: str, service_ids: dict[str, str]) -> str:
    """A service ID for ``host``: its slug, plus a hash of the host when another host already has that slug."""
    key = host.lower().rstrip(".")
    if key in service_ids:
        return service_ids[key]
    taken = set(service_ids.values())
    candidate = f"{PROVIDER_ID}.{slug(key)}"
    if candidate in taken:
        candidate = f"{candidate}-{hashlib.sha256(key.encode()).hexdigest()[:10]}"
        n = 2
        while candidate in taken:
            candidate = f"{candidate}-{n}"
            n += 1
    service_ids[key] = candidate
    return candidate


def _selector_key(op: Operation) -> tuple[str, str]:
    selector = op.selector
    if isinstance(selector, RestSelector):
        return (selector.method, selector.path)
    if isinstance(selector, McpSelector):
        return ("tools/call", selector.tool)
    return ("?", op.id)


def _mined_operation(rule, service_id: str, evidence: Evidence, layer: Layer, used: set[str]) -> Operation | None:
    matcher = rule.matcher
    if matcher is None:
        if not rule.access_preset:
            return None  # connection-level grant: the service itself records it
        selector = RestSelector(method="*", path="/{rest}")
        privilege = _PRESET_PRIVILEGE.get(rule.access_preset, Privilege.UNKNOWN)
        multi = ["rest"]
        summary = f"Access preset {rule.access_preset}."
    elif matcher.method == "tools/call" and matcher.tool:
        selector = McpSelector(tool=matcher.tool)
        privilege = Privilege.UNKNOWN
        multi = []
        summary = f"MCP tool {matcher.tool}."
    elif matcher.method and matcher.path:
        template, multi = glob_to_template(matcher.path)
        selector = RestSelector(method=matcher.method.upper(), path=template)
        privilege = method_privilege(matcher.method) if matcher.method != "*" else Privilege.UNKNOWN
        summary = f"{matcher.method.upper()} {matcher.path}"
    else:
        return None
    if isinstance(selector, RestSelector):
        base = f"{service_id}.{slug(selector.method if selector.method != '*' else 'any')}-{slug(selector.path)}"
    else:
        base = f"{service_id}.tool-{slug(selector.tool)}"
    op_id, n = base, 2
    while op_id in used:
        op_id = f"{base}-{n}"
        n += 1
    return Operation(
        id=op_id,
        service=service_id,
        selector=selector,
        privilege=privilege,
        requires=[],
        multi_segment=multi,
        summary=summary,
        tags=["mined"],
        layer=layer,
        evidence=[Evidence(evidence.kind, evidence.source, evidence.pointer, note="privilege guessed from the method", confidence=0.5)],
    )

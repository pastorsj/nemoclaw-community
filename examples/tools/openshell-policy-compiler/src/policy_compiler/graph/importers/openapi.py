# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Import an OpenAPI 3.x description, optionally with an Overlay, into a catalog document.

This is how you add an API the base catalog does not know, such as a private
internal service. The importer reads the standard parts of the description
(servers, paths, operations, OAuth 2.0 and OpenID Connect scopes) and this
tool's own annotations, which you can add with an OpenAPI Overlay instead of
editing the spec:

``x-openshell-privilege`` (operation)
    ``read``, ``write``, or ``admin``. Overrides every other source of privilege.
``x-openshell-capability`` (operation)
    A capability ID, or a list of IDs any one of which suffices. The provider
    prefix is added when missing, and so is the ``:<level>`` suffix (the
    operation's ``x-openshell-privilege``, or else the HTTP method's level).
``x-openshell-scope-levels`` (root)
    A mapping from OAuth scope to privilege level.
``x-openshell-allowed-ips`` (root or server)
    CIDR ranges the service resolves to, for private networks.
``x-openshell-inspection`` (root or server)
    ``l7`` (default) or ``opaque`` for mTLS or pinned certificates.
``x-openshell-multi-segment`` (operation)
    Path parameters whose values may contain ``/``. Parameters with
    ``x-multi-segment: true`` or ``allowReserved: true`` count as well.
``x-openshell-concepts`` (operation or root)
    Concept IDs that the operation's capabilities realize.

Where the description says nothing about privilege, the level is guessed from
the HTTP method and labelled inferred, and a diagnostic counts those guesses.
"""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import yaml

from policy_compiler.graph.importers.overlay import apply_overlay
from policy_compiler.graph.model import (
    Capability,
    CatalogDocument,
    ConceptLink,
    Operation,
    Origin,
    Provider,
    RestSelector,
    Service,
)
from policy_compiler.ir.access import Diagnostic
from policy_compiler.ir.common import Evidence, EvidenceKind, Inspection, Layer, Privilege, method_privilege

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_DEFAULT_PORTS = {"https": 443, "http": 80}
_LEVELS = {"read": Privilege.READ, "write": Privilege.WRITE, "admin": Privilege.ADMIN}
_OAUTH_TYPES = {"oauth2", "openIdConnect"}
_INFERRED_METHOD_CONFIDENCE = 0.5
_INFERRED_SCOPE_CONFIDENCE = 0.6
_ADMIN_STEMS = ("admin", "delet", "manag")
"""Matched as substrings: admin, administration, delete, deletion, manage, management."""
_READ_WORDS = {"read", "readonly", "view"}
_WRITE_WORDS = {"write", "readwrite"}
_TEMPLATE_PARAM = re.compile(r"\{([^{}/]+)\}")
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def slug(text: str) -> str:
    """Lower-case ``text`` and replace each run of non-alphanumeric characters with ``-``."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_spec_file(path: Path) -> dict:
    """Read an OpenAPI description or overlay from a YAML or JSON file."""
    text = Path(path).read_text(encoding="utf-8")
    # YAML is a superset of JSON, but the json module is much faster on large specs.
    data = json.loads(text) if Path(path).suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


def import_openapi(
    spec: dict,
    *,
    provider_id: str,
    source: str,
    layer: Layer,
    overlay: dict | None = None,
    server_url: str | None = None,
    provider_name: str | None = None,
) -> tuple[CatalogDocument, list[Diagnostic]]:
    """Convert an OpenAPI description into a catalog document for ``provider_id``.

    ``server_url`` replaces an absolute ``servers[0].url``. A relative server
    URL is resolved against it, the way OpenAPI resolves relative server URLs
    against the document's location. When the spec has no server URL at all,
    ``server_url`` is used as given, including its path. Without ``server_url``
    a relative or missing server URL is an error, and no service or operations
    are produced. Path- and operation-level ``servers`` are ignored with a
    warning, because every operation is placed on the root server.

    Problems in the description are returned as diagnostics. An invalid
    ``provider_id`` or a malformed overlay raises ``ValueError``.
    """
    if not _PROVIDER_ID.match(provider_id):
        raise ValueError(f"provider ID must be lower-case letters, digits, '-' or '_': {provider_id!r}")
    diagnostics: list[Diagnostic] = []
    if "swagger" in spec and "openapi" not in spec:
        diagnostics.append(
            Diagnostic(
                "error",
                "unsupported-openapi-version",
                f"Swagger {spec['swagger']} is not supported; convert the description to OpenAPI 3",
                "swagger",
            )
        )
        return CatalogDocument(source=source), diagnostics
    if overlay is not None:
        spec = apply_overlay(spec, overlay, diagnostics=diagnostics)
    importer = _OpenApiImporter(spec, provider_id, source, layer, diagnostics)
    return importer.run(server_url, provider_name), diagnostics


class _OpenApiImporter:
    def __init__(self, spec: dict, provider: str, source: str, layer: Layer, diagnostics: list[Diagnostic]):
        self.spec = spec
        self.provider = provider
        self.source = source
        self.layer = layer
        self.diagnostics = diagnostics
        self.capabilities: dict[str, Capability] = {}
        self.documented: set[str] = set()
        """Capability IDs whose level the API owner or an overlay stated."""
        self.unknown_schemes: set[str] = set()
        self.operations: list[Operation] = []
        self.links: dict[str, ConceptLink] = {}
        self.schemes = {
            name: self._deref(scheme)
            for name, scheme in _dict(_dict(self.spec.get("components")).get("securitySchemes")).items()
        }
        self.scope_descriptions = self._scope_descriptions()
        self.scope_levels = self._scope_levels()
        self.inferred_count = 0

    # --- top level ------------------------------------------------------------------

    def run(self, server_url: str | None, provider_name: str | None) -> CatalogDocument:
        info = _dict(self.spec.get("info"))
        title = str(info.get("title") or "").strip()
        server = self._server(server_url)
        if server is None:
            return CatalogDocument(source=self.source)
        origin, base_path, server_object, server_evidence = server
        provider = Provider(
            id=self.provider,
            name=provider_name or title or self.provider,
            layer=self.layer,
            evidence=[self._evidence(EvidenceKind.DOCUMENTED, "info")],
        )
        service = Service(
            id=f"{self.provider}.{slug(origin.host)}",
            provider=self.provider,
            name=title or origin.host,
            origins=[origin],
            inspection=self._inspection(server_object),
            allowed_ips=self._allowed_ips(server_object),
            description=str(server_object.get("description") or "").strip(),
            layer=self.layer,
            evidence=[server_evidence],
        )
        used_ids: set[str] = set()
        for path, item in _dict(self.spec.get("paths")).items():
            item = self._deref(item)
            if not isinstance(item, dict):
                continue
            self._ignore_servers(item, f"paths.{path}", origin)
            for method, operation in item.items():
                if str(method).lower() in HTTP_METHODS and isinstance(operation, dict):
                    self._ignore_servers(operation, f"paths.{path}.{method}", origin)
                    self._operation(service.id, base_path, str(path), str(method).lower(), item, operation, used_ids)
        if self.inferred_count:
            self.diagnostics.append(
                Diagnostic(
                    "info",
                    "inferred-privilege",
                    f"{self.inferred_count} of {len(self.operations)} operations have an inferred privilege level; "
                    "review them, or set x-openshell-privilege or x-openshell-scope-levels with an overlay",
                )
            )
        return CatalogDocument(
            providers=[provider],
            services=[service],
            capabilities=list(self.capabilities.values()),
            operations=self.operations,
            concept_links=list(self.links.values()),
            source=self.source,
        )

    # --- servers --------------------------------------------------------------------

    def _server(self, server_url: str | None) -> tuple[Origin, str, dict, Evidence] | None:
        servers = _list(self.spec.get("servers"))
        server_object = servers[0] if servers and isinstance(servers[0], dict) else {}
        if not server_object.get("url") and server_url is not None:
            # No server in the spec: the given URL is the whole base URL, path included.
            return self._origin(server_url, server_object, self._evidence(EvidenceKind.USER, "server_url"))
        raw = str(server_object.get("url") or "/")
        url = self._substitute_variables(raw, _dict(server_object.get("variables")))
        if url is None:
            return None
        evidence = self._evidence(EvidenceKind.DOCUMENTED, "servers[0]")
        if not _is_absolute(url):
            if server_url is None:
                self._error(
                    "relative-server-url",
                    f"server URL {raw!r} is relative; pass the API's base URL with server_url",
                    "servers[0].url",
                )
                return None
            url = urljoin(server_url, url)
            evidence = self._evidence(EvidenceKind.USER, "server_url", note=f"relative server URL {raw!r} resolved")
        elif server_url is not None:
            url = server_url
            evidence = self._evidence(EvidenceKind.USER, "server_url", note=f"replaces server URL {raw!r}")
        return self._origin(url, server_object, evidence)

    def _origin(self, url: str, server_object: dict, evidence: Evidence) -> tuple[Origin, str, dict, Evidence] | None:
        """Split an absolute URL into an origin and a base path without a trailing slash."""
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        try:
            port = parts.port
        except ValueError:
            port = None
            scheme = ""
        if scheme not in _DEFAULT_PORTS or not parts.hostname:
            self._error(
                "invalid-server-url", f"server URL {url!r} is not an absolute http or https URL", "servers[0].url"
            )
            return None
        origin = Origin(host=parts.hostname.lower(), port=port or _DEFAULT_PORTS[scheme], scheme=scheme)
        return origin, parts.path.rstrip("/"), server_object, evidence

    def _ignore_servers(self, node: dict, pointer: str, origin: Origin) -> None:
        if _list(node.get("servers")):
            self._warning(
                "ignored-server-override",
                f"{pointer} declares its own servers; its operations are placed on the root server "
                f"{origin.host}:{origin.port} instead",
                f"{pointer}.servers",
            )

    def _substitute_variables(self, url: str, variables: dict) -> str | None:
        def replace(match: re.Match) -> str:
            variable = variables.get(match.group(1))
            if isinstance(variable, dict) and "default" in variable:
                return str(variable["default"])
            return match.group(0)

        resolved = _TEMPLATE_PARAM.sub(replace, url)
        if _TEMPLATE_PARAM.search(resolved):
            self._error(
                "unresolved-server-variable",
                f"server URL {url!r} has a variable without a default",
                "servers[0].variables",
            )
            return None
        return resolved

    def _extension(self, server_object: dict, key: str) -> tuple[Any, str] | None:
        if key in server_object:
            return server_object[key], f"servers[0].{key}"
        if key in self.spec:
            return self.spec[key], key
        return None

    def _inspection(self, server_object: dict) -> Inspection:
        found = self._extension(server_object, "x-openshell-inspection")
        if found is None:
            return Inspection.L7
        value, pointer = found
        try:
            return Inspection(value)
        except ValueError:
            self._warning(
                "invalid-extension", f"x-openshell-inspection must be 'l7' or 'opaque', not {value!r}", pointer
            )
            return Inspection.L7

    def _allowed_ips(self, server_object: dict) -> list[str]:
        found = self._extension(server_object, "x-openshell-allowed-ips")
        if found is None:
            return []
        value, pointer = found
        values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        allowed: list[str] = []
        for entry in values:
            try:
                ipaddress.ip_network(str(entry), strict=False)
            except ValueError:
                self._warning(
                    "invalid-extension", f"x-openshell-allowed-ips entry {entry!r} is not a CIDR range", pointer
                )
                continue
            if str(entry) not in allowed:
                allowed.append(str(entry))
        return allowed

    # --- operations -----------------------------------------------------------------

    def _operation(
        self,
        service_id: str,
        base_path: str,
        path: str,
        method: str,
        item: dict,
        operation: dict,
        used_ids: set[str],
    ) -> None:
        pointer = f"paths.{path}.{method}"
        operation_id = operation.get("operationId")
        name = slug(str(operation_id)) if operation_id else ""
        if not name:
            name = f"{method}-{slug(path) or 'root'}"
        op_id = self._unique_id(f"{self.provider}.{name}", used_ids, pointer)
        tags = [str(tag) for tag in _list(operation.get("tags"))]
        requires, privilege, evidence = self._privilege(method, operation, tags, pointer)
        summary = str(operation.get("summary") or operation.get("description") or "").strip()
        self.operations.append(
            Operation(
                id=op_id,
                service=service_id,
                selector=RestSelector(method.upper(), f"{base_path}/{path.lstrip('/')}"),
                privilege=privilege,
                requires=requires,
                multi_segment=self._multi_segment(path, item, operation, pointer),
                summary=summary,
                tags=tags,
                layer=self.layer,
                evidence=[evidence],
            )
        )
        self._link_concepts(operation, requires, pointer)

    def _unique_id(self, candidate: str, used_ids: set[str], pointer: str) -> str:
        op_id, counter = candidate, 2
        while op_id in used_ids:
            op_id = f"{candidate}-{counter}"
            counter += 1
        if op_id != candidate:
            self._warning("duplicate-operation-id", f"operation ID {candidate} is taken; using {op_id}", pointer)
        used_ids.add(op_id)
        return op_id

    def _multi_segment(self, path: str, item: dict, operation: dict, pointer: str) -> list[str]:
        template = _TEMPLATE_PARAM.findall(path)
        names: list[str] = []
        declared = operation.get("x-openshell-multi-segment")
        if declared is not None:
            for name in [declared] if isinstance(declared, str) else declared if isinstance(declared, list) else []:
                if name in template:
                    names.append(name)
                else:
                    self._warning(
                        "invalid-extension",
                        f"x-openshell-multi-segment names {name!r}, which is not a parameter of {path}",
                        pointer,
                    )
        for parameter in self._parameters(item, operation):
            marked = parameter.get("x-multi-segment") is True or parameter.get("allowReserved") is True
            if marked and parameter.get("in") == "path" and parameter.get("name") in template:
                names.append(parameter["name"])
        return list(dict.fromkeys(names))

    def _parameters(self, item: dict, operation: dict) -> list[dict]:
        """Path-level parameters, overridden by operation-level ones with the same name and location."""
        merged: dict[tuple, dict] = {}
        for parameter in [*_list(item.get("parameters")), *_list(operation.get("parameters"))]:
            parameter = self._deref(parameter)
            if isinstance(parameter, dict):
                merged[(parameter.get("name"), parameter.get("in"))] = parameter
        return list(merged.values())

    # --- capabilities and privilege ----------------------------------------------------

    def _privilege(
        self, method: str, operation: dict, tags: list[str], pointer: str
    ) -> tuple[list[list[str]], Privilege, Evidence]:
        """Return the operation's requirement (OR of AND-lists), its privilege, and the privilege's evidence."""
        declared = self._declared_privilege(operation, pointer)
        explicit, level_guessed = self._explicit_capabilities(operation, method, declared, pointer)
        if explicit:
            if declared is not None:
                return explicit, declared, self._declared_evidence(pointer)
            if level_guessed:
                return explicit, self._dnf_privilege(explicit), self._method_evidence(method, pointer)
            note = "privilege from the levels of its x-openshell-capability"
            return explicit, self._dnf_privilege(explicit), self._evidence(EvidenceKind.DOCUMENTED, pointer, note=note)
        scopes = self._scope_requirements(operation, pointer)
        if scopes:
            if declared is not None:
                return scopes, declared, self._declared_evidence(pointer)
            privilege = self._dnf_privilege(scopes)
            if all(cap in self.documented for and_set in scopes for cap in and_set):
                note = "privilege from the levels of its OAuth scopes"
                return scopes, privilege, self._evidence(EvidenceKind.DOCUMENTED, pointer, note=note)
            self.inferred_count += 1
            note = "privilege from OAuth scopes whose levels were inferred from their names"
            evidence = self._evidence(EvidenceKind.INFERRED, pointer, note=note, confidence=_INFERRED_SCOPE_CONFIDENCE)
            return scopes, privilege, evidence
        # Nothing in the description: guess from the method, name the capability after the first tag.
        if declared is not None:
            privilege, evidence = declared, self._declared_evidence(pointer)
        else:
            privilege, evidence = method_privilege(method), self._method_evidence(method, pointer)
        return [[self._tag_capability(privilege, tags, pointer)]], privilege, evidence

    def _method_evidence(self, method: str, pointer: str) -> Evidence:
        """Evidence for a privilege guessed from the HTTP method. Counts toward the inferred-privilege summary."""
        self.inferred_count += 1
        note = f"privilege inferred from the HTTP method {method.upper()}"
        return self._evidence(EvidenceKind.INFERRED, pointer, note=note, confidence=_INFERRED_METHOD_CONFIDENCE)

    def _dnf_privilege(self, requires: list[list[str]]) -> Privilege:
        """The cheapest way to satisfy the requirement: minimum over OR-sets of the maximum level in each."""
        return min(
            (max((self.capabilities[cap].level for cap in and_set), key=_rank) for and_set in requires), key=_rank
        )

    def _tag_capability(self, privilege: Privilege, tags: list[str], pointer: str) -> str:
        level = _grant_level(privilege)
        group = tags[0] if tags else "api"
        cap_id = f"{self.provider}.{slug(group) or 'api'}:{level.value}"
        note = f"synthesized from the tag {group!r}" if tags else "synthesized for an untagged operation"
        self._add_capability(
            cap_id,
            name=f"{group} ({level.value})",
            level=level,
            evidence=self._evidence(EvidenceKind.INFERRED, pointer, note=note, confidence=_INFERRED_METHOD_CONFIDENCE),
            pointer=pointer,
        )
        return cap_id

    def _declared_privilege(self, operation: dict, pointer: str) -> Privilege | None:
        value = operation.get("x-openshell-privilege")
        if value is None:
            return None
        if value in _LEVELS:
            return _LEVELS[value]
        self._warning(
            "invalid-extension", f"x-openshell-privilege must be read, write, or admin, not {value!r}", pointer
        )
        return None

    def _declared_evidence(self, pointer: str) -> Evidence:
        return self._evidence(EvidenceKind.DOCUMENTED, pointer, note="privilege from x-openshell-privilege")

    def _explicit_capabilities(
        self, operation: dict, method: str, declared: Privilege | None, pointer: str
    ) -> tuple[list[list[str]], bool]:
        """The OR of ``x-openshell-capability`` IDs, and whether any level had to be guessed from the method.

        IDs follow ``<provider>.<name>:<level>``. An ID without a level gets the
        operation's level appended: ``x-openshell-privilege`` when set
        (documented), else the HTTP method's level (inferred).
        """
        value = operation.get("x-openshell-capability")
        if value is None:
            return [], False
        values = [value] if isinstance(value, str) else value if isinstance(value, list) else None
        if not values or not all(isinstance(entry, str) and entry for entry in values):
            self._warning(
                "invalid-extension", "x-openshell-capability must be a capability ID or a list of them", pointer
            )
            return [], False
        requires: list[list[str]] = []
        guessed = False
        for entry in values:
            cap_id = entry if entry.startswith(f"{self.provider}.") else f"{self.provider}.{entry}"
            suffix = cap_id.rsplit(":", 1)[1] if ":" in cap_id else ""
            if suffix in _LEVELS:
                level = _LEVELS[suffix]
                evidence = self._evidence(EvidenceKind.DOCUMENTED, pointer, note="level from the capability ID")
            else:
                if declared is not None:
                    level = declared
                    evidence = self._evidence(
                        EvidenceKind.DOCUMENTED, pointer, note="level appended from x-openshell-privilege"
                    )
                else:
                    guessed = True
                    level = _grant_level(method_privilege(method))
                    note = f"level appended from the HTTP method {method.upper()}"
                    evidence = self._evidence(
                        EvidenceKind.INFERRED, pointer, note=note, confidence=_INFERRED_METHOD_CONFIDENCE
                    )
                cap_id = f"{cap_id}:{level.value}"
            name = cap_id[len(self.provider) + 1 :]
            self._add_capability(cap_id, name=name, level=level, evidence=evidence, pointer=pointer)
            if suffix in _LEVELS:
                # The ID states the level, even if another operation created the capability by inference.
                self.documented.add(cap_id)
            if [cap_id] not in requires:
                requires.append([cap_id])
        return requires, guessed

    def _scope_requirements(self, operation: dict, pointer: str) -> list[list[str]]:
        """OR of AND-lists of scope capabilities. Requirements without OAuth scopes are skipped."""
        security = operation["security"] if "security" in operation else self.spec.get("security")
        requires: list[list[str]] = []
        for requirement in _list(security):
            if not isinstance(requirement, dict):
                continue
            and_set: list[str] = []
            for scheme_name, scopes in requirement.items():
                scheme = self.schemes.get(scheme_name)
                if scheme is None:
                    if scheme_name not in self.unknown_schemes:
                        self.unknown_schemes.add(scheme_name)
                        message = f"security scheme {scheme_name!r} is not defined in components.securitySchemes"
                        self._warning("unknown-security-scheme", message, pointer)
                    continue
                if not isinstance(scheme, dict) or scheme.get("type") not in _OAUTH_TYPES:
                    continue
                for scope in _list(scopes):
                    cap_id = self._scope_capability(str(scope), str(scheme_name))
                    if cap_id not in and_set:
                        and_set.append(cap_id)
            if and_set and and_set not in requires:
                requires.append(and_set)
        return requires

    def _scope_capability(self, scope: str, scheme_name: str) -> str:
        cap_id = f"{self.provider}.{scope}"
        if cap_id in self.capabilities:
            return cap_id
        if scope in self.scope_levels:
            level = self.scope_levels[scope]
            evidence = self._evidence(EvidenceKind.DOCUMENTED, "x-openshell-scope-levels")
        else:
            level = _level_from_name(scope)
            evidence = self._evidence(
                EvidenceKind.INFERRED,
                f"components.securitySchemes.{scheme_name}",
                note="level inferred from the scope name",
                confidence=_INFERRED_SCOPE_CONFIDENCE,
            )
        self._add_capability(
            cap_id,
            name=scope,
            level=level,
            evidence=evidence,
            pointer=evidence.pointer,
            description=self.scope_descriptions.get(scope, ""),
        )
        return cap_id

    def _add_capability(
        self, cap_id: str, *, name: str, level: Privilege, evidence: Evidence, pointer: str, description: str = ""
    ) -> None:
        existing = self.capabilities.get(cap_id)
        if existing is None:
            self.capabilities[cap_id] = Capability(
                id=cap_id,
                provider=self.provider,
                name=name,
                level=level,
                description=description,
                layer=self.layer,
                evidence=[evidence],
            )
            if evidence.kind is EvidenceKind.DOCUMENTED:
                self.documented.add(cap_id)
        elif existing.level is not level:
            self._warning(
                "capability-level-conflict",
                f"capability {cap_id} is {existing.level.value} elsewhere; ignoring level {level.value}",
                pointer,
            )

    def _scope_descriptions(self) -> dict[str, str]:
        descriptions: dict[str, str] = {}
        for scheme in self.schemes.values():
            flows = scheme.get("flows") if isinstance(scheme, dict) else None
            for flow in _dict(flows).values():
                for scope, description in _dict(_dict(flow).get("scopes")).items():
                    descriptions.setdefault(str(scope), str(description or "").strip())
        return descriptions

    def _scope_levels(self) -> dict[str, Privilege]:
        levels: dict[str, Privilege] = {}
        declared = self.spec.get("x-openshell-scope-levels") or {}
        if not isinstance(declared, dict):
            self._warning(
                "invalid-extension", "x-openshell-scope-levels must map scopes to levels", "x-openshell-scope-levels"
            )
            return levels
        for scope, value in declared.items():
            if value in _LEVELS:
                levels[str(scope)] = _LEVELS[value]
            else:
                self._warning(
                    "invalid-extension",
                    f"x-openshell-scope-levels gives {scope!r} the level {value!r}; use read, write, or admin",
                    "x-openshell-scope-levels",
                )
        return levels

    # --- concepts -------------------------------------------------------------------

    def _link_concepts(self, operation: dict, requires: list[list[str]], pointer: str) -> None:
        if "x-openshell-concepts" in operation:
            concepts, concept_pointer = operation["x-openshell-concepts"], pointer
        else:
            concepts, concept_pointer = self.spec.get("x-openshell-concepts"), "x-openshell-concepts"
        if concepts is None:
            return
        if not isinstance(concepts, list) or not all(isinstance(concept, str) for concept in concepts):
            self._warning("invalid-extension", "x-openshell-concepts must be a list of concept IDs", concept_pointer)
            return
        capabilities = list(dict.fromkeys(cap for and_set in requires for cap in and_set))
        for concept in concepts:
            link = self.links.get(concept)
            if link is None:
                link = self.links[concept] = ConceptLink(concept=concept, realized_by=[], layer=self.layer)
            link.realized_by.extend(cap for cap in capabilities if cap not in link.realized_by)
            evidence = self._evidence(EvidenceKind.DOCUMENTED, concept_pointer)
            if evidence not in link.evidence:
                link.evidence.append(evidence)

    # --- helpers --------------------------------------------------------------------

    def _deref(self, value: Any, depth: int = 0) -> Any:
        """Resolve a local ``$ref`` (``#/...``). Other references are left as they are."""
        if not isinstance(value, dict) or not isinstance(value.get("$ref"), str) or depth > 16:
            return value
        ref = value["$ref"]
        if not ref.startswith("#/"):
            return value
        node: Any = self.spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return value
            node = node[part]
        return self._deref(node, depth + 1)

    def _evidence(self, kind: EvidenceKind, pointer: str, *, note: str = "", confidence: float = 1.0) -> Evidence:
        return Evidence(kind=kind, source=self.source, pointer=pointer, note=note, confidence=confidence)

    def _warning(self, code: str, message: str, pointer: str = "") -> None:
        self.diagnostics.append(Diagnostic("warning", code, message, pointer))

    def _error(self, code: str, message: str, pointer: str = "") -> None:
        self.diagnostics.append(Diagnostic("error", code, message, pointer))


def _dict(value: Any) -> dict:
    """``value`` if it is a mapping, else an empty one. Malformed parts of a spec are skipped."""
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    """``value`` if it is a list, else an empty one. Malformed parts of a spec are skipped."""
    return value if isinstance(value, list) else []


def _is_absolute(url: str) -> bool:
    parts = urlsplit(url)
    return bool(parts.scheme and parts.netloc)


def _rank(level: Privilege) -> int:
    return level.rank


def _grant_level(privilege: Privilege) -> Privilege:
    """The capability level for an operation privilege. Unknown (TRACE) is only reachable through an admin grant."""
    return privilege if privilege is not Privilege.UNKNOWN else Privilege.ADMIN


def _level_from_name(name: str) -> Privilege:
    """Guess a level from an OAuth scope name. Mixed names round up, so the level is never understated.

    Admin stems match anywhere in the name (``administration``, ``deletion``,
    ``repo:management``) and win over everything. Read words must be whole
    words, so ``files.readwrite`` and ``spreadsheets`` do not count as read, and
    a write word wins over a read word.
    """
    lowered = name.lower()
    if any(stem in lowered for stem in _ADMIN_STEMS):
        return Privilege.ADMIN
    words = set(re.split(r"[^a-z0-9]+", lowered))
    if words & _READ_WORDS and not words & _WRITE_WORDS:
        return Privilege.READ
    return Privilege.WRITE

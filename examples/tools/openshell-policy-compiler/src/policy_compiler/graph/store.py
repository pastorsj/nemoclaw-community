# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Layered catalog store: parse catalog files and query the capability graph.

Catalog files (see ``docs/catalog-format.md``) are parsed into
``CatalogDocument`` objects and merged into a ``Graph`` layer by layer: the base
catalog, then each org catalog directory, then agent-layer documents built by
importers. ``Graph.finalize()`` applies concept links, checks that every
reference resolves, and builds the lookup indexes the queries use.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import re
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import yaml

from policy_compiler import paths
from policy_compiler.graph.model import (
    Capability,
    CatalogDocument,
    Client,
    ClientUse,
    Concept,
    ConceptLink,
    McpSelector,
    Operation,
    Origin,
    Provider,
    RestSelector,
    Service,
)
from policy_compiler.ir.access import Diagnostic
from policy_compiler.ir.common import Evidence, EvidenceKind, Inspection, Layer, Privilege, Protocol

BASE_CATALOG_ENV = "POLICY_COMPILER_BASE_CATALOG"
BASE_CATALOG_DIR = Path(__file__).resolve().parents[3] / "catalog" / "base"
"""The base catalog shipped with the tool. ``POLICY_COMPILER_BASE_CATALOG`` overrides it at load time."""

CATALOG_VERSION = 1

_T = TypeVar("_T")
_E = TypeVar("_E", bound=StrEnum)

_REST_METHOD = re.compile(r"[A-Z]+|\*")
"""Matched with ``fullmatch`` against the upper-cased method."""
_SCHEMES = ("https", "http")
_CAPABILITY_LEVELS = (Privilege.READ, Privilege.WRITE, Privilege.ADMIN)


class CatalogError(Exception):
    """A catalog file is malformed, or the merged graph is inconsistent.

    ``problems`` lists every problem found, one line each, so that a caller such
    as ``catalog check`` can report them all.
    """

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems if problems is not None else [message]


def base_catalog_dir() -> Path:
    """Return the base catalog directory, honoring ``POLICY_COMPILER_BASE_CATALOG``."""
    override = os.environ.get(BASE_CATALOG_ENV)
    return Path(override) if override else BASE_CATALOG_DIR


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe YAML loader that rejects a key repeated in one mapping.

    PyYAML otherwise keeps the last value silently, so a second ``requires:``
    would replace the first without notice. Merge keys (``<<``) are allowed.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        seen: set[Any] = set()
        for key_node, _value in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


# --- Parsing -------------------------------------------------------------------


class _Fields:
    """Typed, strict access to one mapping in a catalog file.

    Errors name the file and a pointer such as ``operations[3].requires``.
    ``finish()`` rejects keys that no accessor read, so a typo such as
    ``require:`` fails loudly instead of silently making an operation public.
    """

    def __init__(self, data: Any, source: str, pointer: str) -> None:
        self.source = source
        self.pointer = pointer
        if not isinstance(data, dict):
            raise self.error("must be a mapping")
        self.data = data
        self._read: set[str] = set()

    def error(self, message: str, key: str | None = None) -> CatalogError:
        where = self._where(key)
        return CatalogError(f"{self.source}: {where}: {message}" if where else f"{self.source}: {message}")

    def _where(self, key: str | None) -> str:
        return ".".join(part for part in (self.pointer, key) if part)

    def has(self, key: str) -> bool:
        return key in self.data

    def raw(self, key: str, default: Any) -> Any:
        """Return a value without type checks and mark the key as read."""
        self._read.add(key)
        return self.data.get(key, default)

    def text(self, key: str, *, required: bool = False) -> str:
        if required and key not in self.data:
            raise self.error("is required", key)
        value = self.raw(key, "")
        if not isinstance(value, str) or (required and not value):
            raise self.error("must be a non-empty string" if required else "must be a string", key)
        return value

    def texts(self, key: str, *, required: bool = False) -> list[str]:
        if required and key not in self.data:
            raise self.error("is required", key)
        value = self.raw(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise self.error("must be a list of non-empty strings", key)
        if required and not value:
            raise self.error("must not be empty", key)
        return list(value)

    def flag(self, key: str, default: bool) -> bool:
        value = self.raw(key, default)
        if not isinstance(value, bool):
            raise self.error("must be true or false", key)
        return value

    def integer(self, key: str, default: int, low: int, high: int) -> int:
        value = self.raw(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise self.error(f"must be an integer from {low} to {high}", key)
        return value

    def number(self, key: str, default: float, low: float, high: float) -> float:
        value = self.raw(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise self.error(f"must be a number from {low} to {high}", key)
        return float(value)

    def choice(
        self, key: str, enum_type: type[_E], default: _E | None = None, *, only: Iterable[_E] | None = None
    ) -> _E:
        """An enum value, limited to the members in ``only`` when given."""
        if default is None and key not in self.data:
            raise self.error("is required", key)
        value = self.raw(key, default)
        allowed = [member.value for member in (only if only is not None else enum_type)]
        if value not in allowed:
            raise self.error(f"invalid value {value!r} (expected {' | '.join(allowed)})", key)
        return enum_type(value)

    def nested(self, key: str, parse: Callable[[_Fields], _T], *, required: bool = False) -> list[_T]:
        """Parse a list of mappings, one reader per entry."""
        if required and key not in self.data:
            raise self.error("is required", key)
        value = self.raw(key, [])
        if not isinstance(value, list):
            raise self.error("must be a list", key)
        if required and not value:
            raise self.error("must not be empty", key)
        items = []
        for index, entry in enumerate(value):
            child = _Fields(entry, self.source, f"{self._where(key)}[{index}]")
            items.append(parse(child))
            child.finish()
        return items

    def finish(self) -> None:
        unknown = sorted(str(key) for key in self.data if key not in self._read)
        if unknown:
            raise self.error(f"unknown key(s): {', '.join(unknown)}")


def _parse_evidence(f: _Fields) -> list[Evidence]:
    """The curated record for this node, followed by the file's explicit ``evidence`` entries."""
    curated = Evidence(EvidenceKind.CURATED, f"catalog:{f.source}", f.pointer)
    return [curated] + f.nested(
        "evidence",
        lambda e: Evidence(
            kind=e.choice("kind", EvidenceKind),
            source=e.text("source", required=True),
            pointer=e.text("pointer"),
            note=e.text("note"),
            confidence=e.number("confidence", 1.0, 0.0, 1.0),
        ),
    )


def _parse_provider(f: _Fields, layer: Layer) -> Provider:
    return Provider(
        id=f.text("id", required=True),
        name=f.text("name", required=True),
        description=f.text("description"),
        layer=layer,
        evidence=_parse_evidence(f),
    )


def _parse_origin(f: _Fields) -> Origin:
    host = f.text("host", required=True)
    port = f.integer("port", 443, 1, 65535)
    scheme = f.text("scheme") or "https"
    if scheme not in _SCHEMES:
        raise f.error(f"invalid value {scheme!r} (expected {' | '.join(_SCHEMES)})", "scheme")
    return Origin(host=host, port=port, scheme=scheme)


def _parse_service(f: _Fields, layer: Layer) -> Service:
    return Service(
        id=f.text("id", required=True),
        provider=f.text("provider", required=True),
        name=f.text("name", required=True),
        protocol=f.choice("protocol", Protocol, Protocol.REST),
        inspection=f.choice("inspection", Inspection, Inspection.L7),
        origins=f.nested("origins", _parse_origin, required=True),
        allowed_ips=f.texts("allowed_ips"),
        description=f.text("description"),
        layer=layer,
        evidence=_parse_evidence(f),
    )


def _parse_capability(f: _Fields, layer: Layer) -> Capability:
    return Capability(
        id=f.text("id", required=True),
        provider=f.text("provider", required=True),
        name=f.text("name", required=True),
        level=f.choice("level", Privilege, only=_CAPABILITY_LEVELS),
        implies=f.texts("implies"),
        description=f.text("description"),
        layer=layer,
        evidence=_parse_evidence(f),
    )


def _parse_selector(f: _Fields) -> RestSelector | McpSelector:
    is_rest = f.has("method") or f.has("path")
    if f.has("mcp_tool"):
        if is_rest:
            raise f.error("set either mcp_tool or method and path, not both")
        return McpSelector(f.text("mcp_tool", required=True))
    if not is_rest:
        raise f.error("needs mcp_tool, or method and path")
    method = f.text("method", required=True).upper()
    if not _REST_METHOD.fullmatch(method):
        raise f.error(f"invalid value {method!r} (expected an HTTP method such as GET, or *)", "method")
    path = f.text("path", required=True)
    if not path.startswith("/"):
        raise f.error("must start with '/'", "path")
    return RestSelector(method, path)


def _parse_requires(f: _Fields) -> list[list[str]]:
    """An OR of AND-lists. A missing ``requires`` means ``[]``: no capability needed."""
    value = f.raw("requires", [])

    def is_and_list(group: Any) -> bool:
        return isinstance(group, list) and bool(group) and all(isinstance(cap, str) and cap for cap in group)

    if not isinstance(value, list) or not all(is_and_list(group) for group in value):
        raise f.error("must be a list of non-empty capability ID lists, such as [[a:read], [b:read]]", "requires")
    return [list(group) for group in value]


def _parse_operation(f: _Fields, layer: Layer) -> Operation:
    return Operation(
        id=f.text("id", required=True),
        service=f.text("service", required=True),
        selector=_parse_selector(f),
        privilege=f.choice("privilege", Privilege),
        requires=_parse_requires(f),
        multi_segment=f.texts("multi_segment"),
        summary=f.text("summary"),
        tags=f.texts("tags"),
        layer=layer,
        evidence=_parse_evidence(f),
    )


def _parse_concept(f: _Fields, layer: Layer) -> Concept:
    return Concept(
        id=f.text("id", required=True),
        name=f.text("name", required=True),
        privilege=f.choice("privilege", Privilege),
        keywords=f.texts("keywords"),
        realized_by=f.texts("realized_by"),
        description=f.text("description"),
        layer=layer,
        evidence=_parse_evidence(f),
    )


def _parse_concept_link(f: _Fields, layer: Layer) -> ConceptLink:
    return ConceptLink(
        concept=f.text("concept", required=True),
        realized_by=f.texts("realized_by", required=True),
        layer=layer,
        evidence=_parse_evidence(f),
    )


def _parse_client_use(f: _Fields) -> ClientUse:
    return ClientUse(
        operations=f.texts("operations", required=True),
        when_capability=f.texts("when_capability"),
    )


def _parse_client(f: _Fields, layer: Layer) -> Client:
    return Client(
        id=f.text("id", required=True),
        paths=f.texts("paths", required=True),
        description=f.text("description"),
        bypasses_l7=f.flag("bypasses_l7", False),
        can_exfiltrate=f.flag("can_exfiltrate", True),
        uses=f.nested("uses", _parse_client_use),
        layer=layer,
        evidence=_parse_evidence(f),
    )


_PARSERS: dict[str, Callable[[_Fields, Layer], Any]] = {
    "providers": _parse_provider,
    "services": _parse_service,
    "capabilities": _parse_capability,
    "operations": _parse_operation,
    "concepts": _parse_concept,
    "concept_links": _parse_concept_link,
    "clients": _parse_client,
}


def parse_catalog_document(data: dict, *, source: str, layer: Layer) -> CatalogDocument:
    """Parse the data of one catalog file.

    ``source`` labels the file, for example ``base/github.yaml``. Every node gets
    the evidence record ``(curated, "catalog:<source>", "<section>[i]")`` followed
    by any ``evidence`` entries the file gives, and every node is placed in
    ``layer``. Raises ``CatalogError`` on a malformed document.
    """
    top = _Fields(data, source, "")
    if "catalog_version" not in data:
        raise top.error("is required", "catalog_version")
    version = top.raw("catalog_version", None)
    if type(version) is not int or version != CATALOG_VERSION:
        raise top.error(f"unsupported version {version!r} (expected {CATALOG_VERSION})", "catalog_version")
    doc = CatalogDocument(source=source)
    for section, parse in _PARSERS.items():
        setattr(doc, section, top.nested(section, lambda f, parse=parse: parse(f, layer)))
    top.finish()
    return doc


def load_catalog_file(path: Path, *, layer: Layer, source: str | None = None) -> CatalogDocument:
    """Read and parse one catalog file, rejecting keys repeated in one YAML mapping.

    ``source`` labels the file and defaults to ``<directory name>/<file name>``.
    ``load_graph`` passes labels that stay unique across catalog directories.
    """
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.load(stream, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError(f"{path}: cannot read catalog file: {exc}") from exc
    return parse_catalog_document(data, source=source or f"{path.parent.name}/{path.name}", layer=layer)


# --- Dumping -------------------------------------------------------------------


def _compact(data: dict[str, Any], *, keep: tuple[str, ...] = (), **defaults: Any) -> dict[str, Any]:
    """Drop empty strings and lists, and values equal to the given defaults, except keys in ``keep``."""
    return {
        key: value
        for key, value in data.items()
        if key in keep or not (value in ("", []) or (key in defaults and value == defaults[key]))
    }


def _dump_provider(node: Provider) -> dict[str, Any]:
    return _compact({"id": node.id, "name": node.name, "description": node.description})


def _dump_service(node: Service) -> dict[str, Any]:
    origins = [
        _compact({"host": o.host, "port": o.port, "scheme": o.scheme}, port=443, scheme="https")
        for o in node.origins
    ]
    return _compact(
        {
            "id": node.id,
            "provider": node.provider,
            "name": node.name,
            "protocol": node.protocol.value,
            "inspection": node.inspection.value,
            "origins": origins,
            "allowed_ips": list(node.allowed_ips),
            "description": node.description,
        },
        protocol=Protocol.REST.value,
        inspection=Inspection.L7.value,
    )


def _dump_capability(node: Capability) -> dict[str, Any]:
    return _compact(
        {
            "id": node.id,
            "provider": node.provider,
            "name": node.name,
            "level": node.level.value,
            "implies": list(node.implies),
            "description": node.description,
        }
    )


def _dump_operation(node: Operation) -> dict[str, Any]:
    data: dict[str, Any] = {"id": node.id, "service": node.service}
    if isinstance(node.selector, RestSelector):
        data["method"] = node.selector.method
        data["path"] = node.selector.path
    elif isinstance(node.selector, McpSelector):
        data["mcp_tool"] = node.selector.tool
    else:
        raise CatalogError(f"operation {node.id}: a {type(node.selector).__name__} has no catalog-file form")
    data.update(
        multi_segment=list(node.multi_segment),
        privilege=node.privilege.value,
        requires=[list(group) for group in node.requires],
        summary=node.summary,
        tags=list(node.tags),
    )
    return _compact(data, keep=("requires",))


def _dump_concept(node: Concept) -> dict[str, Any]:
    return _compact(
        {
            "id": node.id,
            "name": node.name,
            "privilege": node.privilege.value,
            "keywords": list(node.keywords),
            "realized_by": list(node.realized_by),
            "description": node.description,
        }
    )


def _dump_concept_link(node: ConceptLink) -> dict[str, Any]:
    return {"concept": node.concept, "realized_by": list(node.realized_by)}


def _dump_client(node: Client) -> dict[str, Any]:
    uses = [
        _compact(
            {"when_capability": list(use.when_capability), "operations": list(use.operations)},
            keep=("operations",),
        )
        for use in node.uses
    ]
    return _compact(
        {
            "id": node.id,
            "paths": list(node.paths),
            "description": node.description,
            "bypasses_l7": node.bypasses_l7,
            "can_exfiltrate": node.can_exfiltrate,
            "uses": uses,
        },
        keep=("paths",),
        bypasses_l7=False,
        can_exfiltrate=True,
    )


_DUMPERS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "providers": _dump_provider,
    "services": _dump_service,
    "capabilities": _dump_capability,
    "operations": _dump_operation,
    "concepts": _dump_concept,
    "concept_links": _dump_concept_link,
    "clients": _dump_client,
}


def dump_catalog_document(doc: CatalogDocument) -> dict:
    """Render a document as catalog-file data, the inverse of ``parse_catalog_document``.

    Fields equal to their defaults are left out, except an operation's
    ``requires``: it is always written, because ``[]`` makes an operation public.
    The curated record that parsing generates for each node is left out; every
    other evidence record is written. Layers are not part of the file format.
    """
    data: dict[str, Any] = {"catalog_version": CATALOG_VERSION}
    for section, dump in _DUMPERS.items():
        entries = []
        for index, node in enumerate(getattr(doc, section)):
            entry = dump(node)
            generated = Evidence(EvidenceKind.CURATED, f"catalog:{doc.source}", f"{section}[{index}]")
            evidence = [record.to_dict() for record in node.evidence if record != generated]
            if evidence:
                entry["evidence"] = evidence
            entries.append(entry)
        if entries:
            data[section] = entries
    return data


# --- Graph ---------------------------------------------------------------------

_SEMANTIC_FIELDS: dict[str, tuple[str, ...]] = {
    "provider": ("name",),
    "service": ("provider", "origins"),
    "capability": ("provider", "level"),
    "operation": ("service", "selector", "privilege"),
}
"""Fields that must agree for a repeated ID to merge. Concepts and clients never merge.

When other fields differ, the first definition wins and an ``ignored-redefinition``
warning names the fields that were ignored.
"""

_NOT_COMPARED = frozenset({"id", "layer", "evidence"})


class Graph:
    """The merged capability graph.

    Add documents in layer order with ``add_document()``, then call
    ``finalize()`` once before running queries. Node tables keep insertion order,
    which is catalog order, and every query returns results in that order.
    """

    def __init__(self) -> None:
        self.providers: dict[str, Provider] = {}
        self.services: dict[str, Service] = {}
        self.operations: dict[str, Operation] = {}
        self.capabilities: dict[str, Capability] = {}
        self.concepts: dict[str, Concept] = {}
        self.clients: dict[str, Client] = {}
        self.diagnostics: list[Diagnostic] = []
        """Warnings and info diagnostics from the last ``finalize()``."""
        self._redefinitions: list[Diagnostic] = []
        self._sources: dict[tuple[str, str], str] = {}
        self._pending_links: list[tuple[ConceptLink, str]] = []
        self._operations_by_service: dict[str, list[Operation]] = {}
        self._clients_by_path: dict[str, list[Client]] = {}
        self._finalized = False

    # Building

    def add_document(self, doc: CatalogDocument) -> None:
        """Merge one document into the graph. The document itself is not modified.

        A repeated ID merges into the existing node when the semantic fields
        agree, and raises ``CatalogError`` naming both sources otherwise. When
        other fields differ, the first definition wins, the later evidence is
        noted as ignored for those fields, and ``finalize()`` reports an
        ``ignored-redefinition`` warning. Service and capability IDs must start
        with ``<provider>.``.
        """
        doc = copy.deepcopy(doc)
        source = doc.source or "<unnamed document>"
        for provider in doc.providers:
            self._add("provider", self.providers, provider, source)
        for service in doc.services:
            _check_prefix("service", service.id, service.provider, source)
            self._add("service", self.services, service, source)
        for capability in doc.capabilities:
            _check_prefix("capability", capability.id, capability.provider, source)
            self._add("capability", self.capabilities, capability, source)
        for operation in doc.operations:
            self._add("operation", self.operations, operation, source)
        for concept in doc.concepts:
            self._add("concept", self.concepts, concept, source)
        for client in doc.clients:
            self._add("client", self.clients, client, source)
        self._pending_links.extend((link, source) for link in doc.concept_links)
        self._finalized = False

    def _add(self, kind: str, table: dict[str, Any], node: Any, source: str) -> None:
        existing = table.get(node.id)
        if existing is None:
            table[node.id] = node
            self._sources[kind, node.id] = source
            return
        first = self._sources[kind, node.id]
        fields = _SEMANTIC_FIELDS.get(kind)
        if fields is None:
            hint = "; use concept_links to add realizations" if kind == "concept" else ""
            raise CatalogError(f"{kind} {node.id!r} is defined in {first} and again in {source}{hint}")
        differing = [name for name in fields if getattr(existing, name) != getattr(node, name)]
        if differing:
            raise CatalogError(
                f"{kind} {node.id!r} is defined in {first} and differently in {source} "
                f"(differing fields: {', '.join(differing)})"
            )
        ignored = [
            field.name
            for field in dataclasses.fields(node)
            if field.name not in _NOT_COMPARED
            and field.name not in fields
            and getattr(existing, field.name) != getattr(node, field.name)
        ]
        records = node.evidence
        if ignored:
            names = ", ".join(ignored)
            self._redefinitions.append(
                Diagnostic(
                    "warning",
                    "ignored-redefinition",
                    f"{kind} {node.id!r} in {source} differs from its definition in {first} "
                    f"in {names}; the definition in {first} is kept",
                    _locate(node),
                )
            )
            note = f"redefinition ignored for: {names}"
            records = [dataclasses.replace(r, note=f"{note}; {r.note}" if r.note else note) for r in records]
        for record in records:
            if record not in existing.evidence:
                existing.evidence.append(record)

    def finalize(self) -> list[Diagnostic]:
        """Apply concept links, check references, and build the lookup indexes.

        A concept link appends its capabilities to the concept's ``realized_by``
        (without duplicates, keeping order) and its evidence to the concept's
        evidence. Raises ``CatalogError`` listing every dangling reference and
        operation prefix violation at once.

        Returns the diagnostics, also kept in ``self.diagnostics``:
        ``ignored-redefinition`` and ``agent-layer-extends`` warnings, then
        ``public-operation`` info.
        """
        problems = self._apply_concept_links()
        problems += self._reference_problems()
        if problems:
            problems = list(dict.fromkeys(problems))
            lines = "\n".join(f"  - {problem}" for problem in problems)
            raise CatalogError(f"catalog has {len(problems)} problem(s):\n{lines}", problems)

        self._operations_by_service = {}
        for operation in self.operations.values():
            self._operations_by_service.setdefault(operation.service, []).append(operation)
        self._clients_by_path = {}
        for client in self.clients.values():
            for path in dict.fromkeys(client.paths):
                self._clients_by_path.setdefault(path, []).append(client)

        public = [
            Diagnostic(
                "info",
                "public-operation",
                f"operation {operation.id} requires no capability; only a direct reference grants it",
                _locate(operation),
            )
            for operation in self.operations.values()
            if not operation.requires
        ]
        self.diagnostics = self._redefinitions + self._agent_layer_warnings() + public
        self._finalized = True
        return list(self.diagnostics)

    def _agent_layer_warnings(self) -> list[Diagnostic]:
        """Warn when an agent-layer operation reuses a service or capabilities from an earlier layer.

        Such an operation changes what an existing base or org grant allows, so
        a spec that an agent definition references deserves review.
        """
        warnings = []
        for operation in self.operations.values():
            if operation.layer is not Layer.AGENT:
                continue
            reasons = []
            service = self.services[operation.service]
            if service.layer is not Layer.AGENT:
                reasons.append(f"it attaches to {service.layer.value}-layer service {service.id}")
            required = list(dict.fromkeys(cap for group in operation.requires for cap in group))
            if required and all(self.capabilities[cap].layer is not Layer.AGENT for cap in required):
                reasons.append(f"only earlier-layer capabilities gate it ({', '.join(required)})")
            if reasons:
                warnings.append(
                    Diagnostic(
                        "warning",
                        "agent-layer-extends",
                        f"agent-layer operation {operation.id} extends earlier layers: {'; '.join(reasons)}",
                        _locate(operation),
                    )
                )
        return warnings

    def _apply_concept_links(self) -> list[str]:
        """Apply pending links. Links to unknown concepts stay pending and are reported."""
        problems = []
        unresolved = []
        for link, source in self._pending_links:
            concept = self.concepts.get(link.concept)
            if concept is None:
                problems.append(f"concept link in {source}: concept refers to unknown concept {link.concept!r}")
                unresolved.append((link, source))
                continue
            concept.realized_by = list(dict.fromkeys(concept.realized_by + link.realized_by))
            for record in link.evidence:
                if record not in concept.evidence:
                    concept.evidence.append(record)
        self._pending_links = unresolved
        return problems

    def _reference_problems(self) -> list[str]:
        problems: list[str] = []

        def check(owner: str, field: str, refs: Iterable[str], table: dict[str, Any], kind: str) -> None:
            for ref in refs:
                if ref not in table:
                    problems.append(f"{owner}: {field} refers to unknown {kind} {ref!r}")

        capabilities = self.capabilities
        for service in self.services.values():
            check(f"service {service.id}", "provider", [service.provider], self.providers, "provider")
        for capability in capabilities.values():
            owner = f"capability {capability.id}"
            check(owner, "provider", [capability.provider], self.providers, "provider")
            check(owner, "implies", capability.implies, capabilities, "capability")
        for operation in self.operations.values():
            owner = f"operation {operation.id}"
            service = self.services.get(operation.service)
            if service is None:
                check(owner, "service", [operation.service], self.services, "service")
            elif not operation.id.startswith(f"{service.provider}."):
                prefix = f"{service.provider}."
                problems.append(f"{owner}: ID must start with {prefix!r}, the provider of service {service.id}")
            if any(not group for group in operation.requires):
                problems.append(f"{owner}: requires contains an empty AND-list")
            required = [cap for group in operation.requires for cap in group]
            check(owner, "requires", required, capabilities, "capability")
        for concept in self.concepts.values():
            check(f"concept {concept.id}", "realized_by", concept.realized_by, capabilities, "capability")
        for client in self.clients.values():
            owner = f"client {client.id}"
            for use in client.uses:
                check(owner, "uses.operations", use.operations, self.operations, "operation")
                check(owner, "uses.when_capability", use.when_capability, capabilities, "capability")
        return problems

    # Queries

    def _check_finalized(self) -> None:
        if not self._finalized:
            raise RuntimeError("call Graph.finalize() after adding documents and before querying")

    def services_for(self, host: str, port: int) -> list[Service]:
        """Services with an origin that serves ``host`` on ``port``.

        ``host`` must be a concrete host name, not a pattern. It matches
        literal origin hosts and origin host patterns such as ``*.example.com``
        through ``paths.host_matches``, ignoring case and a trailing dot.
        """
        self._check_finalized()
        return [
            service
            for service in self.services.values()
            if any(origin.port == port and paths.host_matches(origin.host, host) for origin in service.origins)
        ]

    def operations_of(self, service_id: str) -> list[Operation]:
        """Operations served by ``service_id``; empty for an unknown service."""
        self._check_finalized()
        return list(self._operations_by_service.get(service_id, []))

    def capability_closure(self, ids: Iterable[str]) -> set[str]:
        """``ids`` plus every capability they imply, transitively.

        A single ID string is accepted too. Unknown IDs are kept as they are.
        """
        self._check_finalized()
        pending = [ids] if isinstance(ids, str) else list(ids)
        closure: set[str] = set()
        while pending:
            capability_id = pending.pop()
            if capability_id in closure:
                continue
            closure.add(capability_id)
            capability = self.capabilities.get(capability_id)
            if capability is not None:
                pending.extend(capability.implies)
        return closure

    def operations_enabled_by(
        self, capability_ids: Iterable[str], services: set[str] | None = None
    ) -> list[Operation]:
        """Operations with an AND-list in ``requires`` that the capabilities' closure fully covers.

        Operations whose ``requires`` is empty are never returned: they are
        reachable only by direct reference. ``services`` limits the result to
        those service IDs.
        """
        closure = self.capability_closure(capability_ids)
        return [
            operation
            for operation in self.operations.values()
            if operation.requires
            and (services is None or operation.service in services)
            and any(closure.issuperset(group) for group in operation.requires)
        ]

    def capabilities_for_concept(self, concept_id: str, provider: str | None = None) -> list[str]:
        """The capabilities that realize a concept, optionally only those owned by ``provider``."""
        self._check_finalized()
        concept = self.concepts.get(concept_id)
        if concept is None:
            return []
        return [
            capability_id
            for capability_id in concept.realized_by
            if provider is None or self.capabilities[capability_id].provider == provider
        ]

    def concepts_for_capability(self, capability_id: str) -> list[str]:
        """Concepts realized by ``capability_id`` or by a capability that implies it."""
        self._check_finalized()
        return [
            concept.id
            for concept in self.concepts.values()
            if capability_id in self.capability_closure(concept.realized_by)
        ]

    def match_rest(
        self, host: str, port: int, method: str, path_template: str
    ) -> list[tuple[Operation, str]]:
        """Match a declared REST call against the graph.

        Returns each REST operation on a matching service whose method agrees
        (``*`` on either side matches any method) and whose path template can
        describe the same request, paired with the glob from ``paths.align``.
        The glob keeps the declared literal segments.
        """
        method = method.upper()
        matches = []
        for service in self.services_for(host, port):
            for operation in self.operations_of(service.id):
                selector = operation.selector
                if not isinstance(selector, RestSelector):
                    continue
                if "*" not in (method, selector.method) and selector.method != method:
                    continue
                glob = paths.align(path_template, selector.path, set(operation.multi_segment))
                if glob is not None:
                    matches.append((operation, glob))
        return matches

    def clients_for_path(self, binary_path: str) -> list[Client]:
        """Clients that list ``binary_path`` exactly among their executable paths."""
        self._check_finalized()
        return list(self._clients_by_path.get(binary_path, []))


def _check_prefix(kind: str, node_id: str, provider: str, source: str) -> None:
    if not node_id.startswith(f"{provider}."):
        raise CatalogError(f"{source}: {kind} {node_id!r} must start with {provider + '.'!r}, its provider ID")


def _locate(node: Any) -> str:
    """``source#pointer`` of a node's first evidence record, or ``""``."""
    if not node.evidence:
        return ""
    record = node.evidence[0]
    return f"{record.source}#{record.pointer}" if record.pointer else record.source


# --- Loading -------------------------------------------------------------------


def _catalog_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise CatalogError(f"catalog directory not found: {directory}")
    return sorted(path for path in directory.iterdir() if path.suffix in (".yaml", ".yml") and path.is_file())


def load_graph(
    catalog_dirs: Iterable[Path] = (),
    *,
    include_base: bool = True,
    extra_documents: Iterable[CatalogDocument] = (),
) -> Graph:
    """Load, merge, and finalize a graph.

    Layer order: the base catalog (``Layer.BASE``) when ``include_base``, then
    each directory in ``catalog_dirs`` (``Layer.ORG``), then ``extra_documents``,
    whose nodes keep their own layer. Files in a directory load in sorted order.

    Source labels stay unique across directories: ``base/<file>`` for the base
    catalog, wherever it lives, and ``org<n>:<directory name>/<file>`` for the
    n-th catalog directory, counting from 1.
    """
    graph = Graph()
    directories = [(base_catalog_dir(), Layer.BASE, "base")] if include_base else []
    directories += [
        (Path(directory), Layer.ORG, f"org{position}:{Path(directory).name}")
        for position, directory in enumerate(catalog_dirs, start=1)
    ]
    for directory, layer, label in directories:
        for path in _catalog_files(directory):
            graph.add_document(load_catalog_file(path, layer=layer, source=f"{label}/{path.name}"))
    for doc in extra_documents:
        graph.add_document(doc)
    graph.finalize()
    return graph

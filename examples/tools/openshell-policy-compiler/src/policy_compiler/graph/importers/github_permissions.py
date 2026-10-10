# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Import GitHub's fine-grained permission data into a catalog document.

GitHub publishes, for each fine-grained permission, the REST endpoints it
grants (in github/docs, ``src/github-apps/data/``). The format is::

    {perm_key: {title, displayTitle, permissions: [
        {category, slug, subcategory, verb, requestPath, access, additional-permissions}]}}

Each ``(perm_key, access)`` pair becomes a capability ``github.<perm_key>:<access>``
and each distinct ``(verb, requestPath)`` becomes an operation on ``github.rest``.

The flat data cannot say whether an endpoint listed under several permissions
needs any one of them (OR) or all of them (AND), so the importer fails safe:

* If any entry for the endpoint has ``additional-permissions: true``, the
  operation requires every distinct capability listed for it, as one AND-list.
  This can demand more than GitHub does (issue comments accept Issues *or*
  Pull requests), but it never lets a read permission reach a write endpoint.
  When the data lists only one permission for such an endpoint, the others are
  missing from the data, and only the listed one can be required.
* Otherwise each entry is an alternative on its own (OR).
* The operation's privilege is the highest capability level across its entries.

The provider, service, and capability IDs and levels match ``catalog/base/github.yaml``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePath

from policy_compiler.graph.model import (
    Capability,
    CatalogDocument,
    Operation,
    Origin,
    Provider,
    RestSelector,
    Service,
)
from policy_compiler.ir.common import Evidence, EvidenceKind, Layer, Privilege

PROVIDER_ID = "github"
SERVICE_ID = "github.rest"
_ACCESS_LEVELS = {"read": Privilege.READ, "write": Privilege.WRITE, "admin": Privilege.ADMIN}
_ACCESS_ORDER = ("read", "write", "admin")
_ADMIN_PERMISSIONS = frozenset({"administration", "organization_administration"})
"""Permissions whose ``write`` access is admin-level: settings, deletion, and membership.

The base catalog classifies ``github.administration:write`` as admin, so the
importer does the same to keep one level per capability ID.
"""
_REQUIRED_FIELDS = ("verb", "requestPath", "access", "category", "slug")
_AND_FALLBACK_NOTE = (
    "GitHub lists additional permissions; required all of them because the flat data cannot distinguish OR from AND"
)
_MISSING_ADDITIONAL_NOTE = (
    "GitHub lists additional permissions that are not present in this data; only the listed permission is required"
)


def import_github_permissions(data: dict, *, source: str, layer: Layer) -> CatalogDocument:
    """Convert GitHub permission data into a catalog document.

    ``source`` names the data file. Evidence records use the label
    ``github-docs:<file name>``. Malformed data raises ``ValueError``.
    """
    if not isinstance(data, dict):
        raise ValueError("GitHub permission data must be a mapping of permission keys")
    label = source if source.startswith("github-docs:") else f"github-docs:{PurePath(source).name}"

    def evidence(pointer: str = "", note: str = "") -> Evidence:
        return Evidence(kind=EvidenceKind.DOCUMENTED, source=label, pointer=pointer, note=note)

    records = _records(data)
    capabilities = _capabilities(data, records, layer, evidence)
    levels = {cap.id: cap.level for cap in capabilities}

    endpoints: dict[tuple[str, str], list[tuple[str, int, dict]]] = {}
    for perm_key, index, record in records:
        key = (record["verb"].upper(), record["requestPath"])
        endpoints.setdefault(key, []).append((perm_key, index, record))

    used_ids: set[str] = set()
    operations = [
        _operation(method, path, listed, used_ids, levels, layer, evidence)
        for (method, path), listed in endpoints.items()
    ]

    return CatalogDocument(
        providers=[Provider(id=PROVIDER_ID, name="GitHub", layer=layer, evidence=[evidence()])],
        services=[
            Service(
                id=SERVICE_ID,
                provider=PROVIDER_ID,
                name="GitHub REST API",
                origins=[Origin("api.github.com")],
                layer=layer,
                evidence=[evidence()],
            )
        ],
        capabilities=capabilities,
        operations=operations,
        source=source,
    )


def _records(data: dict) -> list[tuple[str, int, dict]]:
    """Validate the data and flatten it to ``(perm_key, index, record)`` in file order."""
    records: list[tuple[str, int, dict]] = []
    for perm_key, permission in data.items():
        if not isinstance(permission, dict) or not isinstance(permission.get("permissions"), list):
            raise ValueError(f"{perm_key}: expected a mapping with a 'permissions' list")
        for index, record in enumerate(permission["permissions"]):
            pointer = f"{perm_key}.permissions[{index}]"
            if not isinstance(record, dict):
                raise ValueError(f"{pointer}: expected a mapping")
            missing = [name for name in _REQUIRED_FIELDS if not isinstance(record.get(name), str) or not record[name]]
            if missing:
                raise ValueError(f"{pointer}: missing {', '.join(missing)}")
            if record["access"] not in _ACCESS_LEVELS:
                raise ValueError(f"{pointer}: unknown access {record['access']!r}")
            records.append((str(perm_key), index, record))
    return records


def _capabilities(
    data: dict, records: list[tuple[str, int, dict]], layer: Layer, evidence: Callable[..., Evidence]
) -> list[Capability]:
    accesses: dict[str, set[str]] = {}
    for perm_key, _, record in records:
        accesses.setdefault(perm_key, set()).add(record["access"])
    capabilities: list[Capability] = []
    for perm_key, present in accesses.items():
        title = str(data[perm_key].get("title") or perm_key)
        ordered = [access for access in _ACCESS_ORDER if access in present]
        for position, access in enumerate(ordered):
            level = _ACCESS_LEVELS[access]
            if perm_key in _ADMIN_PERMISSIONS and access == "write":
                level = Privilege.ADMIN
            # Each level implies the next lower level present in the data.
            implies = [_capability_id(perm_key, ordered[position - 1])] if position else []
            capabilities.append(
                Capability(
                    id=_capability_id(perm_key, access),
                    provider=PROVIDER_ID,
                    name=f"{title} ({access})",
                    level=level,
                    implies=implies,
                    description=str(data[perm_key].get("displayTitle") or ""),
                    layer=layer,
                    evidence=[evidence(perm_key)],
                )
            )
    return capabilities


def _operation(
    method: str,
    path: str,
    listed: list[tuple[str, int, dict]],
    used_ids: set[str],
    levels: dict[str, Privilege],
    layer: Layer,
    evidence: Callable[..., Evidence],
) -> Operation:
    """One operation for an endpoint listed under one or more permissions."""
    first = listed[0][2]
    # The first listing names the operation. A later endpoint with the same name gets a suffix.
    op_id = _unique(f"{PROVIDER_ID}.{first['category']}.{first['slug']}", used_ids)
    capabilities = list(dict.fromkeys(_capability_id(perm_key, record["access"]) for perm_key, _, record in listed))
    all_required = any(record.get("additional-permissions") is True for _, _, record in listed)
    requires = [capabilities] if all_required else [[cap] for cap in capabilities]
    if not all_required:
        note = ""
    elif len(capabilities) == 1:
        note = _MISSING_ADDITIONAL_NOTE
    else:
        note = _AND_FALLBACK_NOTE
    return Operation(
        id=op_id,
        service=SERVICE_ID,
        selector=RestSelector(method, path),
        privilege=max((levels[cap] for cap in capabilities), key=lambda level: level.rank),
        requires=requires,
        multi_segment=["path"] if "{path}" in path else [],
        summary=_sentence(first["slug"]),
        tags=list(dict.fromkeys(str(tag) for tag in (first["category"], first.get("subcategory")) if tag)),
        layer=layer,
        evidence=[evidence(f"{perm_key}.permissions[{index}]", note) for perm_key, index, _ in listed],
    )


def _capability_id(perm_key: str, access: str) -> str:
    return f"{PROVIDER_ID}.{perm_key}:{access}"


def _unique(candidate: str, used_ids: set[str]) -> str:
    op_id, counter = candidate, 2
    while op_id in used_ids:
        op_id = f"{candidate}-{counter}"
        counter += 1
    used_ids.add(op_id)
    return op_id


def _sentence(slug: str) -> str:
    text = slug.replace("-", " ").strip()
    return text[:1].upper() + text[1:]

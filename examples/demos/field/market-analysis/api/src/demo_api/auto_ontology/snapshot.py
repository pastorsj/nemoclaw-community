# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Project an Auto Ontology model export into a bounded, deterministic graph for the UI.

Physical nodes (database, schema, table, column) come from the data layer; semantic nodes (terms,
attributes, metrics, analyses) from the semantic layer, kept only when they touch an included
table or column. Auto Ontology's object ids are replaced by stable opaque ids, and each kind has
a ceiling so a large model still renders.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .models import ModelDocument
from .models import OntologySnapshot
from .models import SnapshotEdge
from .models import SnapshotNode
from .models import SnapshotStats

LIMITS = {
    "schema": 24,
    "table": 96,
    "column": 320,
    "term": 96,
    "attribute": 192,
    "metric": 64,
    "analysis": 24,
    "nodes": 900,
    "edges": 1_800,
}
_NODE_ORDER = {kind: index for index, kind in enumerate(LIMITS)}
_NODE_ORDER["database"] = -1
_EDGE_ORDER = {
    kind: index
    for index, kind in enumerate(
        ("contains", "foreign_key", "join", "represents", "has_attribute", "maps_to", "semantic_fk", "has_metric")
    )
} | {"uses_column": 8}


@dataclass(frozen=True, slots=True)
class _Node:
    raw_id: str
    node: SnapshotNode


def project_snapshot(document: ModelDocument, *, source_id: str, database_name: str) -> OntologySnapshot:
    """The snapshot of the export's single database; raises ValueError if it has more or fewer."""
    if len(document.data_layer.databases) != 1:
        raise ValueError("The model export must contain exactly one database")
    database = document.data_layer.databases[0]
    omitted_nodes = 0
    nodes: list[_Node] = []
    edges: list[tuple[str, str, str]] = []  # (kind, source raw id, target raw id)

    def add(raw_id: str, kind: str, layer: str, label: str, **fields: Any) -> None:
        public_id = f"{kind}:{hashlib.sha256(f'{kind}\0{raw_id}'.encode()).hexdigest()[:24]}"
        text = _text(label, 160) or kind.title()
        nodes.append(_Node(raw_id, SnapshotNode(id=public_id, kind=kind, layer=layer, label=text, **fields)))

    def take[T](kind: str, items: list[T]) -> list[T]:
        nonlocal omitted_nodes
        omitted_nodes += max(0, len(items) - LIMITS[kind])
        return items[: LIMITS[kind]]

    add(database.id, "database", "physical", database_name, dialect=_text(database.dialect, 64) or None)
    tables = []
    for schema in take("schema", sorted(database.schemas, key=lambda item: (item.name.casefold(), item.id))):
        add(schema.id, "schema", "physical", schema.name)
        edges.append(("contains", database.id, schema.id))
        tables.extend((schema.id, table) for table in schema.tables)

    tables = take("table", sorted(tables, key=lambda item: (item[1].name.casefold(), item[1].id)))
    table_ids = {table.id for _, table in tables}
    columns = []
    for schema_id, table in tables:
        description = _text(table.description, 800) or None
        add(
            table.id, "table", "physical", table.name, description=description, table_type=_text(table.type, 64) or None
        )
        edges.append(("contains", schema_id, table.id))
        columns.extend((table, column) for column in table.columns)

    columns = take("column", sorted(columns, key=lambda item: (item[1].name.casefold(), item[1].id)))
    column_ids = {column.id for _, column in columns}
    for table, column in columns:
        add(
            column.id,
            "column",
            "physical",
            column.name,
            description=_text(column.description, 800) or None,
            data_type=_text(column.type, 128) or None,
            nullable=column.is_nullable,
            unique=column.is_unique,
            primary_key=column.name in table.pk or column.id in table.pk,
        )
        edges.append(("contains", table.id, column.id))

    semantic = document.semantic_layer
    terms = [
        term
        for term in semantic.terms
        if set(term.represents) & table_ids or any(a.column_id in column_ids for a in term.columns_attributes)
    ]
    terms = take("term", sorted(terms, key=lambda item: (item.name.casefold(), item.id)))
    term_ids = {term.id for term in terms}
    attributes = []
    for term in terms:
        add(term.id, "term", "semantic", term.name, description=_text(term.description, 800) or None)
        edges.extend(("represents", term.id, table_id) for table_id in sorted(set(term.represents) & table_ids))
        attributes.extend((term.id, a) for a in term.columns_attributes if a.column_id in column_ids)

    attributes = take("attribute", sorted(attributes, key=lambda item: (item[1].name.casefold(), item[1].id)))
    attribute_ids = {attribute.id for _, attribute in attributes}
    for term_id, attribute in attributes:
        add(
            attribute.id, "attribute", "semantic", attribute.name, description=_text(attribute.description, 800) or None
        )
        edges += [("has_attribute", term_id, attribute.id), ("maps_to", attribute.id, attribute.column_id)]

    groups = semantic.sql_attributes
    metrics = {
        metric.id: metric
        for metric in (*groups.manual, *groups.table, *groups.sql, *groups.bridge_table)
        if metric.term_id in term_ids or set(metric.sql_column_is) & column_ids
    }
    for metric in take("metric", sorted(metrics.values(), key=lambda item: (item.name.casefold(), item.id))):
        add(metric.id, "metric", "semantic", metric.name, description=_text(metric.description, 800) or None)
        if metric.term_id in term_ids:
            edges.append(("has_metric", metric.term_id, metric.id))
        edges.extend(
            ("uses_column", metric.id, column_id) for column_id in sorted(set(metric.sql_column_is) & column_ids)
        )

    analyses = sorted(semantic.custom_analyses, key=lambda item: (item.name.casefold(), item.id))
    for analysis in take("analysis", analyses):
        add(analysis.id, "analysis", "semantic", analysis.name, description=_text(analysis.description, 800) or None)
        edges.extend(("uses_column", analysis.id, c) for c in sorted(set(analysis.sql_column_is) & column_ids))

    for foreign_key in document.data_layer.foreign_keys:
        if {foreign_key.source_column_id, foreign_key.target_column_id} <= column_ids:
            edges.append(("foreign_key", foreign_key.source_column_id, foreign_key.target_column_id))
    for join in document.data_layer.joins:
        if {join.source_table_id, join.target_table_id} <= table_ids:
            edges.append(("join", join.source_table_id, join.target_table_id))
    for semantic_fk in semantic.semantic_fks:
        if semantic_fk.column_attribute_id in attribute_ids and semantic_fk.column_id in column_ids:
            edges.append(("semantic_fk", semantic_fk.column_attribute_id, semantic_fk.column_id))

    raw_ids = [item.raw_id for item in nodes]
    if len(raw_ids) != len(set(raw_ids)):
        raise ValueError("The model export repeats an object id")
    nodes.sort(key=lambda item: (_NODE_ORDER[item.node.kind], item.node.label.casefold(), item.raw_id))
    omitted_nodes += max(0, len(nodes) - LIMITS["nodes"])
    nodes = nodes[: LIMITS["nodes"]]
    public = {item.raw_id: item.node.id for item in nodes}

    snapshot_edges: dict[tuple[str, str, str], SnapshotEdge] = {}
    omitted_edges = 0
    for kind, source, target in sorted(edges, key=lambda edge: (_EDGE_ORDER[edge[0]], edge[1], edge[2])):
        if source not in public or target not in public:
            omitted_edges += 1
            continue
        identity = (kind, public[source], public[target])
        edge_id = f"edge:{hashlib.sha256(chr(0).join(identity).encode()).hexdigest()[:24]}"
        snapshot_edges.setdefault(identity, SnapshotEdge(id=edge_id, kind=kind, source=identity[1], target=identity[2]))
    returned_edges = list(snapshot_edges.values())
    omitted_edges += max(0, len(returned_edges) - LIMITS["edges"])
    returned_edges = returned_edges[: LIMITS["edges"]]

    returned_nodes = [item.node for item in nodes]
    stats = SnapshotStats(
        nodes=len(returned_nodes), edges=len(returned_edges), omitted_nodes=omitted_nodes, omitted_edges=omitted_edges
    )
    content = {
        "source_id": source_id,
        "database_name": database_name,
        "stats": stats.model_dump(),
        "nodes": [node.model_dump(exclude_none=True) for node in returned_nodes],
        "edges": [edge.model_dump() for edge in returned_edges],
    }
    revision = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return OntologySnapshot(
        source_id=source_id,
        database_name=database_name,
        revision=f"sha256:{revision}",
        truncated=bool(omitted_nodes or omitted_edges),
        stats=stats,
        nodes=returned_nodes,
        edges=returned_edges,
    )


def _text(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]

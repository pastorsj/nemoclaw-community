# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Auto Ontology's model export (the fields the API reads) and the ontology snapshot it serves.

Parsing keeps only these fields. Everything else in an export (sample values, stored SQL, paths,
connection settings) is dropped on read, so it can never reach the browser.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class _Export(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DatabaseSummary(_Export):
    id: str = Field(min_length=1, max_length=512)
    name: str | None = Field(default=None, max_length=256)


class DatabaseList(_Export):
    data: list[DatabaseSummary]


class ModelColumn(_Export):
    id: str = Field(min_length=1, max_length=512)
    name: str = ""
    description: str = ""
    type: str = ""
    is_nullable: bool = True
    is_unique: bool = False


class ModelTable(_Export):
    id: str = Field(min_length=1, max_length=512)
    name: str = ""
    description: str = ""
    pk: list[str] = Field(default_factory=list)
    type: str = ""
    columns: list[ModelColumn] = Field(default_factory=list)


class ModelSchema(_Export):
    id: str = Field(min_length=1, max_length=512)
    name: str = ""
    tables: list[ModelTable] = Field(default_factory=list)


class ModelDatabase(_Export):
    id: str = Field(min_length=1, max_length=512)
    dialect: str = ""
    schemas: list[ModelSchema] = Field(default_factory=list)


class ForeignKey(_Export):
    source_column_id: str
    target_column_id: str


class Join(_Export):
    source_table_id: str
    target_table_id: str


class DataLayer(_Export):
    databases: list[ModelDatabase] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)


class ColumnAttribute(_Export):
    id: str = Field(min_length=1, max_length=512)
    name: str = ""
    description: str = ""
    column_id: str = ""


class Term(_Export):
    id: str = Field(min_length=1, max_length=512)
    name: str = ""
    description: str = ""
    represents: list[str] = Field(default_factory=list)
    columns_attributes: list[ColumnAttribute] = Field(default_factory=list)


class SemanticForeignKey(_Export):
    column_attribute_id: str
    column_id: str


class SqlAttribute(_Export):
    """A metric: a named calculation over columns, optionally attached to a term."""

    id: str = Field(min_length=1, max_length=512)
    name: str = ""
    description: str = ""
    sql_column_is: list[str] = Field(default_factory=list)
    term_id: str = ""


class SqlAttributes(_Export):
    manual: list[SqlAttribute] = Field(default_factory=list)
    table: list[SqlAttribute] = Field(default_factory=list)
    sql: list[SqlAttribute] = Field(default_factory=list)
    bridge_table: list[SqlAttribute] = Field(default_factory=list)


class CustomAnalysis(_Export):
    id: str = Field(min_length=1, max_length=512)
    name: str = ""
    description: str = ""
    sql_column_is: list[str] = Field(default_factory=list)


class SemanticLayer(_Export):
    terms: list[Term] = Field(default_factory=list)
    semantic_fks: list[SemanticForeignKey] = Field(default_factory=list)
    sql_attributes: SqlAttributes = Field(default_factory=SqlAttributes)
    custom_analyses: list[CustomAnalysis] = Field(default_factory=list)


class ModelDocument(_Export):
    data_layer: DataLayer = Field(default_factory=DataLayer)
    semantic_layer: SemanticLayer = Field(default_factory=SemanticLayer)


NodeKind = Literal["database", "schema", "table", "column", "term", "attribute", "metric", "analysis"]
EdgeKind = Literal[
    "contains",
    "foreign_key",
    "join",
    "represents",
    "has_attribute",
    "maps_to",
    "semantic_fk",
    "has_metric",
    "uses_column",
]


class SnapshotNode(BaseModel):
    id: str
    kind: NodeKind
    layer: Literal["physical", "semantic"]
    label: str
    description: str | None = None
    dialect: str | None = None
    data_type: str | None = None
    table_type: str | None = None
    nullable: bool | None = None
    unique: bool | None = None
    primary_key: bool | None = None


class SnapshotEdge(BaseModel):
    id: str
    kind: EdgeKind
    source: str
    target: str


class SnapshotStats(BaseModel):
    nodes: int
    edges: int
    omitted_nodes: int
    omitted_edges: int


class OntologySnapshot(BaseModel):
    """``GET /v1/data_sources/{id}/ontology``: a bounded graph of one database's ontology."""

    schema_version: Literal["1.0"] = "1.0"
    source_id: str
    database_name: str
    revision: str
    truncated: bool
    stats: SnapshotStats
    nodes: list[SnapshotNode]
    edges: list[SnapshotEdge]

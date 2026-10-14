# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The active pack and its data sources, and the read-only data viewer for the structured source.

- ``schema``: tables, views, columns and keys, read from the DuckDB file.
- ``preview``: the first 100 rows of one table or view.
- ``query``: one bounded SELECT, run in a separate process (``database/worker.py``).
- ``ontology``: the Auto Ontology graph of the database (ontology profile only).
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from pydantic import BaseModel
from pydantic import Field

from demo_api.auto_ontology.client import AutoOntologyClient
from demo_api.auto_ontology.client import AutoOntologyError
from demo_api.auto_ontology.models import OntologySnapshot
from demo_api.database.query import QueryError
from demo_api.database.query import run_query
from demo_api.database.schema import governed_columns
from demo_api.database.schema import read_schema
from demo_api.pack import PackUnavailableError
from demo_api.pack import PackView
from demo_api.pack import Source
from demo_api.services import Services
from demo_api.services import ServicesDep

router = APIRouter(prefix="/v1", tags=["data sources"])


class QueryRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)


@router.get("/pack")
async def pack(services: ServicesDep) -> PackView:
    """Title, disclaimer, demo questions and the example picker's questions of the active data pack."""
    try:
        return services.pack.public_view()
    except PackUnavailableError as error:
        raise HTTPException(503, str(error)) from error


@router.get("/data_sources")
async def data_sources(services: ServicesDep) -> list[dict[str, Any]]:
    """The sources a question can use: those the running tools can serve."""
    try:
        return [source.public() for source in services.pack.sources()]
    except PackUnavailableError as error:
        raise HTTPException(503, str(error)) from error


@router.get("/data_sources/{source_id}/schema")
async def schema(source_id: str, services: ServicesDep) -> dict[str, Any]:
    source = _structured(services, source_id)
    tables = await asyncio.to_thread(read_schema, services.pack.database_path())
    return {"source_id": source.id, "database_name": source.database_name, **tables}


@router.get("/data_sources/{source_id}/preview")
async def preview(
    source_id: str, table: str, services: ServicesDep, limit: Annotated[int, Query(ge=1, le=100)] = 100
) -> dict[str, Any]:
    """The first ``limit`` rows of ``table`` (``name``, or ``schema.name`` outside ``main``)."""
    source = _structured(services, source_id)
    tables = await asyncio.to_thread(read_schema, services.pack.database_path())
    match = next((item for item in tables["tables"] if item["name"] == table), None)
    if match is None:
        raise HTTPException(404, f"No table or view named {table}.")
    relation = f'"{match["schema"]}"."{table.rsplit(".", 1)[-1]}"'
    return {"table": table, **await _query(services, source, f"SELECT * FROM {relation}", tables, max_rows=limit)}


@router.post("/data_sources/{source_id}/query")
async def query(source_id: str, body: QueryRequest, services: ServicesDep) -> dict[str, Any]:
    source = _structured(services, source_id)
    tables = await asyncio.to_thread(read_schema, services.pack.database_path())
    return await _query(services, source, body.sql, tables)


@router.get("/data_sources/{source_id}/ontology")
async def ontology(source_id: str, services: ServicesDep) -> OntologySnapshot:
    source = _structured(services, source_id)
    settings = services.settings
    if not settings.auto_ontology_url:
        raise HTTPException(404, "Auto Ontology is not running (ontology profile).")
    client = AutoOntologyClient(
        settings.auto_ontology_url,
        email=settings.auto_ontology_email,
        password=settings.auto_ontology_password.get_secret_value(),
        origin=settings.auto_ontology_origin,
        transport=services.transport,
    )
    try:
        async with client:
            return await client.ontology_snapshot(source_id=source.id, database_name=source.database_name)
    except AutoOntologyError as error:
        raise HTTPException(error.status_code, str(error)) from error


def _structured(services: Services, source_id: str) -> Source:
    try:
        source = next((source for source in services.pack.sources() if source.id == source_id), None)
    except PackUnavailableError as error:
        raise HTTPException(503, str(error)) from error
    if source is None or source.database_name is None:
        raise HTTPException(404, f"{source_id} is not an available structured data source.")
    return source


async def _query(
    services: Services, source: Source, sql: str, schema: dict[str, Any], *, max_rows: int = 100
) -> dict[str, Any]:
    try:
        path = services.pack.database_path()
        return await run_query(path, source.database_name, sql, governed_columns(schema), max_rows=max_rows)
    except QueryError as error:
        raise HTTPException(error.status_code, error.message) from error

# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""MCP server (streamable HTTP at /mcp) exposing retrieve_evidence, plus GET /health."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import budget
from .datapack import CollectionManifest
from .datapack import Pack
from .search import RetrievalResult
from .search import Retriever
from .settings import Settings

PORT = 8120

logger = logging.getLogger(__name__)


def create_server(retriever: Retriever, document_sources: frozenset[str]) -> MCPServer:
    server = MCPServer(
        "retrieval",
        instructions="Search the document sources selected for this run and return reranked, citable passages.",
    )

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def retrieve_evidence(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=4000,
                description="What the passages should say, in their words; for filings, no form names such as 8-K",
            ),
        ],
        source_ids: Annotated[
            list[str], Field(description="Document sources to search. The application sets this for each run.")
        ],
        top_k: Annotated[
            int, Field(ge=1, le=25, description=f"How many passages to return; at most {budget.MAX_HITS} are returned")
        ] = 8,
    ) -> RetrievalResult:
        """Search the selected document sources and return the best passages with title, URL and date.

        Passages from all sources are ranked together by an NVIDIA Nemotron reranker; one call returns at most 8.
        When you search filings, write the query as one sentence the passage itself would contain, such as "the
        company will close a plant and cut jobs", not as a list of keywords joined by "or", which matches the risk
        lists of forward-looking statements. Leave form names (8-K, 6-K) and words such as "filing" or "current
        report" out of it: every filing's cover page repeats them, so they return cover pages. Each passage's
        metadata gives its form and filing date. Search each topic once, and rephrase at most once: when the
        passages lack a detail, say so instead of searching again.
        """
        requested = sorted(set(source_ids))
        if not requested or not document_sources.issuperset(requested):
            raise ToolError(f"source_ids must be a non-empty subset of {sorted(document_sources)}, got {source_ids}")
        return budget.fit(await retriever.retrieve(query, requested, min(top_k, budget.MAX_HITS)))

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "collection": retriever.collection})

    return server


def serve(settings: Settings, data_dir: Path) -> None:
    pack = Pack.load(data_dir)
    manifest = CollectionManifest.load(data_dir)
    if manifest.embed_model != settings.embed_model:
        raise SystemExit(
            f"{manifest.collection} was indexed with {manifest.embed_model}, but RETRIEVER_EMBED_MODEL is "
            f"{settings.embed_model}. Run `demo-retrieval ingest` to re-index."
        )
    server = create_server(Retriever(settings, manifest.collection), pack.document_sources)
    logger.info("serving %s for sources %s on :%d/mcp", manifest.collection, sorted(pack.document_sources), PORT)
    server.run("streamable-http", host="0.0.0.0", port=PORT, stateless_http=True, json_response=True)

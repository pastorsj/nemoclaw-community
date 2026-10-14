# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Embed the query once, search each selected source for a fair share of candidates, rerank them all at once."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import AbstractContextManager
from typing import Any

from langchain_core.documents import Document
from openinference.semconv.trace import DocumentAttributes
from openinference.semconv.trace import OpenInferenceSpanKindValues as SpanKind
from openinference.semconv.trace import RerankerAttributes
from openinference.semconv.trace import SpanAttributes
from opentelemetry import trace
from opentelemetry.trace import Span
from pydantic import BaseModel
from pydantic import Field
from pymilvus import AsyncMilvusClient

from . import nvidia
from . import store
from .settings import Settings

CANDIDATES_PER_HIT = 4
# A passage is at most one chunk long (ingest.CHUNK_SIZE); a longer one ends in an ellipsis. Passages are not cut
# shorter: the fact a question needs can sit at a chunk's end (Form 8-K's Item 1.05 deadline sits at characters
# 1,403 to 1,540 of its chunk), so a result fits its size budget by returning fewer passages instead.
MAX_SNIPPET_CHARS = 2400

tracer = trace.get_tracer(__name__)


class Hit(BaseModel):
    rank: int
    score: float = Field(description="Reranker relevance (logit); hits are ordered by it")
    vector_score: float = Field(description="Cosine similarity from the vector search")
    source_id: str
    document_id: str
    chunk_id: str
    title: str
    url: str | None
    published_at: str | None
    snippet: str
    metadata: dict[str, Any] = Field(description="Source-specific fields, e.g. filing form or regulation section")


class Models(BaseModel):
    embed: str
    rerank: str


class IndexInfo(BaseModel):
    type: str
    metric: str
    params: dict[str, int]
    search_params: dict[str, int]


class Timings(BaseModel):
    embed_ms: float
    search_ms: float
    rerank_ms: float
    total_ms: float


class RetrievalResult(BaseModel):
    query: str
    source_ids: list[str]
    collection: str = Field(description="The alias the tool searches")
    collection_version: str = Field(description="The index build the alias pointed at: <alias>__<fingerprint>")
    hits: list[Hit]
    candidate_counts: dict[str, int] = Field(description="Vector-search candidates per source, all reranked together")
    models: Models
    index: IndexInfo
    timings: Timings


class Retriever:
    def __init__(self, settings: Settings, collection: str) -> None:
        self.collection = collection
        self.models = Models(embed=settings.embed_model, rerank=settings.rerank_model)
        self._embedder = nvidia.embedder(settings)
        self._reranker = nvidia.reranker(settings)
        self._milvus = AsyncMilvusClient(uri=settings.milvus_uri)  # connects on first use, inside the event loop

    async def close(self) -> None:
        await self._milvus.close()

    async def retrieve(self, query: str, source_ids: list[str], top_k: int) -> RetrievalResult:
        started = time.perf_counter()
        embed = {SpanAttributes.EMBEDDING_MODEL_NAME: self.models.embed, SpanAttributes.INPUT_VALUE: query}
        with _span("embed", SpanKind.EMBEDDING, embed):
            vector = await self._embed_query(query)
        embedded = time.perf_counter()

        # Every source gets the same share, and the shares together fit in one rerank request.
        per_source = min(top_k * CANDIDATES_PER_HIT, nvidia.MAX_RERANK_PASSAGES // len(source_ids))
        with _span("search", SpanKind.RETRIEVER, {SpanAttributes.INPUT_VALUE: query}) as span:
            # Resolve the alias once, so every source is searched in the same build, even during a reindex.
            version = (await self._milvus.describe_alias(self.collection))["collection_name"]
            scope = {"collection": version, "source_ids": source_ids, "per_source": per_source}
            span.set_attribute(SpanAttributes.METADATA, json.dumps(scope))
            searches = [store.search(self._milvus, version, vector, source, per_source) for source in source_ids]
            groups = await asyncio.gather(*searches)
        searched = time.perf_counter()

        candidates = [candidate for group in groups for candidate in group]
        rerank = {
            RerankerAttributes.RERANKER_MODEL_NAME: self.models.rerank,
            RerankerAttributes.RERANKER_QUERY: query,
            RerankerAttributes.RERANKER_TOP_K: top_k,
        }
        with _span("rerank", SpanKind.RERANKER, rerank) as span:
            ranked = (await self._rerank(query, candidates))[:top_k] if candidates else []
            for i, ((fields, _), score) in enumerate(ranked):
                document = f"{RerankerAttributes.RERANKER_OUTPUT_DOCUMENTS}.{i}."
                span.set_attribute(document + DocumentAttributes.DOCUMENT_ID, fields["chunk_id"])
                span.set_attribute(document + DocumentAttributes.DOCUMENT_SCORE, score)
        finished = time.perf_counter()

        return RetrievalResult(
            query=query,
            source_ids=source_ids,
            collection=self.collection,
            collection_version=version,
            hits=[_hit(rank, candidate, score) for rank, (candidate, score) in enumerate(ranked, start=1)],
            candidate_counts={source_id: len(group) for source_id, group in zip(source_ids, groups, strict=True)},
            models=self.models,
            index=IndexInfo(
                type=store.INDEX["index_type"],
                metric=store.INDEX["metric_type"],
                params=store.INDEX["params"],
                search_params=store.SEARCH_PARAMS["params"],
            ),
            timings=Timings(
                embed_ms=_ms(embedded - started),
                search_ms=_ms(searched - embedded),
                rerank_ms=_ms(finished - searched),
                total_ms=_ms(finished - started),
            ),
        )

    @nvidia.retry_transient
    async def _embed_query(self, query: str) -> list[float]:
        return await self._embedder.aembed_query(query)  # input_type=query

    @nvidia.retry_transient
    async def _rerank(self, query: str, candidates: list[store.Candidate]) -> list[tuple[store.Candidate, float]]:
        """All candidates in one request, best first. Passages carry text only, never metadata."""
        documents = [Document(id=fields["chunk_id"], page_content=fields["text"]) for fields, _ in candidates]
        by_id = {fields["chunk_id"]: (fields, similarity) for fields, similarity in candidates}
        ranked = await self._reranker.acompress_documents(documents, query)
        return [(by_id[document.id], document.metadata["relevance_score"]) for document in ranked]


def _hit(rank: int, candidate: store.Candidate, score: float) -> Hit:
    fields, similarity = candidate
    return Hit(
        rank=rank,
        score=score,
        vector_score=similarity,
        source_id=fields["source_id"],
        document_id=fields["document_id"],
        chunk_id=fields["chunk_id"],
        title=fields["title"],
        url=fields["url"],
        published_at=fields["published_at"],
        snippet=clip(fields["text"], MAX_SNIPPET_CHARS),
        metadata={key: value for key, value in fields.items() if key not in store.SCALAR_FIELDS},
    )


def clip(text: str, limit: int) -> str:
    """`text`, or its first `limit` characters cut at a word boundary and ending in an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    space = head.rfind(" ")
    return (head[:space] if space > limit * 0.8 else head).rstrip() + "…"


def _span(name: str, kind: SpanKind, attributes: dict[str, Any]) -> AbstractContextManager[Span]:
    return tracer.start_as_current_span(
        name, attributes={SpanAttributes.OPENINFERENCE_SPAN_KIND: kind.value, **attributes}
    )


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)

# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The active data pack (/data/active): pack.json and corpus/documents.jsonl in, collection-manifest.json (and on a
GPU host the retrieval-benchmark files) out."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from .store import SCALAR_FIELDS
from .store import VECTOR_FIELD

DOCUMENTS = Path("corpus/documents.jsonl")
MANIFEST = Path("collection-manifest.json")
BENCHMARK = Path("retrieval-benchmark.json")  # the comparison the API serves: only the one-shot of `up` writes it
BENCHMARK_GUARD = Path("retrieval-benchmark-guard.json")  # the GPU guard's own measurement, never served
BENCHMARK_QUERIES = Path("retrieval-benchmark-queries.npz")  # the build's benchmark query vectors, embedded once


@dataclass(frozen=True)
class BenchmarkQuery:
    query: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class Pack:
    collection: str  # documents.collection: the Milvus alias that retrieve_evidence searches
    document_sources: frozenset[str]  # the only source_ids retrieve_evidence accepts
    # documents.benchmark_queries: held-out queries for the CPU/GPU index comparison (retrieval-benchmark)
    benchmark_queries: tuple[BenchmarkQuery, ...] = ()

    @classmethod
    def load(cls, data_dir: Path) -> Pack:
        pack = json.loads((data_dir / "pack.json").read_text())
        sources = frozenset(source["id"] for source in pack["sources"] if source.get("kind") == "documents")
        queries = tuple(
            BenchmarkQuery(query=entry["query"], source_ids=tuple(entry["sources"]))
            for entry in pack["documents"].get("benchmark_queries", [])
        )
        return cls(collection=pack["documents"]["collection"], document_sources=sources, benchmark_queries=queries)


class CorpusDocument(BaseModel):
    """One row of corpus/documents.jsonl."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    url: str | None = None
    published_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _no_reserved_keys(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        if "image" in metadata:
            raise ValueError('metadata key "image" is reserved: NVIDIARerank would send it to the reranker as an image')
        # Metadata keys become Milvus dynamic fields, which may not shadow the schema's own fields.
        if clash := sorted(metadata.keys() & {*SCALAR_FIELDS, VECTOR_FIELD}):
            raise ValueError(f"metadata keys {clash} collide with Milvus schema fields")
        return metadata


def iter_documents(data_dir: Path) -> Iterator[CorpusDocument]:
    """corpus/documents.jsonl, one validated row at a time, so a corpus never has to fit in memory."""
    with (data_dir / DOCUMENTS).open(encoding="utf-8") as lines:
        for line in lines:
            if line.strip():
                yield CorpusDocument.model_validate_json(line)


class CollectionManifest(BaseModel):
    """What retrieval-index built. The server and the API read it to find the collection."""

    collection: str
    physical_collection: str
    source_ids: list[str]
    document_count: int
    chunk_count: int
    embed_model: str
    index: dict[str, Any]

    @classmethod
    def load(cls, data_dir: Path) -> CollectionManifest:
        return cls.model_validate_json((data_dir / MANIFEST).read_text())

    def write(self, data_dir: Path) -> None:
        # Write then rename, so a reader never sees a half-written file.
        staged = data_dir / f"{MANIFEST}.tmp"
        staged.write_text(self.model_dump_json(indent=2) + "\n")
        staged.replace(data_dir / MANIFEST)

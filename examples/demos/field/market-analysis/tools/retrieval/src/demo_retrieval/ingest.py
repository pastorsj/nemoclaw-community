# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""retrieval-index: documents.jsonl -> chunks -> passage embeddings -> Milvus -> alias -> collection-manifest.json.

Each build gets its own collection, named by a fingerprint of the corpus and the embed configuration.
Re-running on unchanged input is a no-op. A changed input builds a new collection and moves the alias only
once it is complete, so the server answers from the previous build until then.

The corpus is streamed, never held in memory: one pass validates it, a second chunks, embeds and inserts it in
batches. An interrupted build resumes: the next run finds the unfinished collection (the fingerprint names it),
skips the chunks it already holds, and embeds only the rest.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import batched
from pathlib import Path
from typing import Any

from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pymilvus import MilvusClient

from . import nvidia
from . import store
from .datapack import DOCUMENTS
from .datapack import CollectionManifest
from .datapack import CorpusDocument
from .datapack import Pack
from .datapack import iter_documents
from .settings import Settings

CHUNK_SIZE = 2400  # characters
CHUNK_OVERLAP = 240
EMBED_BATCH = 50  # texts per embeddings request

logger = logging.getLogger(__name__)
splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)


@dataclass(frozen=True)
class Corpus:
    document_count: int
    source_ids: list[str]


def run(settings: Settings, data_dir: Path) -> CollectionManifest:
    pack = Pack.load(data_dir)
    corpus = survey(data_dir)
    if unknown := sorted(set(corpus.source_ids) - pack.document_sources):
        raise ValueError(f"documents.jsonl uses sources that pack.json does not declare as documents: {unknown}")
    collection = store.build_name(pack.collection, _fingerprint(data_dir, settings))

    client = MilvusClient(uri=settings.milvus_uri)
    try:
        if store.alias_target(client, pack.collection) == collection:
            logger.info("%s already serves this corpus; nothing to index", collection)
            chunk_count = sum(1 for _ in chunks(data_dir))
        else:
            chunk_count = build(client, collection, chunks(data_dir), nvidia.embedder(settings))
            store.point_alias(client, pack.collection, collection)
    finally:
        client.close()

    manifest = CollectionManifest(
        collection=pack.collection,
        physical_collection=collection,
        source_ids=corpus.source_ids,
        document_count=corpus.document_count,
        chunk_count=chunk_count,
        embed_model=settings.embed_model,
        index=store.INDEX,
    )
    manifest.write(data_dir)
    logger.info("%s -> %s: %d chunks of %d documents", pack.collection, collection, chunk_count, corpus.document_count)
    return manifest


def survey(data_dir: Path) -> Corpus:
    """Validate every row before anything is embedded: the schema, and document ids that are unique."""
    path = data_dir / DOCUMENTS
    ids: set[str] = set()
    sources: set[str] = set()
    for document in iter_documents(data_dir):
        if document.document_id in ids:
            raise ValueError(f"{path} repeats document_id {document.document_id!r}")
        ids.add(document.document_id)
        sources.add(document.source_id)
    if not ids:
        raise ValueError(f"{path} has no documents")
    return Corpus(document_count=len(ids), source_ids=sorted(sources))


def chunks(data_dir: Path) -> Iterator[dict[str, Any]]:
    for document in iter_documents(data_dir):
        yield from chunk(document)


def chunk(document: CorpusDocument) -> Iterator[dict[str, Any]]:
    for number, text in enumerate(splitter.split_text(document.text), start=1):
        yield {
            **document.metadata,
            "chunk_id": f"{document.document_id}:{number:04d}",
            "source_id": document.source_id,
            "document_id": document.document_id,
            "title": document.title,
            "url": document.url,
            "published_at": document.published_at,
            "text": text,
        }


def build(client: MilvusClient, collection: str, rows: Iterable[dict[str, Any]], embedder: NVIDIAEmbeddings) -> int:
    """Embed and insert the rows in batches, skipping chunks that an interrupted build already inserted.

    Returns the number of chunks. A chunk id is stable for a given corpus, so a present id is a finished chunk.
    """
    exists = client.has_collection(collection)
    if exists:
        logger.info("resuming the unfinished build %s", collection)
        client.load_collection(collection)  # a lookup needs it loaded, and a Milvus restart may have released it
    total = skipped = 0
    for number, batch in enumerate(batched(rows, EMBED_BATCH), start=1):
        total += len(batch)
        present = store.present(client, collection, [row["chunk_id"] for row in batch]) if exists else set()
        skipped += len(present)
        if todo := [row for row in batch if row["chunk_id"] not in present]:
            vectors = _embed_passages(embedder, [row["text"] for row in todo])
            if not exists:  # the first response tells us the embedding dimension
                store.create_collection(client, collection, dimension=len(vectors[0]))
                exists = True
            client.insert(collection, [{**row, store.VECTOR_FIELD: v} for row, v in zip(todo, vectors, strict=True)])
        if number % 20 == 0:
            logger.info("indexed %d chunks into %s, %d of them by an earlier run", total, collection, skipped)
    logger.info("indexed %d chunks into %s, %d of them by an earlier run", total, collection, skipped)
    return total


@nvidia.retry_bulk
def _embed_passages(embedder: NVIDIAEmbeddings, texts: list[str]) -> list[list[float]]:
    return embedder.embed_documents(texts)  # input_type=passage


def _fingerprint(data_dir: Path, settings: Settings) -> str:
    """The corpus bytes plus everything that changes the vectors or the index."""
    with (data_dir / DOCUMENTS).open("rb") as corpus:
        digest = hashlib.file_digest(corpus, "sha256")
    config = [settings.base_url, settings.embed_model, CHUNK_SIZE, CHUNK_OVERLAP, store.INDEX]
    digest.update(json.dumps(config, sort_keys=True).encode())
    return digest.hexdigest()[:12]

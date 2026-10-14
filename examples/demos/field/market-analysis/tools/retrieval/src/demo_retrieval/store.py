# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Milvus layout: one immutable collection per index build, behind an alias the server searches.

Ingest builds with the sync MilvusClient; search runs on the AsyncMilvusClient.
"""

from __future__ import annotations

import json
from typing import Any

from pymilvus import AsyncMilvusClient
from pymilvus import DataType
from pymilvus import MilvusClient

SCALAR_FIELDS = ("chunk_id", "source_id", "document_id", "title", "url", "published_at", "text")
VECTOR_FIELD = "embedding"
INDEX = {"index_type": "HNSW", "metric_type": "COSINE", "params": {"M": 16, "efConstruction": 200}}
# HNSW needs ef >= the search limit; the tool's per-source limit is at most 4 x 25 = 100.
SEARCH_PARAMS = {"metric_type": "COSINE", "params": {"ef": 128}}

# The analytics-gpu profile's mirror of the collection, for retrieval-benchmark only: NVIDIA cuVS IVF-Flat on the GPU,
# as the original demo used, over L2-normalized vectors, so inner product ranks as cosine does on the CPU index. Probing
# half the lists matched the CPU index's neighbors on every benchmark query of a 32,676-chunk corpus (A100). GPU_CAGRA
# returned wrong neighbors on these 2048-dimension vectors in Milvus 2.6.25 (recall 0, whatever its build algorithm).
GPU_INDEX = {"index_type": "GPU_IVF_FLAT", "metric_type": "IP", "params": {"nlist": 128}}
GPU_SEARCH_PARAMS = {"metric_type": "IP", "params": {"nprobe": 64}}

Candidate = tuple[dict[str, Any], float]  # (stored fields including dynamic metadata, cosine similarity)


def create_collection(client: MilvusClient, name: str, dimension: int) -> None:
    # Dynamic fields hold each source's own metadata (form, section, ...) beside these columns.
    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field("chunk_id", DataType.VARCHAR, max_length=512, is_primary=True)
    # The partition key keeps each source's chunks together, so a per-source filter scans one partition.
    schema.add_field("source_id", DataType.VARCHAR, max_length=128, is_partition_key=True)
    schema.add_field("document_id", DataType.VARCHAR, max_length=512)
    schema.add_field("title", DataType.VARCHAR, max_length=4096)
    schema.add_field("url", DataType.VARCHAR, max_length=4096, nullable=True)
    schema.add_field("published_at", DataType.VARCHAR, max_length=64, nullable=True)
    schema.add_field("text", DataType.VARCHAR, max_length=65535)
    schema.add_field(VECTOR_FIELD, DataType.FLOAT_VECTOR, dim=dimension)
    index = client.prepare_index_params()
    index.add_index(VECTOR_FIELD, **INDEX)
    client.create_collection(name, schema=schema, index_params=index, consistency_level="Strong")


def present(client: MilvusClient, collection: str, chunk_ids: list[str]) -> set[str]:
    """Which of these chunk ids the collection already holds (a primary-key lookup).

    The ids are JSON-quoted, as in `search`: MilvusClient.get wraps them in single quotes without escaping, so one
    apostrophe in a document id breaks the expression.
    """
    rows = client.query(collection, filter=f"chunk_id in {json.dumps(chunk_ids)}", output_fields=["chunk_id"])
    return {row["chunk_id"] for row in rows}


def build_name(alias: str, digest: str) -> str:
    return f"{alias}__{digest}"


def alias_target(client: MilvusClient, alias: str) -> str | None:
    # list_aliases returns {"aliases": [...]}, whatever its annotation says; describe_alias on a missing
    # alias would raise and log an error instead.
    if alias not in client.list_aliases()["aliases"]:
        return None
    return client.describe_alias(alias)["collection_name"]


def point_alias(client: MilvusClient, alias: str, collection: str) -> None:
    """Swap the alias to a finished build in one step, then drop every other build behind it."""
    if alias_target(client, alias) is None:
        client.create_alias(collection, alias)
    else:
        client.alter_alias(collection, alias)
    for name in client.list_collections():
        if name.startswith(build_name(alias, "")) and name != collection:
            client.drop_collection(name)


async def search(
    client: AsyncMilvusClient, collection: str, vector: list[float], source_id: str, limit: int
) -> list[Candidate]:
    """The chunks of one source nearest to the vector."""
    results = await client.search(
        collection,
        data=[vector],
        anns_field=VECTOR_FIELD,
        filter=f"source_id == {json.dumps(source_id)}",
        limit=limit,
        search_params=SEARCH_PARAMS,
        output_fields=[*SCALAR_FIELDS, "$meta"],
    )
    return [(hit["entity"], hit["distance"]) for hit in results[0]]

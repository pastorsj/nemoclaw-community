# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""retrieval-benchmark: the CPU index against an NVIDIA GPU (cuVS) index of the same vectors (analytics-gpu profile).

The one-shot reads the active build's vectors from Milvus and mirrors them, L2-normalized, into the GPU Milvus
(MILVUS_GPU_URI) under a GPU_IVF_FLAT index. It embeds the pack's held-out queries (documents.benchmark_queries) once
per build and keeps their vectors beside the manifest (retrieval-benchmark-queries.npz), so every measurement of a
build searches the same vectors: the hosted model's vector for one text differs in the last bits from call to call. It
times their vector searches on both indexes in three workload profiles: one query per request, batches of five query
vectors, and five concurrent requests. Each profile warms both indexes up, then repeats its requests three times,
alternating which index goes first. Only the Milvus search call is timed; nothing is reranked.

Each profile reports recall@10 against the exact inner-product neighbors (computed here from the same vectors) and the
overlap of the two indexes' results. Both compare neighbors by their exact score, from one product of the query with
every vector, so a chunk scores the same number as a true neighbor and as a result. Scores within TIE_TOLERANCE are one
neighbor, so chunks with identical vectors (boilerplate repeated across filings) count as the same neighbor whichever of
them an index returns. A profile claims a GPU speedup only when those quality gates pass and the CPU's total search
time is at least 1.1 times the GPU's.

The one-shot (`demo.sh up`, `data reindex`) writes retrieval-benchmark.json beside collection-manifest.json, where the
API reads it for runs on the same build (demo_api.benchmark.RetrievalBenchmark). On an unchanged build whose GPU copy
has the current index it is a no-op; a new GPU index rebuilds the copy and measures again. `benchmark --guard` (a GPU
guard run by hand) measures the build once more into retrieval-benchmark-guard.json, and never changes
what the API serves. The one-shot never fails the stack: a problem is logged, and the Benchmark tab says no comparison
applies.

Answers never come from the GPU mirror: retrieve_evidence searches the CPU index on every host.
"""

from __future__ import annotations

import json
import logging
import time
import zipfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from functools import cached_property
from pathlib import Path
from statistics import fmean
from typing import Any
from typing import Literal

import numpy as np
from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings
from pymilvus import DataType
from pymilvus import MilvusClient

from . import nvidia
from . import store
from .datapack import BENCHMARK
from .datapack import BENCHMARK_GUARD
from .datapack import BENCHMARK_QUERIES
from .datapack import MANIFEST
from .datapack import BenchmarkQuery
from .datapack import CollectionManifest
from .datapack import Pack
from .settings import Settings

TOP_K = 10
REPETITIONS = 3
WARMUP_ROUNDS = 1
MIN_RECALL = 0.95  # mean recall@k of each index
MIN_WORST_QUERY_RECALL = 0.80
MIN_OVERLAP = 0.95  # mean Jaccard overlap of the two indexes' top-k
MIN_GPU_SPEEDUP = 1.10
# Exact scores this close are one neighbor: identical vectors, and float32 rounding. Distinct chunks sit further apart:
# no two of the 30 best exact scores of any benchmark query were within 1e-6 of each other (docs/retrieval.md).
TIE_TOLERANCE = 1e-6
COPY_BATCH = 500  # rows per read and insert: 2048-dimension vectors keep a batch near 4 MB
INDEX_TIMEOUT_SECONDS = 900.0

logger = logging.getLogger(__name__)

Role = Literal["cpu", "gpu"]


@dataclass(frozen=True)
class Profile:
    profile_id: str
    mode: Literal["single", "batch", "concurrency"]
    batch_size: int = 1
    concurrency: int = 1


PROFILES = (
    Profile("vector-single", "single"),
    Profile("vector-batch", "batch", batch_size=5),
    Profile("vector-concurrency", "concurrency", concurrency=5),
)


@dataclass(frozen=True)
class Index:
    """One side of the comparison: a Milvus, the build's collection in it, and how to search it."""

    role: Role
    client: MilvusClient
    collection: str
    index_type: str
    search_params: dict[str, Any]


@dataclass(frozen=True)
class Vectors:
    """The build's rows: chunk ids, sources and vectors."""

    ids: np.ndarray
    sources: np.ndarray
    matrix: np.ndarray  # float32, each row L2-normalized

    def truth(self, vector: np.ndarray, scope: Sequence[str], k: int = TOP_K) -> Truth:
        """A query's exact neighbors (inner product): one product scores every chunk, and the k best scores of the
        scope's sources are the truth.

        The chunks an index returns are scored from the same product. Scored apart, one dot product each, about 0.5%
        of them came out different from the product's scores once rounded to six decimals, so true neighbors counted
        as misses, and which ones did changed with the last bits of each new query embedding.
        """
        scores = self.matrix @ vector
        best = np.sort(scores[np.isin(self.sources, list(scope))])[::-1][:k]
        return Truth(scores, best.tolist(), self.rows)

    @cached_property
    def rows(self) -> dict[str, int]:
        return {str(chunk_id): row for row, chunk_id in enumerate(self.ids)}


@dataclass(frozen=True)
class Truth:
    """One query's exact scores: every chunk's, by row, and the k best of its scope, best first."""

    scores: np.ndarray
    best: list[float]
    rows: dict[str, int]

    def score(self, chunk_id: str) -> float:
        """A returned chunk's exact score: the same number as in ``best`` when it is a true neighbor."""
        return float(self.scores[self.rows[chunk_id]])


@dataclass(frozen=True)
class Request:
    queries: tuple[int, ...]  # positions in the query list: one vector each
    scope: tuple[str, ...]


@dataclass
class Measurement:
    """One index's timed requests in one profile."""

    total_ms: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)
    requests: int = 0
    vectors: int = 0
    results: list[dict[int, list[Any]]] = field(default_factory=list)  # each repetition's neighbors, by query
    error: str | None = None


def run(
    settings: Settings,
    data_dir: Path,
    *,
    gpu_index: dict[str, Any] = store.GPU_INDEX,
    gpu_search_params: dict[str, Any] = store.GPU_SEARCH_PARAMS,
    guard: bool = False,
) -> dict[str, Any] | None:
    """Measure the active build into retrieval-benchmark.json, the file the API serves; None when there is nothing
    to compare.

    ``guard`` (a GPU guard run by hand) measures the build once more, already measured or not, into
    retrieval-benchmark-guard.json. It reuses the GPU copy and the build's query vectors, and writes nothing else.
    """
    if not settings.milvus_gpu_uri:
        logger.info("MILVUS_GPU_URI is not set: this stack has no GPU index to compare")
        return None
    if not (data_dir / MANIFEST).is_file():
        logger.info("the active build has no retrieval index: nothing to compare")
        return None
    manifest = CollectionManifest.load(data_dir)
    indexed = set(manifest.source_ids)
    queries = [query for query in Pack.load(data_dir).benchmark_queries if set(query.source_ids) <= indexed]
    if not queries:
        logger.warning("the pack has no documents.benchmark_queries for %s: nothing to compare", sorted(indexed))
        return None

    collection = manifest.physical_collection
    cached = _cached_queries(data_dir, manifest, queries)
    cpu = MilvusClient(uri=settings.milvus_uri)
    gpu = MilvusClient(uri=settings.milvus_gpu_uri)
    try:
        # A served result without the build's query vectors beside it is from an earlier version (a new embedding
        # per measurement, rounded scores): it counts as none, and the build is measured again.
        previous = _previous(data_dir, collection) if cached is not None and not guard else None
        if previous is not None and gpu.has_collection(collection) and _has_index(gpu, collection, gpu_index):
            logger.info("%s was already measured: %s", collection, data_dir / BENCHMARK)
            return previous
        vectors = read_vectors(cpu, collection)
        _wait_indexed(cpu, collection, len(vectors.ids))  # time the index, not a growing segment's brute force
        mirror(gpu, manifest.collection, collection, vectors, gpu_index)
        query_vectors = cached if cached is not None else embed_queries(nvidia.embedder(settings), queries)
        truths = [vectors.truth(vector, query.source_ids) for vector, query in zip(query_vectors, queries, strict=True)]
        indexes = {
            "cpu": Index("cpu", cpu, collection, manifest.index["index_type"], store.SEARCH_PARAMS),
            "gpu": Index("gpu", gpu, collection, gpu_index["index_type"], gpu_search_params),
        }
        profiles = [profile_result(profile, indexes, queries, query_vectors, truths=truths) for profile in PROFILES]
    finally:
        cpu.close()
        gpu.close()

    result = {
        "schemaVersion": "1",
        "collection": manifest.collection,
        "collectionVersion": collection,
        "embedModel": manifest.embed_model,
        "sourceIds": sorted({source for query in queries for source in query.source_ids}),
        "queryCount": len(queries),
        "measuredAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "profiles": profiles,
    }
    output = data_dir / (BENCHMARK_GUARD if guard else BENCHMARK)
    staged = output.with_name(f"{output.name}.tmp")  # write then rename, so a reader never sees half a file
    staged.write_text(json.dumps(result, indent=2) + "\n")
    staged.replace(output)
    if cached is None and not guard:  # after the result: its presence marks that result as measured from them
        _save_queries(data_dir, manifest, queries, query_vectors)
    for profile in profiles:
        quality = profile["quality"]
        logger.info(
            "%s: CPU %.1f ms, GPU %.1f ms, recall@%d CPU %s GPU %s, overlap %s, %s",
            profile["profileId"],
            profile["cpu"]["vectorSearchMs"],
            profile["gpu"]["vectorSearchMs"],
            TOP_K,
            quality["cpuRecallAtK"],
            quality["gpuRecallAtK"],
            quality["cpuGpuOverlapAtK"],
            profile["claim"]["decision"],
        )
    logger.info("wrote %s", output)
    return result


def _previous(data_dir: Path, collection: str) -> dict[str, Any] | None:
    try:
        previous = json.loads((data_dir / BENCHMARK).read_text())
    except (OSError, ValueError):
        return None
    return previous if previous.get("collectionVersion") == collection else None


def _cached_queries(
    data_dir: Path, manifest: CollectionManifest, queries: Sequence[BenchmarkQuery]
) -> list[np.ndarray] | None:
    """The query vectors an earlier measurement of this build embedded: same collection build, embed model and
    queries. None otherwise."""
    try:
        with np.load(data_dir / BENCHMARK_QUERIES, allow_pickle=False) as saved:
            key = (str(saved["collection_version"]), str(saved["embed_model"]), saved["queries"].tolist())
            vectors = saved["vectors"]
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    if key != (manifest.physical_collection, manifest.embed_model, [query.query for query in queries]):
        return None
    return list(vectors.astype(np.float32))


def _save_queries(
    data_dir: Path, manifest: CollectionManifest, queries: Sequence[BenchmarkQuery], vectors: Sequence[np.ndarray]
) -> None:
    staged = data_dir / f"{BENCHMARK_QUERIES}.tmp"
    with staged.open("wb") as file:
        np.savez(
            file,
            collection_version=np.str_(manifest.physical_collection),
            embed_model=np.str_(manifest.embed_model),
            queries=np.asarray([query.query for query in queries]),
            vectors=np.asarray(vectors, dtype=np.float32),
        )
    staged.replace(data_dir / BENCHMARK_QUERIES)


def read_vectors(client: MilvusClient, collection: str) -> Vectors:
    """Every row's chunk id, source and vector, L2-normalized."""
    ids: list[str] = []
    sources: list[str] = []
    rows: list[list[float]] = []
    client.load_collection(collection)
    iterator = client.query_iterator(
        collection, batch_size=COPY_BATCH, output_fields=["chunk_id", "source_id", store.VECTOR_FIELD]
    )
    try:
        while batch := iterator.next():
            for row in batch:
                ids.append(row["chunk_id"])
                sources.append(row["source_id"])
                rows.append(row[store.VECTOR_FIELD])
    finally:
        iterator.close()
    if not rows:
        raise ValueError(f"{collection} holds no vectors")
    return Vectors(np.asarray(ids), np.asarray(sources), _normalized(np.asarray(rows, dtype=np.float32)))


def mirror(client: MilvusClient, alias: str, collection: str, vectors: Vectors, index: dict[str, Any]) -> None:
    """The build's GPU copy: its chunk ids, sources and normalized vectors under `index`. Reused once complete.

    Older builds' copies are dropped, so the GPU holds one.
    """
    rows = len(vectors.ids)
    current = client.has_collection(collection) and _row_count(client, collection) == rows
    if current and _has_index(client, collection, index):
        logger.info("reusing the GPU copy of %s", collection)
    else:
        if client.has_collection(collection):
            client.drop_collection(collection)
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=512, is_primary=True)
        schema.add_field("source_id", DataType.VARCHAR, max_length=128, is_partition_key=True)
        schema.add_field(store.VECTOR_FIELD, DataType.FLOAT_VECTOR, dim=vectors.matrix.shape[1])
        client.create_collection(collection, schema=schema, consistency_level="Strong")
        for start in range(0, rows, COPY_BATCH):
            stop = start + COPY_BATCH
            batch = zip(vectors.ids[start:stop], vectors.sources[start:stop], vectors.matrix[start:stop], strict=True)
            client.insert(
                collection,
                [{"chunk_id": str(i), "source_id": str(s), store.VECTOR_FIELD: v.tolist()} for i, s, v in batch],
            )
        client.flush(collection)  # seal the segments, so the index covers every row
        params = client.prepare_index_params()
        params.add_index(store.VECTOR_FIELD, **index)
        client.create_index(collection, params)
        logger.info("copied %d vectors of %s to the GPU Milvus under %s", rows, collection, index["index_type"])
    _wait_indexed(client, collection, rows)
    client.load_collection(collection)
    for name in client.list_collections():
        if name.startswith(store.build_name(alias, "")) and name != collection:
            client.drop_collection(name)


def _has_index(client: MilvusClient, collection: str, index: dict[str, Any]) -> bool:
    """Whether the collection's vector index is `index`: its type, metric and every build parameter."""
    names = client.list_indexes(collection, field_name=store.VECTOR_FIELD)
    if len(names) != 1:
        return False
    described = client.describe_index(collection, index_name=names[0])
    wanted = {"index_type": index["index_type"], "metric_type": index["metric_type"], **index.get("params", {})}
    return all(str(described.get(key)) == str(value) for key, value in wanted.items())


def _row_count(client: MilvusClient, collection: str) -> int:
    return int(client.get_collection_stats(collection).get("row_count", 0))


def _wait_indexed(client: MilvusClient, collection: str, rows: int) -> None:
    """Flush, then wait until the vector index covers every row (Milvus builds it in the background)."""
    client.flush(collection)
    [index_name] = client.list_indexes(collection, field_name=store.VECTOR_FIELD)  # the one vector index
    deadline = time.monotonic() + INDEX_TIMEOUT_SECONDS
    while True:
        described = client.describe_index(collection, index_name=index_name)
        indexed = described.get("indexed_rows")
        if indexed is None or (int(indexed) >= rows and not int(described.get("pending_index_rows", 0))):
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"{collection}: {indexed} of {rows} rows indexed after {INDEX_TIMEOUT_SECONDS:.0f} s")
        time.sleep(2)


def embed_queries(embedder: NVIDIAEmbeddings, queries: Sequence[BenchmarkQuery]) -> list[np.ndarray]:
    """Each query embedded once (input_type=query) and L2-normalized; outside every timed search."""
    return list(_normalized(np.asarray([_embed_query(embedder, query.query) for query in queries], dtype=np.float32)))


@nvidia.retry_bulk
def _embed_query(embedder: NVIDIAEmbeddings, text: str) -> list[float]:
    return embedder.embed_query(text)


def _normalized(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)


def waves(queries: Sequence[BenchmarkQuery], profile: Profile) -> list[list[Request]]:
    """The profile's requests in waves: one request at a time, or `concurrency` requests at once.

    A batch holds queries of one source scope, since one request has one filter.
    """
    if profile.mode == "batch":
        by_scope: dict[tuple[str, ...], list[int]] = {}
        for position, query in enumerate(queries):
            by_scope.setdefault(query.source_ids, []).append(position)
        return [
            [Request(tuple(positions[start : start + profile.batch_size]), scope)]
            for scope, positions in by_scope.items()
            for start in range(0, len(positions), profile.batch_size)
        ]
    requests = [Request((position,), query.source_ids) for position, query in enumerate(queries)]
    if profile.mode == "single":
        return [[request] for request in requests]
    return [requests[start : start + profile.concurrency] for start in range(0, len(requests), profile.concurrency)]


def search(index: Index, vectors: list[np.ndarray], scope: Sequence[str]) -> tuple[float, list[list[str]]]:
    """One search request, timed around the Milvus call only: (milliseconds, the top-k ids per vector)."""
    data = [vector.tolist() for vector in vectors]
    started = time.perf_counter()
    results = index.client.search(
        index.collection,
        data=data,
        anns_field=store.VECTOR_FIELD,
        filter=f"source_id in {json.dumps(list(scope))}",
        limit=TOP_K,
        search_params=index.search_params,
        output_fields=["chunk_id"],
    )
    elapsed = (time.perf_counter() - started) * 1000
    return elapsed, [[str(hit["entity"]["chunk_id"]) for hit in hits] for hits in results]


def _wave(
    index: Index, wave: list[Request], vectors: list[np.ndarray], pool: ThreadPoolExecutor
) -> tuple[float, list[float], dict[int, list[str]]]:
    """(the wave's wall time, each request's time, the ids each query found)."""
    started = time.perf_counter()
    futures = [pool.submit(search, index, [vectors[q] for q in request.queries], request.scope) for request in wave]
    outcomes = [future.result() for future in futures]
    wall = (time.perf_counter() - started) * 1000 if len(wave) > 1 else outcomes[0][0]
    found: dict[int, list[str]] = {}
    for request, (_, ids) in zip(wave, outcomes, strict=True):
        found.update(zip(request.queries, ids, strict=True))
    return wall, [elapsed for elapsed, _ in outcomes], found


def measure(
    profile: Profile, indexes: dict[str, Index], queries: Sequence[BenchmarkQuery], vectors: list[np.ndarray]
) -> dict[str, Measurement]:
    """Warm both indexes up on the first wave, then time every wave REPETITIONS times, alternating which goes first."""
    plan = waves(queries, profile)
    measurements = {role: Measurement() for role in ("cpu", "gpu")}
    with ThreadPoolExecutor(max_workers=profile.concurrency) as pool:
        for repetition in range(-WARMUP_ROUNDS, REPETITIONS):
            for role in ("cpu", "gpu") if repetition % 2 == 0 else ("gpu", "cpu"):
                measured = measurements[role]
                if measured.error:
                    continue
                found: dict[int, list[str]] = {}
                try:
                    for wave in plan[:1] if repetition < 0 else plan:
                        wall, latencies, ids = _wave(indexes[role], wave, vectors, pool)
                        if repetition >= 0:
                            measured.total_ms += wall
                            measured.latencies_ms += latencies
                            measured.requests += len(wave)
                            measured.vectors += sum(len(request.queries) for request in wave)
                        found.update(ids)
                except Exception as error:  # a failed request fails this index for the profile
                    measured.error = f"{type(error).__name__}: {error}"[:300]
                    logger.warning("%s %s search failed: %s", profile.profile_id, role, measured.error)
                    continue
                if repetition >= 0:
                    measured.results.append(found)
    return measurements


def profile_result(
    profile: Profile,
    indexes: dict[str, Index],
    queries: Sequence[BenchmarkQuery],
    vectors: list[np.ndarray],
    *,
    truths: list[Truth],
) -> dict[str, Any]:
    measured = measure(profile, indexes, queries, vectors)
    backends = {role: backend(indexes[role], measured[role]) for role in ("cpu", "gpu")}
    for measurement in measured.values():  # neighbors as their exact scores, so equal vectors are one neighbor
        measurement.results = [
            {position: [truths[position].score(chunk) for chunk in chunks] for position, chunks in found.items()}
            for found in measurement.results
        ]
    gates = quality(measured, [truth.best for truth in truths])
    return {
        "profileId": profile.profile_id,
        "executionMode": profile.mode,
        "batchSize": profile.batch_size,
        "concurrency": profile.concurrency,
        "repetitions": REPETITIONS,
        "topK": TOP_K,
        **backends,
        "quality": gates,
        "claim": claim(gates["passed"], backends["cpu"], backends["gpu"]),
    }


def backend(index: Index, measured: Measurement) -> dict[str, Any]:
    completed = measured.error is None and measured.requests > 0
    latencies = measured.latencies_ms or [0.0]
    return {
        "role": index.role,
        "indexType": index.index_type,
        "status": "completed" if completed else "failed",
        "requestCount": measured.requests,
        "vectorCount": measured.vectors,
        "p50Ms": round(float(np.percentile(latencies, 50)), 3),
        "p95Ms": round(float(np.percentile(latencies, 95)), 3),
        "vectorSearchMs": round(measured.total_ms, 3),
        "vectorQps": round(measured.vectors / (measured.total_ms / 1000), 2) if measured.total_ms else 0.0,
    }


def quality(measured: dict[str, Measurement], exact: list[list[float]]) -> dict[str, Any]:
    """Recall@k of each index against the exact neighbors, and the two indexes' overlap, on the last repetition.

    Neighbors are exact scores: ``exact`` holds each query's k best, and the results the scores of the chunks each
    index returned, from the same product. Scores within TIE_TOLERANCE pair up as one neighbor.
    """
    reasons = [f"{role}_search_failed" for role in ("cpu", "gpu") if measured[role].error or not measured[role].results]
    if reasons:
        return {
            "passed": False,
            "cpuRecallAtK": None,
            "gpuRecallAtK": None,
            "cpuGpuOverlapAtK": None,
            "failureReasons": reasons,
        }
    found = {role: measured[role].results[-1] for role in ("cpu", "gpu")}
    recalls = {
        role: [_recall(found[role].get(position, []), truth) for position, truth in enumerate(exact)]
        for role in ("cpu", "gpu")
    }
    overlap = fmean(_jaccard(found["cpu"].get(p, []), found["gpu"].get(p, [])) for p in range(len(exact)))
    means = {role: fmean(values) for role, values in recalls.items()}
    for role in ("cpu", "gpu"):
        if means[role] < MIN_RECALL:
            reasons.append(f"{role}_recall_below_{MIN_RECALL}")
        if any(not _same(results, measured[role].results[0]) for results in measured[role].results):
            reasons.append(f"{role}_results_not_stable_across_repetitions")
    if min(min(values) for values in recalls.values()) < MIN_WORST_QUERY_RECALL:
        reasons.append(f"worst_query_recall_below_{MIN_WORST_QUERY_RECALL}")
    if overlap < MIN_OVERLAP:
        reasons.append(f"cpu_gpu_overlap_below_{MIN_OVERLAP}")
    return {
        "passed": not reasons,
        "cpuRecallAtK": round(means["cpu"], 4),
        "gpuRecallAtK": round(means["gpu"], 4),
        "cpuGpuOverlapAtK": round(overlap, 4),
        "failureReasons": reasons,
    }


def _matched(left: Sequence[float], right: Sequence[float]) -> int:
    """How many scores of the two lists pair up one to one, each pair within TIE_TOLERANCE.

    Both lists sorted, a greedy walk finds the most pairs: a score more than the tolerance above every score left on
    the other side pairs with none of them.
    """
    left, right = sorted(left, reverse=True), sorted(right, reverse=True)
    i = j = pairs = 0
    while i < len(left) and j < len(right):
        if abs(left[i] - right[j]) <= TIE_TOLERANCE:
            pairs, i, j = pairs + 1, i + 1, j + 1
        elif left[i] > right[j]:
            i += 1
        else:
            j += 1
    return pairs


def _recall(found: Sequence[float], truth: Sequence[float]) -> float:
    return _matched(found, truth) / len(truth) if truth else 1.0


def _jaccard(left: Sequence[float], right: Sequence[float]) -> float:
    pairs = _matched(left, right)
    union = len(left) + len(right) - pairs
    return pairs / union if union else 1.0


def _same(left: dict[int, list[float]], right: dict[int, list[float]]) -> bool:
    """Whether two repetitions found the same neighbors for every query."""
    return left.keys() == right.keys() and all(
        len(left[q]) == len(right[q]) == _matched(left[q], right[q]) for q in left
    )


def claim(passed: bool, cpu: dict[str, Any], gpu: dict[str, Any]) -> dict[str, Any]:
    """A GPU speedup only with passing quality and a CPU/GPU total search time of at least MIN_GPU_SPEEDUP."""
    measured = cpu["status"] == gpu["status"] == "completed" and cpu["vectorSearchMs"] and gpu["vectorSearchMs"]
    if not measured:
        return {"decision": "no_claim", "observedCpuOverGpuRatio": None, "gpuSpeedupFactor": None}
    ratio = round(cpu["vectorSearchMs"] / gpu["vectorSearchMs"], 4)
    if passed and ratio >= MIN_GPU_SPEEDUP:
        return {"decision": "gpu_speedup", "observedCpuOverGpuRatio": ratio, "gpuSpeedupFactor": ratio}
    decision = "cpu_faster_or_equal" if passed else "no_claim"
    return {"decision": decision, "observedCpuOverGpuRatio": ratio, "gpuSpeedupFactor": None}

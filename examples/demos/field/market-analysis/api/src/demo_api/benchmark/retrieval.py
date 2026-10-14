# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Benchmark tab's Milvus comparison: the CPU index against an NVIDIA GPU (cuVS) index of the same vectors.

On a GPU host (the analytics-gpu profile) the ``retrieval-benchmark`` one-shot (``tools/retrieval``) mirrors the
active collection's vectors into a GPU Milvus, then times the pack's held-out queries against both indexes and
writes ``retrieval-benchmark.json`` beside ``collection-manifest.json``. Only the vector search is timed; the
query embeddings are computed once, and nothing is reranked. ``GET /v1/jobs/async/job/{id}/retrieval-benchmark``
serves it for a run whose retrieval calls searched that same collection build. A CPU-only stack has none.

A profile claims a speedup only when its quality gates pass (both indexes find the exact neighbors and agree with
each other) and the CPU's total search time is at least ``MIN_GPU_SPEEDUP`` times the GPU's.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Literal

from pydantic import AwareDatetime
from pydantic import Field
from pydantic import NonNegativeFloat
from pydantic import NonNegativeInt
from pydantic import PositiveFloat
from pydantic import PositiveInt
from pydantic import ValidationError
from pydantic import field_validator
from pydantic import model_validator

from demo_api.events.models import ContractModel
from demo_api.events.models import normalize_aware_datetime

MIN_GPU_SPEEDUP = 1.10
ARTIFACT = Path("retrieval-benchmark.json")


class RetrievalBackend(ContractModel):
    """One index's timed searches in one profile."""

    role: Literal["cpu", "gpu"]
    index_type: str = Field(min_length=1, max_length=64, description="e.g. HNSW or GPU_IVF_FLAT")
    status: Literal["completed", "failed"]
    request_count: NonNegativeInt
    vector_count: NonNegativeInt
    p50_ms: NonNegativeFloat
    p95_ms: NonNegativeFloat
    vector_search_ms: NonNegativeFloat = Field(description="Every timed search request, summed")
    vector_qps: NonNegativeFloat = Field(description="Query vectors searched per second of search time")


class RetrievalQuality(ContractModel):
    """Recall against exact inner-product neighbors, and how much the two indexes agree."""

    passed: bool
    cpu_recall_at_k: float | None = Field(ge=0, le=1)
    gpu_recall_at_k: float | None = Field(ge=0, le=1)
    cpu_gpu_overlap_at_k: float | None = Field(ge=0, le=1, description="Mean Jaccard overlap of the top-k ids")
    failure_reasons: list[str] = Field(max_length=16)


class RetrievalClaim(ContractModel):
    decision: Literal["gpu_speedup", "cpu_faster_or_equal", "no_claim"]
    observed_cpu_over_gpu_ratio: PositiveFloat | None
    gpu_speedup_factor: PositiveFloat | None = Field(description="Only for gpu_speedup")


class RetrievalProfile(ContractModel):
    """One workload shape: single queries, batches of query vectors, or concurrent requests."""

    profile_id: str = Field(min_length=1, max_length=64)
    execution_mode: Literal["single", "batch", "concurrency"]
    batch_size: PositiveInt
    concurrency: PositiveInt
    repetitions: PositiveInt
    top_k: PositiveInt
    cpu: RetrievalBackend
    gpu: RetrievalBackend
    quality: RetrievalQuality
    claim: RetrievalClaim

    @model_validator(mode="after")
    def _claim_needs_evidence(self) -> RetrievalProfile:
        if self.cpu.role != "cpu" or self.gpu.role != "gpu":
            raise ValueError("a profile compares the cpu index with the gpu index")
        if self.claim.decision == "gpu_speedup":
            factor = self.claim.gpu_speedup_factor
            if not self.quality.passed or factor is None or factor < MIN_GPU_SPEEDUP:
                raise ValueError(f"a GPU speedup needs passing quality and a factor of at least {MIN_GPU_SPEEDUP}")
        elif self.claim.gpu_speedup_factor is not None:
            raise ValueError("only a gpu_speedup claim reports a factor")
        return self


class RetrievalBenchmark(ContractModel):
    """``retrieval-benchmark.json``, ``GET /v1/jobs/async/job/{id}/retrieval-benchmark`` and a recorded turn's
    ``retrievalBenchmark``."""

    schema_version: Literal["1"] = "1"
    collection: str = Field(min_length=1, max_length=255, description="The alias retrieve_evidence searches")
    collection_version: str = Field(
        min_length=1, max_length=255, description="The build both indexes hold, as retrieval receipts name it"
    )
    embed_model: str = Field(min_length=1, max_length=255)
    source_ids: list[str] = Field(min_length=1, max_length=32)
    query_count: PositiveInt
    measured_at: AwareDatetime
    profiles: list[RetrievalProfile] = Field(min_length=1, max_length=3)

    @field_validator("measured_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return normalize_aware_datetime(value, field_name="measured_at")


class RetrievalBenchmarkUnavailable(Exception):
    """No comparison applies to the run; the message says why."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


NO_GPU_INDEX = (
    "Retrieval comparison is unavailable: Milvus runs its CPU index only in this stack, so there is no GPU index "
    "to compare."
)


def load(data_dir: Path) -> RetrievalBenchmark | None:
    """The active build's comparison, or None when there is none (a CPU-only stack) or it is unreadable."""
    path = data_dir / ARTIFACT
    try:
        return RetrievalBenchmark.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError):
        return None


def for_run(data_dir: Path, receipts: list[dict[str, Any]]) -> RetrievalBenchmark:
    """The comparison that applies to a run: the one measured on the collection build its retrieval calls searched.

    Raises RetrievalBenchmarkUnavailable (with an HTTP status) when the run searched no documents, the stack has no
    GPU index, or the index was rebuilt after the run.
    """
    versions = {
        (receipt.get("content") or {}).get("collectionVersion")
        for receipt in receipts
        if receipt.get("artifactKind") == "retrieval_evidence" and receipt.get("status") == "completed"
    } - {None}
    if not versions:
        raise RetrievalBenchmarkUnavailable(422, "This run did not search the document sources.")
    benchmark = load(data_dir)
    if benchmark is None:
        raise RetrievalBenchmarkUnavailable(404, NO_GPU_INDEX)
    if versions != {benchmark.collection_version}:
        raise RetrievalBenchmarkUnavailable(
            409, "The document index was rebuilt after this run, so its Milvus comparison no longer applies."
        )
    return benchmark

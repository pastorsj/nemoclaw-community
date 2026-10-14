# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Replay a finished job's market analytics calls on the CPU and GPU engines (see ``models.py``)."""

from __future__ import annotations

import statistics
from datetime import UTC
from datetime import datetime
from typing import Any

import httpx

from .models import MAX_STAGES
from .models import MIN_QUALIFIED_PAIRS
from .models import Benchmark
from .models import BenchmarkStage
from .models import EngineTrials
from .models import SpeedupRange

NO_SERVICE = "The market analytics service is not reachable, so there is nothing to compare on."
# One call's untimed runs and a last pair may overrun the budget; each worker call has its own deadline.
_OVERRUN_SECONDS = 300.0


def market_calls(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The job's successful market analytics receipts, one per call, in the order the calls ended."""
    calls: dict[str, dict[str, Any]] = {}
    for receipt in sorted(receipts, key=lambda receipt: receipt.get("occurredAt", "")):
        content = receipt.get("content") or {}
        if (
            receipt.get("artifactKind") == "analytics_result"
            and receipt.get("status") == "completed"
            and content.get("status") == "succeeded"
            and receipt.get("invocationId") not in calls
        ):
            calls[receipt["invocationId"]] = receipt
    return list(calls.values())[:MAX_STAGES]


def _trials(engine: dict[str, Any] | None) -> EngineTrials | None:
    if not engine or not engine.get("trials_ms"):
        return None
    trials = [float(ms) for ms in engine["trials_ms"]]
    return EngineTrials(
        device=engine["device"],
        library=str(engine.get("library") or engine["device"])[:64],
        version=str(engine.get("version") or "")[:64],
        trials_ms=trials,
        median_ms=statistics.median(trials),
    )


def stage_from(receipt: dict[str, Any], outcome: dict[str, Any]) -> BenchmarkStage:
    """One stage from the service's answer. A speedup is claimed only when every matched pair was faster on the GPU."""
    cpu, gpu = _trials(outcome.get("cpu")), _trials(outcome.get("gpu"))
    status = outcome.get("status") if outcome.get("status") in {"completed", "mismatch", "failed"} else "failed"
    ratios = [c / g for c, g in zip(cpu.trials_ms, gpu.trials_ms, strict=False) if g > 0] if cpu and gpu else []
    pairs = min(len(cpu.trials_ms), len(gpu.trials_ms)) if cpu and gpu else 0
    qualified = (
        status == "completed"
        and outcome.get("parity") is True
        and pairs >= MIN_QUALIFIED_PAIRS
        and len(ratios) == pairs
        and all(ratio > 1 for ratio in ratios)
    )
    return BenchmarkStage(
        invocation_id=receipt["invocationId"],
        receipt_id=receipt["receiptId"],
        tool_name=receipt["content"]["operationId"],
        outcome=status,
        parity=outcome.get("parity"),
        reason=(str(outcome["reason"])[:500] if outcome.get("reason") else None),
        cpu=cpu,
        gpu=gpu,
        speedup=cpu.median_ms / gpu.median_ms if qualified and cpu.median_ms and gpu.median_ms else None,
        speedup_range=SpeedupRange(low=min(ratios), high=max(ratios)) if qualified else None,
        qualified=qualified,
    )


async def run_benchmark(
    http: httpx.AsyncClient,
    *,
    job_id: str,
    receipts: list[dict[str, Any]],
    url: str,
    pairs: int,
    budget_seconds: float,
) -> Benchmark:
    """Compare every market call of the job, one after another, on the service at ``url``."""
    stages: list[BenchmarkStage] = []
    for receipt in market_calls(receipts):
        content = receipt["content"]
        request = {
            "tool": content["operationId"],
            "arguments": content.get("publicParameters") or {},
            "pairs": pairs,
            "budget_seconds": budget_seconds,
        }
        try:
            response = await http.post(
                f"{url.rstrip('/')}/benchmark", json=request, timeout=budget_seconds + _OVERRUN_SECONDS
            )
        except httpx.HTTPError:
            return _unavailable(job_id, pairs, NO_SERVICE)
        if response.status_code == 404:
            return _unavailable(job_id, pairs, NO_SERVICE)
        try:
            outcome = response.json()
        except ValueError:
            outcome = {}
        if response.status_code != 200:
            detail = outcome.get("detail") if isinstance(outcome, dict) else None
            outcome = {"status": "failed", "reason": detail or f"The comparison failed ({response.status_code})."}
        elif not outcome.get("available", False):
            return _unavailable(job_id, pairs, str(outcome.get("reason") or NO_SERVICE))
        stages.append(stage_from(receipt, outcome))
    return Benchmark(
        job_id=job_id,
        status="completed" if any(stage.outcome != "failed" for stage in stages) else "failed",
        reason=None if stages else "This run did not call a market analytics tool.",
        measured_at=datetime.now(UTC),
        pairs=pairs,
        stages=stages,
    )


def _unavailable(job_id: str, pairs: int, reason: str) -> Benchmark:
    return Benchmark(
        job_id=job_id, status="unavailable", reason=reason[:500], measured_at=datetime.now(UTC), pairs=pairs, stages=[]
    )

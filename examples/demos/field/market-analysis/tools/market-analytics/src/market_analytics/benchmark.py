# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Matched CPU and GPU runs of one tool call: `POST /benchmark`, which the API's Benchmark tab calls.

    POST /benchmark {"tool": "market_scan", "arguments": {...}, "pairs": 5, "budget_seconds": 30}

The arguments are the ones a receipt recorded (its public parameters), validated as the MCP tool validates them.
Only the GPU service can compare: its GPU worker runs the call, and a CPU worker, started on the first request (it
loads the pack like the GPU worker, in seconds) and kept, runs the same call on pandas, scikit-learn and NetworkX.
Each engine first runs the call once untimed, which pays any first-call cost of that argument shape. Then the two
run it in pairs, alternating which goes first, until `pairs` pairs or the time budget is spent (at least one pair).
A trial's time is the tool's own compute timer, the one receipts show. The payloads of the last pair are compared
the way the GPU parity tests compare them. Requests run one at a time.

The response is `{"available": false, "reason": ...}` on the CPU service, else
`{"available": true, "status": "completed" | "mismatch" | "failed", "parity", "reason", "cpu", "gpu"}` with
`{"device", "library", "version", "trials_ms"}` per engine. Medians and any speedup claim are the API's.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

CPU_ONLY = (
    "This analytics service runs on the CPU only. Run the analytics-gpu profile on an NVIDIA GPU to compare the "
    "CPU and GPU engines."
)
MAX_PAIRS = 20

Target = Literal["cpu", "gpu"]
Call = Callable[[Target], Awaitable[dict[str, Any]]]


class BenchmarkRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    pairs: int = Field(default=5, ge=1, le=MAX_PAIRS)
    budget_seconds: float = Field(default=30.0, gt=0, le=600)


# Payload fields about how a call ran, not what it returned. Each engine plans a minute-bar scan's batches from its
# own byte budget (bars.py: the CPU's is a quarter of the GPU's), so a scan of many stocks differs in `batches`.
EXECUTION_FIELDS = frozenset({"batches"})


def payload_mismatch(gpu: Any, cpu: Any, path: str = "payload") -> str | None:
    """Where two payloads differ: equal structure, ids, ranks and timestamps; floats within GPU rounding."""
    if isinstance(cpu, dict):
        if path == "payload":
            gpu = {k: v for k, v in gpu.items() if k not in EXECUTION_FIELDS} if isinstance(gpu, dict) else gpu
            cpu = {k: v for k, v in cpu.items() if k not in EXECUTION_FIELDS}
        if not isinstance(gpu, dict) or gpu.keys() != cpu.keys():
            return path
        return next((found for key in cpu if (found := payload_mismatch(gpu[key], cpu[key], f"{path}.{key}"))), None)
    if isinstance(cpu, list):
        if not isinstance(gpu, list) or len(gpu) != len(cpu):
            return path
        pairs = enumerate(zip(gpu, cpu, strict=True))
        return next((found for i, (g, c) in pairs if (found := payload_mismatch(g, c, f"{path}[{i}]"))), None)
    if isinstance(cpu, float) and isinstance(gpu, int | float) and not isinstance(gpu, bool):
        return None if math.isclose(gpu, cpu, rel_tol=1e-4, abs_tol=1e-6) else path
    return None if gpu == cpu else path


def _engine(result: dict[str, Any], trials: list[float]) -> dict[str, Any]:
    engine = result.get("engine") or {}
    return {
        "device": engine.get("device"),
        "library": engine.get("library"),
        "version": engine.get("version", ""),
        "trials_ms": [round(ms, 3) for ms in trials],
    }


def _failure(result: dict[str, Any]) -> str | None:
    if result.get("status") == "failed":
        error = result.get("error") or {}
        return str(error.get("message") or "the call failed")[:500]
    return None


async def compare(call: Call, *, pairs: int, budget_seconds: float) -> dict[str, Any]:
    """Run one call untimed on each engine, then in matched pairs; `call(target)` returns a MarketResult as data."""
    for target in ("gpu", "cpu"):
        if reason := _failure(await call(target)):
            return {"available": True, "status": "failed", "parity": None, "reason": reason, "cpu": None, "gpu": None}

    trials: dict[Target, list[float]] = {"cpu": [], "gpu": []}
    last: dict[Target, dict[str, Any]] = {}
    deadline = time.monotonic() + budget_seconds
    for pair in range(pairs):
        for target in ("gpu", "cpu") if pair % 2 == 0 else ("cpu", "gpu"):
            result = await call(target)
            if reason := _failure(result):
                return {
                    "available": True,
                    "status": "failed",
                    "parity": None,
                    "reason": reason,
                    "cpu": None,
                    "gpu": None,
                }
            trials[target].append(float(result["timing"]["compute_ms"]))
            last[target] = result
        if time.monotonic() > deadline:
            break

    mismatch = payload_mismatch(last["gpu"].get("payload"), last["cpu"].get("payload"))
    return {
        "available": True,
        "status": "mismatch" if mismatch else "completed",
        "parity": mismatch is None,
        "reason": f"The CPU and GPU results differ at {mismatch}." if mismatch else None,
        "cpu": _engine(last["cpu"], trials["cpu"]),
        "gpu": _engine(last["gpu"], trials["gpu"]),
    }

# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The CPU and NVIDIA GPU timings of one finished run's market analytics calls.

``POST /v1/jobs/async/job/{id}/benchmark`` replays each successful market analytics call of the
run, with the arguments its receipt recorded, on the market-analytics service's CPU and GPU
workers (``tools/market-analytics``, ``POST /benchmark``). Each engine first runs the call once
untimed, then the engines run it in matched pairs, alternating which goes first. A trial's time
is the tool's own compute timer, the one receipts show. The agent is not rerun.

A stage claims a speedup only when it is ``qualified``: both engines returned the same payload
(floats within 1e-4), at least ``MIN_QUALIFIED_PAIRS`` pairs ran, and the GPU was faster in every
pair. Otherwise the medians are still reported, without a claim.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime
from pydantic import Field
from pydantic import NonNegativeFloat
from pydantic import PositiveFloat
from pydantic import field_validator
from pydantic import model_validator

from demo_api.events.models import ContractModel
from demo_api.events.models import CorrelationIdentifier
from demo_api.events.models import normalize_aware_datetime
from demo_api.receipts.models import MarketOperation

MIN_QUALIFIED_PAIRS = 5
MAX_PAIRS = 20
MAX_STAGES = 16

BenchmarkStatus = Literal["completed", "unavailable", "failed"]
StageOutcome = Literal["completed", "mismatch", "failed", "unavailable"]


class EngineTrials(ContractModel):
    """One engine's timed trials of one call, in milliseconds of the tool's compute timer."""

    device: Literal["cpu", "gpu"]
    library: str = Field(min_length=1, max_length=64)
    version: str = Field(max_length=64)
    trials_ms: list[NonNegativeFloat] = Field(max_length=MAX_PAIRS)
    median_ms: NonNegativeFloat | None


class SpeedupRange(ContractModel):
    """The lowest and highest CPU/GPU time ratio of a matched pair."""

    low: PositiveFloat
    high: PositiveFloat


class BenchmarkStage(ContractModel):
    """One market analytics call of the run, replayed on both engines."""

    invocation_id: CorrelationIdentifier
    receipt_id: CorrelationIdentifier
    tool_name: MarketOperation
    outcome: StageOutcome
    parity: bool | None = Field(description="Both engines returned the same payload; null when not compared")
    reason: str | None = Field(max_length=500)
    cpu: EngineTrials | None
    gpu: EngineTrials | None
    speedup: PositiveFloat | None = Field(description="Median CPU time over median GPU time; only when qualified")
    speedup_range: SpeedupRange | None = Field(description="Only when qualified")
    qualified: bool

    @model_validator(mode="after")
    def _claim_needs_evidence(self) -> BenchmarkStage:
        if self.qualified:
            pairs = min(len(self.cpu.trials_ms), len(self.gpu.trials_ms)) if self.cpu and self.gpu else 0
            if (
                self.outcome != "completed"
                or self.parity is not True
                or pairs < MIN_QUALIFIED_PAIRS
                or self.speedup is None
                or self.speedup_range is None
            ):
                raise ValueError("a qualified speedup needs a completed, parity-passing stage and its speedup")
        elif self.speedup is not None or self.speedup_range is not None:
            raise ValueError("only a qualified stage reports a speedup")
        return self


class Benchmark(ContractModel):
    """``GET``/``POST /v1/jobs/async/job/{id}/benchmark``, and a recorded turn's ``benchmark``."""

    schema_version: Literal["1"] = "1"
    job_id: CorrelationIdentifier
    status: BenchmarkStatus
    reason: str | None = Field(max_length=500)
    measured_at: AwareDatetime
    pairs: int = Field(ge=1, le=MAX_PAIRS, description="Matched pairs asked of each call")
    stages: list[BenchmarkStage] = Field(max_length=MAX_STAGES)

    @field_validator("measured_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return normalize_aware_datetime(value, field_name="measured_at")

# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The three NeMo Data Designer jobs that write the pack's text with Nemotron.

Each job is a seed dataset (rows we build from seeded draws) plus one structured LLM column whose Pydantic
`output_format` bounds the lengths. Rows go to the model in order, so each answer belongs to one seed row.

The seed is written to a file named after its content. Data Designer's resume fingerprint covers a seed file's
path but not a DataFrame's rows, so an interrupted run resumes only with exactly the same seed.

  companies   one row per issuer slot   company_name, profile
  headlines   event type x sentiment    `headline_variants` templates, each with one {company} placeholder
  stories     one row per story event   headline, summary
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Annotated
from typing import Any

import pandas as pd
from pydantic import BaseModel
from pydantic import Field

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
TEMPLATE_LENGTH = 110
# How each sentiment label reads in a prompt.
TONES = {
    "positive": "good for the company",
    "neutral": "routine or mixed for the company, neither clearly good nor bad",
    "negative": "bad for the company",
}

COMPANY_PROMPT = """\
You write reference data for a software demo in which every company is fictional.
Invent a US-listed company in the industry "{{ industry }}" ({{ sector }}), listed on {{ exchange }}.
- company_name: the invented word "{{ name_root }}" followed by one or two plain English words that suit the
  industry. No legal suffix (Inc., Corp., Ltd.). At most 40 characters.
- profile: one sentence of at most 200 characters on what the company does and for whom. Name no other company,
  brand, product, person or ticker, and give no figures."""

HEADLINE_PROMPT = """\
Write {{ count }} different newswire headline templates about a fictional listed company. The event:
{{ about }} ({{ event_type }}). The news is {{ tone }}.
Every template contains the placeholder {company} exactly once, where the company's name goes, is in sentence
case, and is at most 110 characters long. State the event plainly. Never mention the share price or the stock,
give no numbers, do not use the words positive, neutral or negative, and name no other company, person, brand
or place."""

STORY_PROMPT = """\
Write a short newswire item dated {{ date }} about {{ company_name }}, a fictional company: {{ profile }}
The event: {{ about }} ({{ event_type }}). The news is {{ tone }}.
- headline: at most 110 characters, in sentence case; it names {{ company_name }}.
- summary: two or three sentences, at most 600 characters, with the facts of the event.
Do not describe the share price or how the market reacted. Any other party must be invented or described
generically; name no real company, person or brand."""


class Company(BaseModel):
    company_name: str = Field(max_length=40)
    profile: str = Field(max_length=240)


class Story(BaseModel):
    headline: str = Field(max_length=TEMPLATE_LENGTH)
    summary: str = Field(max_length=600)


def headline_set(count: int) -> type[BaseModel]:
    class HeadlineSet(BaseModel):
        templates: list[Annotated[str, Field(max_length=TEMPLATE_LENGTH)]] = Field(min_length=count, max_length=count)

    return HeadlineSet


@dataclass
class Usage:
    """Successful model calls and tokens, counted from Data Designer's usage events."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_job: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Designer:
    """One Data Designer instance, one model, and the usage it has spent."""

    def __init__(self, work_dir: Path) -> None:
        # Data Designer sends usage telemetry and an attribution header by default; it reads this on import.
        os.environ.setdefault("NEMO_TELEMETRY_ENABLED", "false")
        import data_designer.config as dd
        from data_designer.engine.models.usage_events import subscribe_token_usage
        from data_designer.interface import DataDesigner

        self.dd = dd
        self.work_dir = work_dir
        self.model = os.environ.get("DATA_DESIGNER_MODEL") or DEFAULT_MODEL
        self.parallel = int(os.environ.get("DATA_DESIGNER_PARALLEL") or 8)
        self.usage = Usage()
        self._job = ""
        # api_key names the environment variable Data Designer reads at request time; the key never passes here.
        provider = dd.ModelProvider(
            name="build",
            endpoint=os.environ.get("DATA_DESIGNER_BASE_URL") or DEFAULT_BASE_URL,
            provider_type="openai",
            api_key="DATA_DESIGNER_API_KEY",
        )
        self.designer = DataDesigner(artifact_path=work_dir, model_providers=[provider])
        self.designer.set_run_config(dd.RunConfig(otel_metrics_port=None, progress_interval=30.0))
        subscribe_token_usage(self._count)

    def _count(self, event: Any) -> None:
        self.usage.calls += 1
        self.usage.input_tokens += event.input_tokens
        self.usage.output_tokens += event.output_tokens
        self.usage.by_job[self._job] = self.usage.by_job.get(self._job, 0) + 1

    def run(
        self, job: str, attempt: int, seed: pd.DataFrame, output: type[BaseModel], prompt: str, *, max_tokens: int
    ) -> pd.DataFrame:
        """Ask the model once per seed row; returns the seed rows with an `answer` column (a dict of `output`).

        Rows the model failed to answer are missing from the result, so callers retry them in the next attempt.
        """
        dd = self.dd
        self._job = job
        model = dd.ModelConfig(
            alias="text",
            model=self.model,
            provider="build",
            inference_parameters=dd.ChatCompletionInferenceParams(
                temperature=0.7,
                max_tokens=max_tokens,
                timeout=120,
                max_parallel_requests=self.parallel,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            ),
        )
        name = f"{job}-{attempt}-{hashlib.sha256(seed.to_json(orient='records').encode()).hexdigest()[:12]}"
        seed_path = self.work_dir / "seeds" / f"{name}.parquet"
        seed_path.parent.mkdir(parents=True, exist_ok=True)
        seed.to_parquet(seed_path, index=False)
        builder = dd.DataDesignerConfigBuilder(model_configs=[model])
        seed_source = dd.LocalFileSeedSource(path=str(seed_path))
        builder.with_seed_dataset(seed_source, sampling_strategy=dd.SamplingStrategy.ORDERED)
        builder.add_column(
            dd.LLMStructuredColumnConfig(name="answer", model_alias="text", output_format=output, prompt=prompt)
        )
        result = self.designer.create(
            builder, num_records=len(seed), dataset_name=name, resume=dd.ResumeMode.IF_POSSIBLE
        )
        frame = result.load_dataset()
        return frame[frame["answer"].notna()].reset_index(drop=True)

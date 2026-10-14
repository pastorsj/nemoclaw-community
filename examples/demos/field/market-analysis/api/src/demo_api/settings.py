# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The API's environment contract.

Every setting is read from an environment variable of the same name (case-insensitive).
Secrets can also come from a file named after the setting in ``/run/secrets`` (Compose
secrets); an environment variable of the same name wins.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic import SecretStr
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

SECRETS_DIR = Path("/run/secrets")
# In a source checkout the registry sits at the repo root; the image copies it to /app/contracts.
_REPO_REGISTRY = Path(__file__).resolve().parents[3] / "contracts" / "tool-registry.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        secrets_dir=SECRETS_DIR if SECRETS_DIR.is_dir() else None,
        extra="ignore",
    )

    # Data and contracts
    data_active_dir: Path = Path("/data/active")
    tool_registry_file: Path = _REPO_REGISTRY
    # Tool groups baked into the agent image (the agent's AGENT_FEATURES build argument). A source
    # capability is offered only when a tool of that family is in one of these compose profiles.
    agent_features: str = "retrieval,analytics"

    # Job store and runner
    api_db_path: Path = Path("/var/lib/demo-api/jobs.db")
    job_max_active: int = Field(default=1, ge=1)
    job_max_queued: int = Field(default=4, ge=0)
    job_deadline_seconds: float = Field(default=1_200, gt=0)
    job_retention_seconds: float = Field(default=86_400, gt=0)

    # Hermes Runs API (reached through the hermes-gateway forwarder)
    hermes_url: str = "http://hermes-gateway:8642"
    hermes_api_server_key: SecretStr = SecretStr("")
    hermes_run_wall_timeout_seconds: float = Field(default=900, gt=0)
    hermes_run_poll_interval_seconds: float = Field(default=1, gt=0)
    hermes_run_stop_grace_seconds: float = Field(default=30, gt=0)
    # A long model synthesis can stay silent for minutes (the escalation router buffers judged
    # replies); keep this below the wall timeout.
    hermes_run_idle_timeout_seconds: float = Field(default=600, gt=0)
    hermes_run_no_progress_timeout_seconds: float = Field(default=600, gt=0)
    hermes_run_max_tool_calls: int = Field(default=128, gt=0)
    hermes_run_max_duplicate_events: int = Field(default=64, gt=0)
    hermes_heartbeat_seconds: float = Field(default=15, gt=0)
    hermes_receipt_settle_seconds: float = Field(default=2, ge=0, le=5)

    # The agent plugin's key for /internal/hermes/** (sent as X-Receipt-Key)
    hermes_receipt_api_key: SecretStr = SecretStr("")
    # Switchyard's model ids; the plugin names the tier of each model call from them
    agent_efficient_model: str = ""
    agent_capable_model: str = ""

    # Market analytics, for the Benchmark tab's matched CPU/GPU runs of a finished job's calls. A call claims a
    # speedup only after 5 pairs, so the budget must fit 5 pairs of the slowest call: a scan of the 500 most liquid
    # stocks' minute bars takes about 10 s on the CPU, so 5 pairs take about 55 s.
    market_analytics_url: str = "http://market-analytics:3010"
    benchmark_pairs: int = Field(default=5, ge=1, le=20)
    benchmark_budget_seconds: float = Field(default=90, gt=0, le=300)

    # Voice input (demo_api/speech): NVIDIA Nemotron ASR on build.nvidia.com. Off unless enabled and keyed.
    speech_input_enabled: bool = False
    speech_api_key: SecretStr = SecretStr("")  # an nvapi- key; demo.sh fills it from the retriever's
    speech_input_max_seconds: int = Field(default=60, ge=1, le=90)
    speech_max_concurrent: int = Field(default=2, ge=1, le=8)
    speech_asr_server: str = Field(default="grpc.nvcf.nvidia.com:443", pattern=r"^[A-Za-z0-9.-]+:[0-9]{1,5}$")
    speech_asr_function_id: str = Field(default="bb0837de-8c7b-481f-9ec8-ef5663e9c1fa", min_length=1)
    speech_asr_language: str = "en-US"
    speech_asr_timeout_seconds: float = Field(default=60, ge=1, le=300)
    # A public model id on build.nvidia.com for the deletion-only cleanup; empty = no cleanup
    speech_cleanup_model: str = ""

    # Phoenix, for the job trace link
    aiq_phoenix_internal_url: str = "http://phoenix:6006"
    phoenix_project: str = "market-analysis-agent"

    # Auto Ontology (ontology profile); an empty URL turns the ontology view off
    auto_ontology_url: str = ""
    auto_ontology_email: str = ""
    auto_ontology_password: SecretStr = SecretStr("")
    # The Origin Auto Ontology's sign-in trusts, when it is not AUTO_ONTOLOGY_URL (e.g. http://127.0.0.1:3000)
    auto_ontology_origin: str = ""

    @property
    def features(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.agent_features.split(",") if item.strip())

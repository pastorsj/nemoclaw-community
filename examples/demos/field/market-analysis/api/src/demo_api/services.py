# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The objects routes share, created once per process by the app's lifespan."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from typing import Annotated

import httpx
from fastapi import Depends
from fastapi import Request

from .jobs.runner import JobRunner
from .jobs.store import JobStore
from .pack import ActivePack
from .registry import ToolRegistry
from .settings import Settings
from .speech import SpeechService


@dataclass
class Services:
    settings: Settings
    registry: ToolRegistry
    pack: ActivePack
    store: JobStore
    runner: JobRunner
    http: httpx.AsyncClient  # outgoing calls to Phoenix and market analytics
    transport: httpx.AsyncBaseTransport | None  # for clients made per request (Auto Ontology); tests fake it
    speech: SpeechService  # voice input; off unless configured
    # One CPU/GPU comparison at a time: the GPU worker also serves the agent's calls
    benchmark_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]

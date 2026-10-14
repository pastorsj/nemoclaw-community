# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The FastAPI application.

The lifespan opens the job store, fails jobs a previous process left running (``recover``),
starts the job workers and an hourly retention sweep, and on shutdown fails live jobs and stops
their Hermes runs. Run it with one worker (``uvicorn --workers 1``): the job queue and the cancel
signals live in this process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from collections.abc import Callable

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .hermes.client import HermesClient
from .jobs.executor import HermesJobExecutor
from .jobs.runner import JobExecutor
from .jobs.runner import JobRunner
from .jobs.store import JobStore
from .pack import ActivePack
from .registry import ToolRegistry
from .routes import internal
from .routes import jobs
from .routes import sources
from .routes import speech
from .services import Services
from .services import ServicesDep
from .settings import Settings
from .speech import build_speech_service

logger = logging.getLogger(__name__)
RETENTION_INTERVAL_SECONDS = 3_600


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    executor_factory: Callable[[JobStore], JobExecutor] | None = None,
) -> FastAPI:
    """Build the app. Tests pass a fake ``transport`` for outgoing HTTP, or a fake executor."""
    settings = settings or Settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        registry = ToolRegistry.load(settings.tool_registry_file)
        store = JobStore(settings.api_db_path)
        hermes = HermesClient(
            settings.hermes_url, settings.hermes_api_server_key.get_secret_value(), transport=transport
        )
        executor = executor_factory(store) if executor_factory else HermesJobExecutor(store, hermes, registry, settings)
        runner = JobRunner(
            store,
            executor,
            max_active=settings.job_max_active,
            max_queued=settings.job_max_queued,
            job_deadline_seconds=settings.job_deadline_seconds,
        )
        async with httpx.AsyncClient(transport=transport) as http:
            app.state.services = Services(
                settings=settings,
                registry=registry,
                pack=ActivePack(settings.data_active_dir, registry, settings.features),
                store=store,
                runner=runner,
                http=http,
                transport=transport,
                speech=build_speech_service(settings),
            )
            await runner.start()
            retention = asyncio.create_task(_retention(store, settings.job_retention_seconds))
            try:
                yield
            finally:
                retention.cancel()
                await runner.shutdown()
                await hermes.aclose()

    app = FastAPI(title="Market analysis job API", lifespan=lifespan)
    app.include_router(jobs.router)
    app.include_router(sources.router)
    app.include_router(speech.router)
    app.include_router(internal.router)

    @app.get("/health", tags=["health"])
    async def health(services: ServicesDep) -> JSONResponse:
        """Healthy when the job store answers and the job workers are running."""
        try:
            await services.store.ping()
        except Exception:  # noqa: BLE001 - any store failure means unhealthy
            return JSONResponse({"status": "unhealthy", "reason": "job store"}, status_code=503)
        if not services.runner.healthy:
            return JSONResponse({"status": "unhealthy", "reason": "job runner"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


async def _retention(store: JobStore, retention_seconds: float) -> None:
    """Delete finished jobs older than the retention, at startup and then every hour."""
    while True:
        try:
            if deleted := await store.delete_finished_before(time.time() - retention_seconds):
                logger.info("Retention deleted %d finished jobs", deleted)
        except Exception:
            logger.exception("Retention sweep failed")
        await asyncio.sleep(RETENTION_INTERVAL_SECONDS)

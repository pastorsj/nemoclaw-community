# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""In-process job runner: a bounded FIFO queue drained by a fixed number of asyncio workers.

It replaces the Dask queue that the prototype ran behind NAT. What that gave, and where it lives now:

- one job at a time, in order (``DASK_NTHREADS=1``)  -> ``max_active`` workers reading one ``asyncio.Queue``
- at most 5 live jobs (the admission cap)             -> ``max_active + max_queued``, then ``QueueFullError`` (429)
- cancelling a queued job                             -> status compare-and-set; the worker skips it
- stopping a running job (a 1 s DB-polled monitor)    -> a per-job ``asyncio.Event`` handed to the executor
- the ghost reaper and one-hour submitted timeout     -> a per-job deadline, plus ``recover()`` at startup

Run the API with one uvicorn worker: the queue and the cancel events live in this process.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from .store import ACTIVE_STATUSES
from .store import Job
from .store import JobExistsError
from .store import JobStatus
from .store import JobStore

logger = logging.getLogger(__name__)

CANCELLED_BY_USER = "cancelled by user"  # the UI detects a user cancellation by this text
API_RESTARTED = "The API restarted before this job finished; please retry."


class QueueFullError(Exception):
    """Every slot is taken; the route answers 429 with Retry-After."""


class RunnerUnavailableError(Exception):
    """The runner is not accepting jobs (starting up or shutting down); the route answers 503."""


class JobFailed(Exception):
    """An expected failure whose message is safe to show users."""

    def __init__(self, public_message: str, error_type: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.error_type = error_type


class JobExecutor(Protocol):
    async def run(self, job: Job, cancelled: asyncio.Event) -> None:
        """Run one claimed job.

        Write ``success`` through the store's compare-and-set, raise ``JobFailed`` for expected
        failures, and stop remote work (the Hermes run) as soon as ``cancelled`` is set.
        """

    async def stop_detached(self, job: Job) -> None:
        """Stop remote work that a previous API process started but can no longer observe."""


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        executor: JobExecutor,
        *,
        max_active: int = 1,
        max_queued: int = 4,
        job_deadline_seconds: float = 1_200.0,
        shutdown_grace_seconds: float = 10.0,
    ) -> None:
        self._store = store
        self._executor = executor
        self._max_active = max_active
        self._capacity = max_active + max_queued
        self._job_deadline_seconds = job_deadline_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._admitted: dict[str, asyncio.Event] = {}  # queued or running job id -> its cancel signal
        self._workers: list[asyncio.Task[None]] = []
        self._accepting = False

    @property
    def healthy(self) -> bool:
        return self._accepting and all(not worker.done() for worker in self._workers)

    async def start(self) -> None:
        await self.recover()
        self._accepting = True
        self._workers = [asyncio.create_task(self._work(), name=f"job-worker-{n}") for n in range(self._max_active)]

    async def submit(self, job_id: str, request: dict) -> None:
        if not self._accepting:
            raise RunnerUnavailableError
        if job_id in self._admitted:
            raise JobExistsError(job_id)
        if len(self._admitted) >= self._capacity:
            raise QueueFullError
        # Reserve the slot before the first await, so concurrent submits cannot overshoot the cap.
        self._admitted[job_id] = asyncio.Event()
        try:
            await self._store.create(job_id, request)
        except BaseException:
            del self._admitted[job_id]
            raise
        self._queue.put_nowait(job_id)

    async def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job. Returns False if it already finished."""
        if await self._interrupt(job_id, JobStatus.SUBMITTED):
            self._admitted.pop(job_id, None)  # frees its slot now; the worker will skip it
            return True
        if await self._interrupt(job_id, JobStatus.RUNNING):
            if cancelled := self._admitted.get(job_id):
                cancelled.set()
            return True
        return False

    async def recover(self) -> None:
        """Fail jobs a previous process left active, and stop their Hermes runs.

        Hermes cannot replay a run's event stream, so resuming would produce an answer without an
        inspectable trajectory. A single in-process runner knows at startup that nothing is
        running, so it acts at once instead of waiting for a reaper.
        """
        for job in await self._store.with_status(ACTIVE_STATUSES):
            if not await self._fail(job.job_id, API_RESTARTED, "ApiRestarted", expected=ACTIVE_STATUSES):
                continue
            if job.hermes_run_id:
                try:
                    await self._executor.stop_detached(job)
                except Exception as error:  # noqa: BLE001 - the job is already terminal; stopping is best effort
                    logger.warning("Could not stop detached run for %s (%s)", job.job_id, type(error).__name__)

    async def shutdown(self) -> None:
        """Stop accepting, fail every live job, then give running executors time to stop Hermes."""
        self._accepting = False  # from here on no worker starts a job
        live = list(self._admitted.items())
        for job_id, _ in live:
            await self._fail(job_id, API_RESTARTED, "ApiRestarted", expected=ACTIVE_STATUSES)
        for _, cancelled in live:
            cancelled.set()
        try:
            async with asyncio.timeout(self._shutdown_grace_seconds):
                await self._queue.join()
        except TimeoutError:
            logger.warning("Shutdown grace expired with a job still stopping")
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def _work(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run_one(job_id)
            finally:
                self._admitted.pop(job_id, None)
                self._queue.task_done()

    async def _run_one(self, job_id: str) -> None:
        cancelled = self._admitted.get(job_id)
        if not self._may_start(cancelled):
            return
        if not await self._store.transition(job_id, expected={JobStatus.SUBMITTED}, to=JobStatus.RUNNING):
            return  # cancelled while queued
        if not self._may_start(cancelled):
            return  # a cancel or shutdown won the race with the claim and has recorded the outcome
        job = await self._store.get(job_id)
        deadline = asyncio.timeout(self._job_deadline_seconds)
        try:
            async with deadline:
                await self._executor.run(job, cancelled)
        except JobFailed as error:
            await self._fail(job_id, error.public_message, error.error_type)
        except Exception as error:
            if deadline.expired():
                await self._fail(job_id, "The job exceeded its deadline; please retry.", "JobDeadlineExceeded")
            else:
                logger.exception("Job %s failed", job_id)
                await self._fail(job_id, f"Job failed ({type(error).__name__}); please retry.", type(error).__name__)
        else:
            # A returning executor has written success, or the job was cancelled; anything else is a bug.
            await self._fail(job_id, "The job ended without a result; please retry.", "JobIncomplete")

    def _may_start(self, cancelled: asyncio.Event | None) -> bool:
        return self._accepting and cancelled is not None and not cancelled.is_set()

    async def _interrupt(self, job_id: str, current: JobStatus) -> bool:
        return await self._store.transition(
            job_id,
            expected={current},
            to=JobStatus.INTERRUPTED,
            error=CANCELLED_BY_USER,
            events=[{"type": "job.cancellation_requested", "data": {"reason": CANCELLED_BY_USER}}],
        )

    async def _fail(
        self,
        job_id: str,
        message: str,
        error_type: str,
        *,
        expected: frozenset[JobStatus] = frozenset({JobStatus.RUNNING}),
    ) -> bool:
        return await self._store.transition(
            job_id,
            expected=expected,
            to=JobStatus.FAILURE,
            error=message,
            events=[{"type": "job.error", "data": {"error": message, "error_type": error_type}}],
        )

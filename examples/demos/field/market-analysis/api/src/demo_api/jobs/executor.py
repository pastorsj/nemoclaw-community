# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run one job on Hermes: start the run, record its events, then publish the answer.

1. Build the run request from the job (and earlier turns of its conversation) and start the run,
   with the job id as the idempotency key.
2. Bind the run to the job, then follow its events, storing each as an ``execution.v2`` event,
   while the progress guard and the cancel signal can stop it.
3. Wait briefly until every registered tool call has its receipt (the plugin posts them to
   ``/internal/hermes/.../tool-receipts``), resolve the answer's citations against them, record the
   publication events (response formatted, citations resolved, run metrics), and store the report
   and ``success`` in one transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from demo_api.hermes.client import HermesClient
from demo_api.hermes.client import HermesError
from demo_api.hermes.client import RunEvent
from demo_api.hermes.client import RunStatus
from demo_api.hermes.lifecycle import HermesRunDeadlineExceeded
from demo_api.hermes.lifecycle import HermesRunLifecycle
from demo_api.hermes.lifecycle import RunLimits
from demo_api.hermes.normalizer import EventNormalizer
from demo_api.hermes.progress import HermesRunBudgetExceeded
from demo_api.hermes.progress import ProgressGuard
from demo_api.hermes.progress import ProgressLimits
from demo_api.hermes.request import MODEL
from demo_api.hermes.request import PriorTurn
from demo_api.hermes.request import build_run_request
from demo_api.registry import ToolRegistry
from demo_api.reports.publication import citations_from_receipts
from demo_api.reports.publication import publish_report
from demo_api.settings import Settings

from .hermes_run import run_bound
from .runner import JobFailed
from .store import Job
from .store import JobStatus
from .store import JobStore

MAX_PRIOR_TURNS = 3
MAX_PRIOR_ANSWER_CHARS = 8_000
_FAILURE_MESSAGES = {
    "cancelled": "Hermes stopped the run before it finished; please retry.",
    "interrupted": "Hermes was interrupted before it finished; please retry.",
}
_RUN_FAILED = "Hermes could not complete this request; please retry or check service health."


def final_report_event(markdown: str, citations: list[dict[str, Any]]) -> dict[str, Any]:
    """The event the UI renders as the answer."""
    return {
        "type": "artifact.update",
        "data": {"type": "output", "output_category": "final_report", "content": markdown, "citations": citations},
    }


def _wall_ms(status: RunStatus) -> int | None:
    """The run's wall time, from Hermes's own creation and completion times."""
    if status.created_at is None or status.updated_at is None:
        return None
    return max(0, round((status.updated_at - status.created_at) * 1000))


class HermesJobExecutor:
    def __init__(self, store: JobStore, client: HermesClient, registry: ToolRegistry, settings: Settings) -> None:
        self._store = store
        self._client = client
        self._registry = registry
        self._settings = settings
        self._lifecycle = HermesRunLifecycle(
            client,
            RunLimits(
                wall_timeout_seconds=settings.hermes_run_wall_timeout_seconds,
                poll_interval_seconds=settings.hermes_run_poll_interval_seconds,
                stop_grace_seconds=settings.hermes_run_stop_grace_seconds,
            ),
        )
        self._progress_limits = ProgressLimits(
            idle_timeout_seconds=settings.hermes_run_idle_timeout_seconds,
            no_progress_timeout_seconds=settings.hermes_run_no_progress_timeout_seconds,
            max_tool_calls=settings.hermes_run_max_tool_calls,
            max_duplicate_events=settings.hermes_run_max_duplicate_events,
        )

    async def run(self, job: Job, cancelled: asyncio.Event) -> None:
        request = job.request
        session_id = request.get("conversation_id") or job.job_id
        prior_turns = await self._prior_turns(job)
        payload = build_run_request(
            job_id=job.job_id,
            session_id=session_id,
            question=request["question"],
            catalog=request["catalog"],
            toolsets=request["toolsets"],
            prior_turns=prior_turns,
        )
        try:
            binding = await self._lifecycle.start(payload, idempotency_key=job.job_id)
        except HermesError as error:
            raise JobFailed("Hermes could not start this request; please retry.", "HermesUnavailable") from error

        normalizer = EventNormalizer(job_id=job.job_id, session_id=session_id, registry=self._registry)
        guard = ProgressGuard(binding.run_id, self._progress_limits)

        async def on_event(event: RunEvent) -> None:
            guard.observe(event)
            if (normalized := normalizer.normalize(event)) is not None:
                await self._store.append_event(job.job_id, normalized.to_event_store_dict())

        async def on_heartbeat() -> None:
            guard.check()
            await on_event(RunEvent("run.heartbeat", binding.run_id, {}, time.time()))

        try:
            await self._store.bind_run(job.job_id, binding.run_id)
            counts = {"prior_turn_count": len(prior_turns), "conversation_message_count": 2 * len(prior_turns)}
            await on_event(RunEvent("run.created", binding.run_id, counts, time.time()))
        except Exception:
            with contextlib.suppress(HermesError):
                await self._lifecycle.cancel(binding, reason="the job could not record its run")
            raise

        try:
            status = await run_bound(
                self._lifecycle,
                binding,
                cancelled=cancelled,
                on_event=on_event,
                on_heartbeat=on_heartbeat,
                heartbeat_seconds=self._settings.hermes_heartbeat_seconds,
            )
        except HermesRunBudgetExceeded as error:
            message = f"Hermes stopped after exceeding its {error.budget} budget; please retry."
            raise JobFailed(message, "HermesRunBudgetExceeded") from error
        except HermesRunDeadlineExceeded as error:
            message = "Hermes reached the run deadline before finishing; please retry."
            raise JobFailed(message, "HermesRunDeadlineExceeded") from error
        except HermesError as error:
            raise JobFailed(_RUN_FAILED, "HermesRunFailure") from error
        if status is None:
            return  # cancelled by the user; the runner has recorded it

        if not normalizer.terminal_seen:  # the stream broke; record the outcome the status reported
            data = {"usage": status.usage, "output": status.output or "", "error": status.error}
            await on_event(RunEvent(f"run.{status.status}", binding.run_id, data, status.updated_at))
        if status.status != "completed" or not (status.output or "").strip():
            raise JobFailed(_FAILURE_MESSAGES.get(status.status, _RUN_FAILED), "HermesRunFailure")

        receipts = await self._settled_receipts(job.job_id, normalizer.completed_registered_calls)
        report = publish_report(status.output, citations_from_receipts(receipts, self._registry))
        metrics = {
            # The model the run asked Hermes for (Switchyard's route), as Hermes reports it
            "runtime_profile": status.model or MODEL,
            **({"wall_duration_ms": wall_ms} if (wall_ms := _wall_ms(status)) is not None else {}),
            "tool_call_count": normalizer.tool_call_count,
            "known_tool_duration_ms": normalizer.known_tool_duration_ms,
            **{key: value for key, value in normalizer.usage.items() if key != "reasoning_tokens"},
        }
        publication = normalizer.publication(binding.run_id, resolution=report.resolution(), metrics=metrics)
        # With the success, in one transaction: a job cancelled or failed meanwhile gets no publication events
        await self._store.transition(
            job.job_id,
            expected={JobStatus.RUNNING},
            to=JobStatus.SUCCESS,
            output={
                "report": report.markdown,
                "citations": report.citations,
                "invalid_evidence_ids": report.invalid_evidence_ids,
                "run_id": binding.run_id,
                "usage": status.usage,
            },
            events=[
                *(event.to_event_store_dict() for event in publication),
                final_report_event(report.markdown, report.citations),
            ],
        )

    async def stop_detached(self, job: Job) -> None:
        await self._client.stop_run(job.hermes_run_id)

    async def _prior_turns(self, job: Job) -> list[PriorTurn]:
        """Up to three earlier answers of the conversation, oldest first.

        An answer is reused only if every source behind it is still selected, so a follow-up can
        never see evidence from a source the user has since turned off.
        """
        conversation_id = job.request.get("conversation_id")
        if not conversation_id:
            return []
        selected = set(job.request["source_ids"])
        turns: list[PriorTurn] = []
        for prior in await self._store.answered_in_conversation(conversation_id, before=job.job_id, limit=12):
            answer = (prior.output or {}).get("report")
            if answer and set(prior.request.get("source_ids", [])) <= selected:
                turns.append(PriorTurn(prior.request["question"], answer[:MAX_PRIOR_ANSWER_CHARS]))
            if len(turns) == MAX_PRIOR_TURNS:
                break
        return turns[::-1]

    async def _settled_receipts(self, job_id: str, expected: set[str]) -> list[dict[str, Any]]:
        """The job's receipts, once one has arrived for every registered tool call.

        The plugin posts receipts outside the Hermes event stream, so the last ones can land just
        after the run completes. An answer whose evidence is missing fails rather than
        publishing citations that cannot be inspected.
        """
        deadline = time.monotonic() + self._settings.hermes_receipt_settle_seconds
        while True:
            receipts = await self._store.receipts(job_id)
            if expected <= {receipt["invocationId"] for receipt in receipts}:
                return receipts
            if time.monotonic() >= deadline:
                message = "Hermes finished, but the evidence for its tool calls was not recorded; please retry."
                raise JobFailed(message, "HermesReceiptDeliveryIncomplete")
            await asyncio.sleep(0.05)

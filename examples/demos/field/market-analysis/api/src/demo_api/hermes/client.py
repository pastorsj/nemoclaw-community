# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Async client for the Hermes Runs API, served by the agent in the OpenShell sandbox.

- ``POST /v1/runs`` starts a run. The job id is the ``Idempotency-Key``, so a retried start
  never creates a second run.
- ``GET /v1/runs/{id}/events`` streams the run's events once, as Server-Sent Events. Hermes keeps
  no cursor, so a lost stream cannot be replayed; callers fall back to polling the status.
- ``GET /v1/runs/{id}`` returns the status, and the answer once the run is done.
- ``POST /v1/runs/{id}/stop`` asks the run to stop.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import ValidationError

TERMINAL_STATUSES = frozenset({"cancelled", "completed", "failed", "interrupted"})
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_EVENT_CHARS = 1_048_576


class HermesError(RuntimeError):
    """A failure talking to Hermes; the message is safe to log."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RunStatus(BaseModel):
    """``GET /v1/runs/{run_id}``. Unknown fields are kept, so a Hermes upgrade can add some."""

    model_config = ConfigDict(extra="allow")

    run_id: str
    status: str
    session_id: str | None = None
    model: str | None = None
    output: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    created_at: float | None = None
    updated_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One event from a run's stream. ``data`` is the whole JSON payload, ``event`` included."""

    event: str
    run_id: str
    data: dict[str, Any]
    timestamp: float | None = None
    sse_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.event in {f"run.{status}" for status in TERMINAL_STATUSES}


class HermesClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 45.0,
        max_attempts: int = 3,
        retry_base_seconds: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_seconds, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create_run(self, payload: dict[str, Any], *, idempotency_key: str) -> str:
        """Start a run and return its id."""
        response = await self._request(
            "POST", "/v1/runs", json=payload, headers={"Idempotency-Key": idempotency_key}, expected={202}
        )
        body = response.json()
        run_id = body.get("run_id") if isinstance(body, dict) else None
        if not isinstance(run_id, str) or not run_id:
            raise HermesError("Hermes returned a run admission without a run_id")
        return run_id

    async def get_run(self, run_id: str) -> RunStatus:
        return self._status(await self._request("GET", f"/v1/runs/{quote(run_id, safe='')}"))

    async def stop_run(self, run_id: str) -> RunStatus:
        return self._status(await self._request("POST", f"/v1/runs/{quote(run_id, safe='')}/stop"))

    async def stream_events(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Yield the run's events until Hermes closes the stream.

        Connecting is retried; once events have started, a transport failure is raised, because
        the stream cannot resume.
        """
        path = f"/v1/runs/{quote(run_id, safe='')}/events"
        for attempt in range(self._max_attempts):
            connected = False
            try:
                async with self._http.stream("GET", path, headers={"Accept": "text/event-stream"}) as response:
                    connected = True
                    if response.status_code != 200:
                        await response.aread()
                        if self._should_retry(attempt, response.status_code):
                            await self._backoff(attempt)
                            continue
                        raise self._error(response)
                    async for event in _decode_events(response.aiter_lines(), run_id):
                        yield event
                    return
            except httpx.TransportError as error:
                if connected or attempt + 1 >= self._max_attempts:
                    raise HermesError(f"Hermes event stream failed ({type(error).__name__})") from error
                await self._backoff(attempt)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected: set[int] | None = None,
    ) -> httpx.Response:
        """Send a retry-safe request: transport errors and busy statuses are retried with backoff."""
        expected = expected or {200}
        for attempt in range(self._max_attempts):
            try:
                response = await self._http.request(method, path, json=json, headers=headers)
            except httpx.TransportError as error:
                if attempt + 1 >= self._max_attempts:
                    raise HermesError(f"Hermes request failed ({type(error).__name__})") from error
                await self._backoff(attempt)
                continue
            if response.status_code in expected:
                return response
            if not self._should_retry(attempt, response.status_code):
                raise self._error(response)
            await self._backoff(attempt)
        raise AssertionError("unreachable")

    def _should_retry(self, attempt: int, status_code: int) -> bool:
        return status_code in _RETRYABLE_STATUS_CODES and attempt + 1 < self._max_attempts

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(min(self._retry_base_seconds * 2**attempt, 5.0))

    def _error(self, response: httpx.Response) -> HermesError:
        message = "Hermes rejected the request"
        try:
            error = response.json().get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
        except (ValueError, AttributeError):
            pass
        if self._api_key:
            message = message.replace(self._api_key, "[redacted]")
        message = " ".join(message.split())[:500] or "Hermes rejected the request"
        return HermesError(f"{message} (HTTP {response.status_code})", status_code=response.status_code)

    @staticmethod
    def _status(response: httpx.Response) -> RunStatus:
        try:
            return RunStatus.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise HermesError("Hermes returned an invalid run status") from error


async def _decode_events(lines: AsyncIterator[str], run_id: str) -> AsyncIterator[RunEvent]:
    """Turn Server-Sent Events lines into run events. Comments and ``[DONE]`` are skipped."""
    data: list[str] = []
    name: str | None = None
    sse_id: str | None = None
    async for line in lines:
        if line:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                data.append(value)
                if sum(map(len, data)) > _MAX_EVENT_CHARS:
                    raise HermesError("A Hermes event exceeded the size limit")
            elif field == "event":
                name = value or None
            elif field == "id":
                sse_id = value
            continue
        if data and (event := _parse_event("\n".join(data), name, sse_id, run_id)):
            yield event
        data, name = [], None
    if data and (event := _parse_event("\n".join(data), name, sse_id, run_id)):
        yield event


def _parse_event(text: str, name: str | None, sse_id: str | None, run_id: str) -> RunEvent | None:
    if text.strip() == "[DONE]":
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise HermesError("Hermes sent an event that is not JSON") from error
    if not isinstance(payload, dict):
        raise HermesError("Hermes sent an event that is not a JSON object")
    event = payload.get("event") or name
    if not isinstance(event, str) or not event:
        raise HermesError("Hermes sent an event without a name")
    if payload.get("run_id", run_id) != run_id:
        raise HermesError("Hermes sent an event for a different run")
    timestamp = payload.get("timestamp")
    return RunEvent(
        event=event,
        run_id=run_id,
        data=payload,
        timestamp=float(timestamp) if isinstance(timestamp, int | float) else None,
        sse_id=sse_id,
    )

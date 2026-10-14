# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""SQLite job store: one row per job, its append-only events, its tool receipts and its benchmark.

Every method is async and runs its SQLite work in a thread, so the API event loop (SSE,
receipts, /health) never waits on disk. Status changes are compare-and-set, so a late writer
can never resurrect a finished job. The database runs in WAL mode, so readers never block the
writer.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobStatus(StrEnum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    INTERRUPTED = "interrupted"


ACTIVE_STATUSES = frozenset({JobStatus.SUBMITTED, JobStatus.RUNNING})


class JobExistsError(Exception):
    """A job with this id already exists."""


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    status: JobStatus
    request: dict[str, Any]
    error: str | None
    output: dict[str, Any] | None
    hermes_run_id: str | None
    created_at: float
    updated_at: float

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    request         TEXT NOT NULL,
    conversation_id TEXT,
    error           TEXT,
    output          TEXT,
    hermes_run_id   TEXT UNIQUE,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_conversation ON jobs(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS job_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    event_data TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_events_job_id_id ON job_events(job_id, id);
CREATE TABLE IF NOT EXISTS receipts (
    job_id        TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    receipt_id    TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    receipt       TEXT NOT NULL,
    created_at    REAL NOT NULL,
    PRIMARY KEY (job_id, receipt_id)
);
CREATE TABLE IF NOT EXISTS benchmarks (
    job_id     TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
    benchmark  TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class JobStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
        finally:
            connection.close()

    async def ping(self) -> None:
        await self._run(lambda connection: connection.execute("SELECT 1").fetchone())

    async def create(self, job_id: str, request: dict[str, Any]) -> None:
        now = time.time()

        def insert(connection: sqlite3.Connection) -> None:
            try:
                connection.execute(
                    "INSERT INTO jobs (job_id, status, request, conversation_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, JobStatus.SUBMITTED, json.dumps(request), request.get("conversation_id"), now, now),
                )
            except sqlite3.IntegrityError as error:
                raise JobExistsError(job_id) from error

        await self._run(insert)

    async def get(self, job_id: str) -> Job | None:
        def select(connection: sqlite3.Connection) -> Job | None:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return _job_from_row(row) if row else None

        return await self._run(select)

    async def with_status(self, statuses: Iterable[JobStatus]) -> list[Job]:
        wanted = tuple(statuses)
        placeholders = ", ".join("?" * len(wanted))

        def select(connection: sqlite3.Connection) -> list[Job]:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at", wanted
            ).fetchall()
            return [_job_from_row(row) for row in rows]

        return await self._run(select)

    async def answered_in_conversation(self, conversation_id: str, *, before: str, limit: int) -> list[Job]:
        """Successful jobs of one conversation, newest first, excluding the job ``before``."""

        def select(connection: sqlite3.Connection) -> list[Job]:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE conversation_id = ? AND status = ? AND job_id != ? "
                "ORDER BY created_at DESC LIMIT ?",
                (conversation_id, JobStatus.SUCCESS, before, limit),
            ).fetchall()
            return [_job_from_row(row) for row in rows]

        return await self._run(select)

    async def transition(
        self,
        job_id: str,
        *,
        expected: Iterable[JobStatus],
        to: JobStatus,
        error: str | None = None,
        output: dict[str, Any] | None = None,
        events: Sequence[dict[str, Any]] = (),
    ) -> bool:
        """Move the job to ``to`` only if its current status is one of ``expected``.

        ``events`` are appended in the same transaction, so SSE never sees a terminal status
        without the event that explains it.
        """
        allowed = tuple(expected)
        placeholders = ", ".join("?" * len(allowed))
        encoded_output = json.dumps(output) if output is not None else None

        def update(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                f"UPDATE jobs SET status = ?, error = ?, output = ?, updated_at = ? "
                f"WHERE job_id = ? AND status IN ({placeholders})",
                (to, error, encoded_output, time.time(), job_id, *allowed),
            )
            if cursor.rowcount != 1:
                return False
            for event in events:
                _insert_event(connection, job_id, event)
            return True

        return await self._run(update)

    async def bind_run(self, job_id: str, hermes_run_id: str) -> None:
        """Record the Hermes run so receipts can join it and a restarted API can stop it."""

        def update(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE jobs SET hermes_run_id = ? WHERE job_id = ?", (hermes_run_id, job_id))

        await self._run(update)

    async def append_event(self, job_id: str, event: dict[str, Any]) -> int:
        """Persist one event; its id is the SSE cursor."""
        return await self._run(lambda connection: _insert_event(connection, job_id, event))

    async def events(self, job_id: str, *, after_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        """Events after cursor ``after_id``, oldest first; each carries its cursor as ``_id``."""

        def select(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT id, event_data FROM job_events WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?",
                (job_id, after_id, limit),
            ).fetchall()
            return [{"_id": row["id"], **json.loads(row["event_data"])} for row in rows]

        return await self._run(select)

    async def add_receipt(self, job_id: str, receipt: dict[str, Any], event: dict[str, Any]) -> bool:
        """Store a receipt and its event once. Returns False if the receipt was already stored."""

        def insert(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO receipts (job_id, receipt_id, invocation_id, receipt, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, receipt["receiptId"], receipt["invocationId"], json.dumps(receipt), time.time()),
            )
            if cursor.rowcount != 1:
                return False
            _insert_event(connection, job_id, event)
            return True

        return await self._run(insert)

    async def receipts(self, job_id: str) -> list[dict[str, Any]]:
        """The job's receipts (camelCase wire form), in arrival order."""

        def select(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT receipt FROM receipts WHERE job_id = ? ORDER BY created_at, rowid", (job_id,)
            ).fetchall()
            return [json.loads(row["receipt"]) for row in rows]

        return await self._run(select)

    async def save_benchmark(self, job_id: str, benchmark: dict[str, Any]) -> None:
        """Store (or replace) the job's CPU/GPU comparison (camelCase wire form)."""

        def upsert(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR REPLACE INTO benchmarks (job_id, benchmark, created_at) VALUES (?, ?, ?)",
                (job_id, json.dumps(benchmark), time.time()),
            )

        await self._run(upsert)

    async def benchmark(self, job_id: str) -> dict[str, Any] | None:
        """The job's stored CPU/GPU comparison, or None."""

        def select(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute("SELECT benchmark FROM benchmarks WHERE job_id = ?", (job_id,)).fetchone()
            return json.loads(row["benchmark"]) if row else None

        return await self._run(select)

    async def delete_finished_before(self, cutoff: float) -> int:
        """Retention: drop finished jobs (and, by cascade, all they hold) last updated before ``cutoff``."""
        placeholders = ", ".join("?" * len(ACTIVE_STATUSES))

        def delete(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                f"DELETE FROM jobs WHERE updated_at < ? AND status NOT IN ({placeholders})",
                (cutoff, *ACTIVE_STATUSES),
            )
            return cursor.rowcount

        return await self._run(delete)

    async def _run[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._run_sync, operation)

    def _run_sync[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        connection = self._connect()
        try:
            with connection:  # one transaction: commit on success, roll back on error
                return operation(connection)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


def _insert_event(connection: sqlite3.Connection, job_id: str, event: dict[str, Any]) -> int:
    cursor = connection.execute(
        "INSERT INTO job_events (job_id, event_type, event_data, created_at) VALUES (?, ?, ?, ?)",
        (job_id, event.get("type", "unknown"), json.dumps(event), time.time()),
    )
    return cursor.lastrowid


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        job_id=row["job_id"],
        status=JobStatus(row["status"]),
        request=json.loads(row["request"]),
        error=row["error"],
        output=json.loads(row["output"]) if row["output"] is not None else None,
        hermes_run_id=row["hermes_run_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

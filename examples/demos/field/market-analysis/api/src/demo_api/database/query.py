# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run a data-viewer query in ``worker.py``: at most two at once, five seconds each."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

MAX_CONCURRENT_QUERIES = 2
TIMEOUT_SECONDS = 5.0
_WORKER = Path(__file__).with_name("worker.py")
_slots = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)


class QueryError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def run_query(
    path: Path, database_name: str, sql: str, tables: dict[str, list[str]], *, max_rows: int = 100
) -> dict[str, Any]:
    """Return ``{columns, types, rows, truncated, duration_ms}``; raise ``QueryError`` with an HTTP status otherwise."""
    if _slots.locked():
        raise QueryError(429, "Database queries are busy. Try again shortly.")
    started = time.monotonic()
    async with _slots:
        result = await _run_worker(
            {"path": str(path), "database_name": database_name, "sql": sql, "tables": tables, "max_rows": max_rows}
        )
    return result | {"duration_ms": round((time.monotonic() - started) * 1000)}


async def _run_worker(payload: dict[str, Any]) -> dict[str, Any]:
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(_WORKER),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"LANG": "C.UTF-8"},
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise QueryError(504, "The query took longer than five seconds. Narrow it and try again.") from error
        if process.returncode or not output:
            raise QueryError(422, "The query could not finish within the viewer's resource limits.")
        result = json.loads(output)
        if result.get("error") == "query_rejected":
            raise QueryError(422, result["message"])
        if "error" in result:
            raise QueryError(422, "The query could not run. Check table and column names, types and size.")
        return result
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()

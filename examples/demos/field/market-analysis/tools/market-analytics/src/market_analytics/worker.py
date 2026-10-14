# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The analytics worker: one spawned process that loads the pack once and runs tool calls under a deadline.

A separate process gives each call a hard deadline: a call that overruns (or a native crash, or CUDA running out
of memory) kills the worker, and a fresh one replaces it, so the next call succeeds. `spawn` rather than `fork`
because forking a process that has initialized CUDA is unsafe. Calls run one at a time, in arrival order.

Before it reports ready, the worker runs every tool once, so the first question does not pay the accelerators'
first-call cost (CUDA kernels compiled on first use: about 11 s on an A100).
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
import time
from datetime import timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from . import bootstrap
from . import tools
from .data import MarketData
from .data import Pack
from .data import footprint
from .tools import anomaly_gpu

logger = logging.getLogger(__name__)

STARTUP_TIMEOUT_SECONDS = 300.0


class WorkerError(RuntimeError):
    """The worker could not start, or died during a call."""


class Worker:
    def __init__(self, root: Path, *, timeout: float, engine: str | None = None) -> None:
        self.root = root
        self.timeout = timeout  # seconds one call may run before the worker is replaced
        self.engine = engine  # "cpu" or "gpu"; None follows MARKET_ANALYTICS_ENGINE
        self._lock = threading.Lock()
        self._process: multiprocessing.process.BaseProcess | None = None
        self._connection: Connection | None = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def start(self) -> None:
        with self._lock:
            self._start()

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool in the worker and return its MarketResult data. Blocks; call it from a thread."""
        with self._lock:
            if not self.alive:
                self._start()
            assert self._connection is not None
            try:
                self._connection.send((tool, arguments))
                if self._connection.poll(self.timeout):
                    return self._connection.recv()
            except (EOFError, OSError):
                self._restart(f"the worker exited during {tool}")
                raise WorkerError(f"the analytics worker exited during {tool}") from None
            self._restart(f"{tool} exceeded its {self.timeout:g} s deadline")
            raise TimeoutError(f"{tool} exceeded its {self.timeout:g} s deadline")

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _start(self) -> None:
        self._stop()
        context = multiprocessing.get_context("spawn")
        self._connection, child = context.Pipe()
        self._process = context.Process(
            target=bootstrap.worker_main,
            args=(child, self.root, self.engine),
            name=f"market-analytics-{self.engine}-worker" if self.engine else "market-analytics-worker",
            daemon=True,
        )
        self._process.start()
        child.close()
        if not self._connection.poll(STARTUP_TIMEOUT_SECONDS):
            self._stop()
            raise WorkerError(f"the analytics worker did not load the data within {STARTUP_TIMEOUT_SECONDS:g} s")
        try:
            status, detail = self._connection.recv()
        except EOFError:
            status, detail = "failed", "it exited while loading the data"
        if status != "ready":
            self._stop()
            raise WorkerError(f"the analytics worker failed to start: {detail}")
        logger.info("analytics worker %d ready: %s", self._process.pid, detail)

    def _restart(self, reason: str) -> None:
        logger.warning("replacing analytics worker %s: %s", self.pid, reason)
        self._stop()
        try:
            self._start()
        except WorkerError:
            logger.exception("the replacement worker failed to start; the next call will try again")

    def _stop(self) -> None:
        if self._process is not None:
            self._process.kill()
            self._process.join()
            self._process.close()
        if self._connection is not None:
            self._connection.close()
        self._process = self._connection = None


def serve(connection: Connection, root: Path) -> None:
    """The worker process: load the pack, then answer (tool, arguments) requests until the pipe closes."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(processName)s %(name)s: %(message)s")
    try:
        pack = Pack.load(root)
        logger.info("loading %s: %s", root, footprint(pack, tools.engine("tabular").device))
        data = MarketData.load(pack)
    except Exception as error:
        logger.exception("loading %s failed", root)
        connection.send(("failed", f"{type(error).__name__}: {error}"))
        return
    # The frame type shows whether cudf.pandas took effect: a proxy type on GPU, pandas.DataFrame on CPU.
    frame = f"{type(data.prices).__module__}.{type(data.prices).__qualname__}"
    engines = ", ".join(str(tools.engine(family)) for family in ("tabular", "ml", "graph"))
    connection.send(("ready", f"{len(data.prices):,} price rows as {frame}; {engines}; {warm_up(data)}"))
    while True:
        try:
            tool, arguments = connection.recv()
        except EOFError:
            return
        connection.send(tools.run(data, tool, arguments))


def warm_up(data: MarketData) -> str:
    """Run each tool once on the pack's own data and discard the results. Never raises.

    The GPU libraries pay a one-time cost on first use (cudf.pandas compiles CUDA kernels: about 11 s on an A100);
    on the CPU engine this is about 2 s of pandas.
    """
    started = time.perf_counter()
    try:
        calls = _warm_up_calls(data)
    except Exception as error:  # a failed warm-up only means the first calls pay the cost
        logger.warning("the warm-up could not start: %s: %s", type(error).__name__, error)
        calls = []
    statuses = []
    for tool, arguments in calls:
        status = tools.run(data, tool, arguments)["status"]  # never raises
        statuses.append(f"{tool} {status}")
    try:
        anomaly_gpu.prepare(data)  # every universe's anomaly features on the device, so no scan pays for it
    except Exception as error:  # the first scan of a universe then pays for it
        logger.warning("the anomaly features could not be kept on the device: %s: %s", type(error).__name__, error)
    return f"warm-up in {time.perf_counter() - started:.1f} s: {', '.join(statuses) or 'failed'}"


def _warm_up_calls(data: MarketData) -> list[tuple[str, dict[str, Any]]]:
    """Every tool the pack supports, on the largest universe: the scans over the whole price history, the others
    over its last month, and intraday_scan over ten assets' last week."""
    universe = max(sorted(data.universes), key=lambda name: len(data.universes[name]))
    timestamps = data.prices["timestamp"]
    start, end = timestamps.min().to_pydatetime(), timestamps.max().to_pydatetime()
    middle = start + (end - start) / 2
    month = {"start": end - timedelta(days=30), "end": end}
    assets = data.universes[universe]
    calls = [
        ("market_scan", {"universe_id": universe, "start": start, "end": end, "metrics": ["return", "volatility"]}),
        (
            "market_anomaly_scan",
            {
                "universe_id": universe,
                "training_start": start,
                "training_end": middle,
                "scoring_start": middle + timedelta(seconds=1),
                "scoring_end": end,
            },
        ),
        ("price_context", {"asset_ids": [assets[0]], "frequency": "weekly", **month}),
        ("sentiment_timeline", {"frequency": "weekly", **month}),
        ("analyze_news_price_relationship", {"published_from": month["start"], "published_to": end}),
        ("analyze_market_relationships", {}),
        ("intraday_scan", {"asset_ids": list(assets[:10]), "start": end - timedelta(days=7), "end": end}),
    ]
    return [(tool, arguments) for tool, arguments in calls if tools.available(data.pack, tool)]

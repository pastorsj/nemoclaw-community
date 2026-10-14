# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The market tools and the dispatcher the worker process runs them through.

Two tools need data a pack may not have: the news tools a ticker-linked news table, and intraday_scan minute bars.
They stay registered everywhere, so the tool registry, the sandbox policy and the UI never change with the pack;
on a pack without that data they fail at once with `news_unavailable` or `minute_bars_unavailable`.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Literal

from ..data import MarketData
from ..data import Pack
from ..models import Engine
from ..models import ErrorCode
from ..models import Failure
from ..models import InvalidRequest
from ..models import MarketResult
from ..models import Timing
from . import anomaly
from . import intraday
from . import market_scan
from . import news
from . import price_context
from . import relationships
from .common import Output

logger = logging.getLogger(__name__)

Family = Literal["tabular", "ml", "graph"]
Needs = Literal["news", "minute_bars"]


@dataclass(frozen=True)
class Tool:
    run: Callable[..., Output]
    family: Family  # which library does the work, for the engine badge
    limitations: tuple[str, ...]
    needs: Needs | None = None  # data a pack may not have


TOOLS = {
    "market_scan": Tool(market_scan.run, "tabular", market_scan.LIMITATIONS),
    "market_anomaly_scan": Tool(anomaly.run, "ml", anomaly.LIMITATIONS),
    "price_context": Tool(price_context.run, "tabular", price_context.LIMITATIONS),
    "sentiment_timeline": Tool(news.sentiment_timeline, "tabular", news.SENTIMENT_LIMITATIONS, needs="news"),
    "analyze_news_price_relationship": Tool(
        news.news_price_relationship, "tabular", news.NEWS_PRICE_LIMITATIONS, needs="news"
    ),
    "analyze_market_relationships": Tool(relationships.run, "graph", relationships.LIMITATIONS),
    "intraday_scan": Tool(intraday.run, "tabular", intraday.LIMITATIONS, needs="minute_bars"),
}
UNAVAILABLE: dict[Needs, tuple[ErrorCode, str]] = {
    "news": (
        "news_unavailable",
        "The active data pack has no ticker-linked news table, so this tool is unavailable. "
        "Use retrieve_evidence for filings and other documents.",
    ),
    "minute_bars": (
        "minute_bars_unavailable",
        "The active data pack has no minute bars, so this tool is unavailable. "
        "Use price_context or market_scan for daily prices.",
    ),
}


def available(pack: Pack, name: str) -> bool:
    """Whether the pack has the data the tool needs."""
    needs = TOOLS[name].needs
    return not (needs == "news" and pack.news_table is None or needs == "minute_bars" and pack.minute_bars is None)


# The library that does each family's work, as (label, module that reports the version). bootstrap.py installs
# the GPU accelerators, which run the same pandas, scikit-learn and NetworkX calls.
CPU_LIBRARIES: dict[Family, tuple[str, str]] = {
    "tabular": ("pandas", "pandas"),
    "ml": ("scikit-learn", "sklearn"),
    "graph": ("networkx", "networkx"),
}
GPU_LIBRARIES: dict[Family, tuple[str, str]] = {
    "tabular": ("cudf.pandas", "cudf"),
    "ml": ("cuml", "cuml"),
    "graph": ("nx-cugraph", "nx_cugraph"),
}
# The engine ids the receipts show, by family and device: the same method on each device, so a CPU id names the
# GPU id's twin. Bump the version when an engine's method changes its results.
ENGINE_IDS: dict[Family, dict[str, str]] = {
    "tabular": {"cpu": "pandas-cpu.v1", "gpu": "cudf-gpu.v1"},
    "ml": {"cpu": "sklearn-pca-anomaly-cpu.v1", "gpu": "cuml-pca-anomaly-gpu.v1"},
    "graph": {"cpu": "networkx-pagerank-cpu.v1", "gpu": "cugraph-pagerank-gpu.v1"},
}


def engine(family: Family) -> Engine:
    device = "gpu" if os.environ.get("MARKET_ANALYTICS_ENGINE", "cpu") == "gpu" else "cpu"
    label, module = (GPU_LIBRARIES if device == "gpu" else CPU_LIBRARIES)[family]
    version = importlib.import_module(module).__version__
    return Engine(device=device, library=label, version=version, engine_id=ENGINE_IDS[family][device])


def _naive_utc(value: Any) -> Any:
    """Timezone-aware arguments as naive UTC, the frames' timestamp model (see data.py). Naive ones are UTC."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def run(data: MarketData, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one tool and return its MarketResult as JSON-ready data. Never raises.

    The timing has three measured spans on one clock: the setup before the calculation (reading the arguments),
    the calculation (`compute_ms`), and the setup after it (building and serializing the result). The setup is
    their sum and `engine_ms` the whole span; the server sets `total_ms`, the call as the service saw it.
    """
    tool = TOOLS[name]
    started = time.perf_counter()
    outcome: dict[str, Any]
    if not available(data.pack, name):
        code, message = UNAVAILABLE[tool.needs]
        outcome = {"status": "failed", "error": Failure(code=code, message=message)}
        computing = computed = time.perf_counter()
    else:
        converted = {key: _naive_utc(value) for key, value in arguments.items()}
        computing = time.perf_counter()
        outcome = _outcome(data, name, converted)
        computed = time.perf_counter()
    result = MarketResult(
        operation_id=name,
        source_id=data.pack.source_id,
        database_name=data.pack.database_name,
        timing=Timing(compute_ms=0.0, total_ms=0.0),  # set below, once the result is serialized
        **outcome,
    ).model_dump(mode="json", serialize_as_any=True)
    finished = time.perf_counter()
    compute_ms = (computed - computing) * 1000
    setup_ms = ((computing - started) + (finished - computed)) * 1000
    engine_ms = (finished - started) * 1000
    # The server replaces total_ms with the call's whole time in the service
    timing = Timing(compute_ms=compute_ms, setup_ms=setup_ms, engine_ms=engine_ms, total_ms=engine_ms)
    return result | {"timing": timing.model_dump(mode="json")}


def _outcome(data: MarketData, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The MarketResult fields a tool call decides: its status and payload, or its error."""
    tool = TOOLS[name]
    try:
        output = tool.run(data, **arguments)
    except InvalidRequest as error:
        return {"status": "failed", "error": Failure(code="invalid_request", message=str(error))}
    except Exception:
        logger.exception("%s failed", name)
        message = f"{name} could not complete; see the server log."
        return {"status": "failed", "error": Failure(code="execution_failed", message=message)}
    return {
        "status": "empty" if output.empty else "succeeded",
        "payload": output.payload,
        "engine": engine(tool.family),
        "rows_scanned": output.rows_scanned,
        "asset_count": int(output.assets),
        "warnings": list(output.warnings),
        "limitations": list(tool.limitations),
    }

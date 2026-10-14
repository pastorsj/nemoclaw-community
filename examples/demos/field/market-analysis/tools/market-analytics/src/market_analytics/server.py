# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""MCP server (streamable HTTP at :3010/mcp) exposing the market tools, plus GET /health and POST /benchmark.

The tools are async and hand each call to the worker process on a thread, so a long calculation never blocks
the event loop: `tools/list` and other requests stay responsive while calls queue for the worker.

POST /benchmark (benchmark.py) runs one tool call on the GPU service's two engines for the API's Benchmark tab.
The agent's sandbox can reach only /mcp.

No `from __future__ import annotations` here: the tool signatures use a type built from the pack at runtime
(UniverseId), which the MCP SDK must be able to resolve when it reads them.
"""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Literal

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import benchmark
from . import budget
from .data import ContractError
from .data import Pack
from .data import validate
from .models import Comparison
from .models import Direction
from .models import ErrorCode
from .models import Failure
from .models import Frequency
from .models import IntradayMetric
from .models import IntradayScanPayload
from .models import MarketAnomalyPayload
from .models import MarketRelationshipsPayload
from .models import MarketResult
from .models import MarketScanPayload
from .models import Metric
from .models import NewsPriceRelationshipPayload
from .models import PriceContextPayload
from .models import SentimentTimelinePayload
from .models import Timing
from .worker import Worker
from .worker import WorkerError

PORT = 3010
# The Kumo Relational service's key, as a Compose secret (compose.yaml). KUMO_API_KEY in the environment also works.
KUMO_API_KEY_SECRET = Path("/run/secrets/kumo_api_key")
READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

logger = logging.getLogger(__name__)

Start = Annotated[AwareDatetime, Field(description="Window start, inclusive, with a timezone (2026-08-01T00:00:00Z)")]
End = Annotated[AwareDatetime, Field(description="Window end, inclusive, with a timezone (2026-08-31T23:59:59Z)")]
Assets = Annotated[list[str], Field(min_length=1, max_length=50, description="Asset ids, tickers or company names")]
OptionalAssets = Annotated[
    list[str] | None, Field(max_length=50, description="Only these assets (ids, tickers or company names)")
]
SourceNames = Annotated[list[str] | None, Field(max_length=20, description="Only articles from these news sources")]
SourceIds = Annotated[
    list[str] | None, Field(description="The sources selected for this run. The application sets it; leave it out.")
]
NO_NEWS = "Unavailable in the active data pack, which has no ticker-linked news table. "
NO_MINUTE_BARS = "Unavailable in the active data pack, which has no minute bars. "

# The worker a tool call runs in when not the service's own: /benchmark runs the same MCP tool on each engine.
_TARGET: ContextVar[Worker | None] = ContextVar("market_analytics_target", default=None)


def create_server(pack: Pack, worker: Worker, *, cpu_worker: Callable[[], Worker] | None = None) -> MCPServer:
    """`cpu_worker`, on the GPU service only, returns a started CPU worker for /benchmark (see benchmark.py)."""
    server = MCPServer(
        "market_analytics",
        instructions="Read-only market analytics over the active data pack. Results are descriptive, not advice.",
    )
    universe_help = "; ".join(f"{name}: {u.description.rstrip('.')}" for name, u in pack.universes.items())
    pick = "The asset universe to analyze: the one that matches the group the question names"
    UniverseId = Annotated[Literal[tuple(pack.universes)], Field(description=f"{pick}. {universe_help}")]
    OptionalUniverse = Annotated[
        Literal[tuple(pack.universes)] | None,
        Field(description=f"{pick}, when no asset_ids are named. {universe_help}"),
    ]
    news = "" if pack.news_table else NO_NEWS

    async def run_in_worker[P: BaseModel](
        tool: str, payload: type[P], source_ids: list[str] | None, **arguments: Any
    ) -> MarketResult[P]:
        started = time.perf_counter()

        def failed(code: ErrorCode, message: str) -> MarketResult[P]:
            return MarketResult[payload](
                operation_id=tool,
                status="failed",
                source_id=pack.source_id,
                database_name=pack.database_name,
                error=Failure(code=code, message=message),
                timing=Timing(compute_ms=0.0, total_ms=0.0),
            )

        if source_ids is not None and pack.source_id not in source_ids:
            result = failed("source_not_selected", f"{pack.source_id} is not one of the selected sources")
        else:
            try:
                target = _TARGET.get() or worker
                result = MarketResult[payload].model_validate(
                    await anyio.to_thread.run_sync(target.call, tool, arguments)
                )
            except TimeoutError as error:
                result = failed("deadline_exceeded", str(error))
            except WorkerError as error:
                result = failed("execution_failed", str(error))
        result.timing.total_ms = (time.perf_counter() - started) * 1000
        return budget.fit(result)

    @server.tool(annotations=READ_ONLY)
    async def market_scan(
        *,
        universe_id: UniverseId,
        end: End,
        metrics: Annotated[list[Metric], Field(min_length=1, max_length=4, description="The first metric ranks")],
        start: Annotated[
            AwareDatetime | None, Field(description="Window start, inclusive, with a timezone; omit it with sessions")
        ] = None,
        sessions: Annotated[
            int | None, Field(ge=2, le=260, description="The number of trading sessions ending at end; replaces start")
        ] = None,
        comparison: Annotated[
            Comparison, Field(description="Rank the value, its magnitude, or its z-score")
        ] = "absolute",
        direction: Direction = "highest",
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        source_ids: SourceIds = None,
    ) -> MarketResult[MarketScanPayload]:
        """Rank a universe's assets by return, volume, volatility or peer-relative return over a window.

        Use direction=highest for leaders and direction=lowest for laggards; make two calls when both ends of the
        ranking are needed. The other metrics are reported next to the ranking one, and every metric also has its
        z-score among the universe's assets (zscores), which says how unusual a value is. Returns and volatility are
        fractions (0.25 is 25%; volatility is the daily standard deviation). A return runs from the close before
        the window's first session to its last close. peer_relative_return is an asset's return minus the mean
        return of the universe's assets, not of its industry peers. For "the N sessions ending D", pass end=D and
        sessions=N instead of start. At most 50 assets are listed.
        """
        return await run_in_worker(
            "market_scan",
            MarketScanPayload,
            source_ids,
            universe_id=universe_id,
            start=start,
            end=end,
            sessions=sessions,
            metrics=metrics,
            comparison=comparison,
            direction=direction,
            limit=limit,
        )

    @server.tool(annotations=READ_ONLY)
    async def market_anomaly_scan(
        *,
        universe_id: UniverseId,
        training_start: Annotated[AwareDatetime, Field(description="Baseline window start, with a timezone")],
        training_end: Annotated[AwareDatetime, Field(description="Baseline window end; before scoring_start")],
        scoring_start: Annotated[AwareDatetime, Field(description="Scored window start, with a timezone")],
        scoring_end: Annotated[AwareDatetime, Field(description="Scored window end, with a timezone")],
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
        minimum_percentile: Annotated[float | None, Field(ge=0, le=100)] = None,
        source_ids: SourceIds = None,
    ) -> MarketResult[MarketAnomalyPayload]:
        """Find sessions whose price and volume behavior differs from an earlier baseline window.

        Use it for unusual observed behavior, not for leaders and laggards or for predictions. Scores measure how
        unusual a session was; they are not probabilities, forecasts or evidence of a cause. observed_deviations
        are robust z-scores against the baseline window, not returns or percentages. At most 25 sessions are listed.
        """
        return await run_in_worker(
            "market_anomaly_scan",
            MarketAnomalyPayload,
            source_ids,
            universe_id=universe_id,
            training_start=training_start,
            training_end=training_end,
            scoring_start=scoring_start,
            scoring_end=scoring_end,
            limit=limit,
            minimum_percentile=minimum_percentile,
        )

    @server.tool(annotations=READ_ONLY)
    async def price_context(
        *,
        asset_ids: Assets,
        start: Start,
        end: End,
        frequency: Frequency = "daily",
        include_series: bool = True,
        point_limit: Annotated[int, Field(ge=1, le=2000)] = 250,
        source_ids: SourceIds = None,
    ) -> MarketResult[PriceContextPayload]:
        """Summarize return, price range and volume for named assets over a window, with an optional price series.

        Weekly and monthly series hold each period's last adjusted close and its total volume. The summary always
        comes from the daily bars, whatever the frequency; its return runs from the close before the window's first
        session (start_price) to its last close. A long series is shortened to keep the result readable whole; the
        summaries still cover the whole window.
        """
        return await run_in_worker(
            "price_context",
            PriceContextPayload,
            source_ids,
            asset_ids=asset_ids,
            start=start,
            end=end,
            frequency=frequency,
            include_series=include_series,
            point_limit=point_limit,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{news}Count positive, neutral and negative news labels per day, week or month over a publication "
            "window. Name asset_ids or a universe_id to count only their news. The labels are stored with the news; "
            "the timeline is descriptive and does not explain market moves. At most the 100 most recent periods are "
            "listed."
        ),
    )
    async def sentiment_timeline(
        *,
        start: Start,
        end: End,
        asset_ids: OptionalAssets = None,
        universe_id: OptionalUniverse = None,
        source_names: SourceNames = None,
        frequency: Frequency = "weekly",
        point_limit: Annotated[int, Field(ge=1, le=500, description="Keep the most recent periods")] = 100,
        source_ids: SourceIds = None,
    ) -> MarketResult[SentimentTimelinePayload]:
        return await run_in_worker(
            "sentiment_timeline",
            SentimentTimelinePayload,
            source_ids,
            start=start,
            end=end,
            asset_ids=asset_ids,
            universe_id=universe_id,
            source_names=source_names,
            frequency=frequency,
            point_limit=point_limit,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{news}Relate news sentiment labels to the returns that followed them. Each article is aligned with "
            "its asset's first session at or after publication (session_return is that session's own return), and "
            "its forward return runs to return_horizon_sessions sessions later. Every article is listed and counted, "
            "even when the data ends before its horizon: then its forward_return is null. Name asset_ids or a "
            "universe_id to use only their news. Reports label counts and the mean forward return per asset "
            "(asset_summaries), and per-label averages and the sentiment/return correlation over the articles with "
            "a forward return. At most 50 events and 50 assets are listed. Correlation does not establish causation."
        ),
    )
    async def analyze_news_price_relationship(
        *,
        published_from: Start,
        published_to: End,
        asset_ids: OptionalAssets = None,
        universe_id: OptionalUniverse = None,
        source_names: SourceNames = None,
        return_horizon_sessions: Annotated[int, Field(ge=1, le=20)] = 2,
        event_limit: Annotated[int, Field(ge=1, le=500, description="How many events to list, at most 50")] = 25,
        source_ids: SourceIds = None,
    ) -> MarketResult[NewsPriceRelationshipPayload]:
        return await run_in_worker(
            "analyze_news_price_relationship",
            NewsPriceRelationshipPayload,
            source_ids,
            published_from=published_from,
            published_to=published_to,
            asset_ids=asset_ids,
            universe_id=universe_id,
            source_names=source_names,
            return_horizon_sessions=return_horizon_sessions,
            event_limit=event_limit,
        )

    start, end = pack.graph_window
    pairs = "declared same-industry peers" if pack.graph_mode == "sparse_declared_peers" else "every pair of assets"

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"Rank assets by PageRank centrality in the fixed graph of daily-return correlations from {start} to "
            f"{end}, whose links join {pairs}, and list its strongest links by absolute correlation: a negative "
            "correlation means the two moved in opposite directions. The graph covers the whole dataset and cannot "
            "be limited to a universe or window; for correlations within a chosen group of assets, query the "
            "database instead."
        ),
    )
    async def analyze_market_relationships(
        top_k: Annotated[int, Field(ge=1, le=50, description="How many assets and links to return")] = 10,
        source_ids: SourceIds = None,
    ) -> MarketResult[MarketRelationshipsPayload]:
        return await run_in_worker("analyze_market_relationships", MarketRelationshipsPayload, source_ids, top_k=top_k)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            f"{'' if pack.minute_bars else NO_MINUTE_BARS}Rank asset sessions by how they traded minute by minute "
            "in the regular session: intraday range, realized volatility, open-to-close return, the deepest drawdown "
            "from the session's running high, or volume. Name asset_ids, or a universe_id to scan every asset in "
            "it. Each session also reports its VWAP and the share of its volume in the first and last 30 minutes. "
            "Ranges, returns, drawdowns and volume shares are fractions (0.05 is 5%). "
            "max_drawdown is negative (-0.08 is an 8% fall from the session's high): rank the deepest with "
            "direction=lowest. "
            "It reads the raw minute bars in place, batch by batch. At most 30 sessions are listed."
        ),
    )
    async def intraday_scan(
        *,
        start: Start,
        end: End,
        asset_ids: OptionalAssets = None,
        universe_id: OptionalUniverse = None,
        rank_by: Annotated[IntradayMetric, Field(description="The metric that ranks the sessions")] = "intraday_range",
        direction: Direction = "highest",
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        source_ids: SourceIds = None,
    ) -> MarketResult[IntradayScanPayload]:
        return await run_in_worker(
            "intraday_scan",
            IntradayScanPayload,
            source_ids,
            start=start,
            end=end,
            asset_ids=asset_ids,
            universe_id=universe_id,
            rank_by=rank_by,
            direction=direction,
            limit=limit,
        )

    benchmark_lock = asyncio.Lock()

    @server.custom_route("/benchmark", methods=["POST"])
    async def compare_engines(request: Request) -> JSONResponse:
        """Matched CPU and GPU runs of one tool call (benchmark.py). Not reachable from the agent's sandbox."""
        if cpu_worker is None:
            return JSONResponse({"available": False, "reason": benchmark.CPU_ONLY})
        try:
            body = benchmark.BenchmarkRequest.model_validate(await request.json())
        except (ValueError, ValidationError) as error:
            return JSONResponse({"detail": f"invalid benchmark request: {error}"[:500]}, status_code=422)
        if body.tool not in {tool.name for tool in await server.list_tools()} - {"predict_asset_outcomes"}:
            return JSONResponse({"detail": f"{body.tool} is not a market analytics tool"}, status_code=422)

        async with benchmark_lock:
            try:
                cpu = await anyio.to_thread.run_sync(cpu_worker)
            except WorkerError as error:
                return JSONResponse({"available": False, "reason": f"The CPU worker did not start: {error}"[:500]})
            targets = {"cpu": cpu, "gpu": worker}

            async def call(target: benchmark.Target) -> dict[str, Any]:
                token = _TARGET.set(targets[target])
                try:
                    result = await server.call_tool(body.tool, dict(body.arguments))
                finally:
                    _TARGET.reset(token)
                if result.is_error:  # the arguments failed the tool's own validation
                    text = result.content[0].text if result.content else "invalid arguments"
                    return {"status": "failed", "error": {"message": text}}
                return result.structured_content

            try:
                outcome = await benchmark.compare(call, pairs=body.pairs, budget_seconds=body.budget_seconds)
            except Exception as error:  # noqa: BLE001 - e.g. a ToolError for unknown arguments
                outcome = {"available": True, "status": "failed", "parity": None, "cpu": None, "gpu": None}
                outcome["reason"] = f"{type(error).__name__}: {error}"[:500]
        return JSONResponse(outcome)

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        """Healthy while the worker process is up; a dead worker is replaced by the next call."""
        return JSONResponse({"worker": worker.pid, "alive": worker.alive}, status_code=200 if worker.alive else 503)

    return server


class KumoConfigError(ValueError):
    """Only one of KUMO_RELATIONAL_URL and KUMO_API_KEY is set."""


def kumo_endpoint(env: Mapping[str, str] = os.environ, secret: Path = KUMO_API_KEY_SECRET) -> tuple[str, str] | None:
    """The Kumo Relational service's URL and key, or None when Kumo is off.

    Kumo is configured by its URL and its key together (docs/kumo-service.md): both set turns predict_asset_outcomes
    on, neither leaves it off, and one without the other is an error. The key comes from KUMO_API_KEY or the
    kumo_api_key secret; Compose writes no secret file for an empty value.
    """
    url = env.get("KUMO_RELATIONAL_URL", "").strip()
    key = env.get("KUMO_API_KEY", "").strip() or (secret.read_text().strip() if secret.is_file() else "")
    if url and key:
        return url, key
    if url or key:
        missing = "KUMO_API_KEY" if url else "KUMO_RELATIONAL_URL"
        raise KumoConfigError(
            f"Kumo needs both KUMO_RELATIONAL_URL and KUMO_API_KEY, and {missing} is empty: set both to turn "
            "predict_asset_outcomes on, or neither to leave it off (docs/kumo-service.md)"
        )
    return None


def register_kumo(server: MCPServer, pack: Pack, endpoint: tuple[str, str] | None) -> bool:
    """Register predict_asset_outcomes when Kumo is configured; True when it was."""
    if endpoint is None:
        logger.info("Kumo is off: predict_asset_outcomes needs KUMO_RELATIONAL_URL and KUMO_API_KEY")
        return False
    from .prediction import register  # only with the optional kumo extra

    url, key = endpoint
    register(server, pack, url, api_key=key)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(processName)s %(name)s: %(message)s")
    # /data/active is a symlink that moves when the data one-shot activates another build. Resolve it once, so a
    # replacement worker loads the same build that the tool schemas describe.
    root = Path(os.environ.get("DATA_ACTIVE_DIR", "/data/active")).resolve()
    pack = Pack.load(root)
    try:
        validate(pack)
        kumo = kumo_endpoint()
    except (ContractError, KumoConfigError) as error:
        raise SystemExit(str(error)) from None
    timeout = float(os.environ.get("MARKET_ANALYTICS_TIMEOUT_SECONDS", "120"))
    worker = Worker(root, timeout=timeout)
    worker.start()
    cpu_worker = None
    if os.environ.get("MARKET_ANALYTICS_ENGINE", "cpu") == "gpu":
        benchmark_cpu = Worker(root, timeout=timeout, engine="cpu")

        def cpu_worker() -> Worker:
            if not benchmark_cpu.alive:
                benchmark_cpu.start()
            return benchmark_cpu

    server = create_server(pack, worker, cpu_worker=cpu_worker)
    register_kumo(server, pack, kumo)
    logger.info("serving %s (%s) on :%d/mcp", pack.source_id, root, PORT)
    try:
        server.run("streamable-http", host="0.0.0.0", port=PORT, json_response=True)
    finally:
        worker.close()
        if cpu_worker is not None:
            benchmark_cpu.close()

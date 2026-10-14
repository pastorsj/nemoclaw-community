# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""predict_asset_outcomes: run one of the pack's curated PQL templates on NVIDIA Kumo Relational.

Registered only when both KUMO_RELATIONAL_URL and KUMO_API_KEY are set (server.kumo_endpoint): a Kumo Relational
service behind a key-checking proxy (docs/kumo-service.md). The model picks a template and an asset scope; it never
writes PQL. The graph is the pack's prediction views, with the keys, time columns and links from pack.json
`prediction`, read from the pack's DuckDB file on each call.
"""

import logging
import re
from typing import Annotated
from typing import Literal

import anyio.to_thread
import duckdb
import pandas as pd
from kumo_relational_client import RelationalClient
from kumo_relational_client import relational
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel
from pydantic import Field

from .data import Pack
from .server import READ_ONLY
from .server import SourceIds

logger = logging.getLogger(__name__)

MODEL = "kumo-relational"
# One attempt and no retries, so a slow or warming NIM yields `available=false` well inside the agent's MCP
# timeout for this server (180 s) rather than a transport error with no receipt.
TIMEOUT_SECONDS = 60.0
# A PQL target's window, e.g. "COUNT(return_outcomes.*, 0, 5, days)": outcomes from the anchor to 5 days after.
WINDOW = re.compile(r",\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*([a-z]+)\s*\)", re.IGNORECASE)


class Horizon(BaseModel):
    value: int
    unit: str


class AssetProbability(BaseModel):
    asset_id: str
    probability: float = Field(description="Probability of the template's outcome, 0 to 1")


class PredictionResult(BaseModel):
    available: bool = Field(description="False when the prediction could not run; `reason` says why")
    reason: str | None = None
    template_id: str
    pql: str
    anchor: str = Field(description="The point in time the prediction starts from")
    horizon: Horizon = Field(description="How far past the anchor the outcome is counted")
    rows: list[AssetProbability] = []
    model: str = MODEL


class Predictor:
    def __init__(self, pack: Pack, url: str, api_key: str | None = None) -> None:
        if pack.prediction is None:
            raise ValueError("Kumo is configured, but the active data pack has no prediction section")
        self.config = pack.prediction
        self.database = pack.database
        self.templates = {template["id"]: template for template in self.config["templates"]}
        self.horizons = {template_id: horizon(template["pql"]) for template_id, template in self.templates.items()}
        self.population: list[str] = self.config["population"]["ids"]
        self.url = url
        self.api_key = api_key

    def result(
        self, template_id: str, *, rows: list[AssetProbability] | None = None, reason: str | None = None
    ) -> PredictionResult:
        """A template's result: its rows, or the reason it is unavailable."""
        return PredictionResult(
            available=reason is None,
            reason=reason,
            template_id=template_id,
            pql=self.templates[template_id]["pql"],
            anchor=self.config["anchor"],
            horizon=self.horizons[template_id],
            rows=rows or [],
        )

    def predict(self, template_id: str, asset_ids: list[str] | None = None) -> PredictionResult:
        unknown = sorted(set(asset_ids or ()) - set(self.population))
        if unknown:
            raise ToolError(f"no predictions for {unknown}; the population is {self.population}")
        try:
            with RelationalClient(self.url, self.api_key, timeout=TIMEOUT_SECONDS, max_retries=0) as client:
                predictions = client.relational(self.graph()).predict(
                    self.templates[template_id]["pql"],
                    asset_ids or self.population,
                    anchor_time=pd.Timestamp(self.config["anchor"]),
                    run_mode="fast",
                    num_retries=0,
                    verbose=False,
                )
        except Exception as error:  # the NIM is down, still warming up, or rejected the request
            logger.warning("Kumo prediction %s failed: %r", template_id, error)
            return self.result(template_id, reason=f"{type(error).__name__}: {error}"[:500])
        # A binary template returns one row per entity; TRUE_PROB is the probability of the outcome.
        rows = [
            AssetProbability(asset_id=str(asset_id), probability=probability)
            for asset_id, probability in zip(predictions["ENTITY"], predictions["TRUE_PROB"], strict=True)
        ]
        return self.result(template_id, rows=rows)

    def graph(self) -> relational.Graph:
        schema, tables, entity = self.config["schema"], self.config["tables"], self.config["entity"]["table"]
        with duckdb.connect(str(self.database), read_only=True) as db:
            db.execute("SET TimeZone = 'UTC'")
            frames = {name: db.sql(f'SELECT * FROM "{schema}"."{name}"').df() for name in tables}
        graph = relational.Graph.from_data(frames, edges=[], verbose=False)
        for name, table in tables.items():
            graph[name].primary_key = table["primary_key"]
            if "time_column" in table:
                graph[name].time_column = table["time_column"]
            if "links_to_entity" in table:
                graph.link(name, table["links_to_entity"], entity)
        return graph


def horizon(pql: str) -> Horizon:
    """A template's horizon, read from its PQL window so it always matches the query that runs."""
    match = WINDOW.search(pql)
    if match is None:
        raise ValueError(f"no aggregation window in PQL template: {pql}")
    start, end, unit = match.groups()
    return Horizon(value=int(end) - int(start), unit=unit.lower())


def register(server: MCPServer, pack: Pack, url: str, api_key: str | None = None) -> Predictor:
    predictor = Predictor(pack, url, api_key)
    outcomes = "; ".join(f"{template_id}: {t['description']}" for template_id, t in predictor.templates.items())
    TemplateId = Annotated[
        Literal[tuple(predictor.templates)], Field(description=f"The outcome to predict. {outcomes}")
    ]
    AssetIds = Annotated[
        list[str] | None, Field(description="Asset ids to score; leave it out to score every asset in the population")
    ]

    @server.tool(annotations=READ_ONLY)
    async def predict_asset_outcomes(
        template_id: TemplateId, asset_ids: AssetIds = None, source_ids: SourceIds = None
    ) -> PredictionResult:
        """Predict, per asset, the probability of a curated future outcome with NVIDIA Kumo Relational.

        Use it only for questions about future outcomes that one of the templates describes. The template fixes
        the outcome and the horizon; the result states both, and the anchor the prediction starts from.
        """
        if source_ids is not None and pack.source_id not in source_ids:
            return predictor.result(template_id, reason=f"{pack.source_id} is not one of the selected sources")
        return await anyio.to_thread.run_sync(predictor.predict, template_id, asset_ids)

    return predictor

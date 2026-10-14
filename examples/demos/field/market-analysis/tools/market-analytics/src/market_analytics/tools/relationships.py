# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""analyze_market_relationships: PageRank centrality over the pack's return-correlation graph.

Each edge is weighted by the absolute correlation of the two assets' daily returns over the pack's window. On
GPU, nx-cugraph runs the same `nx.pagerank` call.
"""

from __future__ import annotations

import networkx as nx

from ..data import MarketData
from ..models import CentralAsset
from ..models import MarketRelationshipsPayload
from ..models import RelationshipEdge
from .common import Output

LIMITATIONS = ("Centrality summarizes the return-correlation graph and does not establish causation.",)
# NetworkX stops once the L1 change is below N * tol; 1e-12 keeps a 2,000-node graph well converged.
TOLERANCE = 1e-12


def run(data: MarketData, *, top_k: int = 10) -> Output:
    scores = nx.pagerank(data.graph, alpha=0.85, weight="weight", tol=TOLERANCE, max_iter=500)
    central = sorted(scores.items(), key=lambda item: (-round(item[1], 6), item[0]))[:top_k]
    edges = data.edges
    strongest = (
        edges[edges["source"] < edges["target"]]
        .sort_values(["weight", "source", "target"], ascending=[False, True, True])
        .head(top_k)
    )
    start, end = data.pack.graph_window
    payload = MarketRelationshipsPayload(
        window_start=start,
        window_end=end,
        node_count=data.graph.number_of_nodes(),
        edge_count=data.graph.number_of_edges(),
        central_assets=[
            CentralAsset(rank=rank, asset_id=asset_id, centrality=score)
            for rank, (asset_id, score) in enumerate(central, start=1)
        ],
        strongest_edges=[
            RelationshipEdge(
                source_asset_id=row["source"], target_asset_id=row["target"], correlation=row["correlation"]
            )
            for row in strongest.to_dict("records")
        ],
    )
    return Output(payload, rows_scanned=len(edges), assets=payload.node_count)

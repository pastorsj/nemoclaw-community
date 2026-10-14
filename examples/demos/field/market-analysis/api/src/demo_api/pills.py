# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The technology pills of a recorded session: what its runs actually used, for the UI's replays list.

Each completed registered tool call (an ``artifact.available`` event) contributes its tool's registry ``pills``, in
the registry's pill order, as one pill per kind across the session's turns. The market tools' pills (cudf, cuml,
cugraph) carry the engine their receipts report: ``gpu``, or ``cpu`` for a run on pandas, scikit-learn or NetworkX,
which the UI labels as such. Hermes's own tools (skills, memory, terminal and the rest) have no pill.
"""

from __future__ import annotations

from typing import Any

from .registry import ToolRegistry

PILL_ORDER = ("cudf", "cuml", "cugraph", "kumo", "retrieval", "ontology")
ENGINE_PILLS = frozenset({"cudf", "cuml", "cugraph"})


def session_pills(turns: list[dict[str, Any]], registry: ToolRegistry) -> list[dict[str, Any]]:
    """``[{pill, device, tools}]``: each pill once per engine, with the tool ids that brought it."""
    found: dict[tuple[str, str | None], list[str]] = {}
    for turn in turns:
        receipts = {receipt.get("receiptId"): receipt for receipt in turn.get("receipts", [])}
        for event in turn.get("events", []):
            tool = registry.by_id(event.get("toolName"))
            if event.get("eventKind") != "artifact.available" or tool is None:
                continue
            engines = [
                ((receipts.get(ref) or {}).get("content") or {}).get("engine") or {}
                for ref in event.get("artifactRefs", [])
            ]
            device = next((engine.get("device") for engine in engines if engine.get("device")), None)
            for pill in tool.pills:
                tools = found.setdefault((pill, device if pill in ENGINE_PILLS else None), [])
                if tool.id not in tools:
                    tools.append(tool.id)
    ordered = sorted(found.items(), key=lambda item: (PILL_ORDER.index(item[0][0]), item[0][1] or ""))
    return [{"pill": pill, "device": device, "tools": tools} for (pill, device), tools in ordered]

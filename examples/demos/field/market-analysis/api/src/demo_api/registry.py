# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The tool registry (``contracts/tool-registry.json``): one entry per MCP tool the agent can call.

The API uses it to label execution events, check receipts and pick the Hermes toolsets a
job may use. It never authorizes a call; the sandbox policy does that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Tool:
    id: str
    server: str
    hermes_name: str
    family: str
    label: str
    receipt_kind: str
    feature: str  # the agent feature that provides it (AGENT_FEATURES)
    pills: tuple[str, ...] = ()  # the technology pills the UI shows for a run that uses the tool


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self.tools = tools
        self._by_hermes_name = {tool.hermes_name: tool for tool in tools}
        self._by_id = {tool.id: tool for tool in tools}

    @classmethod
    def load(cls, path: Path) -> ToolRegistry:
        document = json.loads(path.read_text(encoding="utf-8"))
        fields = Tool.__dataclass_fields__
        return cls(
            [
                Tool(**{key: tuple(value) if key == "pills" else value for key, value in tool.items() if key in fields})
                for tool in document["tools"]
            ]
        )

    def by_hermes_name(self, name: str) -> Tool | None:
        return self._by_hermes_name.get(name)

    def by_id(self, tool_id: str | None) -> Tool | None:
        return self._by_id.get(tool_id or "")

    def available(self, features: frozenset[str]) -> list[Tool]:
        """The tools baked into an agent image built with these features."""
        return [tool for tool in self.tools if tool.feature in features]

    def families(self, features: frozenset[str]) -> set[str]:
        return {tool.family for tool in self.available(features)}

    def toolsets(self, families: set[str], features: frozenset[str]) -> list[str]:
        """Hermes toolsets for a job: always ``skills``, plus the server of every usable tool."""
        servers = {tool.server for tool in self.available(features) if tool.family in families}
        return ["skills", *sorted(servers)]

# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The active data pack, as the ``data`` one-shot built it under ``/data/active``.

``pack.json`` lists the pack's sources and questions. A source is offered to users only when the
running stack can serve it:

- its capabilities are narrowed to the tool families in the agent image (``AGENT_FEATURES``);
- a document source also needs the retrieval index, which ``retrieval-index`` records in
  ``collection-manifest.json``.

``GET /v1/pack`` (``PackView``, a contract: ``contracts/schemas/pack.schema.json``) offers the questions of those
sources, and among them the examples of the composer's picker.

Files are read on every call, because ``/data/active`` is a symlink that a rebuild can switch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from .registry import ToolRegistry

# The most examples the composer's picker offers
MAX_EXAMPLES = 12


class PackUnavailableError(Exception):
    """``/data/active/pack.json`` is missing or unreadable (the data one-shot has not run)."""


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    name: str
    description: str
    agent_description: str
    kind: str
    capabilities: tuple[str, ...]
    synthetic: bool
    default_enabled: bool
    example_questions: tuple[str, ...]
    database_name: str | None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "default_enabled": self.default_enabled,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "synthetic": self.synthetic,
            "database_name": self.database_name,
        }

    def catalog_entry(self) -> dict[str, Any]:
        """How the agent sees this source in its run instructions."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.agent_description,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "synthetic": self.synthetic,
            "example_questions": list(self.example_questions),
        }


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_serialization_defaults_required=True)


class PackQuestionView(_View):
    """A demo question whose sources this stack serves."""

    id: str
    label: str
    tag: str | None = None
    description: str | None = None
    question: str
    sources: list[str]
    tools: list[str] = Field(
        default_factory=list,
        description="The tools it is expected to use, as pills (Pill in contracts/tool-registry.schema.json).",
    )
    featured: bool = Field(default=False, description="Shown on the landing page.")


class PackConversationView(_View):
    """A multi-turn conversation that `demo-api record` asks; not listed in the UI."""

    id: str
    label: str
    tag: str | None = None
    description: str | None = None
    sources: list[str]
    turns: list[str]


class PackView(_View):
    """``GET /v1/pack``: the active pack's title, disclaimer, questions, examples and conversations."""

    id: str
    version: str | None = None
    title: str
    description: str | None = None
    as_of: str | None = None
    disclaimer: str | None = None
    questions: list[PackQuestionView]
    examples: list[str] = Field(
        max_length=MAX_EXAMPLES,
        description=(
            "The ids of the questions the composer's example picker offers, in order: the pack's `examples` "
            "(questions.yaml), or else its featured questions and then the others, at most 12, all among `questions`."
        ),
    )
    conversations: list[PackConversationView]


def picker_examples(questions: list[dict[str, Any]], declared: list[str] | None) -> list[str]:
    """The ids of the picker's examples among ``questions``, at most ``MAX_EXAMPLES``.

    The declared ones in their order, or without a list the featured questions and then the others.
    """
    offered = [question["id"] for question in questions]
    if declared is None:
        featured = [question["id"] for question in questions if question.get("featured")]
        declared = featured + [question_id for question_id in offered if question_id not in featured]
    return [question_id for question_id in declared if question_id in offered][:MAX_EXAMPLES]


class ActivePack:
    def __init__(self, data_dir: Path, registry: ToolRegistry, features: frozenset[str]) -> None:
        self.data_dir = data_dir
        self._families = registry.families(features)
        # The pills of the tools this stack serves: a question whose `tools` name another, such as kumo without a
        # Kumo service, is not offered.
        self._pills = {pill for tool in registry.available(features) for pill in tool.pills}

    def manifest(self) -> dict[str, Any]:
        try:
            return json.loads((self.data_dir / "pack.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise PackUnavailableError("The active data pack is not built yet") from error

    def collection_manifest(self) -> dict[str, Any] | None:
        try:
            return json.loads((self.data_dir / "collection-manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def sources(self) -> list[Source]:
        manifest = self.manifest()
        structured = manifest.get("structured") or {}
        indexed = set((self.collection_manifest() or {}).get("source_ids", []))
        sources = []
        for entry in manifest.get("sources", []):
            capabilities = tuple(family for family in entry.get("capabilities", []) if family in self._families)
            if entry["kind"] == "documents" and entry["id"] not in indexed:
                capabilities = ()
            if not capabilities:
                continue
            sources.append(
                Source(
                    id=entry["id"],
                    name=entry["name"],
                    description=entry.get("description", ""),
                    agent_description=entry.get("agent_description") or entry.get("description", ""),
                    kind=entry["kind"],
                    capabilities=capabilities,
                    synthetic=bool(entry.get("synthetic", False)),
                    default_enabled=entry.get("default_enabled", True),
                    example_questions=tuple(entry.get("example_questions", [])),
                    database_name=structured.get("database_name") if entry["id"] == structured.get("source") else None,
                )
            )
        return sources

    def public_view(self) -> PackView:
        """``GET /v1/pack``: what the UI shows on its landing page and in the composer's example picker, and the
        conversations `demo-api record` asks.

        Only what this stack can answer: the sources a question or conversation names, and the tools a question
        declares (questions.yaml ``tools``), are all available. A build from before ``examples`` existed has none in
        its pack.json, so it gets the default list too.
        """
        manifest = self.manifest()
        available = {source.id for source in self.sources()}
        questions = [
            question
            for question in manifest.get("questions", [])
            if set(question.get("sources", [])) <= available and set(question.get("tools", [])) <= self._pills
        ]
        return PackView(
            **{key: manifest.get(key) for key in ("id", "version", "title", "description", "as_of", "disclaimer")},
            questions=[
                PackQuestionView(
                    **{key: question.get(key) for key in ("id", "label", "tag", "description", "question", "sources")},
                    tools=question.get("tools", []),
                    featured=bool(question.get("featured", False)),
                )
                for question in questions
            ],
            examples=picker_examples(questions, manifest.get("examples")),
            conversations=[
                PackConversationView(
                    **{key: conversation.get(key) for key in ("id", "label", "tag", "description", "sources", "turns")}
                )
                for conversation in manifest.get("conversations", [])
                if set(conversation.get("sources", [])) <= available
            ],
        )

    def database_path(self) -> Path | None:
        database = (self.manifest().get("structured") or {}).get("database")
        return self.data_dir / database if database else None

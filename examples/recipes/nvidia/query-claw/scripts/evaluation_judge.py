#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Privacy-bounded semantic scoring for Query Claw evaluation answers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DIMENSIONS = (
    "groundedness",
    "completeness",
    "evidence_type_separation",
    "uncertainty_and_format",
)
RUBRIC_VERSION = 2
JUDGE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "query_claw_judge",
        "schema": {
            "type": "object",
            "properties": {
                "scores": {
                    "type": "object",
                    "properties": {
                        dimension: {"type": "integer", "minimum": 0, "maximum": 2}
                        for dimension in DIMENSIONS
                    },
                    "required": list(DIMENSIONS),
                    "additionalProperties": False,
                },
                "material_hallucination": {"type": "boolean"},
            },
            "required": ["scores", "material_hallucination"],
            "additionalProperties": False,
        },
    },
}


class JudgeError(RuntimeError):
    """The judge request or response did not satisfy its strict contract."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class JudgeResult:
    scores: dict[str, int]
    material_hallucination: bool

    @property
    def passed(self) -> bool:
        return not self.material_hallucination and all(
            self.scores[dimension] >= 1 for dimension in DIMENSIONS
        )

    def receipt(self) -> dict[str, Any]:
        """Return only bounded scores and a boolean; never evaluation content."""
        return {
            "scores": self.scores,
            "material_hallucination": self.material_hallucination,
        }


def _chat_completions_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise JudgeError("judge base URL is invalid")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise JudgeError("judge base URL must use HTTPS or loopback HTTP")
    root = base_url.rstrip("/")
    suffix = (
        "/chat/completions"
        if parsed.path.rstrip("/").endswith("/v1")
        else "/v1/chat/completions"
    )
    return root + suffix


def parse_result(raw: Any) -> JudgeResult:
    if not isinstance(raw, dict) or set(raw) != {
        "scores",
        "material_hallucination",
    }:
        raise JudgeError("judge response has an invalid top-level shape")
    scores = raw["scores"]
    if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS):
        raise JudgeError("judge response has invalid score dimensions")
    if any(
        isinstance(scores[name], bool)
        or not isinstance(scores[name], int)
        or not 0 <= scores[name] <= 2
        for name in DIMENSIONS
    ):
        raise JudgeError("judge scores must be integers from 0 to 2")
    hallucination = raw["material_hallucination"]
    if not isinstance(hallucination, bool):
        raise JudgeError("material_hallucination must be true or false")
    return JudgeResult(dict(scores), hallucination)


def score_answer(
    *,
    base_url: str,
    api_key: str,
    model: str,
    question: str,
    answer: str,
    expected_facts: tuple[str, ...],
    expected_citations: tuple[str, ...],
    expected_routes: tuple[str, ...],
    expected_format: str,
    expect_abstention: bool,
    timeout: int,
) -> JudgeResult:
    """Score one answer without logging or returning the submitted content."""
    if not api_key or not model or timeout <= 0:
        raise JudgeError("judge API key, model, and positive timeout are required")
    rubric = (
        "Score the assistant answer from 0 (fails), 1 (partial), or 2 (meets) on: "
        "groundedness, completeness, evidence_type_separation, and uncertainty_and_format. "
        "Give 2 when a dimension is genuinely not applicable and the answer handles that "
        "appropriately. "
        "The minimum_checks are required anchors, not an exhaustive transcript of source "
        "evidence. Do not reject a detail merely because it is absent from minimum_checks. "
        "Judge groundedness from consistency with those anchors, source labels, citations, "
        "and uncertainty. Set material_hallucination true only when the answer contradicts "
        "an anchor, invents an incompatible source or identifier, or refuses to abstain when "
        "abstention is required. Return JSON only with exactly: scores (the four "
        "named integer fields) and material_hallucination (boolean). Do not include prose or "
        "quote the inputs."
    )
    evidence = {
        "question": question,
        "answer": answer,
        "minimum_checks": {
            "facts": list(expected_facts),
            "citations": list(expected_citations),
            "evidence_types": list(expected_routes),
            "format": expected_format or "unspecified",
            "abstention_required": expect_abstention,
        },
    }
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": JUDGE_RESPONSE_FORMAT,
        "messages": [
            {"role": "system", "content": rubric},
            {"role": "user", "content": json.dumps(evidence, separators=(",", ":"))},
        ],
    }
    request = Request(
        _chat_completions_url(base_url),
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            document = json.load(response)
        content = document["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError
        return parse_result(json.loads(content))
    except HTTPError as exc:
        raise JudgeError(f"judge returned HTTP {exc.code}") from exc
    except (
        KeyError,
        IndexError,
        TypeError,
        OSError,
        URLError,
        json.JSONDecodeError,
    ) as exc:
        raise JudgeError(f"judge request failed: {type(exc).__name__}") from exc

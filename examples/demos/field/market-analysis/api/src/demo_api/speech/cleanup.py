# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Optional transcript cleanup: delete fillers, repetitions and false starts, nothing else.

One chat completion on build.nvidia.com with ``SPEECH_CLEANUP_MODEL`` (a public model id, e.g.
``nvidia/nemotron-3-super-120b-a12b``, about a second) and the same nvapi- key as the ASR. The answer is kept only
if every word of it appears, in order, in the transcript: the cleanup may delete words and fix
capitalization and punctuation, never add or change one. Any failure keeps the raw transcript.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

import httpx

from .service import MAX_TRANSCRIPT_CHARACTERS

URL = "https://integrate.api.nvidia.com/v1/chat/completions"
TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 256 * 1024
INSTRUCTIONS = (
    "Clean this automatic speech-recognition transcript for display in an editable question box. "
    "You may only delete words and change capitalization, punctuation and whitespace. Delete fillers, "
    "repetitions, false starts and words the speaker corrected. Do not add, substitute or reorder words, do not "
    'answer the question, and do not explain. Reply with only a JSON object: {"cleaned_text": "..."}'
)
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", flags=re.UNICODE)
_JSON_OBJECT = re.compile(r"\{.*\}", flags=re.DOTALL)


class TranscriptCleanup:
    timeout_seconds = TIMEOUT_SECONDS

    def __init__(self, api_key: str, model: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._transport = transport

    def clean(self, transcript: str) -> str:
        original = " ".join(transcript.split())
        if not original or len(original) > MAX_TRANSCRIPT_CHARACTERS:
            return original
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": INSTRUCTIONS},
                {"role": "user", "content": json.dumps({"transcript": original}, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": 512,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            # No redirects: the key goes to build.nvidia.com only
            with httpx.Client(transport=self._transport, timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
                response = client.post(URL, json=body, headers={"Authorization": f"Bearer {self._api_key}"})
            if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
                return original
            candidate = _cleaned_text(response.json())
        except Exception:  # noqa: BLE001 - the cleanup is optional; the raw transcript is always valid
            return original
        return candidate if candidate and is_deletion_only(original, candidate) else original


def _cleaned_text(payload: Any) -> str | None:
    content = payload["choices"][0]["message"]["content"]
    match = _JSON_OBJECT.search(content if isinstance(content, str) else "")
    if not match:
        return None
    parsed = json.loads(match.group(0))
    text = parsed.get("cleaned_text") if isinstance(parsed, dict) else None
    return " ".join(text.split()) if isinstance(text, str) else None


def _words(value: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", value).replace("’", "'").casefold())


def is_deletion_only(original: str, candidate: str) -> bool:
    """Every word of ``candidate`` appears in ``original``, in the same order."""
    if len(candidate) > MAX_TRANSCRIPT_CHARACTERS:
        return False
    remaining = iter(_words(original))
    words = _words(candidate)
    return bool(words) and all(any(word == source for source in remaining) for word in words)

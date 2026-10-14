# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NVIDIA embed and rerank clients from langchain-nvidia-ai-endpoints. The only module that knows endpoints.

Every client gets an explicit base_url and api_key: left unset, LangChain falls back to the global
NVIDIA_BASE_URL / NVIDIA_API_KEY, which in this stack belong to other services.
"""

from __future__ import annotations

import logging
import re
from functools import cache
from typing import Literal

import aiohttp
import requests
from langchain_nvidia_ai_endpoints import Model
from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings
from langchain_nvidia_ai_endpoints import NVIDIARerank
from langchain_nvidia_ai_endpoints import register_model
from tenacity import before_sleep_log
from tenacity import retry
from tenacity import retry_if_exception
from tenacity import stop_after_attempt
from tenacity import wait_exponential_jitter

from .settings import BUILD_NVIDIA_URL
from .settings import Settings

# One rerank request scores every candidate: search never collects more than this many.
MAX_RERANK_PASSAGES = 200

logger = logging.getLogger(__name__)


def embedder(settings: Settings) -> NVIDIAEmbeddings:
    # nemotron-3-embed-1b is not in the 1.4.3 model table. Registering it skips the blocking
    # GET /v1/models lookup the client otherwise runs at construction.
    _register(settings.embed_model, "embedding", "NVIDIAEmbeddings", "{base_url}/embeddings")
    return NVIDIAEmbeddings(
        model=settings.embed_model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        truncate="END",
    )


def reranker(settings: Settings) -> NVIDIARerank:
    """Posts to RETRIEVER_RERANK_URL if set, else the model's build.nvidia.com endpoint, else {base_url}/ranking."""
    base_url = settings.base_url
    if settings.rerank_url:
        _register(settings.rerank_model, "ranking", "NVIDIARerank", settings.rerank_url)
        # The client only honours a registered endpoint in hosted mode, which the base URL selects.
        # No request goes to the base URL itself.
        base_url = BUILD_NVIDIA_URL
    return NVIDIARerank(
        model=settings.rerank_model,
        base_url=base_url,
        api_key=settings.api_key,
        truncate="END",
        top_n=MAX_RERANK_PASSAGES,
        max_batch_size=MAX_RERANK_PASSAGES,
    )


@cache  # registration is process-wide; registering the same model again only warns
def _register(
    model_id: str,
    model_type: Literal["embedding", "ranking"],
    client: Literal["NVIDIAEmbeddings", "NVIDIARerank"],
    endpoint: str,
) -> None:
    register_model(Model(id=model_id, model_type=model_type, client=client, endpoint=endpoint))


def _is_transient(error: BaseException) -> bool:
    """Dropped connections, timeouts, throttling and server errors. Anything else fails at once."""
    if isinstance(error, (requests.ConnectionError, requests.Timeout, aiohttp.ClientError, TimeoutError)):
        return True
    # HTTP errors are plain Exceptions whose message starts with "[<status>]". The async client writes "[###]"
    # when the error body is not JSON, as a gateway's 502/503/504 page usually is.
    status = re.match(r"\[(\d{3}|###)\]", str(error))
    return status is not None and (status[1] in {"408", "429", "###"} or status[1].startswith("5"))


def _retry(attempts: int, max_wait: float):
    return retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=0.5, max=max_wait),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


# The clients have no retries of their own; wrap each embed or rerank call with one of these.
# A tool call has an agent waiting on it, so it gives up after a few seconds.
retry_transient = _retry(attempts=3, max_wait=4)
# Ingest sends hundreds of requests in a row; one that fails for good stops the build until the next run resumes it.
retry_bulk = _retry(attempts=8, max_wait=8)

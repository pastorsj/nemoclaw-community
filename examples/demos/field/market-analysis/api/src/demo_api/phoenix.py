# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Find a job's trace in Phoenix.

Each run request carries ``aiq.job.ref`` metadata, which NeMo Relay promotes to an attribute of the
turn span (Hermes patch 0001). Relay exports that span only once the turn ends, so the search falls back
to ``session.id``: Switchyard's spans in the same trace carry the Hermes session id, which is the job id,
and arrive with each model call.
Any matching span names the trace. The browser opens it at ``<Phoenix URL>/redirects/traces/<trace id>``.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from .hermes.request import correlation_ref


class PhoenixUnavailableError(Exception):
    pass


async def find_trace_id(http: httpx.AsyncClient, *, base_url: str, project: str, job_id: str) -> str | None:
    url = f"{base_url.rstrip('/')}/v1/projects/{quote(project, safe='')}/spans"
    for attribute in (f"aiq.job.ref:{correlation_ref(job_id)}", f"session.id:{job_id}"):
        try:
            response = await http.get(url, params={"attribute": attribute, "limit": 1}, timeout=5.0)
        except httpx.HTTPError as error:
            raise PhoenixUnavailableError from error
        if response.status_code == 404:
            return None  # the project appears with its first trace
        if response.status_code != 200:
            raise PhoenixUnavailableError
        if spans := response.json().get("data"):
            return spans[0]["context"]["trace_id"]
    return None

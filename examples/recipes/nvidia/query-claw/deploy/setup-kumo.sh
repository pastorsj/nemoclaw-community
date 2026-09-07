#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
initialize_deploy_env
require_var KUMO_RFM_API_URL
require_command sha256sum
qualification_marker="$RUNTIME_DIR/kumo-qualified.sha256"
rm -f "$qualification_marker"

compose build query-claw-mcp
compose up -d --no-deps --force-recreate --remove-orphans query-claw-mcp

container_id="$(compose ps -q query-claw-mcp)"
for _ in {1..60}; do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' "$container_id" 2>/dev/null)" == healthy ]] && break
  sleep 2
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' "$container_id" 2>/dev/null)" == healthy ]] || \
  die "query-claw-mcp did not become healthy"

compose exec -T query-claw-mcp python - <<'PY'
import asyncio
import importlib.util
import os

import requests


spec = importlib.util.spec_from_file_location("query_claw_mcp", "/opt/query-claw/app.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

endpoint = module._validated_kumo_endpoint(os.environ["KUMO_RFM_API_URL"])
headers = {}
if api_key := os.environ.get("KUMO_RFM_API_KEY"):
    headers["X-API-Key"] = api_key
response = requests.get(
    endpoint + "/v1/health/ready",
    headers=headers,
    timeout=30,
    allow_redirects=False,
    stream=True,
)
try:
    if response.is_redirect:
        raise RuntimeError(
            "KUMO_RFM_API_URL redirects instead of serving a Kumo prediction API; "
            "set the direct API base URL"
        )
    if response.status_code != 200:
        raise RuntimeError(
            "the Kumo readiness endpoint returned HTTP "
            f"{response.status_code}"
        )
finally:
    response.close()


async def qualify():
    runtime = module.KumoRuntime()
    try:
        await runtime.ready()
        result = await asyncio.to_thread(
            runtime.model.predict,
            module.KUMO_PREDICTION_PQL,
            indices=runtime.prediction_entity_ids[:1],
            anchor_time=runtime.prediction_cutoff,
            run_mode="fast",
        )
        if result is None:
            raise RuntimeError("Kumo returned no prediction result")
        module._rank_prediction_rows(
            result,
            runtime.prediction_entity_ids[:1],
            runtime.prediction_cutoff.date().isoformat(),
        )
    finally:
        await asyncio.to_thread(runtime.close)


asyncio.run(qualify())
PY

(umask 077 && kumo_qualification_fingerprint >"$qualification_marker")

printf 'ready: bounded Query Claw MCP facade and Kumo prediction API\n'

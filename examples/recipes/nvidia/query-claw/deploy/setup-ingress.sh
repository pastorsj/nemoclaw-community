#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
initialize_deploy_env

# Recreate so an idempotent rerun also applies Caddyfile changes.
compose up -d --no-deps --force-recreate mcp-ingress
CA_FILE="$RUNTIME_DIR/query-claw-mcp-root.crt"
for _ in {1..30}; do
  if compose cp mcp-ingress:/data/caddy/pki/authorities/local/root.crt "$CA_FILE" \
    >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
[[ -s "$CA_FILE" ]] || die "Caddy root certificate was not created"
# A root certificate is public trust material. Keep it readable by the
# unprivileged official GSF MCP process through its read-only bind mount.
chmod 644 "$CA_FILE"
private_curl=(curl --noproxy '*' --connect-timeout 3 --max-time 5 \
  --proto '=https' --cacert "$CA_FILE")

if [[ "$QUERY_CLAW_HAS_DOCUMENTS" == 1 ]]; then
  for _ in {1..60}; do
    if "${private_curl[@]}" --fail --silent \
      "https://$QUERY_CLAW_PRIVATE_HOST:9443/query-claw/health" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  "${private_curl[@]}" --fail --silent --show-error \
    "https://$QUERY_CLAW_PRIVATE_HOST:9443/query-claw/health" >/dev/null || \
    die "native NeMo Retriever MCP TLS ingress did not become ready"
fi

# The official endpoint must return its exact unauthenticated OAuth discovery
# chain. Probe without a bearer token; neither response contains credentials.
if [[ "$QUERY_CLAW_HAS_STRUCTURED" == 1 ]]; then
  gsf_code=''
  for _ in {1..60}; do
    gsf_code="$("${private_curl[@]}" --silent --output /dev/null \
      --write-out '%{http_code}' \
      "https://$QUERY_CLAW_PRIVATE_HOST:9444/mcp" 2>/dev/null || true)"
    [[ "$gsf_code" == 401 ]] && break
    sleep 2
  done
  [[ "$gsf_code" == 401 ]] || \
    die "official GSF MCP ingress did not return its OAuth challenge (HTTP ${gsf_code:-none})"

  umask 077
  probe_dir="$(mktemp -d "$RUNTIME_DIR/gsf-oauth-probe.XXXXXX")"
  cleanup_probe() {
    rm -rf "$probe_dir"
  }
  trap cleanup_probe EXIT
  challenge_headers="$probe_dir/challenge.headers"
  challenge_body="$probe_dir/challenge.body"
  gsf_code="$("${private_curl[@]}" --silent --show-error \
    --dump-header "$challenge_headers" --output "$challenge_body" \
    --write-out '%{http_code}' \
    "https://$QUERY_CLAW_PRIVATE_HOST:9444/mcp" 2>/dev/null || true)"
  [[ "$gsf_code" == 401 ]] || \
    die "official GSF MCP challenge changed during verification (HTTP ${gsf_code:-none})"
  metadata_url="$(python3 "$DEPLOY_DIR/lib/verify_gsf_oauth.py" \
    challenge-resource-metadata "$challenge_headers" "$GSF_MCP_URL")" || \
    die "official GSF MCP returned an invalid OAuth challenge"

  metadata_headers="$probe_dir/protected-resource.headers"
  metadata_body="$probe_dir/protected-resource.json"
  metadata_code="$("${private_curl[@]}" --silent --show-error \
    --dump-header "$metadata_headers" --output "$metadata_body" \
    --write-out '%{http_code}' "$metadata_url" 2>/dev/null || true)"
  [[ "$metadata_code" == 200 ]] || \
    die "GSF protected-resource metadata returned HTTP ${metadata_code:-none}"
  python3 "$DEPLOY_DIR/lib/verify_gsf_oauth.py" protected-resource-document \
    "$metadata_headers" "$metadata_body" "$GSF_MCP_URL" "$GSF_PUBLIC_URL" || \
    die "official GSF MCP returned invalid protected-resource metadata"
  cleanup_probe
  trap - EXIT
fi

[[ "$QUERY_CLAW_HAS_DOCUMENTS" != 1 ]] || \
  printf 'ready: native NeMo Retriever MCP at https://%s:9443/mcp\n' \
    "$QUERY_CLAW_PRIVATE_HOST"
[[ "$QUERY_CLAW_HAS_STRUCTURED" != 1 ]] || \
  printf 'ready: official GSF MCP at https://%s:9444/mcp (OAuth required)\n' \
    "$QUERY_CLAW_PRIVATE_HOST"

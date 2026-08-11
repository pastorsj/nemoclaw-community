#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$DIR/_lib.sh"

pass() { printf '  [ok] %s\n' "$1"; }
fail() { printf '  [!!] %s\n' "$1" >&2; exit 1; }

load_env
require_runtime_config
require_command openshell
mkdir -p "$STATE_DIR"

echo "Offline recipe checks"
while IFS= read -r test_file; do
  python3 "$test_file" >/dev/null || fail "$(basename "$test_file")"
  pass "$(basename "$test_file")"
done < <(find "$EXAMPLE_DIR" -type f -name 'test_*.py' | sort)

echo "Live deployment checks"
if curl -fsS http://127.0.0.1:6006/healthz >/dev/null; then
  pass "Phoenix health"
else
  fail "Phoenix health"
fi
if [[ "$(sandbox_phase)" == "Ready" ]]; then
  pass "sandbox Ready"
else
  fail "sandbox Ready"
fi
if sandbox_healthy; then
  pass "Hermes gateway health"
else
  fail "Hermes gateway health"
fi
start_api_forward >/dev/null
if curl -fsS -H 'Authorization: Bearer nemoclaw-internal' \
  http://127.0.0.1:8642/health >/dev/null; then
  pass "loopback API forward"
else
  fail "loopback API forward"
fi

if command -v lsof >/dev/null 2>&1; then
  listeners="$(lsof -nP -iTCP:8642 -sTCP:LISTEN 2>/dev/null || true)"
  [[ -n "$listeners" ]] || fail "API listener discovery"
  if printf '%s\n' "$listeners" | awk 'NR > 1 {print $9}' \
    | grep -qvE '^(127\.0\.0\.1|\[::1\]):8642$'; then
    fail "API forward is not loopback-only"
  fi
  pass "API forward loopback binding"
fi

if openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 30 -- \
  /opt/hermes/.venv/bin/python -c \
  'from importlib.metadata import version; expected={"hermes-agent":"0.20.0","nemo-relay":"0.7.2","cryptography":"50.0.0"}; actual={k:version(k) for k in expected}; assert actual==expected,(actual,expected)' \
  >/dev/null; then
  pass "Hermes 0.20.0 + Relay 0.7.2 runtime"
else
  fail "Hermes/Relay runtime versions"
fi

if openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 30 -- \
  /opt/hermes/.venv/bin/python -c \
  'import tomllib; from nemo_relay import plugin; p="/etc/nemo-relay/config/plugins.toml"; c=tomllib.load(open(p,"rb")); r=plugin.validate(c); assert not r["diagnostics"],r' \
  >/dev/null; then
  pass "native Relay configuration"
else
  fail "native Relay configuration"
fi

if openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 15 -- \
  sh -c "ps -eo comm,args | grep -E '(^|/)(nemo[-_]relay)( |$)' | grep -v grep" \
  >/dev/null 2>&1; then
  fail "unexpected standalone Relay process"
else
  pass "Relay runs in-process with Hermes"
fi

if openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 60 -- \
  bash -lc 'python3 /sandbox/.hermes-data/skills/financial-market-snapshot/scripts/finance_snapshot.py quote NVDA' \
  >"$STATE_DIR/yahoo-smoke.json" \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["ok"] and d["quotes"][0]["symbol"]=="NVDA"' "$STATE_DIR/yahoo-smoke.json"; then
  pass "Yahoo quote route"
else
  fail "Yahoo quote route"
fi

if openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 60 -- \
  bash -lc 'python3 /sandbox/.hermes-data/skills/sec-company-facts/scripts/sec_company_facts.py facts NVDA' \
  >"$STATE_DIR/sec-smoke.json" \
  && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["ok"] and d["result"]["company"]["ticker"]=="NVDA" and d["result"]["metrics"]' "$STATE_DIR/sec-smoke.json"; then
  pass "SEC public-data routes"
else
  fail "SEC public-data routes"
fi

if openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 15 -- \
  bash -lc "python3 -c 'import urllib.request; urllib.request.urlopen(\"https://example.com\", timeout=5)'" \
  >/dev/null 2>&1; then
  fail "unapproved egress unexpectedly succeeded"
else
  pass "deny-by-default unapproved egress"
fi

echo "Native Hermes + Relay turn"
start_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 10 -- \
  touch /tmp/financial-assistant-verify-start
if HERMES_API_KEY=nemoclaw-internal HERMES_TIMEOUT=360 \
  python3 "$DIR/smoke-hermes-api.py" >"$STATE_DIR/hermes-smoke.json"; then
  pass "Hermes tool-using API turn"
else
    cat "$STATE_DIR/hermes-smoke.json" >&2 || true
    fail "Hermes tool-using API turn"
fi

atif_ready=0
for _ in $(seq 1 30); do
  if openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 15 -- \
    sh -c "find /sandbox/.hermes-data/atif -type f -name '*.json' -newer /tmp/financial-assistant-verify-start -print -quit | grep -q ."; then
    atif_ready=1
    break
  fi
  sleep 1
done
[[ "$atif_ready" == 1 ]] || fail "local ATIF export"
if openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 30 -- \
  /opt/hermes/.venv/bin/python -c \
  'import json,pathlib; fs=sorted(pathlib.Path("/sandbox/.hermes-data/atif").glob("*.json"),key=lambda p:p.stat().st_mtime); d=json.loads(fs[-1].read_text()); s=json.dumps(d).lower(); assert "terminal" in s and "tool_calls" in s,fs[-1]' \
  >/dev/null; then
  pass "ATIF contains terminal tool evidence"
else
  fail "ATIF terminal tool evidence"
fi

if python3 "$DIR/check-phoenix.py" \
  --project "$PHOENIX_PROJECT_NAME" --start-time "$start_time" \
  >"$STATE_DIR/phoenix-smoke.json"; then
  pass "Phoenix correlated OK LLM + terminal spans"
else
    cat "$STATE_DIR/phoenix-smoke.json" >&2 || true
    fail "Phoenix trace correlation"
fi

echo "Financial Assistant verification passed."

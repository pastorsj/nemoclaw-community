#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
mkdir -p "$TEST_ROOT/state" "$TEST_ROOT/runtime"
export HOME="$TEST_ROOT/home"
export NEMOCLAW_GATEWAY_PORT=18081
mkdir -p "$HOME/.nemoclaw/gateways/$NEMOCLAW_GATEWAY_PORT"
python3 - "$HOME/.nemoclaw/gateways/$NEMOCLAW_GATEWAY_PORT/sandboxes.json" <<'PY'
import json
from pathlib import Path
import sys
Path(sys.argv[1]).write_text(json.dumps({"sandboxes":{"query-claw":{
    "agent":"hermes",
    "nemoclawVersion":"0.0.120",
    "agentVersion":"0.20.6",
}}}), encoding="utf-8")
PY

export QUERY_CLAW_TEST_LOG="$TEST_ROOT/commands.log"
export QUERY_CLAW_TEST_STATE="$TEST_ROOT/state"
export QUERY_CLAW_RUNTIME_DIR="$TEST_ROOT/runtime"
export PATH="$EXAMPLE_DIR/tests/fixtures/bin:$PATH"
export NEMOCLAW_CLI=nemohermes
export NEMOCLAW_SANDBOX_NAME=query-claw
export MCP_TRUSTED_PRIVATE_HOST=query-claw.internal
export QUERY_CLAW_MCP_URL=https://query-claw.internal:9443/mcp/
export QUERY_CLAW_MCP_TOKEN=secret-query-claw-canary

common="$EXAMPLE_DIR/deploy/lib/common.sh"
host="$(bash -c 'source "$1"; chat_ui_host "$2"' _ "$common" \
  'https://query-claw.example.test/')"
[[ "$host" == query-claw.example.test ]] || fail "external dashboard host was not normalized"
forward_origin="$(bash -c 'source "$1"; chat_ui_forward_origin "$2" 18789' _ \
  "$common" 'https://QUERY-CLAW.example.test/')"
[[ "$forward_origin" == https://query-claw.example.test:18789 ]] || \
  fail "external dashboard origin did not preserve its host for the local forward"

skill_names=(
  query-claw query-claw-structured query-claw-documents
  query-claw-predictive query-claw-reporting
)
registration_names=(query-claw)

: >"$QUERY_CLAW_TEST_LOG"
bash "$EXAMPLE_DIR/scripts/setup.sh" >"$TEST_ROOT/setup.out"
log="$(<"$QUERY_CLAW_TEST_LOG")"
for name in "${skill_names[@]}"; do
  [[ "$log" == *"skill install $EXAMPLE_DIR/skills/$name"* ]] || \
    fail "$name was not installed through the native skill lifecycle"
  [[ -f "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || fail "$name fixture state is missing"
done
for name in "${registration_names[@]}"; do
  [[ "$log" == *"mcp add $name"* ]] || fail "$name was not added natively"
  [[ "$log" == *"--trusted-private-host query-claw.internal --no-probe"* ]] || \
    fail "$name did not use the private-host pin and bounded add"
  [[ -f "$QUERY_CLAW_TEST_STATE/$name" ]] || fail "$name fixture state is missing"
done
[[ -f "$QUERY_CLAW_TEST_STATE/profile-applied" ]] || fail "narrow Hermes profile was not applied"
[[ -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" ]] || \
  fail "Hermes profile restore receipt is missing"
runtime_mode="$(stat -c '%a' "$QUERY_CLAW_RUNTIME_DIR" 2>/dev/null || stat -f '%Lp' "$QUERY_CLAW_RUNTIME_DIR")"
receipt_mode="$(stat -c '%a' "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" 2>/dev/null || stat -f '%Lp' "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json")"
[[ "$runtime_mode" == 700 && "$receipt_mode" == 600 ]] || fail "profile receipt permissions differ"
setup_output="$(<"$TEST_ROOT/setup.out")"
for canary in secret-query-claw-canary; do
  [[ "$log$setup_output" != *"$canary"* ]] || fail "an MCP token leaked"
done
[[ "$(grep -c 'gateway restart --quiet' "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
  fail "fresh setup did not restart Hermes exactly once for its profile"
gateway_line="$(grep -n -m1 'gateway restart --quiet' "$QUERY_CLAW_TEST_LOG" | cut -d: -f1)"
probe_line="$(grep -n -m1 'mcp status query-claw --tools --json' "$QUERY_CLAW_TEST_LOG" | cut -d: -f1)"
(( gateway_line < probe_line )) || fail "live tools were checked before the profile reload"

# A retry updates skills and rotates the native credential with restart. It
# neither duplicates a registration nor restarts an unchanged raw profile.
bash "$EXAMPLE_DIR/scripts/setup.sh" >"$TEST_ROOT/repeat.out"
for name in "${registration_names[@]}"; do
  [[ "$(grep -c "mcp add $name" "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
    fail "repeat setup duplicated $name"
  [[ "$(grep -c "mcp restart $name" "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
    fail "repeat setup did not reconcile $name"
done
[[ "$(grep -c 'gateway restart --quiet' "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
  fail "repeat setup restarted an unchanged profile"

# Profile drift is detected before teardown removes a skill or registration.
remove_count="$(grep -c 'mcp remove ' "$QUERY_CLAW_TEST_LOG" || true)"
if QUERY_CLAW_TEST_PROFILE_DRIFT=1 bash "$EXAMPLE_DIR/scripts/teardown.sh" \
  >"$TEST_ROOT/profile-drift.out" 2>&1; then
  fail "teardown accepted Hermes profile drift"
fi
[[ "$(grep -c 'mcp remove ' "$QUERY_CLAW_TEST_LOG" || true)" == "$remove_count" ]] || \
  fail "profile drift caused a partial MCP teardown"
for name in "${skill_names[@]}"; do
  [[ -f "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || fail "profile drift removed $name"
done

if QUERY_CLAW_TEST_GATEWAY_FAIL=1 bash "$EXAMPLE_DIR/scripts/teardown.sh" \
  >"$TEST_ROOT/gateway-failure.out" 2>&1; then
  fail "teardown accepted a failed gateway reload"
fi
[[ -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" ]] || \
  fail "failed gateway reload discarded the profile receipt"

# The retry must reload the already-restored profile before it can discard the
# receipt; skills and registrations removed by the first attempt stay absent.
bash "$EXAMPLE_DIR/scripts/teardown.sh" >"$TEST_ROOT/teardown.out"
for name in "${registration_names[@]}"; do
  [[ ! -e "$QUERY_CLAW_TEST_STATE/$name" ]] || fail "$name remains after teardown"
  [[ "$(grep -c "mcp remove $name" "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
    fail "$name was not removed through NemoClaw"
done
for name in "${skill_names[@]}"; do
  [[ ! -e "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || fail "$name remains after teardown"
done
[[ ! -e "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" ]] || \
  fail "profile receipt remains after teardown"
[[ -e "$QUERY_CLAW_TEST_STATE/profile-restored" ]] || fail "prior profile was not restored"
bash "$EXAMPLE_DIR/scripts/teardown.sh" >/dev/null
for name in "${registration_names[@]}"; do
  [[ "$(grep -c "mcp remove $name" "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
    fail "idempotent teardown repeated $name removal"
done

# A same-name registration with a different definition fails in the complete
# preflight, before setup installs skills or changes any other native state.
: >"$QUERY_CLAW_TEST_LOG"
python3 - "$QUERY_CLAW_TEST_STATE/query-claw" <<'PY'
import json
from pathlib import Path
import sys
Path(sys.argv[1]).write_text(json.dumps({
    "url":"https://replacement.example.test/mcp",
    "env":"QUERY_CLAW_MCP_TOKEN",
    "private_host":"query-claw.internal",
}), encoding="utf-8")
PY
if bash "$EXAMPLE_DIR/scripts/setup.sh" >"$TEST_ROOT/conflict.out" 2>&1; then
  fail "setup adopted a conflicting native registration"
fi
log="$(<"$QUERY_CLAW_TEST_LOG")"
[[ "$log" != *"skill install"* && "$log" != *"mcp add"* && "$log" != *"mcp restart"* ]] || \
  fail "registration conflict mutated the sandbox"
rm -f "$QUERY_CLAW_TEST_STATE/query-claw"

# Failure after a fresh native add removes only registrations created by that
# run. Native skills remain safe and retryable; the raw profile is restored.
: >"$QUERY_CLAW_TEST_LOG"
if QUERY_CLAW_TEST_EXTRA_KUMO_TOOL=1 bash "$EXAMPLE_DIR/scripts/setup.sh" \
  >"$TEST_ROOT/extra-tool.out" 2>&1; then
  fail "setup accepted an out-of-contract Kumo inventory"
fi
for name in "${registration_names[@]}"; do
  [[ ! -e "$QUERY_CLAW_TEST_STATE/$name" ]] || fail "rollback left $name registered"
  grep -q "mcp remove $name --force" "$QUERY_CLAW_TEST_LOG" || \
    fail "rollback did not use native forced cleanup for $name"
done
[[ ! -e "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" ]] || \
  fail "fresh rollback retained a restored profile receipt"
for name in "${skill_names[@]}"; do
  [[ -f "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || fail "rollback unsafely removed $name"
done
bash "$EXAMPLE_DIR/scripts/teardown.sh" >/dev/null

if QUERY_CLAW_TEST_VERSION=v0.0.119 bash "$EXAMPLE_DIR/scripts/setup.sh" \
  >"$TEST_ROOT/version.out" 2>&1; then
  fail "setup accepted a different NemoClaw version"
fi

touch "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json"
if NEMOCLAW_CLI=query-claw-missing-cli bash "$EXAMPLE_DIR/scripts/teardown.sh" \
  >"$TEST_ROOT/missing-cli.out" 2>&1; then
  fail "teardown discarded a profile receipt without NemoClaw"
fi
[[ -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" ]] || \
  fail "missing NemoClaw discarded the profile receipt"
rm -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json"

printf 'PASS: Query Claw native lifecycle command contracts\n'

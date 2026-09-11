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
    "nemoclawVersion":"0.0.123",
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
export NEMO_RETRIEVER_API_TOKEN=secret-query-claw-canary
export QUERY_CLAW_GSF_MCP_URL=https://query-claw.internal:9444/mcp
export QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON='{"supply-chain":"query-claw-supply-chain"}'
export QUERY_CLAW_RETRIEVER_PROVENANCE_JSON='{"schema_version":1,"kind":"query-claw-nemo-retriever-provenance","service":{"api_version":"26.08.1"},"ingestion":{"document_parser":{"method":"pdfium"},"text_parser":{"method":"plain-text"},"text_chunker":{"method":"token","max_tokens":1024,"overlap_tokens":0,"tokenizer_model":"nvidia/llama-nemotron-embed-vl-1b-v2"},"embedding":{"model":"nvidia/nemotron-3-embed-1b"}},"retrieval":{"reranking_model":"nvidia/llama-3.2-nv-rerankqa-1b-v2"}}'
export QUERY_CLAW_PREDICTION_CONTEXT_JSON='{"scope":{"entity":"facilities.id"},"targets":["delay"]}'
export QUERY_CLAW_TEST_EXPECTED_PREDICTION_CONTEXT_JSON="$QUERY_CLAW_PREDICTION_CONTEXT_JSON"

common="$EXAMPLE_DIR/deploy/lib/common.sh"
host="$(bash -c 'source "$1"; chat_ui_host "$2"' _ "$common" \
  'https://query-claw.example.test/')"
[[ "$host" == query-claw.example.test ]] || fail "external dashboard host was not normalized"
forward_origin="$(bash -c 'source "$1"; chat_ui_forward_origin "$2" 18789' _ \
  "$common" 'https://QUERY-CLAW.example.test/')"
[[ "$forward_origin" == https://query-claw.example.test:18789 ]] || \
  fail "external dashboard origin did not preserve its host for the local forward"

skill_names=(
  query-claw query-claw-structured retriever-mcp
  query-claw-predictive
)
registration_names=(retriever)
retriever_deny_tools=(
  answer get_document get_job health list_job_documents pipeline_config
)

set_retriever_deny_tools() {
  python3 - "$QUERY_CLAW_TEST_STATE/retriever" \
    "$HOME/.nemoclaw/gateways/$NEMOCLAW_GATEWAY_PORT/sandboxes.json" "$@" <<'PY'
import json
from pathlib import Path
import sys
state_path,registry_path,*deny_tools=sys.argv[1:]
state_file=Path(state_path)
state=json.loads(state_file.read_text(encoding="utf-8"))
state["deny_tools"]=deny_tools
state_file.write_text(json.dumps(state), encoding="utf-8")
registry_file=Path(registry_path)
registry=json.loads(registry_file.read_text(encoding="utf-8"))
registry["sandboxes"]["query-claw"]["mcp"]["bridges"]["retriever"]["denyTools"]=deny_tools
registry_file.write_text(json.dumps(registry), encoding="utf-8")
PY
}

: >"$QUERY_CLAW_TEST_LOG"
bash "$EXAMPLE_DIR/hermes/setup.sh" >"$TEST_ROOT/setup.out" 2>&1 || {
  cat "$TEST_ROOT/setup.out" >&2
  fail "initial Hermes setup failed"
}
log="$(<"$QUERY_CLAW_TEST_LOG")"
for name in "${skill_names[@]}"; do
  [[ "$log" == *"skill install $EXAMPLE_DIR/skills/$name"* ]] || \
    fail "$name was not installed through the native skill lifecycle"
  [[ -f "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || fail "$name fixture state is missing"
done
for name in "${registration_names[@]}"; do
  [[ "$log" == *"mcp add $name"* ]] || fail "$name was not added natively"
  for tool in "${retriever_deny_tools[@]}"; do
    [[ "$log" == *"--deny-tool $tool"* ]] || \
      fail "$name did not deny native Retriever tool $tool"
  done
  [[ "$log" == *"--trusted-private-host query-claw.internal --no-probe"* ]] || \
    fail "$name did not use the private-host pin and bounded add"
  [[ -f "$QUERY_CLAW_TEST_STATE/$name" ]] || fail "$name fixture state is missing"
done
[[ -f "$QUERY_CLAW_TEST_STATE/profile-applied" ]] || fail "narrow Hermes profile was not applied"
[[ -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" ]] || \
  fail "Hermes profile restore receipt is missing"
cat >"$QUERY_CLAW_RUNTIME_DIR/query-claw-gsf-oauth-policy.applied.yaml" <<'EOF'
preset:
  name: query-claw-gsf-oauth
network_policies:
  query-claw-gsf-mcp:
    endpoints: []
EOF
cp "$QUERY_CLAW_RUNTIME_DIR/query-claw-gsf-oauth-policy.applied.yaml" \
  "$QUERY_CLAW_RUNTIME_DIR/query-claw-gsf-oauth-policy.yaml"
printf 'match\n' >"$QUERY_CLAW_TEST_STATE/gsf-policy-state"
runtime_mode="$(stat -c '%a' "$QUERY_CLAW_RUNTIME_DIR" 2>/dev/null || stat -f '%Lp' "$QUERY_CLAW_RUNTIME_DIR")"
receipt_mode="$(stat -c '%a' "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" 2>/dev/null || stat -f '%Lp' "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json")"
skill_receipt_mode="$(stat -c '%a' "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-skills.json" 2>/dev/null || stat -f '%Lp' "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-skills.json")"
[[ "$runtime_mode" == 700 && "$receipt_mode" == 600 && \
  "$skill_receipt_mode" == 600 ]] || fail "lifecycle receipt permissions differ"
setup_output="$(<"$TEST_ROOT/setup.out")"
[[ "$log$setup_output" != *"secret-query-claw-canary"* ]] || \
  fail "an MCP token leaked"
[[ "$(grep -c 'gateway restart --quiet' "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
  fail "fresh setup did not restart Hermes exactly once for its profile"
gateway_line="$(grep -n -m1 'gateway restart --quiet' "$QUERY_CLAW_TEST_LOG" | cut -d: -f1)"
probe_line="$(grep -n -m1 'mcp status retriever --tools --json' "$QUERY_CLAW_TEST_LOG" | cut -d: -f1)"
oauth_line="$(grep -n 'hermes mcp test gsf' "$QUERY_CLAW_TEST_LOG" | tail -n1 | cut -d: -f1 || true)"
oauth_line="${oauth_line:-0}"
skill_line="$(grep -n 'skill install ' "$QUERY_CLAW_TEST_LOG" | tail -n1 | cut -d: -f1)"
(( gateway_line < probe_line && gateway_line > oauth_line && gateway_line > skill_line )) || \
  fail "Hermes did not verify managed MCP after adopting its complete profile"

# Both reported and durable denied-tool intent are part of native registration
# ownership. A mismatch fails before setup mutates any state.
mutation_count="$(grep -Ec 'mcp (add|restart|remove)|skill (install|remove)|gateway restart' \
  "$QUERY_CLAW_TEST_LOG")"
set_retriever_deny_tools answer
if bash "$EXAMPLE_DIR/hermes/setup.sh" >"$TEST_ROOT/deny-drift-setup.out" 2>&1; then
  fail "setup accepted denied-tool drift"
fi
[[ "$(grep -Ec 'mcp (add|restart|remove)|skill (install|remove)|gateway restart' \
  "$QUERY_CLAW_TEST_LOG")" == "$mutation_count" ]] || \
  fail "denied-tool drift caused a partial setup"
set_retriever_deny_tools "${retriever_deny_tools[@]}"

# A retry preserves byte-identical owned skills and rotates the native
# credential. It does not duplicate a registration, but does replace the
# resident GSF transport after the deployment's planned endpoint restart.
bash "$EXAMPLE_DIR/hermes/setup.sh" >"$TEST_ROOT/repeat.out"
for name in "${registration_names[@]}"; do
  [[ "$(grep -c "mcp add $name" "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
    fail "repeat setup duplicated $name"
  [[ "$(grep -c "mcp restart $name" "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
    fail "repeat setup did not reconcile $name"
done
for name in "${skill_names[@]}"; do
  [[ "$(grep -Fxc "args=query-claw skill install $EXAMPLE_DIR/skills/$name " \
    "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
    fail "repeat setup reinstalled unchanged skill $name"
done
[[ "$(grep -c 'gateway restart --quiet' "$QUERY_CLAW_TEST_LOG")" == 2 ]] || \
  fail "repeat setup did not refresh the resident GSF transport exactly once"
repeat_gateway_line="$(grep -n 'gateway restart --quiet' "$QUERY_CLAW_TEST_LOG" | tail -n1 | cut -d: -f1)"
repeat_oauth_line="$(grep -n 'hermes mcp test gsf' "$QUERY_CLAW_TEST_LOG" | tail -n1 | cut -d: -f1 || true)"
repeat_oauth_line="${repeat_oauth_line:-0}"
(( repeat_gateway_line > repeat_oauth_line )) || \
  fail "repeat setup restarted Hermes before validating GSF OAuth"

# Dataset activation removes only exact-owned skills for disabled capabilities,
# and a later activation can install them again from the recipe.
QUERY_CLAW_ENABLE_RETRIEVER=0 QUERY_CLAW_ENABLE_PREDICTION=0 \
  bash "$EXAMPLE_DIR/hermes/setup.sh" >"$TEST_ROOT/reduced-capabilities.out"
for name in retriever-mcp query-claw-predictive; do
  [[ ! -e "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || \
    fail "disabled capability retained skill $name"
  [[ "$(python3 "$EXAMPLE_DIR/hermes/skills.py" receipt-get \
    "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-skills.json" "$name")" == unowned ]] || \
    fail "disabled capability retained ownership for $name"
done
for name in query-claw query-claw-structured; do
  [[ -f "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || \
    fail "reduced activation removed enabled skill $name"
done
bash "$EXAMPLE_DIR/hermes/setup.sh" >"$TEST_ROOT/restored-capabilities.out"
for name in "${skill_names[@]}"; do
  [[ -f "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || \
    fail "full activation did not restore skill $name"
done

# Teardown proves exact policy and registration ownership before removing any
# native state.
remove_count="$(grep -c 'mcp remove ' "$QUERY_CLAW_TEST_LOG" || true)"
printf 'drift\n' >"$QUERY_CLAW_TEST_STATE/gsf-policy-state"
if bash "$EXAMPLE_DIR/hermes/teardown.sh" >"$TEST_ROOT/policy-drift.out" 2>&1; then
  fail "teardown accepted GSF policy drift"
fi
[[ "$(grep -c 'mcp remove ' "$QUERY_CLAW_TEST_LOG" || true)" == "$remove_count" ]] || \
  fail "policy drift caused a partial MCP teardown"
for name in "${skill_names[@]}"; do
  [[ -f "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || fail "policy drift removed $name"
done
printf 'match\n' >"$QUERY_CLAW_TEST_STATE/gsf-policy-state"

# A changed owned skill blocks teardown before the profile, skills, or native
# registrations can be mutated.
saved_skill_hash="$(<"$QUERY_CLAW_TEST_STATE/skill-retriever-mcp")"
printf '%064d\n' 0 >"$QUERY_CLAW_TEST_STATE/skill-retriever-mcp"
if bash "$EXAMPLE_DIR/hermes/teardown.sh" >"$TEST_ROOT/skill-drift.out" 2>&1; then
  fail "teardown accepted Hermes skill drift"
fi
[[ "$(grep -c 'mcp remove ' "$QUERY_CLAW_TEST_LOG" || true)" == "$remove_count" ]] || \
  fail "skill drift caused a partial MCP teardown"
[[ -f "$QUERY_CLAW_TEST_STATE/profile-applied" ]] || \
  fail "skill drift restored the profile before failing"
[[ -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-skills.json" ]] || \
  fail "skill drift discarded the ownership receipt"
printf '%s\n' "$saved_skill_hash" >"$QUERY_CLAW_TEST_STATE/skill-retriever-mcp"

set_retriever_deny_tools answer
if bash "$EXAMPLE_DIR/hermes/teardown.sh" >"$TEST_ROOT/deny-drift-teardown.out" 2>&1; then
  fail "teardown accepted denied-tool drift"
fi
[[ "$(grep -c 'mcp remove ' "$QUERY_CLAW_TEST_LOG" || true)" == "$remove_count" ]] || \
  fail "denied-tool drift caused a partial MCP teardown"
set_retriever_deny_tools "${retriever_deny_tools[@]}"

python3 - "$QUERY_CLAW_TEST_STATE/retriever" <<'PY'
import json
from pathlib import Path
import sys
path = Path(sys.argv[1])
state = json.loads(path.read_text(encoding="utf-8"))
state["url"] = "https://replacement.example.test/mcp"
path.write_text(json.dumps(state), encoding="utf-8")
PY
if bash "$EXAMPLE_DIR/hermes/teardown.sh" \
  >"$TEST_ROOT/teardown-registration-conflict.out" 2>&1; then
  fail "teardown removed a conflicting native registration"
fi
[[ "$(grep -c 'mcp remove ' "$QUERY_CLAW_TEST_LOG" || true)" == "$remove_count" ]] || \
  fail "registration conflict caused a partial MCP teardown"
python3 - "$QUERY_CLAW_TEST_STATE/retriever" <<'PY'
import json
from pathlib import Path
import sys
path = Path(sys.argv[1])
state = json.loads(path.read_text(encoding="utf-8"))
state["url"] = "https://query-claw.internal:9443/mcp/"
path.write_text(json.dumps(state), encoding="utf-8")
PY

# Profile drift is detected before teardown removes a skill or registration.
if QUERY_CLAW_TEST_PROFILE_DRIFT=1 bash "$EXAMPLE_DIR/hermes/teardown.sh" \
  >"$TEST_ROOT/profile-drift.out" 2>&1; then
  fail "teardown accepted Hermes profile drift"
fi
[[ "$(grep -c 'mcp remove ' "$QUERY_CLAW_TEST_LOG" || true)" == "$remove_count" ]] || \
  fail "profile drift caused a partial MCP teardown"
for name in "${skill_names[@]}"; do
  [[ -f "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || fail "profile drift removed $name"
done
[[ -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-gsf-oauth-policy.applied.yaml" ]] || \
  fail "profile drift discarded the GSF policy receipt"

if QUERY_CLAW_TEST_GATEWAY_FAIL=1 bash "$EXAMPLE_DIR/hermes/teardown.sh" \
  >"$TEST_ROOT/gateway-failure.out" 2>&1; then
  fail "teardown accepted a failed gateway reload"
fi
[[ -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" ]] || \
  fail "failed gateway reload discarded the profile receipt"

# The retry must reload the already-restored profile before it can discard the
# receipt. The failed reload happens before native skills or registrations are
# removed, so teardown remains retryable without bypassing NemoClaw ownership.
bash "$EXAMPLE_DIR/hermes/teardown.sh" >"$TEST_ROOT/teardown.out"
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
[[ ! -e "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-skills.json" ]] || \
  fail "skill ownership receipt remains after teardown"
[[ ! -e "$QUERY_CLAW_RUNTIME_DIR/query-claw-gsf-oauth-policy.applied.yaml" ]] || \
  fail "GSF policy receipt remains after teardown"
[[ -e "$QUERY_CLAW_TEST_STATE/policy-removed" ]] || \
  fail "GSF OAuth policy was not removed"
[[ -e "$QUERY_CLAW_TEST_STATE/profile-restored" ]] || fail "prior profile was not restored"
bash "$EXAMPLE_DIR/hermes/teardown.sh" >/dev/null
for name in "${registration_names[@]}"; do
  [[ "$(grep -c "mcp remove $name" "$QUERY_CLAW_TEST_LOG")" == 1 ]] || \
    fail "idempotent teardown repeated $name removal"
done

# An unowned same-name skill is rejected even when its bytes happen to match.
# Setup must never infer ownership before mutating native state.
: >"$QUERY_CLAW_TEST_LOG"
python3 "$EXAMPLE_DIR/hermes/skills.py" hash \
  "$EXAMPLE_DIR/skills/retriever-mcp" \
  >"$QUERY_CLAW_TEST_STATE/skill-retriever-mcp"
if bash "$EXAMPLE_DIR/hermes/setup.sh" >"$TEST_ROOT/skill-collision.out" 2>&1; then
  fail "setup inferred ownership of an unowned same-name skill"
fi
log="$(<"$QUERY_CLAW_TEST_LOG")"
[[ "$log" != *"skill install"* && "$log" != *"skill remove"* && \
  "$log" != *"mcp add"* && "$log" != *"mcp remove"* && \
  "$log" != *"mcp restart"* && "$log" != *"gateway restart"* ]] || \
  fail "skill collision mutated the sandbox"
[[ -f "$QUERY_CLAW_TEST_STATE/skill-retriever-mcp" ]] || \
  fail "skill collision removed the unowned skill"
rm -f "$QUERY_CLAW_TEST_STATE/skill-retriever-mcp"

# A same-name registration with a different definition fails in the complete
# preflight, before setup installs skills or changes any other native state.
: >"$QUERY_CLAW_TEST_LOG"
python3 - "$QUERY_CLAW_TEST_STATE/retriever" <<'PY'
import json
from pathlib import Path
import sys
Path(sys.argv[1]).write_text(json.dumps({
    "url":"https://replacement.example.test/mcp",
    "env":"NEMO_RETRIEVER_API_TOKEN",
    "private_host":"query-claw.internal",
}), encoding="utf-8")
PY
if bash "$EXAMPLE_DIR/hermes/setup.sh" >"$TEST_ROOT/conflict.out" 2>&1; then
  fail "setup accepted a conflicting native registration"
fi
log="$(<"$QUERY_CLAW_TEST_LOG")"
[[ "$log" != *"skill install"* && "$log" != *"mcp add"* && "$log" != *"mcp restart"* ]] || \
  fail "registration conflict mutated the sandbox"
rm -f "$QUERY_CLAW_TEST_STATE/retriever"

# Failure during a fresh skill install removes only exact-byte skills installed
# earlier in that run, plus registrations created by the run.
: >"$QUERY_CLAW_TEST_LOG"
if QUERY_CLAW_TEST_SKILL_FAIL=retriever-mcp bash "$EXAMPLE_DIR/hermes/setup.sh" \
  >"$TEST_ROOT/skill-install-failure.out" 2>&1; then
  fail "setup accepted a failed skill installation"
fi
for name in "${registration_names[@]}"; do
  [[ ! -e "$QUERY_CLAW_TEST_STATE/$name" ]] || fail "rollback left $name registered"
  grep -q "mcp remove $name --force" "$QUERY_CLAW_TEST_LOG" || \
    fail "rollback did not use native forced cleanup for $name"
done
[[ ! -e "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" ]] || \
  fail "fresh rollback retained a restored profile receipt"
for name in "${skill_names[@]}"; do
  [[ ! -e "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || \
    fail "rollback retained newly installed skill $name"
done
[[ ! -e "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-skills.json" ]] || \
  fail "fresh rollback retained the skill ownership receipt"

# Automated GSF OAuth remains inside the same transaction as the profile,
# skills, and MCP registration. A failed login must leave none of them behind.
: >"$QUERY_CLAW_TEST_LOG"
if printf '%s\n' operator@example.test secret-password | \
  QUERY_CLAW_TEST_OAUTH_FAIL=1 \
  QUERY_CLAW_GSF_OAUTH_ORIGIN=https://gsf.example.test \
  QUERY_CLAW_GSF_OAUTH_SCRIPT='raise SystemExit(2)' \
  bash "$EXAMPLE_DIR/hermes/setup.sh" \
  >"$TEST_ROOT/oauth-failure.out" 2>&1; then
  fail "setup accepted a failed GSF OAuth login"
fi
for name in "${registration_names[@]}"; do
  [[ ! -e "$QUERY_CLAW_TEST_STATE/$name" ]] || \
    fail "OAuth rollback left $name registered"
done
for name in "${skill_names[@]}"; do
  [[ ! -e "$QUERY_CLAW_TEST_STATE/skill-$name" ]] || \
    fail "OAuth rollback retained newly installed skill $name"
done
[[ ! -e "$QUERY_CLAW_TEST_STATE/profile-applied" ]] || \
  fail "OAuth rollback retained the narrow Hermes profile"
[[ ! -e "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" ]] || \
  fail "OAuth rollback retained a restored profile receipt"
[[ ! -e "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-skills.json" ]] || \
  fail "OAuth rollback retained the skill ownership receipt"

if QUERY_CLAW_TEST_VERSION=v0.0.119 bash "$EXAMPLE_DIR/hermes/setup.sh" \
  >"$TEST_ROOT/version.out" 2>&1; then
  fail "setup accepted a different NemoClaw version"
fi

touch "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json"
python3 "$EXAMPLE_DIR/hermes/skills.py" receipt-set \
  "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-skills.json" query-claw \
  "$(python3 "$EXAMPLE_DIR/hermes/skills.py" hash \
    "$EXAMPLE_DIR/skills/query-claw")"
cat >"$QUERY_CLAW_RUNTIME_DIR/query-claw-gsf-oauth-policy.applied.yaml" <<'EOF'
preset:
  name: query-claw-gsf-oauth
network_policies:
  query-claw-gsf-mcp:
    endpoints: []
EOF
if NEMOCLAW_CLI=query-claw-missing-cli bash "$EXAMPLE_DIR/hermes/teardown.sh" \
  >"$TEST_ROOT/missing-cli.out" 2>&1; then
  fail "teardown discarded a profile receipt without NemoClaw"
fi
[[ -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json" ]] || \
  fail "missing NemoClaw discarded the profile receipt"
[[ -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-skills.json" ]] || \
  fail "missing NemoClaw discarded the skill ownership receipt"
[[ -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-gsf-oauth-policy.applied.yaml" ]] || \
  fail "missing NemoClaw discarded the policy receipt"
rm -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-profile.json"
rm -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-hermes-skills.json"
rm -f "$QUERY_CLAW_RUNTIME_DIR/query-claw-gsf-oauth-policy.applied.yaml"

printf 'PASS: Query Claw native lifecycle command contracts\n'

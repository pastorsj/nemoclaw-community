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
cleanup() {
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

common="$EXAMPLE_DIR/deploy/lib/common.sh"
runtime_env="$TEST_ROOT/runtime.env"
printf '%s\n' \
  'NEMOCLAW_SANDBOX_NAME=query-claw-v120' \
  'NEMOCLAW_GATEWAY_PORT=18081' \
  'NEMOCLAW_DASHBOARD_PORT=18801' \
  'NEMOCLAW_HERMES_API_PORT=8651' >"$runtime_env"
chmod 600 "$runtime_env"
loaded_runtime="$(
  env -u NEMOCLAW_SANDBOX_NAME -u NEMOCLAW_GATEWAY_PORT \
    -u NEMOCLAW_DASHBOARD_PORT -u NEMOCLAW_HERMES_API_PORT \
    QUERY_CLAW_DEPLOY_ENV="$runtime_env" bash -c '
    source "$1"
    load_deploy_env
    bash -c '\''printf "%s\n%s\n%s\n%s\n" \
      "$NEMOCLAW_SANDBOX_NAME" "$NEMOCLAW_GATEWAY_PORT" \
      "$NEMOCLAW_DASHBOARD_PORT" "$NEMOCLAW_HERMES_API_PORT"'\''
  ' _ "$common"
)"
[[ "$loaded_runtime" == $'query-claw-v120\n18081\n18801\n8651' ]] || \
  fail "deploy.env runtime identity was not exported to child processes"

defaults="$(env -u NVIDIA_BASE_URL -u LLM_MODEL -u NEMOCLAW_PROVIDER \
  bash -c '
    source "$1"
    NVIDIA_INFERENCE_API_KEY=test
    export_runtime_env
    printf "%s\n%s\n%s\n" \
      "$NEMOCLAW_ENDPOINT_URL" "$NEMOCLAW_MODEL" "$NEMOCLAW_PROVIDER"
  ' _ "$common")"
[[ "$defaults" == $'https://integrate.api.nvidia.com/v1\nnvidia/llama-3.3-nemotron-super-49b-v1.5\nbuild' ]] || \
  fail "Hermes inference defaults are incomplete"

custom="$(env -u LLM_MODEL -u NEMOCLAW_PROVIDER bash -c '
  source "$1"
  NVIDIA_BASE_URL=https://inference.example.test/v1
  NVIDIA_INFERENCE_API_KEY=test
  export_runtime_env
  printf "%s\n%s\n%s\n" \
    "$NEMOCLAW_ENDPOINT_URL" "$NEMOCLAW_MODEL" "$NEMOCLAW_PROVIDER"
' _ "$common")"
[[ "$custom" == $'https://inference.example.test/v1\nnvidia/llama-3.3-nemotron-super-49b-v1.5\ncustom' ]] || \
  fail "custom Hermes inference defaults are incomplete"

for ip in 10.0.0.1 10.255.255.254 172.16.0.1 172.31.255.254 \
  192.168.0.1 192.168.255.254; do
  bash -c 'source "$1"; validate_private_ipv4 "$2"' _ "$common" "$ip" || \
    fail "RFC1918 address was rejected: $ip"
done
for ip in 0.0.0.0 127.0.0.1 169.254.1.1 8.8.8.8 172.15.255.255 \
  172.32.0.1 ::1 2001:db8::1 not-an-address; do
  if bash -c 'source "$1"; validate_private_ipv4 "$2"' \
    _ "$common" "$ip" >"$TEST_ROOT/private-ip.out" 2>&1; then
    fail "non-RFC1918 address was accepted: $ip"
  fi
done

setup="$EXAMPLE_DIR/deploy/setup.sh"
setup_log="$TEST_ROOT/setup-failure.log"
if SETUP_LOG="$setup_log" bash -c '
  source "$1"
  require_command() { :; }
  initialize_deploy_env() {
    KUMO_RFM_API_URL=https://prediction.example.test/v1
    QUERY_CLAW_DATASETS=supply-chain
    QUERY_CLAW_PACKS_ROOT=/unused
  }
  compose() { printf "compose %s\n" "$*" >>"$SETUP_LOG"; }
  docker() { printf "%s\n" 2.24.4; }
  python3() {
    if [[ "$1" == - ]]; then
      command cat >/dev/null
      return 0
    fi
    printf "python %s\n" "$*" >>"$SETUP_LOG"
    [[ "$1" != */prepare_data_packs.py ]]
  }
  main
' _ "$setup" >"$TEST_ROOT/setup-failure.out" 2>&1; then
  fail "setup unexpectedly succeeded after data-pack activation failed"
fi
expected_setup_log="$(cat <<EOF
compose stop mcp-ingress query-claw-mcp
python $EXAMPLE_DIR/scripts/generate_data.py --validate
python $EXAMPLE_DIR/scripts/prepare_data_packs.py --datasets supply-chain --packs-root /unused
compose stop mcp-ingress query-claw-mcp
EOF
)"
[[ "$(cat "$setup_log")" == "$expected_setup_log" ]] || \
  fail "failed setup did not keep the ingress and facade down"

for url in https://prediction.example.test/v1 http://127.0.0.1:9000 http://localhost:9000; do
  bash -c 'source "$1"; validate_credentialed_service_url "$2" KUMO_RFM_API_URL' \
    _ "$common" "$url" || fail "safe Kumo URL was rejected: $url"
done
for url in http://prediction.example.test/v1 'https://user:secret@example.test/v1' \
  'https://prediction.example.test/v1?token=secret' 'https://prediction.example.test/v1#fragment'; do
  if bash -c 'source "$1"; validate_credentialed_service_url "$2" KUMO_RFM_API_URL' \
    _ "$common" "$url" >"$TEST_ROOT/kumo-url.out" 2>&1; then
    fail "unsafe Kumo URL was accepted"
  fi
done

if grep -q '^NEMOCLAW_VERSION=' "$EXAMPLE_DIR/.env.example"; then
  fail "the code-owned NemoClaw version leaked back into .env.example"
fi

registry="$TEST_ROOT/sandboxes.json"
python3 - "$registry" <<'PY'
import json
from pathlib import Path
import sys

Path(sys.argv[1]).write_text(json.dumps({
    "sandboxes": {
        "query-claw": {
            "agent": "hermes",
            "nemoclawVersion": "0.0.120",
            "agentVersion": "0.20.6",
            "openshellVersion": "0.0.106",
        }
    }
}), encoding="utf-8")
PY
setup_hermes="$EXAMPLE_DIR/deploy/setup-hermes.sh"
policy_contract="$(env -u NEMOCLAW_POLICY_TIER -u NEMOCLAW_WEB_SEARCH_PROVIDER \
  bash -c '
    source "$1"
    CHAT_UI_URL=
    configure_hermes_env
    printf "%s\n%s\n" "$NEMOCLAW_POLICY_TIER" "$NEMOCLAW_WEB_SEARCH_PROVIDER"
  ' _ "$setup_hermes")"
[[ "$policy_contract" == $'restricted\nnone' ]] || \
  fail "Hermes onboarding did not preserve the restricted policy contract"

default_registry="$(HOME="$TEST_ROOT/home" bash -c '
  source "$1"
  nemoclaw_registry_path
' _ "$setup_hermes")"
[[ "$default_registry" == "$TEST_ROOT/home/.nemoclaw/sandboxes.json" ]] || \
  fail "default gateway registry path was reported as $default_registry"
scoped_registry="$(HOME="$TEST_ROOT/home" NEMOCLAW_GATEWAY_PORT=18081 bash -c '
  source "$1"
  nemoclaw_registry_path
' _ "$setup_hermes")"
[[ "$scoped_registry" == "$TEST_ROOT/home/.nemoclaw/gateways/18081/sandboxes.json" ]] || \
  fail "non-default gateway registry path was reported as $scoped_registry"

for port in 8642 8652; do
  NEMOCLAW_HERMES_API_PORT="$port" NEMOCLAW_DASHBOARD_PORT=18789 \
    bash -c 'source "$1"; validate_hermes_ports' _ "$setup_hermes" ||
    fail "supported Hermes API port $port was rejected"
done

for port in 1024 65535; do
  NEMOCLAW_HERMES_API_PORT=8642 NEMOCLAW_DASHBOARD_PORT="$port" \
    bash -c 'source "$1"; validate_hermes_ports' _ "$setup_hermes" ||
    fail "supported dashboard port $port was rejected"
done

for port in 1023 8642 8652 65536 not-a-port; do
  if NEMOCLAW_HERMES_API_PORT=8643 NEMOCLAW_DASHBOARD_PORT="$port" \
    bash -c 'source "$1"; validate_hermes_ports' _ "$setup_hermes" \
      >"$TEST_ROOT/dashboard-port-$port.out" 2>&1; then
    fail "invalid dashboard port $port was accepted"
  fi
done

install_marker="$TEST_ROOT/invalid-port-installed-cli"
if NEMOCLAW_HERMES_API_PORT=8653 bash -c '
  source "$1"
  install_marker="$2"
  initialize_deploy_env() { :; }
  ensure_nemohermes_cli() { : >"$install_marker"; }
  main
' _ "$setup_hermes" "$install_marker" >"$TEST_ROOT/api-port.out" 2>&1; then
  fail "unsupported Hermes API port 8653 was accepted"
fi
[[ ! -e "$install_marker" ]] || \
  fail "unsupported Hermes API port reached CLI installation"
grep -q 'between 8642 and 8652' "$TEST_ROOT/api-port.out" || \
  fail "unsupported Hermes API port did not report its supported range"

install_marker="$TEST_ROOT/cli-installed"
bash -c '
  source "$1"
  install_marker="$2"
  nemohermes_is_available() { return 1; }
  install_nemohermes() { : >"$install_marker"; }
  nemohermes_installed_version() { printf "%s\n" "$NEMOCLAW_VERSION"; }
  nemohermes_source_commit() { printf "%s\n" "$NEMOCLAW_COMMIT"; }
  ensure_nemohermes_cli
' _ "$setup_hermes" "$install_marker"
[[ -f "$install_marker" ]] || fail "an absent nemohermes CLI was not installed"

rm -f "$install_marker"
if bash -c '
  source "$1"
  install_marker="$2"
  nemohermes_is_available() { return 0; }
  nemohermes_installed_version() { printf "%s\n" "v0.0.118"; }
  install_nemohermes() { : >"$install_marker"; }
  ensure_nemohermes_cli
' _ "$setup_hermes" "$install_marker" >"$TEST_ROOT/cli-mismatch.out" 2>&1; then
  fail "a mismatched existing nemohermes CLI was accepted"
fi
[[ ! -e "$install_marker" ]] || \
  fail "a mismatched existing nemohermes CLI was silently replaced"
grep -q 'required v0.0.120' "$TEST_ROOT/cli-mismatch.out" || \
  fail "CLI mismatch did not report the required version"
grep -q 'explicitly upgrade or downgrade' "$TEST_ROOT/cli-mismatch.out" || \
  fail "CLI mismatch did not provide an explicit operator action"

if bash -c '
  source "$1"
  nemohermes_is_available() { return 0; }
  nemohermes_installed_version() { printf "%s\n" "$NEMOCLAW_VERSION"; }
  nemohermes_source_commit() { printf "%s\n" bad-commit; }
  ensure_nemohermes_cli
' _ "$setup_hermes" >"$TEST_ROOT/commit-mismatch.out" 2>&1; then
  fail "a same-version CLI from a different commit was accepted"
fi
grep -q 'required commit 2444537f5a77c7b2789de4d59430e228328b8279' \
  "$TEST_ROOT/commit-mismatch.out" || fail "commit mismatch was not actionable"

state="$(bash -c 'source "$1"; sandbox_registry_state "$2" query-claw' \
  _ "$setup_hermes" "$registry")"
[[ "$state" == current ]] || fail "current sandbox release was reported as $state"

python3 - "$registry" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
state = json.loads(path.read_text(encoding="utf-8"))
sandbox = state["sandboxes"]["query-claw"]
sandbox["nemoclawVersion"] = "0.0.118"
path.write_text(json.dumps(state), encoding="utf-8")
PY
export QUERY_CLAW_TEST_REGISTRY="$registry"
if bash -c '
  source "$1"
  NEMOCLAW_SANDBOX_NAME=query-claw
  reconcile_sandbox_release "$QUERY_CLAW_TEST_REGISTRY"
' _ "$setup_hermes" >"$TEST_ROOT/reconcile.out" 2>&1; then
  fail "a drifted sandbox was rebuilt without operator authorization"
fi
grep -q 'choose an unused NEMOCLAW_SANDBOX_NAME' "$TEST_ROOT/reconcile.out" || \
  fail "drift refusal did not provide safe recovery guidance"

printf '{not-json\n' >"$registry"
if bash -c '
  source "$1"
  NEMOCLAW_SANDBOX_NAME=query-claw
  reconcile_sandbox_release "$2"
' _ "$setup_hermes" "$registry" >"$TEST_ROOT/invalid.out" 2>&1; then
  fail "an invalid NemoClaw registry was treated as an absent sandbox"
fi

grep -q '^readonly NEMOCLAW_VERSION=v0\.0\.120$' "$setup_hermes" || \
  fail "NemoClaw version is not a statically discoverable release literal"
grep -q '^readonly NEMOCLAW_COMMIT=2444537f5a77c7b2789de4d59430e228328b8279$' \
  "$setup_hermes" || fail "NemoClaw commit is not pinned for static analysis"
grep -q '^  export NEMOCLAW_INSTALL_TAG=v0\.0\.120$' "$setup_hermes" || \
  fail "installer tag is not a static version literal"
grep -q 'git -C "$HOME/\.nemoclaw/source" rev-parse HEAD' "$setup_hermes" || \
  fail "installed source commit is not checked operationally"

printf 'PASS: Query Claw deployment release contracts\n'

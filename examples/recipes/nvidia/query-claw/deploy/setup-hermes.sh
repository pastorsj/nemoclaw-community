#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"

readonly NEMOCLAW_VERSION=v0.0.120
readonly NEMOCLAW_COMMIT=2444537f5a77c7b2789de4d59430e228328b8279
readonly HERMES_AGENT_VERSION=0.20.6
readonly OPENSHELL_VERSION=0.0.106
CA_FILE="$RUNTIME_DIR/query-claw-mcp-root.crt"

validate_hermes_ports() {
  local dashboard_port="${NEMOCLAW_DASHBOARD_PORT:-18789}"
  local api_port="${NEMOCLAW_HERMES_API_PORT:-8642}"

  if [[ ! "$dashboard_port" =~ ^[0-9]{1,5}$ ]] ||
    ((10#$dashboard_port < 1024 || 10#$dashboard_port > 65535)); then
    die "NEMOCLAW_DASHBOARD_PORT must be an integer between 1024 and 65535"
  fi
  ((10#$dashboard_port < 8642 || 10#$dashboard_port > 8652)) ||
    die "NEMOCLAW_DASHBOARD_PORT must not use the reserved Hermes API port range 8642-8652"
  if [[ ! "$api_port" =~ ^[0-9]{1,5}$ ]] ||
    ((10#$api_port < 8642 || 10#$api_port > 8652)); then
    die "NEMOCLAW_HERMES_API_PORT must be an integer between 8642 and 8652"
  fi
}

nemoclaw_registry_path() {
  local gateway_port="${NEMOCLAW_GATEWAY_PORT:-8080}"

  if [[ ! "$gateway_port" =~ ^[0-9]{1,5}$ ]] ||
    ((10#$gateway_port < 1 || 10#$gateway_port > 65535)); then
    die "NEMOCLAW_GATEWAY_PORT must be an integer between 1 and 65535"
  fi
  if ((10#$gateway_port == 8080)); then
    printf '%s/.nemoclaw/sandboxes.json\n' "$HOME"
  else
    printf '%s/.nemoclaw/gateways/%s/sandboxes.json\n' "$HOME" "$((10#$gateway_port))"
  fi
}

expected_dashboard_url() {
  local dashboard_port="${NEMOCLAW_DASHBOARD_PORT:-18789}"
  if [[ -n "${CHAT_UI_URL:-}" ]]; then
    # NemoClaw 0.0.120 uses CHAT_UI_URL's port both for its managed browser
    # profile and the host-side loopback forward. Brev terminates public HTTPS
    # separately, so preserve the public hostname while selecting the local
    # dashboard forward port here.
    chat_ui_forward_origin "$CHAT_UI_URL" "$dashboard_port"
  else
    printf 'http://127.0.0.1:%s\n' "$dashboard_port"
  fi
}

configure_hermes_env() {
  export NVIDIA_INFERENCE_API_KEY NEMOCLAW_PROVIDER NEMOCLAW_ENDPOINT_URL
  export NEMOCLAW_MODEL COMPATIBLE_API_KEY
  export NEMOCLAW_NON_INTERACTIVE=1
  export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
  export NEMOCLAW_WEB_SEARCH_PROVIDER=none
  export NEMOCLAW_POLICY_TIER=restricted
  export NEMOCLAW_HERMES_API_PORT="${NEMOCLAW_HERMES_API_PORT:-8642}"
  export NEMOCLAW_CORPORATE_CA_BUNDLE="$CA_FILE"
  if [[ -n "${CHAT_UI_URL:-}" ]]; then
    CHAT_UI_URL="$(chat_ui_forward_origin \
      "$CHAT_UI_URL" "${NEMOCLAW_DASHBOARD_PORT:-18789}")"
    export CHAT_UI_URL
    unset NEMOCLAW_DASHBOARD_PORT
  else
    unset CHAT_UI_URL
    export NEMOCLAW_DASHBOARD_PORT="${NEMOCLAW_DASHBOARD_PORT:-18789}"
  fi
}

onboard_hermes() (
  local -a arguments=(
    --non-interactive --yes --name "$NEMOCLAW_SANDBOX_NAME"
    --yes-i-accept-third-party-software
  )
  configure_hermes_env
  exec nemohermes onboard "${arguments[@]}"
)

install_nemohermes() (
  configure_hermes_env
  export NEMOCLAW_SANDBOX_NAME
  export NEMOCLAW_INSTALL_REF=''
  export NEMOCLAW_INSTALL_TAG=v0.0.120
  export NEMOCLAW_AGENT=hermes
  curl -fsSL --proto '=https' --proto-redir '=https' \
    https://www.nvidia.com/nemoclaw.sh | bash
)

nemohermes_is_available() {
  command -v nemohermes >/dev/null 2>&1
}

nemohermes_installed_version() {
  local output installed
  output="$(nemohermes --version 2>/dev/null)" || return 1
  installed="$(sed -nE 's/.*v?([0-9]+\.[0-9]+\.[0-9]+).*/v\1/p' <<<"$output")"
  [[ -n "$installed" ]] || return 1
  printf '%s\n' "$installed"
}

nemohermes_version_matches() {
  [[ "$(nemohermes_installed_version)" == "$NEMOCLAW_VERSION" ]]
}

nemohermes_source_commit() {
  command -v git >/dev/null 2>&1 || return 1
  git -C "$HOME/.nemoclaw/source" rev-parse HEAD 2>/dev/null
}

nemohermes_commit_matches() {
  [[ "$(nemohermes_source_commit)" == "$NEMOCLAW_COMMIT" ]]
}

ensure_nemohermes_cli() {
  local installed
  if ! nemohermes_is_available; then
    install_nemohermes
    nemohermes_version_matches || \
      die "NemoClaw installer did not provide required nemohermes $NEMOCLAW_VERSION"
    nemohermes_commit_matches || \
      die "NemoClaw installer did not provide required source commit $NEMOCLAW_COMMIT"
    return
  fi

  installed="$(nemohermes_installed_version)" || \
    die "cannot determine the existing nemohermes version; Query Claw requires $NEMOCLAW_VERSION; explicitly upgrade, downgrade, or activate that exact CLI, then rerun setup"
  [[ "$installed" == "$NEMOCLAW_VERSION" ]] || \
    die "installed nemohermes $installed does not match Query Claw's required $NEMOCLAW_VERSION; explicitly upgrade or downgrade the shared CLI to $NEMOCLAW_VERSION, then rerun setup"
  nemohermes_commit_matches || \
    die "installed nemohermes source does not match Query Claw's required commit $NEMOCLAW_COMMIT; explicitly reinstall $NEMOCLAW_VERSION, then rerun setup"
}

sandbox_registry_state() {
  local state_path="$1" sandbox_name="$2"
  python3 - "$state_path" "$sandbox_name" \
    "${NEMOCLAW_VERSION#v}" "$HERMES_AGENT_VERSION" \
    "$OPENSHELL_VERSION" <<'PY'
import json
from pathlib import Path
import sys

state_path, sandbox_name, expected_nemoclaw, expected_agent, expected_openshell = sys.argv[1:]
path = Path(state_path)
if not path.exists():
    print("absent")
    raise SystemExit(0)

try:
    state = json.loads(path.read_text(encoding="utf-8"))
    sandboxes = state["sandboxes"]
except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
    print(f"invalid: cannot inspect NemoClaw registry: {exc}")
    raise SystemExit(0)

if not isinstance(sandboxes, dict):
    print("invalid: NemoClaw registry sandboxes value is not an object")
    raise SystemExit(0)
entry = sandboxes.get(sandbox_name)
if entry is None:
    print("absent")
    raise SystemExit(0)
if not isinstance(entry, dict):
    print(f"invalid: NemoClaw registry entry for {sandbox_name} is not an object")
    raise SystemExit(0)

def normalized(value):
    if not isinstance(value, str) or not value.strip():
        return "<missing>"
    return value.strip().removeprefix("v")

recorded_nemoclaw = entry.get("nemoclawVersion")
legacy_nemoclaw = entry.get("nemoclawsVersion")
if recorded_nemoclaw is None:
    recorded_nemoclaw = legacy_nemoclaw
nemoclaw = normalized(recorded_nemoclaw)
agent = normalized(entry.get("agentVersion"))
openshell = normalized(entry.get("openshellVersion"))
identity = entry.get("agent")
conflicting_alias = (
    entry.get("nemoclawVersion") is not None
    and legacy_nemoclaw is not None
    and normalized(entry["nemoclawVersion"]) != normalized(legacy_nemoclaw)
)
if (
    identity == "hermes"
    and not conflicting_alias
    and nemoclaw == expected_nemoclaw
    and agent == expected_agent
    and openshell == expected_openshell
):
    print("current")
else:
    print(
        "drifted: "
        f"agent={identity or '<missing>'}, "
        f"NemoClaw={nemoclaw}, Hermes={agent}, OpenShell={openshell}"
    )
PY
}

reconcile_sandbox_release() {
  local registry_path="$1" state
  state="$(sandbox_registry_state "$registry_path" "$NEMOCLAW_SANDBOX_NAME")"
  case "$state" in
    absent)
      onboard_hermes
      ;;
    current)
      ;;
    drifted:*)
      die "existing sandbox is not the required Query Claw release ($state); choose an unused NEMOCLAW_SANDBOX_NAME or explicitly rebuild it first"
      ;;
    invalid:*)
      die "$state"
      ;;
    *)
      die "unexpected NemoClaw registry inspection result"
      ;;
  esac

  state="$(sandbox_registry_state "$registry_path" "$NEMOCLAW_SANDBOX_NAME")"
  [[ "$state" == current ]] || \
    die "sandbox release contract remains unsatisfied after onboarding/rebuild: $state"
}

validate_sandbox_contract() {
  local registry_path="$1"
  python3 - "$registry_path" "$NEMOCLAW_SANDBOX_NAME" \
    "$CA_FILE" "$NEMOCLAW_ENDPOINT_URL" "$NEMOCLAW_MODEL" \
    "$(expected_dashboard_url)" "${NEMOCLAW_DASHBOARD_PORT:-18789}" \
    "${NEMOCLAW_HERMES_API_PORT:-8642}" <<'PY'
import base64
import json
from pathlib import Path
import sys

(
    state_path,
    sandbox_name,
    ca_path,
    endpoint,
    model,
    browser_url,
    dashboard_port,
    api_port,
) = sys.argv[1:]
try:
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    sandbox = state["sandboxes"][sandbox_name]
    encoded = sandbox["workload"]["encodedProfile"]
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    profile = json.loads(raw)
except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"cannot inspect the existing NemoClaw sandbox contract: {exc}")

expected_ca = base64.b64encode(Path(ca_path).read_bytes()).decode("ascii")
checks = {
    "dashboard port": (sandbox.get("dashboardPort"), int(dashboard_port)),
    "Hermes API port": (sandbox.get("hermesApiPort"), int(api_port)),
    "inference endpoint": (sandbox.get("endpointUrl"), endpoint),
    "model": (sandbox.get("model"), model),
    "dashboard browser URL": (
        profile.get("dashboard", {}).get("browserUrl", "").rstrip("/"),
        browser_url,
    ),
    "private MCP CA": (
        (sandbox.get("workload") or {}).get("corporateCaB64"),
        expected_ca,
    ),
}
errors = [name for name, (actual, expected) in checks.items() if actual != expected]
if errors:
    raise SystemExit(
        "existing sandbox does not match Query Claw onboarding: " + ", ".join(errors)
    )
PY
}

main() {
  local dashboard_host dashboard_code
  local registry_path
  initialize_deploy_env
  validate_hermes_ports
  registry_path="$(nemoclaw_registry_path)"
  CA_FILE="$RUNTIME_DIR/query-claw-mcp-root.crt"
  [[ -s "$CA_FILE" ]] || die "run setup-ingress.sh before setup-hermes.sh"

  ensure_nemohermes_cli

  reconcile_sandbox_release "$registry_path"
  validate_sandbox_contract "$registry_path"

  NEMOCLAW_SANDBOX_NAME="$NEMOCLAW_SANDBOX_NAME" \
    MCP_TRUSTED_PRIVATE_HOST="$QUERY_CLAW_PRIVATE_HOST" \
    QUERY_CLAW_MCP_URL="https://$QUERY_CLAW_PRIVATE_HOST:9443/mcp/" \
    QUERY_CLAW_MCP_TOKEN="$QUERY_CLAW_MCP_TOKEN" \
    bash "$EXAMPLE_DIR/scripts/setup.sh"
  if [[ -n "${CHAT_UI_URL:-}" ]]; then
    dashboard_host="$(chat_ui_host "$CHAT_UI_URL")"
    dashboard_code="$(curl --silent --output /dev/null --write-out '%{http_code}' \
      --max-time 5 --header "Host: $dashboard_host" \
      "http://127.0.0.1:${NEMOCLAW_DASHBOARD_PORT:-18789}/" 2>/dev/null || true)"
    [[ "$dashboard_code" == 200 ]] || \
      die "Hermes dashboard rejected configured host $dashboard_host (HTTP ${dashboard_code:-none})"
    printf 'ready: Hermes dashboard accepts %s\n' "$dashboard_host"
  fi
  printf 'ready: Hermes in the %s OpenShell sandbox\n' "$NEMOCLAW_SANDBOX_NAME"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

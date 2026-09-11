#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"

readonly NEMOCLAW_VERSION=v0.0.123
readonly NEMOCLAW_COMMIT=f75f722bb4a1ec9642c8df36c8924e24500d78f0
readonly HERMES_VERSION=0.20.6
readonly OPENSHELL_VERSION=0.0.106
CA_FILE="$RUNTIME_DIR/query-claw-mcp-root.crt"
GSF_POLICY_FILE="$RUNTIME_DIR/query-claw-gsf-oauth-policy.yaml"
GSF_POLICY_MARKER="$RUNTIME_DIR/query-claw-gsf-oauth-policy.applied.yaml"
NEMOCLAW_CLI="${NEMOCLAW_CLI:-nemohermes}"
# Consumed by functions sourced from deploy/lib/gsf-policy.sh.
# shellcheck disable=SC2034
PROFILE_SCRIPT="$(<"$EXAMPLE_DIR/hermes/profile.py")"
GSF_OAUTH_SCRIPT="$(<"$DEPLOY_DIR/lib/complete_gsf_oauth.py")"
# shellcheck source=lib/gsf-policy.sh
source "$DEPLOY_DIR/lib/gsf-policy.sh"
GSF_POLICY_ADDED=0
GSF_POLICY_MARKER_CREATED=0
GSF_POLICY_CANDIDATE=""

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

write_gsf_policy() {
  local destination="${1:-$GSF_POLICY_FILE}"
  cat >"$destination" <<EOF
preset:
  name: query-claw-gsf-oauth
  description: "Official GSF MCP and OAuth endpoints"
network_policies:
  query-claw-gsf-mcp:
    name: query-claw-gsf-mcp
    endpoints:
      - host: $QUERY_CLAW_PRIVATE_HOST
        port: 9444
        path: /mcp
        protocol: mcp
        enforcement: enforce
        mcp:
          max_body_bytes: 131072
          strict_tool_names: true
          allow_all_known_mcp_methods: false
        rules:
          - allow: { method: initialize }
          - allow: { method: notifications/initialized }
          - allow: { method: ping }
          - allow: { method: tools/list }
          - allow: { method: tools/call }
          - allow: { method: server/discover }
          - allow: { method: messages/listen }
          - allow: { method: notifications/cancelled }
          - allow: { method: notifications/progress }
          - allow: { method: notifications/roots/list_changed }
          - allow: { method: notifications/elicitation/complete }
    binaries:
      - { path: /usr/local/bin/hermes }
      - { path: /usr/bin/python3* }
      - { path: /opt/hermes/.venv/bin/python }
      - { path: /opt/hermes/.venv/bin/python3 }
  query-claw-gsf-oauth:
    name: query-claw-gsf-oauth
    endpoints:
      - host: $QUERY_CLAW_PRIVATE_HOST
        port: 9444
        protocol: rest
        enforcement: enforce
        rules:
          - allow: { method: GET, path: "/.well-known/**" }
          - allow: { method: GET, path: "/api/auth/mcp/**" }
          - allow: { method: POST, path: "/api/auth/mcp/**" }
          - allow: { method: DELETE, path: "/api/auth/mcp/**" }
    binaries:
      - { path: /usr/local/bin/hermes }
      - { path: /usr/bin/python3* }
      - { path: /opt/hermes/.venv/bin/python }
      - { path: /opt/hermes/.venv/bin/python3 }
  query-claw-gsf-admin-bootstrap:
    name: query-claw-gsf-admin-bootstrap
    endpoints:
      - host: $QUERY_CLAW_PRIVATE_HOST
        port: 9444
        protocol: rest
        enforcement: enforce
        rules:
          - allow: { method: POST, path: "/api/auth/sign-in/email" }
    binaries:
      - { path: /opt/hermes/.venv/bin/python3 }
EOF
  chmod 600 "$destination"
}

apply_gsf_policy() {
  local candidate state
  candidate="$(mktemp "$RUNTIME_DIR/.query-claw-gsf-policy.XXXXXX")"
  GSF_POLICY_CANDIDATE="$candidate"
  write_gsf_policy "$candidate"
  if [[ -e "$GSF_POLICY_MARKER" ]]; then
    [[ -s "$GSF_POLICY_MARKER" ]] || \
      die "Query Claw GSF policy receipt is empty; inspect it before retrying"
    gsf_policy_definitions_match "$candidate" "$GSF_POLICY_MARKER" || \
      die "Query Claw GSF policy definition changed; run teardown with the prior configuration before retrying"
    rm -f "$candidate"
    GSF_POLICY_CANDIDATE=""
    [[ -s "$GSF_POLICY_FILE" ]] || cp "$GSF_POLICY_MARKER" "$GSF_POLICY_FILE"
  else
    state="$(gsf_policy_state "$candidate")" || \
      die "could not inspect the existing GSF OAuth policy"
    [[ "$state" == absent ]] || \
      die "an unowned or drifted policy named query-claw-gsf-oauth already exists; remove or rename it explicitly"
    mv -f "$candidate" "$GSF_POLICY_FILE"
    GSF_POLICY_CANDIDATE=""
    cp "$GSF_POLICY_FILE" "$GSF_POLICY_MARKER"
    chmod 600 "$GSF_POLICY_MARKER"
    GSF_POLICY_MARKER_CREATED=1
  fi

  state="$(gsf_policy_state "$GSF_POLICY_MARKER")" || \
    die "could not inspect the existing GSF OAuth policy"
  case "$state" in
    match)
      return
      ;;
    absent) ;;
    drift)
      die "the owned query-claw-gsf-oauth policy has drifted; refusing to overwrite operator changes"
      ;;
    *)
      die "unexpected query-claw-gsf-oauth policy state: $state"
      ;;
  esac

  GSF_POLICY_ADDED=1
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" policy add \
    --from-file "$GSF_POLICY_MARKER" \
    --trusted-private-host "$QUERY_CLAW_PRIVATE_HOST" --yes
  [[ "$(gsf_policy_state "$GSF_POLICY_MARKER")" == match ]] || \
    die "query-claw-gsf-oauth did not match its receipt after apply"
}

rollback_gsf_policy() {
  (( GSF_POLICY_ADDED )) || return 0
  if ! gsf_policy_remove_exact; then
    printf 'warning: retained the Query Claw GSF policy receipt for manual recovery\n' >&2
    return 1
  fi
  if (( GSF_POLICY_MARKER_CREATED )); then
    rm -f "$GSF_POLICY_MARKER" "$GSF_POLICY_FILE"
  fi
  GSF_POLICY_ADDED=0
}

rollback_setup() {
  local status=$?
  trap - EXIT
  [[ -z "$GSF_POLICY_CANDIDATE" ]] || rm -f "$GSF_POLICY_CANDIDATE"
  if (( status != 0 )) && ! rollback_gsf_policy; then
    status=1
  fi
  exit "$status"
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
    # NemoClaw 0.0.123 uses CHAT_UI_URL's port both for its managed browser
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
  export NEMOCLAW_INSTALL_TAG=v0.0.123
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
    "${NEMOCLAW_VERSION#v}" "$HERMES_VERSION" \
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

nemoclaw = normalized(entry.get("nemoclawVersion"))
agent = normalized(entry.get("agentVersion"))
openshell = normalized(entry.get("openshellVersion"))
identity = entry.get("agent")
if (
    identity == "hermes"
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

run_native_setup() (
  # Use a subshell so credentials are inherited only as environment state by
  # the fixed setup script; never construct an `env KEY=value` argv that a
  # process listing could disclose.
  export NEMOCLAW_SANDBOX_NAME MCP_TRUSTED_PRIVATE_HOST
  export QUERY_CLAW_MCP_URL NEMO_RETRIEVER_API_TOKEN
  export QUERY_CLAW_GSF_MCP_URL QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON
  export QUERY_CLAW_RETRIEVER_PROVENANCE_JSON QUERY_CLAW_PREDICTION_CONTEXT_JSON
  export QUERY_CLAW_ENABLE_GSF QUERY_CLAW_ENABLE_PREDICTION
  export QUERY_CLAW_ENABLE_RETRIEVER QUERY_CLAW_RUNTIME_DIR
  if [[ "$QUERY_CLAW_HAS_STRUCTURED" == 1 ]]; then
    export QUERY_CLAW_GSF_OAUTH_ORIGIN QUERY_CLAW_GSF_OAUTH_SCRIPT
  else
    unset QUERY_CLAW_GSF_OAUTH_ORIGIN QUERY_CLAW_GSF_OAUTH_SCRIPT
  fi
  exec bash "$EXAMPLE_DIR/hermes/setup.sh"
)

main() {
  local dashboard_host dashboard_code prediction_context retriever_collections
  local registry_path
  initialize_deploy_env
  validate_hermes_ports
  trap rollback_setup EXIT
  registry_path="$(nemoclaw_registry_path)"
  CA_FILE="$RUNTIME_DIR/query-claw-mcp-root.crt"
  [[ -s "$CA_FILE" ]] || die "run setup-ingress.sh before setup-hermes.sh"

  ensure_nemohermes_cli

  reconcile_sandbox_release "$registry_path"
  validate_sandbox_contract "$registry_path"
  [[ "$QUERY_CLAW_HAS_STRUCTURED" != 1 ]] || apply_gsf_policy

  retriever_collections="$(python3 "$DEPLOY_DIR/configure_dataset.py" \
    "$QUERY_CLAW_ACTIVE_MANIFEST" field retriever-map)" || \
    die "could not read the active dataset's Retriever collection"
  prediction_context="$(python3 "$DEPLOY_DIR/configure_dataset.py" \
    "$QUERY_CLAW_ACTIVE_MANIFEST" field prediction-context)" || \
    die "could not read the active dataset's reviewed prediction context"

  MCP_TRUSTED_PRIVATE_HOST="$QUERY_CLAW_PRIVATE_HOST"
  QUERY_CLAW_MCP_URL="https://$QUERY_CLAW_PRIVATE_HOST:9443/mcp/"
  QUERY_CLAW_GSF_MCP_URL="$GSF_MCP_URL"
  QUERY_CLAW_RETRIEVER_COLLECTIONS_JSON="$retriever_collections"
  QUERY_CLAW_PREDICTION_CONTEXT_JSON="$prediction_context"
  QUERY_CLAW_ENABLE_GSF="$QUERY_CLAW_HAS_STRUCTURED"
  QUERY_CLAW_ENABLE_PREDICTION="$QUERY_CLAW_HAS_PREDICTION"
  QUERY_CLAW_ENABLE_RETRIEVER="$QUERY_CLAW_HAS_DOCUMENTS"
  QUERY_CLAW_RUNTIME_DIR="$RUNTIME_DIR"
  QUERY_CLAW_GSF_OAUTH_ORIGIN="$GSF_PUBLIC_URL"
  QUERY_CLAW_GSF_OAUTH_SCRIPT="$GSF_OAUTH_SCRIPT"
  if [[ "$QUERY_CLAW_HAS_DOCUMENTS" == 1 ]]; then
    [[ -s "$RUNTIME_DIR/retriever-provenance.json" ]] || \
      die "run setup-retriever.sh before setup-hermes.sh"
    QUERY_CLAW_RETRIEVER_PROVENANCE_JSON="$(<"$RUNTIME_DIR/retriever-provenance.json")"
  else
    QUERY_CLAW_RETRIEVER_PROVENANCE_JSON=""
  fi
  if [[ "$QUERY_CLAW_HAS_STRUCTURED" == 1 ]]; then
    # Supply GSF credentials only on stdin; hermes/setup.sh keeps OAuth inside
    # the same rollback boundary as the Hermes profile and managed skills.
    {
      printf '%s\n' "$GSF_ADMIN_EMAIL"
      printf '%s\n' "$GSF_ADMIN_PASSWORD"
    } | run_native_setup
  else
    run_native_setup
  fi
  if [[ -n "${CHAT_UI_URL:-}" ]]; then
    dashboard_host="$(chat_ui_host "$CHAT_UI_URL")"
    dashboard_code="$(curl --silent --output /dev/null --write-out '%{http_code}' \
      --max-time 5 --header "Host: $dashboard_host" \
      "http://127.0.0.1:${NEMOCLAW_DASHBOARD_PORT:-18789}/" 2>/dev/null || true)"
    [[ "$dashboard_code" == 200 ]] || \
      die "Hermes dashboard rejected configured host $dashboard_host (HTTP ${dashboard_code:-none})"
    printf 'ready: Hermes dashboard accepts %s\n' "$dashboard_host"
  fi
  [[ "$QUERY_CLAW_HAS_STRUCTURED" != 1 ]] || \
    printf 'configured: official GSF MCP in Hermes with private OAuth verified\n'
  printf 'ready: Hermes in the %s OpenShell sandbox\n' "$NEMOCLAW_SANDBOX_NAME"
  trap - EXIT
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

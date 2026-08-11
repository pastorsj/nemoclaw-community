# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC2034  # consumed by scripts that source this library
STATE_DIR="$EXAMPLE_DIR/.tmp"

# Read only this recipe's documented names from .env. Treating .env as data,
# rather than sourcing it as shell code, prevents command substitution or other
# executable syntax from running during lifecycle operations.
load_env() {
  local env_file="$EXAMPLE_DIR/.env" key value parsed
  [[ -f "$env_file" ]] || {
    echo "Missing $env_file — copy .env.example to .env and fill it in." >&2
    return 1
  }
  parsed="$(python3 - "$env_file" <<'PY'
import pathlib
import shlex
import sys

allowed = {
    "GITHUB_TOKEN",
    "NEMOCLAW_ENDPOINT_URL",
    "NEMOCLAW_DOCKER_CONTEXT",
    "NEMOCLAW_MODEL",
    "NEMOCLAW_SANDBOX_NAME",
    "NVIDIA_API_KEY",
    "OPENSHELL_GATEWAY",
    "OPENSHELL_GATEWAY_ENDPOINT",
    "PHOENIX_COLLECTOR_ENDPOINT",
    "PHOENIX_PROJECT_NAME",
    "SANDBOX_READY_TIMEOUT_SECS",
    "SEC_USER_AGENT",
}
for number, raw in enumerate(pathlib.Path(sys.argv[1]).read_text().splitlines(), 1):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit(f"invalid .env line {number}: expected NAME=value")
    key, raw_value = line.split("=", 1)
    key = key.strip()
    if key not in allowed:
        raise SystemExit(f"unsupported .env name on line {number}: {key}")
    try:
        tokens = shlex.split(raw_value, comments=True, posix=True)
    except ValueError as exc:
        raise SystemExit(f"invalid .env line {number}: {exc}") from exc
    value = " ".join(tokens)
    if "\t" in value or "\n" in value or "\r" in value:
        raise SystemExit(f"invalid control character on .env line {number}")
    print(f"{key}\t{value}")
PY
  )" || return 1
  while IFS=$'\t' read -r key value; do
    [[ -n "$key" ]] || continue
    if [[ -z "${!key+x}" ]]; then
      printf -v "$key" '%s' "$value"
      # shellcheck disable=SC2163  # $key intentionally names the variable
      export "$key"
    fi
  done <<<"$parsed"

  export NEMOCLAW_SANDBOX_NAME="${NEMOCLAW_SANDBOX_NAME:-financial-assistant}"
  export OPENSHELL_GATEWAY="${OPENSHELL_GATEWAY:-openshell}"
  export OPENSHELL_GATEWAY_ENDPOINT="${OPENSHELL_GATEWAY_ENDPOINT:-https://127.0.0.1:17670}"
  export NEMOCLAW_MODEL="${NEMOCLAW_MODEL:-nvidia/nemotron-3-ultra-550b-a55b}"
  export NEMOCLAW_ENDPOINT_URL="${NEMOCLAW_ENDPOINT_URL:-https://integrate.api.nvidia.com/v1}"
  export PHOENIX_PROJECT_NAME="${PHOENIX_PROJECT_NAME:-financial-assistant}"
  export PHOENIX_COLLECTOR_ENDPOINT="${PHOENIX_COLLECTOR_ENDPOINT:-http://host.openshell.internal:6006/v1/traces}"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    return 1
  }
}

require_runtime_config() {
  [[ -n "${NVIDIA_API_KEY:-}" ]] || {
    echo "NVIDIA_API_KEY is required in $EXAMPLE_DIR/.env" >&2
    return 1
  }
  [[ "${NEMOCLAW_ENDPOINT_URL%/}" == "https://integrate.api.nvidia.com/v1" ]] || {
    echo "This policy permits only https://integrate.api.nvidia.com/v1; got $NEMOCLAW_ENDPOINT_URL" >&2
    return 1
  }
  python3 - "${SEC_USER_AGENT:-}" "$NEMOCLAW_MODEL" \
    "$PHOENIX_PROJECT_NAME" "$PHOENIX_COLLECTOR_ENDPOINT" \
    "$NEMOCLAW_SANDBOX_NAME" <<'PY'
import re
import sys

value, model, project, collector, sandbox = sys.argv[1:]
if not (1 <= len(value) <= 200) or not value.isascii() or not value.isprintable():
    raise SystemExit("SEC_USER_AGENT must be 1-200 printable ASCII characters")
if not re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", value):
    raise SystemExit("SEC_USER_AGENT must identify the caller and include a contact email")
if len(value.split()) < 2:
    raise SystemExit("SEC_USER_AGENT must identify the caller and include a contact email")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
    raise SystemExit("NEMOCLAW_MODEL contains unsupported characters")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", project):
    raise SystemExit("PHOENIX_PROJECT_NAME contains unsupported characters")
if collector != "http://host.openshell.internal:6006/v1/traces":
    raise SystemExit(
        "PHOENIX_COLLECTOR_ENDPOINT must match the policy-approved local collector"
    )
if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", sandbox):
    raise SystemExit("NEMOCLAW_SANDBOX_NAME must be a lowercase slug")
PY
}

default_gateway_endpoint() {
  case "$OPENSHELL_GATEWAY" in
    openshell) echo "$OPENSHELL_GATEWAY_ENDPOINT" ;;
    snap-docker) echo "${OPENSHELL_GATEWAY_ENDPOINT:-http://127.0.0.1:17670}" ;;
    *) echo "$OPENSHELL_GATEWAY_ENDPOINT" ;;
  esac
}

sandbox_phase() {
  openshell sandbox list 2>/dev/null | awk -v wanted="$NEMOCLAW_SANDBOX_NAME" '
    { gsub(/\033\[[0-9;]*m/, "") }
    NR > 1 && $1 == wanted { print $NF; found = 1; exit }
    END { if (!found) print "Missing" }
  '
}

sandbox_healthy() {
  openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 15 -- \
    curl -fsS http://127.0.0.1:18642/health >/dev/null 2>&1
}

stop_api_forward() {
  openshell forward stop 8642 "$NEMOCLAW_SANDBOX_NAME" >/dev/null 2>&1 || true
  local pid_file="$STATE_DIR/api-forward.pid" pid="" command=""
  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
      command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
      if [[ "$command" == *"openshell forward start 127.0.0.1:8642"* \
        && "$command" == *"$NEMOCLAW_SANDBOX_NAME"* ]]; then
        kill -TERM -- -"$pid" >/dev/null 2>&1 \
          || kill -TERM "$pid" >/dev/null 2>&1 \
          || true
      fi
    fi
    rm -f "$pid_file"
  fi
}

start_api_forward() {
  stop_api_forward
  mkdir -p "$STATE_DIR"
  # OpenShell 0.0.85's --background child can inherit the caller's process
  # group on macOS and die when a non-interactive phase shell exits. Start the
  # blocking forward in its own session so the lifecycle contract is stable on
  # both Linux and the package-managed macOS gateway.
  python3 -c \
    'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
    openshell forward start 127.0.0.1:8642 "$NEMOCLAW_SANDBOX_NAME" \
    </dev/null >"$STATE_DIR/api-forward.log" 2>&1 &
  local forward_pid=$!
  printf '%s\n' "$forward_pid" >"$STATE_DIR/api-forward.pid"
  for _ in $(seq 1 30); do
    kill -0 "$forward_pid" 2>/dev/null || {
      cat "$STATE_DIR/api-forward.log" >&2 || true
      echo "OpenShell API forward exited during startup" >&2
      stop_api_forward
      return 1
    }
    if curl -fsS -H 'Authorization: Bearer nemoclaw-internal' \
      http://127.0.0.1:8642/health >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Hermes loopback forward did not become healthy" >&2
  stop_api_forward
  return 1
}

provider_type_matches() {
  local name="$1" expected="$2"
  openshell provider get "$name" 2>/dev/null \
    | sed $'s/\x1b\\[[0-9;]*m//g' \
    | grep -qE "^[[:space:]]*Type:[[:space:]]+${expected}[[:space:]]*$"
}

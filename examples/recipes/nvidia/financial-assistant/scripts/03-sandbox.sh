#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$DIR/_lib.sh"

recover_error=0
force_rebuild=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --recover-error) recover_error=1 ;;
    --rebuild) force_rebuild=1 ;;
    -h|--help)
      echo "Usage: $(basename "$0") [--rebuild] [--recover-error]"
      echo "  --rebuild        replace a healthy sandbox and rebuild with cached layers"
      echo "  --recover-error  replace an unhealthy or Error-state sandbox"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

load_env
require_runtime_config
require_command docker
require_command openshell

timeout="${SANDBOX_READY_TIMEOUT_SECS:-1200}"
if [[ ! "$timeout" =~ ^[0-9]+$ || ${#timeout} -gt 5 ]]; then
  echo "SANDBOX_READY_TIMEOUT_SECS must be an integer from 30 to 7200" >&2
  exit 1
fi
timeout=$((10#$timeout))
if ((timeout < 30 || timeout > 7200)); then
  echo "SANDBOX_READY_TIMEOUT_SECS must be an integer from 30 to 7200" >&2
  exit 1
fi

start_new_session() {
  if command -v setsid >/dev/null 2>&1; then
    exec setsid "$@"
  fi
  exec python3 -c \
    'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"
}

docker_command=(docker)
if [[ -n "${NEMOCLAW_DOCKER_CONTEXT:-}" ]]; then
  docker_command+=(--context "$NEMOCLAW_DOCKER_CONTEXT")
elif [[ "$(uname -s)" == "Darwin" ]] \
  && docker context inspect "colima-$OPENSHELL_GATEWAY" >/dev/null 2>&1; then
  docker_command+=(--context "colima-$OPENSHELL_GATEWAY")
fi
echo "Sandbox image engine: $("${docker_command[@]}" context show)"

build_id="$(python3 - "$EXAMPLE_DIR" "$NEMOCLAW_MODEL" \
  "$PHOENIX_PROJECT_NAME" "$PHOENIX_COLLECTOR_ENDPOINT" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for relative_root in ("agents/hermes", "certs", "scripts/lib", "skills"):
    for path in sorted((root / relative_root).rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or "tests" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
for value in sys.argv[2:]:
    digest.update(b"\0")
    digest.update(value.encode())
print(digest.hexdigest())
PY
)"
image="nemoclaw-financial-assistant:${build_id:0:16}"
staged="$EXAMPLE_DIR/.Dockerfile.staged"
rendered="$staged.rendered"
create_pid=""

cleanup_create_stream() {
  [[ -n "$create_pid" ]] || return 0
  kill -TERM -- -"$create_pid" >/dev/null 2>&1 || true
  wait "$create_pid" >/dev/null 2>&1 || true
  create_pid=""
}
# shellcheck disable=SC2329  # invoked by trap
cleanup() {
  cleanup_create_stream
  rm -f "$staged" "$rendered"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

phase="$(sandbox_phase)"
phase_lower="$(printf '%s' "$phase" | tr '[:upper:]' '[:lower:]')"
case "$phase_lower" in
  missing) ;;
  ready)
    installed_id="$(openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 10 -- \
      cat /etc/nemoclaw-financial-assistant-build-id 2>/dev/null || true)"
    if [[ "$force_rebuild" == 0 && "$installed_id" == "$build_id" ]] && sandbox_healthy; then
      echo "Reusing healthy sandbox $NEMOCLAW_SANDBOX_NAME (build ${build_id:0:16})"
      openshell policy set --policy "$EXAMPLE_DIR/policy.yaml" --wait "$NEMOCLAW_SANDBOX_NAME"
      start_api_forward
      exit 0
    fi
    if [[ "$installed_id" == "$build_id" && "$force_rebuild" == 0 && "$recover_error" == 0 ]]; then
      echo "Sandbox $NEMOCLAW_SANDBOX_NAME is Ready but its Hermes workload is unhealthy." >&2
      echo "Inspect its logs, or rerun with --recover-error." >&2
      exit 1
    fi
    echo "Replacing sandbox $NEMOCLAW_SANDBOX_NAME for build ${build_id:0:16}"
    stop_api_forward
    openshell sandbox delete "$NEMOCLAW_SANDBOX_NAME"
    ;;
  error)
    if [[ "$recover_error" == 0 ]]; then
      echo "Sandbox $NEMOCLAW_SANDBOX_NAME is in Error state; rerun with --recover-error." >&2
      exit 1
    fi
    stop_api_forward
    openshell sandbox delete "$NEMOCLAW_SANDBOX_NAME"
    ;;
  *)
    echo "Sandbox $NEMOCLAW_SANDBOX_NAME is in phase '$phase'; wait and rerun." >&2
    exit 1
    ;;
esac

for _ in $(seq 1 60); do
  [[ "$(sandbox_phase)" == "Missing" ]] && break
  sleep 1
done
[[ "$(sandbox_phase)" == "Missing" ]] || {
  echo "Sandbox deletion did not complete" >&2
  exit 1
}

cp "$EXAMPLE_DIR/agents/hermes/Dockerfile" "$staged"
sed \
  -e "s|^ARG NEMOCLAW_MODEL=.*|ARG NEMOCLAW_MODEL=$NEMOCLAW_MODEL|" \
  -e "s|^ARG PHOENIX_COLLECTOR_ENDPOINT=.*|ARG PHOENIX_COLLECTOR_ENDPOINT=$PHOENIX_COLLECTOR_ENDPOINT|" \
  -e "s|^ARG PHOENIX_PROJECT_NAME=.*|ARG PHOENIX_PROJECT_NAME=$PHOENIX_PROJECT_NAME|" \
  -e "s|^ARG NEMOCLAW_BUILD_ID=.*|ARG NEMOCLAW_BUILD_ID=$build_id|" \
  "$staged" >"$rendered"
mv "$rendered" "$staged"

echo "Building $image"
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  DOCKER_BUILDKIT=1 GITHUB_TOKEN="$GITHUB_TOKEN" "${docker_command[@]}" build \
    --secret id=github_token,env=GITHUB_TOKEN \
    --tag "$image" --file "$staged" "$EXAMPLE_DIR"
else
  DOCKER_BUILDKIT=1 "${docker_command[@]}" build \
    --tag "$image" --file "$staged" "$EXAMPLE_DIR"
fi

echo "Creating sandbox $NEMOCLAW_SANDBOX_NAME"
start_new_session openshell sandbox create \
  --from "$image" \
  --name "$NEMOCLAW_SANDBOX_NAME" \
  --policy "$EXAMPLE_DIR/policy.yaml" \
  --no-tty \
  --env "SEC_USER_AGENT=$SEC_USER_AGENT" \
  -- env nemoclaw-start </dev/null &
create_pid=$!

deadline=$(( $(date +%s) + timeout ))
ready=0
while [[ $(date +%s) -lt "$deadline" ]]; do
  if [[ "$(sandbox_phase)" == "Ready" ]]; then
    ready=1
    break
  fi
  if ! kill -0 "$create_pid" 2>/dev/null; then
    wait "$create_pid" || true
    create_pid=""
    echo "Sandbox creation exited before reaching Ready" >&2
    exit 1
  fi
  sleep 2
done

cleanup_create_stream
[[ "$ready" == 1 ]] || {
  echo "Sandbox did not reach Ready within ${timeout}s" >&2
  exit 1
}

openshell policy set --policy "$EXAMPLE_DIR/policy.yaml" --wait "$NEMOCLAW_SANDBOX_NAME"
echo "Waiting for the native Hermes gateway"
for _ in $(seq 1 180); do
  sandbox_healthy && {
    start_api_forward
    echo "Financial Assistant ready at http://127.0.0.1:8642/v1"
    exit 0
  }
  sleep 1
done

openshell sandbox exec --name "$NEMOCLAW_SANDBOX_NAME" --timeout 10 -- \
  tail -120 /tmp/gateway.log >&2 || true
echo "Hermes did not become healthy" >&2
exit 1

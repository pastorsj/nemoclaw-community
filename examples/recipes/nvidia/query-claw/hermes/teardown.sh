#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${QUERY_CLAW_RUNTIME_DIR:-$EXAMPLE_DIR/.runtime}"
NEMOCLAW_SANDBOX_NAME="${NEMOCLAW_SANDBOX_NAME:-query-claw}"
NEMOCLAW_CLI="${NEMOCLAW_CLI:-nemohermes}"
PROFILE_RECEIPT="$RUNTIME_DIR/query-claw-hermes-profile.json"
PROFILE_SCRIPT="$(<"$EXAMPLE_DIR/hermes/profile.py")"
SKILL_RECEIPT="$RUNTIME_DIR/query-claw-hermes-skills.json"
SKILL_SCRIPT="$(<"$EXAMPLE_DIR/hermes/skills.py")"
GSF_POLICY_FILE="$RUNTIME_DIR/query-claw-gsf-oauth-policy.yaml"
GSF_POLICY_MARKER="$RUNTIME_DIR/query-claw-gsf-oauth-policy.applied.yaml"
# shellcheck source=../deploy/lib/gsf-policy.sh
source "$EXAMPLE_DIR/deploy/lib/gsf-policy.sh"
registration_names=(retriever)
registration_url="${QUERY_CLAW_MCP_URL:-}"
registration_env=NEMO_RETRIEVER_API_TOKEN
registration_deny_tools=(
  answer
  get_document
  get_job
  health
  list_job_documents
  pipeline_config
)
managed_skill_names=(
  query-claw
  query-claw-structured
  query-claw-predictive
  retriever-mcp
)

export -n NEMO_RETRIEVER_API_TOKEN 2>/dev/null || true

if ! command -v "$NEMOCLAW_CLI" >/dev/null; then
  if [[ -f "$PROFILE_RECEIPT" || -f "$SKILL_RECEIPT" || \
    -f "$GSF_POLICY_MARKER" ]]; then
    printf 'error: %s is not installed; retained the Hermes profile, skill, and policy receipts\n' \
      "$NEMOCLAW_CLI" >&2
    exit 1
  fi
  printf 'Nothing removed: %s is not installed.\n' "$NEMOCLAW_CLI"
  exit 0
fi
if ! "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" status --json >/dev/null 2>&1; then
  if [[ -f "$PROFILE_RECEIPT" || -f "$SKILL_RECEIPT" || \
    -f "$GSF_POLICY_MARKER" ]]; then
    printf "error: sandbox '%s' is unavailable; retained its Hermes profile, skill, and policy receipts\n" \
      "$NEMOCLAW_SANDBOX_NAME" >&2
    exit 1
  fi
  printf "Nothing removed: sandbox '%s' is not available.\n" "$NEMOCLAW_SANDBOX_NAME"
  exit 0
fi
gateway_port="${NEMOCLAW_GATEWAY_PORT:-8080}"
registry_path="$HOME/.nemoclaw/sandboxes.json"
if [[ "$gateway_port" != 8080 ]]; then
  registry_path="$HOME/.nemoclaw/gateways/$gateway_port/sandboxes.json"
fi

if [[ -e "$GSF_POLICY_MARKER" ]]; then
  [[ -s "$GSF_POLICY_MARKER" ]] || {
    printf 'error: Query Claw GSF policy receipt is empty; refusing teardown\n' >&2
    exit 1
  }
  policy_state="$(gsf_policy_state "$GSF_POLICY_MARKER")" || {
    printf 'error: could not inspect the live Query Claw GSF policy\n' >&2
    exit 1
  }
  if [[ "$policy_state" == drift ]]; then
    printf 'error: Query Claw GSF policy drifted; refusing teardown\n' >&2
    exit 1
  fi
  [[ "$policy_state" == match || "$policy_state" == absent ]] || {
    printf 'error: unrecognized Query Claw GSF policy state: %s\n' \
      "$policy_state" >&2
    exit 1
  }
fi

encode_stdin() {
  python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())'
}

run_profile() {
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" exec --no-tty -- \
    python3 -c "$PROFILE_SCRIPT" "$@"
}

skill_live_hash() {
  "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" exec --no-tty -- \
    python3 -c "$SKILL_SCRIPT" state "$1"
}

skill_receipt_get() {
  python3 "$EXAMPLE_DIR/hermes/skills.py" receipt-get \
    "$SKILL_RECEIPT" "$1"
}

skill_receipt_delete() {
  python3 "$EXAMPLE_DIR/hermes/skills.py" receipt-delete \
    "$SKILL_RECEIPT" "$1"
}

classify_registration() {
  local name="$1" url="$2" token_env="$3" status
  status="$("$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" mcp status "$name" \
    --json --no-probe 2>/dev/null)" || return 2
  python3 -c \
    'import json, pathlib, sys
d=json.load(sys.stdin)
name,url,token_env,private_host,registry_path,sandbox,*expected_deny_tools=sys.argv[1:]
provider=d.get("provider") if isinstance(d.get("provider"),dict) else {}
policy=d.get("policy") if isinstance(d.get("policy"),dict) else {}
adapter=d.get("adapter") if isinstance(d.get("adapter"),dict) else {}
present=bool(d.get("url") or d.get("addState") or provider.get("registryPresent") or policy.get("registryPresent") or adapter.get("registered") is not None)
if not present:
    print("absent")
    raise SystemExit(0)
errors=[]
support=d.get("support") if isinstance(d.get("support"),dict) else {}
if d.get("server") not in (None,name): errors.append("server name differs")
if d.get("agent") != "hermes": errors.append("agent is not hermes")
if support.get("supported") is not True or support.get("mode") != "bridge" or support.get("adapter") != "hermes-config": errors.append("Hermes native bridge support differs")
if not url: errors.append("QUERY_CLAW_MCP_URL is required to prove ownership")
elif d.get("url") != url: errors.append("URL differs")
env=d.get("env") if isinstance(d.get("env"),dict) else {}
if env.get("names") != [token_env]: errors.append("credential environment differs")
target=d.get("trustedPrivateTarget")
if private_host:
    if not isinstance(target,dict) or target.get("host") != private_host: errors.append("trusted private host differs")
    elif not target.get("recordedPins"): errors.append("trusted private host has no recorded pins")
    elif target.get("state") != "match": errors.append("trusted private pins are " + str(target.get("state", "unknown")))
elif target is not None:
    errors.append("MCP_TRUSTED_PRIVATE_HOST is required to prove private-target ownership")
state=d.get("addState")
if state not in (None,"prepared","preflighted"): errors.append("add transaction state differs")
try:
    registry=json.loads(pathlib.Path(registry_path).read_text(encoding="utf-8"))
    bridge=registry["sandboxes"][sandbox]["mcp"]["bridges"][name]
except (OSError, KeyError, TypeError, json.JSONDecodeError):
    errors.append("durable denied-tool intent is unavailable")
else:
    if bridge.get("denyTools", []) != expected_deny_tools: errors.append("denied tools differ")
    if "pendingDenyTools" in bridge: errors.append("denied-tool update is incomplete")
if "denyTools" in d and d["denyTools"] != expected_deny_tools:
    errors.append("reported denied tools differ")
if errors:
    print("conflict: " + "; ".join(errors))
elif state:
    print("resumable")
else:
    print("current")' \
    "$name" "$url" "$token_env" "${MCP_TRUSTED_PRIVATE_HOST:-}" \
    "$registry_path" "$NEMOCLAW_SANDBOX_NAME" \
    "${registration_deny_tools[@]}" <<<"$status"
}

present_servers=()
for name in "${registration_names[@]}"; do
  registration_state="$(classify_registration "$name" \
    "$registration_url" "$registration_env")" || {
      printf "error: could not inspect native MCP registration '%s'\n" "$name" >&2
      exit 1
    }
  case "$registration_state" in
    absent) ;;
    current|resumable) present_servers+=("$name") ;;
    conflict:*)
      printf "error: native MCP registration '%s' is not exact-owned by Query Claw (%s); refusing teardown\n" \
        "$name" "${registration_state#conflict: }" >&2
      exit 1
      ;;
    *)
      printf "error: unrecognized native MCP state for '%s'\n" "$name" >&2
      exit 1
      ;;
  esac
done

# Refuse a partial teardown if any installed skill no longer matches the bytes
# Query Claw recorded when it installed that skill.
python3 "$EXAMPLE_DIR/hermes/skills.py" receipt-validate "$SKILL_RECEIPT"
owned_skill_names=()
owned_skill_hashes=()
for name in "${managed_skill_names[@]}"; do
  owned_hash="$(skill_receipt_get "$name")"
  [[ "$owned_hash" != unowned ]] || continue
  live_hash="$(skill_live_hash "$name")" || {
    printf "error: could not inspect Query Claw-owned Hermes skill '%s'\n" \
      "$name" >&2
    exit 1
  }
  if [[ "$live_hash" != absent && "$live_hash" != "$owned_hash" ]]; then
    printf "error: Query Claw-owned Hermes skill '%s' changed after installation; refusing teardown\n" \
      "$name" >&2
    exit 1
  fi
  owned_skill_names+=("$name")
  owned_skill_hashes+=("$owned_hash")
done

encoded_receipt=""
if [[ -f "$PROFILE_RECEIPT" ]]; then
  chmod 600 "$PROFILE_RECEIPT"
  encoded_receipt="$(encode_stdin <"$PROFILE_RECEIPT")"
  run_profile restore-check "$encoded_receipt" >/dev/null
fi

profile_restored=0
if [[ -n "$encoded_receipt" ]]; then
  restore_result="$(run_profile restore "$encoded_receipt")"
  if [[ "$restore_result" == restored || "$restore_result" == already-restored ]]; then
    profile_restored=1
    "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" gateway restart --quiet
  else
    printf 'error: Hermes profile helper returned an unexpected restore result\n' >&2
    exit 1
  fi
fi

removed_skills=()
for ((index = 0; index < ${#owned_skill_names[@]}; index += 1)); do
  name="${owned_skill_names[index]}"
  live_hash="$(skill_live_hash "$name")" || {
    printf "error: could not recheck Query Claw-owned Hermes skill '%s'\n" \
      "$name" >&2
    exit 1
  }
  if [[ "$live_hash" == "${owned_skill_hashes[index]}" ]]; then
    "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" skill remove "$name"
    removed_skills+=("$name")
  elif [[ "$live_hash" != absent ]]; then
    printf "error: Query Claw-owned Hermes skill '%s' changed during teardown; refusing to remove it\n" \
      "$name" >&2
    exit 1
  fi
  skill_receipt_delete "$name"
done

removed_servers=()
for name in "${present_servers[@]}"; do
  registration_state="$(classify_registration "$name" \
    "$registration_url" "$registration_env")" || {
      printf "error: could not recheck native MCP registration '%s'\n" "$name" >&2
      exit 1
    }
  case "$registration_state" in
    absent) ;;
    current|resumable)
      "$NEMOCLAW_CLI" "$NEMOCLAW_SANDBOX_NAME" mcp remove "$name"
      removed_servers+=("$name")
      ;;
    conflict:*)
      printf "error: native MCP registration '%s' changed during teardown (%s); refusing to remove it\n" \
        "$name" "${registration_state#conflict: }" >&2
      exit 1
      ;;
    *)
      printf "error: unrecognized native MCP state for '%s'\n" "$name" >&2
      exit 1
      ;;
  esac
done

if (( profile_restored )); then
  rm -f "$PROFILE_RECEIPT"
fi

if (( ${#removed_servers[@]} )); then
  printf 'Removed native MCP registrations from %s: %s.\n' \
    "$NEMOCLAW_SANDBOX_NAME" "${removed_servers[*]}"
elif (( ${#removed_skills[@]} || profile_restored )); then
  printf 'Removed the remaining Query Claw Hermes configuration from %s.\n' \
    "$NEMOCLAW_SANDBOX_NAME"
else
  printf 'No Query Claw native MCP registrations or profile receipt remained.\n'
fi
if [[ -f "$GSF_POLICY_MARKER" ]]; then
  gsf_policy_remove_exact
  rm -f "$GSF_POLICY_MARKER" "$GSF_POLICY_FILE"
  printf 'Removed the Query Claw GSF OAuth network policy.\n'
fi
printf 'The sandbox and generated local data were left in place.\n'

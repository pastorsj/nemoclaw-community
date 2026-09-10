#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
RUNTIME_DIR="$EXAMPLE_DIR/.runtime"
DEPLOY_ENV="${QUERY_CLAW_DEPLOY_ENV:-$RUNTIME_DIR/deploy.env}"
DATA_DIR="$RUNTIME_DIR/active-data"

# NemoClaw installs its launcher here on Linux. Brev's non-login execution
# shells do not source the profile update written by the installer.
export PATH="$HOME/.local/bin:$PATH"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_var() {
  [[ -n "${!1:-}" ]] || die "set $1 in $DEPLOY_ENV"
}

chat_ui_url_part() {
  local url="${1:-}"
  local part="${2:-host}"
  local override_port="${3:-}"
  [[ -n "$url" ]] || return 1
  python3 - "$url" "$part" "$override_port" <<'PY'
import ipaddress
import sys
from urllib.parse import urlparse

raw = sys.argv[1]
part = sys.argv[2]
override_port = sys.argv[3]
try:
    parsed = urlparse(raw)
    host = parsed.hostname
    port = parsed.port
except ValueError as exc:
    raise SystemExit(f"invalid CHAT_UI_URL: {exc}") from exc

if (
    parsed.scheme.lower() not in {"http", "https"}
    or not parsed.netloc
    or not host
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
    or parsed.path not in {"", "/"}
):
    raise SystemExit("CHAT_UI_URL must be an HTTP(S) origin without credentials, query, or path")

normalized = host.lower().rstrip(".")
try:
    address = ipaddress.ip_address(normalized)
except ValueError:
    loopback = normalized == "localhost"
else:
    loopback = address.is_loopback
    if address.is_unspecified:
        raise SystemExit("CHAT_UI_URL cannot use an unspecified address")

if not loopback and parsed.scheme.lower() != "https":
    raise SystemExit("a non-loopback CHAT_UI_URL must use HTTPS")
if port is not None and not 1024 <= port <= 65535:
    raise SystemExit("CHAT_UI_URL port must be between 1024 and 65535")
if part == "host":
    print(normalized)
elif part == "forward-origin":
    try:
        forward_port = int(override_port)
    except ValueError as exc:
        raise SystemExit("dashboard forward port must be an integer") from exc
    if not 1024 <= forward_port <= 65535:
        raise SystemExit("dashboard forward port must be between 1024 and 65535")
    bracketed = f"[{normalized}]" if ":" in normalized else normalized
    print(f"{parsed.scheme.lower()}://{bracketed}:{forward_port}")
else:
    raise SystemExit("unsupported CHAT_UI_URL part")
PY
}

chat_ui_host() {
  chat_ui_url_part "$1" host
}

chat_ui_forward_origin() {
  chat_ui_url_part "$1" forward-origin "$2"
}

validate_credentialed_service_url() {
  local url="${1:-}" label="${2:-service URL}"
  python3 - "$url" "$label" <<'PY'
import ipaddress
import sys
from urllib.parse import urlparse

raw, label = sys.argv[1:]
try:
    parsed = urlparse(raw)
    host = parsed.hostname
    parsed.port
except ValueError as exc:
    raise SystemExit(f"{label} is invalid") from exc
if (
    not host
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
):
    raise SystemExit(f"{label} is invalid")
normalized = host.lower().rstrip(".")
try:
    address = ipaddress.ip_address(normalized)
except ValueError:
    loopback = normalized == "localhost"
else:
    loopback = address.is_loopback
    if address.is_unspecified:
        raise SystemExit(f"{label} is invalid")
if parsed.scheme.lower() != "https" and not (
    parsed.scheme.lower() == "http" and loopback
):
    raise SystemExit(f"{label} must use HTTPS or loopback HTTP")
PY
}

private_ipv4() {
  hostname -I 2>/dev/null | tr ' ' '\n' | awk '
    /^10\./ || /^192\.168\./ {print; exit}
    /^172\./ {
      split($0, octet, ".")
      if (octet[2] >= 16 && octet[2] <= 31) {print; exit}
    }'
}

validate_private_ipv4() {
  python3 - "$1" <<'PY'
import ipaddress
import sys

try:
    address = ipaddress.IPv4Address(sys.argv[1])
except ipaddress.AddressValueError as exc:
    raise SystemExit("QUERY_CLAW_PRIVATE_IP must be an RFC1918 IPv4 address") from exc

networks = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
if not any(address in network for network in networks):
    raise SystemExit("QUERY_CLAW_PRIVATE_IP must be an RFC1918 IPv4 address")
PY
}

private_hostname() {
  getent hosts "$1" 2>/dev/null | awk -v address="$1" '
    $1 == address && NF >= 2 {print $2; exit}'
}

append_default() {
  local key="$1" value="$2"
  python3 - "$DEPLOY_ENV" "$key" "$value" <<'PY'
import os
from pathlib import Path
import re
import sys
import tempfile

path = Path(sys.argv[1])
key = sys.argv[2]
default = sys.argv[3]
if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or "\n" in default:
    raise SystemExit("invalid deployment environment assignment")
lines = path.read_text(encoding="utf-8").splitlines()
indices = [index for index, line in enumerate(lines) if line.startswith(key + "=")]
values = {lines[index].split("=", 1)[1] for index in indices if lines[index].split("=", 1)[1]}
if len(values) > 1:
    raise SystemExit(f"conflicting duplicate values for {key}; keep exactly one assignment")
chosen = next(iter(values), default)
replacement = f"{key}={chosen}"
if indices:
    first = indices[0]
    lines = [
        replacement if index == first else line
        for index, line in enumerate(lines)
        if index == first or index not in indices
    ]
else:
    lines.append(replacement)
mode = path.stat().st_mode & 0o777
with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
    handle.write("\n".join(lines) + "\n")
    temporary = handle.name
os.chmod(temporary, mode)
os.replace(temporary, path)
PY
}

append_secret() {
  append_default "$1" "$(openssl rand -hex 32)"
}

remove_derived_env() {
  python3 - "$DEPLOY_ENV" "$@" <<'PY'
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
keys = set(sys.argv[2:])
lines = [
    line
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.split("=", 1)[0] not in keys
]
mode = path.stat().st_mode & 0o777
with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
    handle.write("\n".join(lines) + "\n")
    temporary = handle.name
os.chmod(temporary, mode)
os.replace(temporary, path)
PY
  unset QUERY_CLAW_DEPLOY_DIR QUERY_CLAW_DATA_DIR QUERY_CLAW_ACTIVE_MANIFEST \
    QUERY_CLAW_STRUCTURED_DIR DEFAULT_MODELS_API_KEY \
    DEFAULT_MODELS_ENDPOINT DEFAULT_MODELS_MODEL EMBED_API_KEY EMBED_ENDPOINT \
    EMBED_MODEL CONNECTION_STRINGS NEMOCLAW_ENDPOINT_URL NEMOCLAW_MODEL \
    COMPATIBLE_API_KEY NEMOCLAW_VERSION ONTOLOGY_MCP_TOKEN \
    NEMO_RETRIEVER_MCP_TOKEN KUMO_MCP_TOKEN 2>/dev/null || true
}

export_runtime_env() {
  local inference_base="${NVIDIA_BASE_URL:-https://integrate.api.nvidia.com/v1}"
  local inference_model="${LLM_MODEL:-nvidia/llama-3.3-nemotron-super-49b-v1.5}"
  export QUERY_CLAW_DEPLOY_DIR="$DEPLOY_DIR"
  export QUERY_CLAW_DATA_DIR="$DATA_DIR"
  export QUERY_CLAW_ACTIVE_MANIFEST="$DATA_DIR/active-data-packs.json"
  export QUERY_CLAW_DATASETS="${QUERY_CLAW_DATASETS:-supply-chain}"
  export QUERY_CLAW_PACKS_ROOT="${QUERY_CLAW_PACKS_ROOT:-$RUNTIME_DIR/data-packs}"
  export QUERY_CLAW_STRUCTURED_DIR="$DATA_DIR/packs/supply-chain/structured"
  export QUERY_CLAW_DEPLOY_ENV="$DEPLOY_ENV"
  export NEMO_RETRIEVER_SOURCE_DIR="${NEMO_RETRIEVER_SOURCE_DIR:-$RUNTIME_DIR/sources/nemo-retriever}"
  export DEFAULT_MODELS_API_KEY="${NVIDIA_INFERENCE_API_KEY:-}"
  export DEFAULT_MODELS_ENDPOINT="$inference_base"
  export DEFAULT_MODELS_MODEL="${ONTOLOGY_MODEL:-$inference_model}"
  NVIDIA_EMBED_INVOKE_URL="${NVIDIA_EMBED_INVOKE_URL:-${inference_base%/}/embeddings}"
  NVIDIA_EMBED_MODEL="${NVIDIA_EMBED_MODEL:-nvidia/llama-nemotron-embed-vl-1b-v2}"
  export NVIDIA_EMBED_INVOKE_URL NVIDIA_EMBED_MODEL
  export EMBED_API_KEY="${NVIDIA_INFERENCE_API_KEY:-}"
  # GSF passes its endpoint to an OpenAI-compatible client that appends
  # `/embeddings`; Retriever requires the complete invoke URL instead.
  export EMBED_ENDPOINT="$inference_base"
  export EMBED_MODEL="$NVIDIA_EMBED_MODEL"
  export CONNECTION_STRINGS="postgresql://query_claw_reader:${GSF_QUERY_PASSWORD:-}@postgres:5432/query_claw"
  if [[ -z "${NEMOCLAW_PROVIDER:-}" ]]; then
    if [[ "$inference_base" == "https://integrate.api.nvidia.com/v1" ]]; then
      NEMOCLAW_PROVIDER=build
    else
      NEMOCLAW_PROVIDER=custom
    fi
  fi
  export NEMOCLAW_PROVIDER
  local runtime_name
  for runtime_name in NEMOCLAW_SANDBOX_NAME NEMOCLAW_GATEWAY_PORT \
    NEMOCLAW_DASHBOARD_PORT NEMOCLAW_HERMES_API_PORT; do
    if [[ -n "${!runtime_name:-}" ]]; then
      export "$runtime_name"
    fi
  done
  export NEMOCLAW_ENDPOINT_URL="$inference_base"
  export NEMOCLAW_MODEL="$inference_model"
  export COMPATIBLE_API_KEY="${NVIDIA_INFERENCE_API_KEY:-}"
}

load_deploy_env() {
  [[ -f "$DEPLOY_ENV" ]] || die "deployment environment not found: $DEPLOY_ENV"
  local mode
  mode="$(stat -c '%a' "$DEPLOY_ENV" 2>/dev/null || stat -f '%Lp' "$DEPLOY_ENV")"
  [[ "$mode" == "600" ]] || die "$DEPLOY_ENV must have mode 0600 (found $mode)"
  # shellcheck disable=SC1090
  source "$DEPLOY_ENV"
  export_runtime_env
}

initialize_deploy_env() {
  require_command getent
  require_command openssl
  require_command python3
  (umask 077 && mkdir -p "$RUNTIME_DIR")
  chmod 700 "$RUNTIME_DIR"
  if [[ ! -f "$DEPLOY_ENV" ]]; then
    (umask 077 && cp "$EXAMPLE_DIR/.env.example" "$DEPLOY_ENV")
  fi
  chmod 600 "$DEPLOY_ENV"
  load_deploy_env
  # These values are derived from the current path, credentials, and selected
  # endpoint on every run. Remove values written by older recipe revisions so
  # a credential rotation or checkout move cannot leave split configuration;
  # NEMOCLAW_VERSION is also retired because setup-hermes.sh owns that pin.
  remove_derived_env QUERY_CLAW_DEPLOY_DIR QUERY_CLAW_DATA_DIR \
    QUERY_CLAW_ACTIVE_MANIFEST QUERY_CLAW_STRUCTURED_DIR \
    DEFAULT_MODELS_API_KEY DEFAULT_MODELS_ENDPOINT DEFAULT_MODELS_MODEL \
    EMBED_API_KEY EMBED_ENDPOINT EMBED_MODEL CONNECTION_STRINGS \
    NEMOCLAW_ENDPOINT_URL NEMOCLAW_MODEL COMPATIBLE_API_KEY NEMOCLAW_VERSION \
    ONTOLOGY_MCP_TOKEN NEMO_RETRIEVER_MCP_TOKEN KUMO_MCP_TOKEN

  local actual_gsf_revision ip private_host resolved_addresses
  ip="${QUERY_CLAW_PRIVATE_IP:-$(private_ipv4)}"
  [[ -n "$ip" ]] || die "could not discover an RFC1918 host address"
  validate_private_ipv4 "$ip" || \
    die "invalid QUERY_CLAW_PRIVATE_IP in $DEPLOY_ENV"
  private_host="${QUERY_CLAW_PRIVATE_HOST:-$(private_hostname "$ip")}"
  [[ -n "$private_host" ]] || die "could not discover a DNS name for $ip"
  append_default QUERY_CLAW_PRIVATE_IP "$ip"
  append_default QUERY_CLAW_PRIVATE_HOST "$private_host"
  append_default QUERY_CLAW_DATASETS supply-chain
  append_default QUERY_CLAW_PACKS_ROOT ''
  append_default POSTGRES_USER postgres
  append_default POSTGRES_PORT 5432
  append_default POSTGRES_DATABASE gsf
  append_default NEO4J_USERNAME neo4j
  append_default PGADMIN_EMAIL admin@example.com
  append_default GSF_ADMIN_EMAIL admin@example.com
  append_default APP_URL http://127.0.0.1:3000
  append_default CHAT_UI_URL ''
  append_default PYTHON_API_URL http://gsf:3001
  append_default INGESTION_SERVICE_URL http://ingestion-service:3002
  append_secret POSTGRES_PASSWORD
  append_secret GSF_QUERY_PASSWORD
  append_secret NEO4J_PASSWORD
  append_secret PGADMIN_PASSWORD
  append_secret GSF_ADMIN_PASSWORD
  append_secret AUTH_SECRET
  append_secret QUERY_CLAW_MCP_TOKEN
  append_secret NEMO_RETRIEVER_API_TOKEN
  append_secret NRL_INTERNAL_VDB_TOKEN
  load_deploy_env

  require_var GSF_SOURCE_DIR
  require_var GSF_SOURCE_REVISION
  require_var NVIDIA_INFERENCE_API_KEY
  if [[ -n "${KUMO_RFM_API_URL:-}" ]]; then
    validate_credentialed_service_url "$KUMO_RFM_API_URL" KUMO_RFM_API_URL || \
      die "invalid KUMO_RFM_API_URL in $DEPLOY_ENV"
  fi
  if [[ -n "${CHAT_UI_URL:-}" ]]; then
    chat_ui_host "$CHAT_UI_URL" >/dev/null || die "invalid CHAT_UI_URL in $DEPLOY_ENV"
  fi
  [[ -d "$GSF_SOURCE_DIR" ]] || die "GSF_SOURCE_DIR is not a directory: $GSF_SOURCE_DIR"
  if command -v git >/dev/null 2>&1 && \
    [[ "$(git -C "$GSF_SOURCE_DIR" rev-parse --is-inside-work-tree 2>/dev/null)" == true ]]; then
    actual_gsf_revision="$(git -C "$GSF_SOURCE_DIR" rev-parse HEAD 2>/dev/null)" || \
      die "could not determine the NVIDIA Ontology source revision"
    [[ -z "$(git -C "$GSF_SOURCE_DIR" status --porcelain --untracked-files=all)" ]] || \
      die "NVIDIA Ontology source has local changes; use a clean $GSF_SOURCE_REVISION checkout"
  elif [[ -f "$GSF_SOURCE_DIR/SOURCE_COMMIT" ]]; then
    actual_gsf_revision="$(tr -d '[:space:]' <"$GSF_SOURCE_DIR/SOURCE_COMMIT")"
  else
    die "GSF_SOURCE_DIR must be a Git checkout or source export with SOURCE_COMMIT"
  fi
  [[ "$actual_gsf_revision" == "$GSF_SOURCE_REVISION" ]] || \
    die "NVIDIA Ontology source is at $actual_gsf_revision; expected $GSF_SOURCE_REVISION"
  resolved_addresses="$(getent ahosts "$QUERY_CLAW_PRIVATE_HOST" 2>/dev/null | \
    awk '{print $1}' | sort -u)"
  [[ "$resolved_addresses" == "$QUERY_CLAW_PRIVATE_IP" ]] || \
    die "$QUERY_CLAW_PRIVATE_HOST must resolve only to $QUERY_CLAW_PRIVATE_IP"

}

compose() {
  docker compose \
    --project-name query-claw \
    --env-file "$DEPLOY_ENV" \
    -f "$GSF_SOURCE_DIR/docker-compose.yml" \
    -f "$DEPLOY_DIR/compose.override.yaml" \
    "$@"
}

kumo_qualification_fingerprint() {
  printf '%s\0%s' "${KUMO_RFM_API_URL:-}" "${KUMO_RFM_API_KEY:-}" | \
    sha256sum | cut -d ' ' -f 1
}

wait_http() {
  local url="$1" description="$2" attempts="${3:-60}"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if curl --fail --silent --show-error --max-time 5 "$url" >/dev/null 2>&1; then
      printf 'ready: %s\n' "$description"
      return 0
    fi
    sleep 5
  done
  die "$description did not become ready at $url"
}

#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
RUNTIME_DIR="$EXAMPLE_DIR/.runtime"
DEPLOY_ENV="${QUERY_CLAW_DEPLOY_ENV:-$RUNTIME_DIR/deploy.env}"
DATA_DIR="$RUNTIME_DIR/active-data"
readonly QUERY_CLAW_GSF_COMMIT=2cd9aa334f2f8f09c285f5b43c55650f3c57f9a0
readonly QUERY_CLAW_RETRIEVER_VERSION=26.08.1
readonly QUERY_CLAW_RETRIEVER_COMMIT=1992e3f09746b9fc150a266567c9737746781fdd
readonly QUERY_CLAW_RETRIEVER_AMD64_IMAGE=nvcr.io/nvidia/nemo-microservices/nrl-service@sha256:597f0ac7404329b600669f5ece03b65fc4158f2068c2892f3cfb3df2d75add58

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

retriever_image_for_arch() {
  case "$1" in
    x86_64|amd64)
      printf '%s\n' "$QUERY_CLAW_RETRIEVER_AMD64_IMAGE"
      ;;
    aarch64|arm64)
      printf 'query-claw-retriever:%s-arm64\n' "$QUERY_CLAW_RETRIEVER_VERSION"
      ;;
    *)
      die "unsupported host architecture: $1"
      ;;
  esac
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

https_origin_parts() {
  local url="${1:-}" label="${2:-URL}"
  python3 - "$url" "$label" <<'PY'
import ipaddress
import re
import sys
from urllib.parse import urlparse

raw, label = sys.argv[1:]
try:
    parsed = urlparse(raw)
    host = parsed.hostname
    port = parsed.port or 443
except ValueError as exc:
    raise SystemExit(f"{label} is invalid") from exc
if (
    parsed.scheme.lower() != "https"
    or not host
    or parsed.username is not None
    or parsed.password is not None
    or parsed.path not in {"", "/"}
    or parsed.query
    or parsed.fragment
):
    raise SystemExit(f"{label} must be an HTTPS origin without credentials, query, or path")
if not 1 <= port <= 65535:
    raise SystemExit(f"{label} has an invalid port")
normalized = host.lower().rstrip(".")
try:
    address = ipaddress.ip_address(normalized)
except ValueError:
    if len(normalized) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
        for part in normalized.split(".")
    ):
        raise SystemExit(f"{label} has an invalid hostname")
else:
    if address.is_unspecified:
        raise SystemExit(f"{label} cannot use an unspecified address")
print(normalized)
print(port)
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

export_runtime_env() {
  local inference_base="${NVIDIA_BASE_URL:-https://integrate.api.nvidia.com/v1}"
  local inference_model="${LLM_MODEL:-nvidia/nemotron-3-super-120b-a12b}"
  local database_engine='' database_path='' encoded_gsf_query_password
  local has_structured has_prediction has_documents
  export QUERY_CLAW_DEPLOY_DIR="$DEPLOY_DIR"
  QUERY_CLAW_HOST_GID="$(id -g)"
  export QUERY_CLAW_HOST_GID
  export QUERY_CLAW_DATA_DIR="$DATA_DIR"
  export QUERY_CLAW_DATASET_REPOSITORY="${QUERY_CLAW_DATASET_REPOSITORY:-}"
  export QUERY_CLAW_DATASET_MANIFEST="${QUERY_CLAW_DATASET_MANIFEST:-}"
  if [[ (-n "$QUERY_CLAW_DATASET_REPOSITORY" && \
    -z "$QUERY_CLAW_DATASET_MANIFEST") || \
    (-z "$QUERY_CLAW_DATASET_REPOSITORY" && \
    -n "$QUERY_CLAW_DATASET_MANIFEST") ]]; then
    die "set QUERY_CLAW_DATASET_REPOSITORY and QUERY_CLAW_DATASET_MANIFEST together"
  fi
  export QUERY_CLAW_ACTIVE_MANIFEST="$DATA_DIR/active-dataset.json"
  export QUERY_CLAW_STRUCTURED_DIR="$DATA_DIR/structured"
  export QUERY_CLAW_DOCUMENTS_DIR="$DATA_DIR/documents"
  has_structured=0
  has_prediction=0
  has_documents=0
  if [[ -f "$QUERY_CLAW_ACTIVE_MANIFEST" ]]; then
    has_structured="$(python3 "$DEPLOY_DIR/configure_dataset.py" \
      "$QUERY_CLAW_ACTIVE_MANIFEST" field has-structured)" || \
      die "active dataset contract is invalid"
    has_documents="$(python3 "$DEPLOY_DIR/configure_dataset.py" \
      "$QUERY_CLAW_ACTIVE_MANIFEST" field has-documents)" || \
      die "active dataset contract is invalid"
    has_prediction="$(python3 "$DEPLOY_DIR/configure_dataset.py" \
      "$QUERY_CLAW_ACTIVE_MANIFEST" field has-prediction)" || \
      die "active dataset contract is invalid"
    if [[ "$has_structured" == 1 ]]; then
      database_engine="$(python3 "$DEPLOY_DIR/configure_dataset.py" \
        "$QUERY_CLAW_ACTIVE_MANIFEST" field database-engine)" || \
        die "active dataset contract is invalid"
      database_path="$(python3 "$DEPLOY_DIR/configure_dataset.py" \
        "$QUERY_CLAW_ACTIVE_MANIFEST" field database-path)" || \
        die "active dataset contract is invalid"
      if [[ "$database_engine" == duckdb ]]; then
        export QUERY_CLAW_STRUCTURED_DIR="$DATA_DIR/database"
      else
        export QUERY_CLAW_STRUCTURED_DIR="$DATA_DIR/$database_path"
      fi
    fi
  fi
  export QUERY_CLAW_DATABASE_ENGINE="$database_engine"
  export QUERY_CLAW_DATABASE_PATH="$database_path"
  export QUERY_CLAW_HAS_STRUCTURED="$has_structured"
  export QUERY_CLAW_HAS_PREDICTION="$has_prediction"
  export QUERY_CLAW_HAS_DOCUMENTS="$has_documents"
  # The GSF service always has structured data when it is started. Reuse that
  # safe view as an empty prediction mount unless a reviewed graph is active.
  export QUERY_CLAW_PREDICTION_DIR="$QUERY_CLAW_STRUCTURED_DIR"
  if [[ "$has_prediction" == 1 && "$database_engine" == duckdb ]]; then
    export QUERY_CLAW_PREDICTION_DIR="$DATA_DIR/prediction"
  fi
  # Keep operator credentials in deploy.env, but expose them to GSF only when
  # the active dataset declares a prediction contract. This makes dataset
  # activation—not stale host configuration—the capability boundary.
  if [[ "$has_prediction" == 1 ]]; then
    export QUERY_CLAW_KUMO_RFM_API_URL="${KUMO_RFM_API_URL:-}"
    export QUERY_CLAW_KUMO_RFM_API_KEY="${KUMO_RFM_API_KEY:-}"
    if [[ "$database_engine" == duckdb ]]; then
      export QUERY_CLAW_KUMO_GRAPH_CONTRACTS_FILE=/query-claw-prediction/graph.json
    else
      export QUERY_CLAW_KUMO_GRAPH_CONTRACTS_FILE=''
    fi
  else
    export QUERY_CLAW_KUMO_RFM_API_URL=''
    export QUERY_CLAW_KUMO_RFM_API_KEY=''
    export QUERY_CLAW_KUMO_GRAPH_CONTRACTS_FILE=''
  fi
  export QUERY_CLAW_DEPLOY_ENV="$DEPLOY_ENV"
  NEMO_RETRIEVER_IMAGE="$(retriever_image_for_arch "$(uname -m)")"
  export NEMO_RETRIEVER_IMAGE
  export NEMO_RETRIEVER_SOURCE_DIR="${NEMO_RETRIEVER_SOURCE_DIR:-$RUNTIME_DIR/sources/nemo-retriever}"
  export DEFAULT_MODELS_API_KEY="${NVIDIA_INFERENCE_API_KEY:-}"
  export DEFAULT_MODELS_ENDPOINT="$inference_base"
  export DEFAULT_MODELS_MODEL="${ONTOLOGY_MODEL:-$inference_model}"
  export REASONING_API_KEY="${NVIDIA_INFERENCE_API_KEY:-}"
  export REASONING_ENDPOINT="$inference_base"
  export REASONING_MODEL="${GSF_REASONING_MODEL:-$DEFAULT_MODELS_MODEL}"
  export NON_REASONING_API_KEY="${NVIDIA_INFERENCE_API_KEY:-}"
  export NON_REASONING_ENDPOINT="$inference_base"
  export NON_REASONING_MODEL="${GSF_NON_REASONING_MODEL:-$DEFAULT_MODELS_MODEL}"
  if [[ -z "${NVIDIA_EMBED_INVOKE_URL:-}" ]]; then
    NVIDIA_EMBED_INVOKE_URL=https://inference-api.nvidia.com/v1/embeddings
    NVIDIA_EMBED_MODEL_PROVIDER_PREFIX="${NVIDIA_EMBED_MODEL_PROVIDER_PREFIX:-nvidia}"
  fi
  NVIDIA_EMBED_MODEL="${NVIDIA_EMBED_MODEL:-nvidia/nemotron-3-embed-1b}"
  NVIDIA_EMBED_MODEL_PROVIDER_PREFIX="${NVIDIA_EMBED_MODEL_PROVIDER_PREFIX:-}"
  NVIDIA_RERANK_INVOKE_URL="${NVIDIA_RERANK_INVOKE_URL:-https://inference-api.nvidia.com/v1/rerank}"
  NVIDIA_RERANK_MODEL="${NVIDIA_RERANK_MODEL:-nvidia/nvidia/llama-3.2-nv-rerankqa-1b-v2}"
  export NVIDIA_EMBED_INVOKE_URL NVIDIA_EMBED_MODEL
  export NVIDIA_EMBED_MODEL_PROVIDER_PREFIX
  export NVIDIA_RERANK_INVOKE_URL NVIDIA_RERANK_MODEL
  export EMBED_API_KEY="${NVIDIA_INFERENCE_API_KEY:-}"
  # GSF appends `/embeddings` to its endpoint and owns a model lane independent
  # of Retriever's complete invoke URL, provider prefix, and model selection.
  export EMBED_ENDPOINT="$inference_base"
  export EMBED_MODEL="${GSF_EMBED_MODEL:-nvidia/nemotron-3-embed-1b}"
  encoded_gsf_query_password="$(python3 - "${GSF_QUERY_PASSWORD:-}" <<'PY'
import sys
from urllib.parse import quote

print(quote(sys.argv[1], safe=""))
PY
)"
  case "$QUERY_CLAW_DATABASE_ENGINE" in
    duckdb)
      export CONNECTION_STRINGS="duckdb:///query-claw-structured/${database_path##*/}"
      ;;
    postgres-csv)
      export CONNECTION_STRINGS="postgresql://query_claw_reader:${encoded_gsf_query_password}@postgres:5432/query_claw"
      ;;
    '')
      export CONNECTION_STRINGS=''
      ;;
    *)
      die "active dataset has an unsupported database engine"
      ;;
  esac
  # GSF's UI/OAuth API and MCP resource share one private, Caddy-terminated
  # origin. This avoids a second public Brev URL and keeps OAuth inside the
  # same OpenShell network policy as the MCP endpoint.
  export GSF_PUBLIC_URL="https://${QUERY_CLAW_PRIVATE_HOST:-query-claw.invalid}:9444"
  export APP_URL="$GSF_PUBLIC_URL"
  export GSF_MCP_URL="$GSF_PUBLIC_URL/mcp"
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
      export "${runtime_name?}"
    fi
  done
  export NEMOCLAW_ENDPOINT_URL="$inference_base"
  export NEMOCLAW_MODEL="$inference_model"
  export COMPATIBLE_API_KEY="${NVIDIA_INFERENCE_API_KEY:-}"
}

validate_gsf_source_dir() {
  local actual_gsf_revision canonical_source git_root canonical_git_root
  require_command git
  canonical_source="$(cd -- "$GSF_SOURCE_DIR" 2>/dev/null && pwd -P)" || \
    die "GSF_SOURCE_DIR is not a directory: $GSF_SOURCE_DIR"
  GSF_SOURCE_DIR="$canonical_source"
  export GSF_SOURCE_DIR

  [[ -f "$GSF_SOURCE_DIR/docker-compose.yml" && \
    -f "$GSF_SOURCE_DIR/mcp/pyproject.toml" && \
    -d "$GSF_SOURCE_DIR/mcp/gsf_mcp" ]] || \
    die "GSF_SOURCE_DIR must be the root of pinned NVIDIA GSF source with its official MCP server"

  [[ "$(git -C "$GSF_SOURCE_DIR" rev-parse --is-inside-work-tree 2>/dev/null)" == true ]] || \
    die "GSF_SOURCE_DIR must be a Git checkout of NVIDIA/GSF"
  git_root="$(git -C "$GSF_SOURCE_DIR" rev-parse --show-toplevel 2>/dev/null)" || \
    die "could not determine the NVIDIA GSF repository root"
  canonical_git_root="$(cd -- "$git_root" 2>/dev/null && pwd -P)" || \
    die "could not resolve the NVIDIA GSF repository root"
  [[ "$canonical_source" == "$canonical_git_root" ]] || \
    die "GSF_SOURCE_DIR must name the NVIDIA GSF repository root: $canonical_git_root"
  actual_gsf_revision="$(git -C "$GSF_SOURCE_DIR" rev-parse HEAD 2>/dev/null)" || \
    die "could not determine the NVIDIA GSF source revision"
  [[ -z "$(git -C "$GSF_SOURCE_DIR" status --porcelain --untracked-files=all)" ]] || \
    die "NVIDIA GSF source has local changes; use a clean $QUERY_CLAW_GSF_COMMIT checkout"
  [[ "$actual_gsf_revision" == "$QUERY_CLAW_GSF_COMMIT" ]] || \
    die "NVIDIA GSF source is at $actual_gsf_revision; expected $QUERY_CLAW_GSF_COMMIT"
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

  local ip private_host resolved_addresses
  ip="${QUERY_CLAW_PRIVATE_IP:-$(private_ipv4)}"
  [[ -n "$ip" ]] || die "could not discover an RFC1918 host address"
  validate_private_ipv4 "$ip" || \
    die "invalid QUERY_CLAW_PRIVATE_IP in $DEPLOY_ENV"
  private_host="${QUERY_CLAW_PRIVATE_HOST:-$(private_hostname "$ip")}"
  [[ -n "$private_host" ]] || die "could not discover a DNS name for $ip"
  append_default QUERY_CLAW_PRIVATE_IP "$ip"
  append_default QUERY_CLAW_PRIVATE_HOST "$private_host"
  append_default QUERY_CLAW_DATASET_REPOSITORY ''
  append_default QUERY_CLAW_DATASET_MANIFEST ''
  append_default POSTGRES_USER postgres
  append_default POSTGRES_PORT 5432
  append_default POSTGRES_DATABASE gsf
  append_default PGADMIN_EMAIL admin@example.com
  append_default GSF_ADMIN_EMAIL admin@example.com
  append_default CHAT_UI_URL ''
  append_default PYTHON_API_URL http://gsf:3001
  append_default INGESTION_SERVICE_URL http://ingestion-service:3002
  append_secret POSTGRES_PASSWORD
  append_secret GSF_QUERY_PASSWORD
  append_secret PGADMIN_PASSWORD
  append_secret GSF_ADMIN_PASSWORD
  append_secret AUTH_SECRET
  append_secret NEMO_RETRIEVER_API_TOKEN
  append_secret NRL_INTERNAL_VDB_TOKEN
  load_deploy_env

  require_var GSF_SOURCE_DIR
  require_var NVIDIA_INFERENCE_API_KEY
  if [[ -n "${KUMO_RFM_API_URL:-}" ]]; then
    validate_credentialed_service_url "$KUMO_RFM_API_URL" KUMO_RFM_API_URL || \
      die "invalid KUMO_RFM_API_URL in $DEPLOY_ENV"
  fi
  if [[ -n "${CHAT_UI_URL:-}" ]]; then
    chat_ui_host "$CHAT_UI_URL" >/dev/null || die "invalid CHAT_UI_URL in $DEPLOY_ENV"
  fi
  validate_gsf_source_dir
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

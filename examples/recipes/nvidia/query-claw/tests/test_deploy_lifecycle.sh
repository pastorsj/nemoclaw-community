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
  'NEMOCLAW_SANDBOX_NAME=query-claw-release-test' \
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
[[ "$loaded_runtime" == $'query-claw-release-test\n18081\n18801\n8651' ]] || \
  fail "deploy.env runtime identity was not exported to child processes"

defaults="$(env -u NVIDIA_BASE_URL -u LLM_MODEL -u NEMOCLAW_PROVIDER \
  bash -c '
    source "$1"
    NVIDIA_INFERENCE_API_KEY=test
    export_runtime_env
    printf "%s\n%s\n%s\n" \
      "$NEMOCLAW_ENDPOINT_URL" "$NEMOCLAW_MODEL" "$NEMOCLAW_PROVIDER"
  ' _ "$common")"
[[ "$defaults" == $'https://integrate.api.nvidia.com/v1\nnvidia/nemotron-3-super-120b-a12b\nbuild' ]] || \
  fail "Hermes inference defaults are incomplete"

retriever_defaults="$(env -u NVIDIA_EMBED_INVOKE_URL -u NVIDIA_EMBED_MODEL \
  -u NVIDIA_EMBED_MODEL_PROVIDER_PREFIX -u NVIDIA_RERANK_INVOKE_URL \
  -u NVIDIA_RERANK_MODEL bash -c '
  source "$1"
  NVIDIA_INFERENCE_API_KEY=test
  export_runtime_env
  printf "%s\n%s\n%s\n%s\n%s\n" \
    "$NVIDIA_EMBED_INVOKE_URL" "$NVIDIA_EMBED_MODEL" \
    "$NVIDIA_EMBED_MODEL_PROVIDER_PREFIX" \
    "$NVIDIA_RERANK_INVOKE_URL" "$NVIDIA_RERANK_MODEL"
' _ "$common")"
[[ "$retriever_defaults" == $'https://inference-api.nvidia.com/v1/embeddings\nnvidia/nemotron-3-embed-1b\nnvidia\nhttps://inference-api.nvidia.com/v1/rerank\nnvidia/nvidia/llama-3.2-nv-rerankqa-1b-v2' ]] || \
  fail "NeMo Retriever hosted defaults differ from the live-qualified stack"

amd64_image="$(NEMO_RETRIEVER_IMAGE=operator-override bash -c '
  source "$1"
  uname() { printf "x86_64\n"; }
  export_runtime_env
  printf "%s\n" "$NEMO_RETRIEVER_IMAGE"
' _ "$common")"
[[ "$amd64_image" == nvcr.io/nvidia/nemo-microservices/nrl-service@sha256:597f0ac7404329b600669f5ece03b65fc4158f2068c2892f3cfb3df2d75add58 ]] || \
  fail "amd64 NeMo Retriever image is not code-owned"
arm64_image="$(NEMO_RETRIEVER_IMAGE=operator-override bash -c '
  source "$1"
  uname() { printf "arm64\n"; }
  export_runtime_env
  printf "%s\n" "$NEMO_RETRIEVER_IMAGE"
' _ "$common")"
[[ "$arm64_image" == query-claw-retriever:26.08.1-arm64 ]] || \
  fail "arm64 NeMo Retriever image is not code-owned"
gsf_embed_model="$(NVIDIA_BASE_URL=https://inference.example.test/v1 \
  NVIDIA_EMBED_MODEL=nvidia/nemotron-3-embed-1b \
  NVIDIA_EMBED_MODEL_PROVIDER_PREFIX=nvidia bash -c '
  source "$1"
  export_runtime_env
  printf "%s\n" "$EMBED_MODEL"
' _ "$common")"
[[ "$gsf_embed_model" == nvidia/nvidia/nemotron-3-embed-1b ]] || \
  fail "GSF did not receive the same provider-prefixed embedding model as Retriever"
grep -q '^  api_key: "${NVIDIA_API_KEY}"$' \
  "$EXAMPLE_DIR/deploy/retriever-service.yaml" || \
  fail "NeMo Retriever does not pass its NVIDIA key to the official reranker"

custom="$(env -u LLM_MODEL -u NEMOCLAW_PROVIDER bash -c '
  source "$1"
  NVIDIA_BASE_URL=https://inference.example.test/v1
  NVIDIA_INFERENCE_API_KEY=test
  export_runtime_env
  printf "%s\n%s\n%s\n" \
    "$NEMOCLAW_ENDPOINT_URL" "$NEMOCLAW_MODEL" "$NEMOCLAW_PROVIDER"
' _ "$common")"
[[ "$custom" == $'https://inference.example.test/v1\nnvidia/nemotron-3-super-120b-a12b\ncustom' ]] || \
  fail "custom Hermes inference defaults are incomplete"

mkdir -p "$TEST_ROOT/active/structured"
printf 'supplier_id\nSUP-001\n' >"$TEST_ROOT/active/structured/suppliers.csv"
cat >"$TEST_ROOT/active/active-dataset.json" <<'JSON'
{"schema_version":1,"id":"supply-chain","fingerprint":"0000000000000000000000000000000000000000000000000000000000000000","database":{"engine":"postgres-csv","name":"query_claw","path":"structured"},"ontology":null,"documents":null,"prediction":null,"prediction_contract_exists":false}
JSON
connection_string="$(GSF_QUERY_PASSWORD='spaces @:/?#% encoded' bash -c '
  source "$1"
  DATA_DIR="$2"
  export_runtime_env
  printf "%s\n" "$CONNECTION_STRINGS"
' _ "$common" "$TEST_ROOT/active")"
[[ "$connection_string" == \
  'postgresql://query_claw_reader:spaces%20%40%3A%2F%3F%23%25%20encoded@postgres:5432/query_claw' ]] || \
  fail "GSF read-only password was not percent-encoded in CONNECTION_STRINGS"

kumo_disabled="$(KUMO_RFM_API_URL=https://prediction.example.test/v1 \
  KUMO_RFM_API_KEY=secret bash -c '
  source "$1"
  DATA_DIR="$2"
  export_runtime_env
  printf "<%s>\n<%s>\n<%s>\n" \
    "$QUERY_CLAW_KUMO_RFM_API_URL" "$QUERY_CLAW_KUMO_RFM_API_KEY" \
    "$QUERY_CLAW_KUMO_GRAPH_CONTRACTS_FILE"
' _ "$common" "$TEST_ROOT/active")"
[[ "$kumo_disabled" == $'<>\n<>\n<>' ]] || \
  fail "non-predictive activation exposed Kumo configuration to GSF"

mkdir -p "$TEST_ROOT/active-prediction/structured"
printf 'supplier_id\nSUP-001\n' > \
  "$TEST_ROOT/active-prediction/structured/suppliers.csv"
cat >"$TEST_ROOT/active-prediction/active-dataset.json" <<'JSON'
{"schema_version":1,"id":"supply-chain","fingerprint":"0000000000000000000000000000000000000000000000000000000000000000","database":{"engine":"postgres-csv","name":"query_claw","path":"structured"},"ontology":null,"documents":null,"prediction":{"mode":"native"},"prediction_contract_exists":true}
JSON
kumo_enabled="$(KUMO_RFM_API_URL=https://prediction.example.test/v1 \
  KUMO_RFM_API_KEY=secret bash -c '
  source "$1"
  DATA_DIR="$2"
  export_runtime_env
  printf "%s\n%s\n<%s>\n" \
    "$QUERY_CLAW_KUMO_RFM_API_URL" "$QUERY_CLAW_KUMO_RFM_API_KEY" \
    "$QUERY_CLAW_KUMO_GRAPH_CONTRACTS_FILE"
' _ "$common" "$TEST_ROOT/active-prediction")"
[[ "$kumo_enabled" == $'https://prediction.example.test/v1\nsecret\n<>' ]] || \
  fail "native predictive activation received a reviewed graph path"

mkdir -p "$TEST_ROOT/active-reviewed/database" \
  "$TEST_ROOT/active-reviewed/ontology" "$TEST_ROOT/active-reviewed/prediction"
: >"$TEST_ROOT/active-reviewed/database/cloud.duckdb"
: >"$TEST_ROOT/active-reviewed/ontology/model.gsf.yaml"
: >"$TEST_ROOT/active-reviewed/prediction/graph.json"
: >"$TEST_ROOT/active-reviewed/prediction/pql-examples.json"
cat >"$TEST_ROOT/active-reviewed/active-dataset.json" <<'JSON'
{"schema_version":1,"id":"cloud","fingerprint":"0000000000000000000000000000000000000000000000000000000000000000","database":{"engine":"duckdb","name":"cloud","path":"database/cloud.duckdb"},"ontology":{"path":"ontology/model.gsf.yaml"},"documents":null,"prediction":{"mode":"reviewed","graph_path":"prediction/graph.json","pql_examples_path":"prediction/pql-examples.json"},"prediction_contract_exists":true}
JSON
reviewed_mounts="$(KUMO_RFM_API_URL=https://prediction.example.test/v1 \
  KUMO_RFM_API_KEY=secret bash -c '
  source "$1"
  DATA_DIR="$2"
  export_runtime_env
  printf "%s\n%s\n%s\n%s\n%s\n" \
    "$QUERY_CLAW_KUMO_GRAPH_CONTRACTS_FILE" "$CONNECTION_STRINGS" \
    "$QUERY_CLAW_STRUCTURED_DIR" "$QUERY_CLAW_PREDICTION_DIR" \
    "$QUERY_CLAW_DOCUMENTS_DIR"
' _ "$common" "$TEST_ROOT/active-reviewed")"
[[ "$reviewed_mounts" == \
  $'/query-claw-prediction/graph.json\nduckdb:///query-claw-structured/cloud.duckdb\n'"$TEST_ROOT"$'/active-reviewed/database\n'"$TEST_ROOT"$'/active-reviewed/prediction\n'"$TEST_ROOT"'/active-reviewed/documents' ]] || \
  fail "reviewed structured, prediction, and document mount paths are incomplete"

compose_override="$EXAMPLE_DIR/deploy/compose.override.yaml"
if grep -q '\${QUERY_CLAW_DATA_DIR}:/query-claw-active' "$compose_override"; then
  fail "a service still receives the full active dataset bind mount"
fi
[[ "$(grep -c 'source: ${QUERY_CLAW_STRUCTURED_DIR}' "$compose_override")" == 3 ]] || \
  fail "structured data is not isolated to its three intended consumers"
[[ "$(grep -c 'source: ${QUERY_CLAW_PREDICTION_DIR}' "$compose_override")" == 1 ]] || \
  fail "prediction assets are not isolated to GSF"
[[ "$(grep -c 'source: ${QUERY_CLAW_DOCUMENTS_DIR}' "$compose_override")" == 1 ]] || \
  fail "documents are not isolated to Retriever"
[[ "$(grep -c 'source: ${QUERY_CLAW_ACTIVE_MANIFEST}' "$compose_override")" == 1 ]] || \
  fail "only Retriever should receive the active dataset manifest"
[[ "$(grep -c 'create_host_path: false' "$compose_override")" == 6 ]] || \
  fail "dataset bind mounts may create undeclared host paths"

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
setup_hermes="$EXAMPLE_DIR/deploy/setup-hermes.sh"
grep -q '^compose up -d --force-recreate retriever$' \
  "$EXAMPLE_DIR/deploy/setup-retriever.sh" || \
  fail "Retriever does not refresh its atomic active-dataset bind mount"
grep -q '^PROFILE_SCRIPT="$(<"$EXAMPLE_DIR/hermes/profile.py")"$' \
  "$setup_hermes" || fail "GSF policy ownership helper is not loaded by Hermes setup"
setup_log="$TEST_ROOT/setup-failure.log"
if SETUP_LOG="$setup_log" bash -c '
  source "$1"
  require_command() { :; }
  initialize_deploy_env() {
    KUMO_RFM_API_URL=https://prediction.example.test/v1
    QUERY_CLAW_HAS_STRUCTURED=1
    QUERY_CLAW_DATASET_REPOSITORY=/external-repository
    QUERY_CLAW_DATASET_MANIFEST=/external-repository/dataset.json
    QUERY_CLAW_DATA_DIR=/runtime/active-data
  }
  compose() { printf "compose %s\n" "$*" >>"$SETUP_LOG"; }
  docker() { printf "%s\n" 2.24.4; }
  python3() {
    if [[ "$1" == - ]]; then
      command cat >/dev/null
      return 0
    fi
    printf "python %s\n" "$*" >>"$SETUP_LOG"
    [[ "$1" != */activate_dataset.py ]]
  }
  main
' _ "$setup" >"$TEST_ROOT/setup-failure.out" 2>&1; then
  fail "setup unexpectedly succeeded after dataset activation failed"
fi
expected_setup_log="$(cat <<EOF
compose stop mcp-ingress gsf-mcp
python $EXAMPLE_DIR/scripts/activate_dataset.py --dataset /external-repository/dataset.json --repository-root /external-repository --output /runtime/active-data
compose stop mcp-ingress gsf-mcp
EOF
)"
[[ "$(cat "$setup_log")" == "$expected_setup_log" ]] || \
  fail "failed setup did not keep the ingress down"

if env -u KUMO_RFM_API_URL bash -c '
  source "$1"
  require_command() { :; }
  initialize_deploy_env() {
    QUERY_CLAW_HAS_STRUCTURED=0
    QUERY_CLAW_HAS_PREDICTION=0
    QUERY_CLAW_HAS_DOCUMENTS=0
    QUERY_CLAW_DATASET_REPOSITORY=/external-repository
    QUERY_CLAW_DATASET_MANIFEST=/external-repository/dataset.json
    QUERY_CLAW_DATA_DIR=/runtime/active-data
    DEPLOY_ENV=/runtime/deploy.env
  }
  export_runtime_env() {
    QUERY_CLAW_HAS_STRUCTURED=1
    QUERY_CLAW_HAS_PREDICTION=1
    QUERY_CLAW_HAS_DOCUMENTS=0
  }
  compose() { :; }
  docker() { printf "%s\n" 2.24.4; }
  python3() {
    if [[ "$1" == - ]]; then command cat >/dev/null; fi
    return 0
  }
  main
' _ "$setup" >"$TEST_ROOT/kumo-after-activation.out" 2>&1; then
  fail "predictive activation started without its Kumo endpoint"
fi
grep -q 'set KUMO_RFM_API_URL in /runtime/deploy.env' \
  "$TEST_ROOT/kumo-after-activation.out" || \
  fail "Kumo was not validated from the newly activated dataset contract"

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

origin_parts="$(bash -c 'source "$1"; https_origin_parts "$2" GSF_PUBLIC_URL' \
  _ "$common" 'https://GSF.example.test:8443/')"
[[ "$origin_parts" == $'gsf.example.test\n8443' ]] || \
  fail "GSF HTTPS origin was not normalized"
for url in http://gsf.example.test 'https://user@example.test' \
  'https://gsf.example.test/path' 'https://gsf.example.test?token=secret' \
  'https://bad host.example' 'https://-bad.example' \
  'https://bad..example' 'https://[::]'; do
  if bash -c 'source "$1"; https_origin_parts "$2" GSF_PUBLIC_URL' \
    _ "$common" "$url" >"$TEST_ROOT/gsf-url.out" 2>&1; then
    fail "unsafe GSF public origin was accepted: $url"
  fi
done

gsf_export="$TEST_ROOT/gsf-export"
mkdir -p "$gsf_export/mcp/gsf_mcp"
: >"$gsf_export/docker-compose.yml"
: >"$gsf_export/mcp/pyproject.toml"
printf '%s\n' self-attested-revision >"$gsf_export/SOURCE_COMMIT"
if GSF_SOURCE_DIR="$gsf_export" bash -c '
  source "$1"
  validate_gsf_source_dir
' _ "$common" >"$TEST_ROOT/gsf-export.out" 2>&1; then
  fail "a self-attested GSF source export was accepted"
fi

gsf_pin="$(bash -c 'source "$1"; printf "%s\n" "$QUERY_CLAW_GSF_COMMIT"' \
  _ "$common")"
[[ "$gsf_pin" =~ ^[0-9a-f]{40}$ ]] || \
  fail "the code-owned GSF source pin is not a commit SHA"

gsf_repo="$TEST_ROOT/gsf-repo"
mkdir -p "$gsf_repo/mcp/gsf_mcp/subdirectory"
: >"$gsf_repo/docker-compose.yml"
: >"$gsf_repo/mcp/pyproject.toml"
git -C "$gsf_repo" init -q
git -C "$gsf_repo" -c user.name=Query-Claw -c user.email=query-claw@example.test \
  add docker-compose.yml mcp/pyproject.toml
git -C "$gsf_repo" -c user.name=Query-Claw -c user.email=query-claw@example.test \
  commit -qm initial
if GSF_SOURCE_DIR="$gsf_repo" bash -c '
  source "$1"
  validate_gsf_source_dir
' _ "$common" >"$TEST_ROOT/gsf-revision.out" 2>&1; then
  fail "a GSF checkout at a non-code-owned revision was accepted"
fi
if GSF_SOURCE_DIR="$gsf_repo/mcp" bash -c '
  source "$1"
  validate_gsf_source_dir
' _ "$common" >"$TEST_ROOT/gsf-subdirectory.out" 2>&1; then
  fail "a GSF repository subdirectory was accepted as GSF_SOURCE_DIR"
fi

oauth_probe="$EXAMPLE_DIR/deploy/lib/verify_gsf_oauth.py"
authorization_document="$TEST_ROOT/authorization-server.json"
cat >"$authorization_document" <<'JSON'
{
  "issuer": "https://gsf.example.test",
  "authorization_endpoint": "https://gsf.example.test/api/auth/mcp/authorize",
  "token_endpoint": "https://gsf.example.test/api/auth/mcp/token",
  "registration_endpoint": "https://gsf.example.test/api/auth/mcp/register"
}
JSON
python3 "$oauth_probe" authorization-server-document \
  https://gsf.example.test "$authorization_document" || \
  fail "valid GSF authorization-server metadata was rejected"
python3 - "$authorization_document" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
document = json.loads(path.read_text(encoding="utf-8"))
document["issuer"] += "/"
path.write_text(json.dumps(document), encoding="utf-8")
PY
if python3 "$oauth_probe" authorization-server-document \
  https://gsf.example.test "$authorization_document" \
  >"$TEST_ROOT/authorization-server-slash.out" 2>&1; then
  fail "non-identical GSF OAuth issuer was accepted"
fi
python3 - "$authorization_document" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
document = json.loads(path.read_text(encoding="utf-8"))
document["issuer"] = "https://gsf.example.test"
document["registration_endpoint"] = "https://attacker.example.test/register"
path.write_text(json.dumps(document), encoding="utf-8")
PY
if python3 "$oauth_probe" authorization-server-document \
  https://gsf.example.test "$authorization_document" \
  >"$TEST_ROOT/authorization-server.out" 2>&1; then
  fail "cross-origin GSF OAuth metadata was accepted"
fi

challenge_headers="$TEST_ROOT/challenge.headers"
cat >"$challenge_headers" <<'HEADERS'
HTTP/2 401 Unauthorized
content-length: 0
www-authenticate: Bearer resource_metadata="https://query-claw.internal:9444/.well-known/oauth-protected-resource/mcp"

HEADERS
resource_metadata_url="$(python3 "$oauth_probe" challenge-resource-metadata \
  "$challenge_headers" 'https://query-claw.internal:9444/mcp')" || \
  fail "valid GSF Bearer challenge was rejected"
[[ "$resource_metadata_url" == \
  'https://query-claw.internal:9444/.well-known/oauth-protected-resource/mcp' ]] || \
  fail "GSF Bearer challenge produced the wrong metadata URL"

resource_headers="$TEST_ROOT/protected-resource.headers"
resource_document="$TEST_ROOT/protected-resource.json"
cat >"$resource_headers" <<'HEADERS'
HTTP/2 200 OK
content-type: application/json; charset=utf-8

HEADERS
cat >"$resource_document" <<'JSON'
{
  "resource": "https://query-claw.internal:9444/mcp",
  "authorization_servers": ["https://gsf.example.test"]
}
JSON
python3 "$oauth_probe" protected-resource-document "$resource_headers" \
  "$resource_document" 'https://query-claw.internal:9444/mcp' \
  'https://gsf.example.test' || \
  fail "valid GSF protected-resource metadata was rejected"
python3 - "$resource_document" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
document = json.loads(path.read_text(encoding="utf-8"))
document["authorization_servers"] = ["https://gsf.example.test/"]
path.write_text(json.dumps(document), encoding="utf-8")
PY
if python3 "$oauth_probe" protected-resource-document "$resource_headers" \
  "$resource_document" 'https://query-claw.internal:9444/mcp' \
  'https://gsf.example.test' >"$TEST_ROOT/protected-resource-slash.out" 2>&1; then
  fail "non-identical GSF authorization-server identifier was accepted"
fi
python3 - "$resource_document" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
document = json.loads(path.read_text(encoding="utf-8"))
document["authorization_servers"] = ["https://gsf.example.test"]
document["resource"] = "https://query-claw.internal:9444/not-mcp"
path.write_text(json.dumps(document), encoding="utf-8")
PY
if python3 "$oauth_probe" protected-resource-document "$resource_headers" \
  "$resource_document" 'https://query-claw.internal:9444/mcp' \
  'https://gsf.example.test' >"$TEST_ROOT/protected-resource.out" 2>&1; then
  fail "incorrect GSF protected resource was accepted"
fi

setup_gsf="$EXAMPLE_DIR/deploy/setup-gsf.sh"
python3 - "$setup_gsf" <<'PY' || fail "GSF quiesce/migration order is unsafe"
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
stop = source.index("compose stop gsf-mcp gsf-frontend ingestion-service gsf")
postgres = source.index("compose up -d --force-recreate postgres")
backend_migration = source.index("compose run --rm --no-deps gsf-migrate")
frontend_migration = source.index("compose run --rm --no-deps frontend-migrate")
reset = source.index("delete_all_data")
backend = source.index("compose up -d --force-recreate --no-deps gsf\n")
ontology = source.index("import-ontology")
ingestion = source.index("compose up -d --force-recreate --no-deps ingestion-service")
postgres_csv = source.index("compose up -d --force-recreate --no-deps gsf ingestion-service")
frontend = source.index("compose up -d --no-deps gsf-frontend")
mcp = source.index("compose up -d --no-deps --force-recreate gsf-mcp")
seed = source.index("seed-pql")
assert stop < postgres < backend_migration < reset < backend < ontology < ingestion < seed < frontend < mcp
assert reset < postgres_csv < frontend
assert stop < frontend_migration < reset
assert "delete_all_data()" in source
assert 'delete_all_data("query_claw")' not in source
assert 'ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role"' in source
assert "ALTER DEFAULT PRIVILEGES FOR ROLE postgres" not in source
PY

status_script="$EXAMPLE_DIR/deploy/status.sh"
external_status="$TEST_ROOT/active-dataset.json"
cat >"$external_status" <<'JSON'
{"schema_version":1,"id":"supply-chain-operations","industry":{"title":"Manufacturing"}}
JSON
status_output="$(bash -c 'source "$1"; print_active_dataset "$2"' \
  _ "$status_script" "$external_status")"
[[ "$status_output" == '  supply-chain-operations (Manufacturing)' ]] || \
  fail "deployment status did not understand the normalized active dataset"

grep -q -- "--noproxy '\*' --connect-timeout 3 --max-time 5" \
  "$EXAMPLE_DIR/deploy/setup-ingress.sh" || \
  fail "private OAuth probes are not bounded and proxy-independent"

if grep -q '^NEMOCLAW_VERSION=' "$EXAMPLE_DIR/.env.example"; then
  fail "the code-owned NemoClaw version leaked back into .env.example"
fi
if grep -q '^NEMO_RETRIEVER_IMAGE=' "$EXAMPLE_DIR/.env.example"; then
  fail "the code-owned NeMo Retriever image leaked back into .env.example"
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
            "nemoclawVersion": "0.0.123",
            "agentVersion": "0.20.6",
            "openshellVersion": "0.0.106",
        }
    }
}), encoding="utf-8")
PY
policy_file="$TEST_ROOT/query-claw-gsf-oauth-policy.yaml"
bash -c '
  source "$1"
  GSF_POLICY_FILE="$2"
  GSF_PUBLIC_URL=https://gsf.example.test
  QUERY_CLAW_PRIVATE_HOST=query-claw.internal
  write_gsf_policy
' _ "$setup_hermes" "$policy_file"
grep -q '^  name: query-claw-gsf-oauth$' "$policy_file" || \
  fail "GSF OAuth policy name is missing"
grep -q 'host: query-claw.internal' "$policy_file" || \
  fail "GSF MCP private host is missing from policy"
grep -q 'port: 9444' "$policy_file" || \
  fail "GSF MCP private port is missing from policy"
[[ "$(grep -c 'host: query-claw.internal' "$policy_file")" == 3 ]] || \
  fail "GSF policies do not share the exact private origin"
grep -A8 '^  query-claw-gsf-mcp:$' "$policy_file" | \
  grep -q 'protocol: mcp' || \
  fail "GSF MCP route does not use OpenShell's native MCP protocol"
grep -A12 '^  query-claw-gsf-mcp:$' "$policy_file" | \
  grep -q 'max_body_bytes: 131072' || \
  fail "GSF MCP route does not bound request bodies"
grep -q 'method: messages/listen' "$policy_file" || \
  fail "GSF MCP policy does not preserve the receive stream"
grep -q '^  query-claw-gsf-oauth:$' "$policy_file" || \
  fail "GSF OAuth routes are not isolated from MCP transport"
grep -q 'path: "/api/auth/mcp/\*\*"' "$policy_file" || \
  fail "GSF OAuth route is missing from policy"
grep -q 'path: "/api/auth/sign-in/email"' "$policy_file" || \
  fail "GSF credential sign-in route is missing from policy"
grep -q '^  query-claw-gsf-admin-bootstrap:$' "$policy_file" || \
  fail "GSF credential bootstrap is not isolated from its runtime policy"
grep -q 'path: /usr/local/bin/hermes' "$policy_file" || \
  fail "GSF runtime policy does not admit the Hermes executable"
grep -q 'path: /usr/bin/python3\*' "$policy_file" || \
  fail "GSF runtime policy does not admit Hermes' Python interpreter"
grep -q 'path: /opt/hermes/\.venv/bin/python3' "$policy_file" || \
  fail "GSF OAuth policy does not pin the trusted Hermes interpreter"
grep -q 'QUERY_CLAW_GSF_OAUTH_SCRIPT="$GSF_OAUTH_SCRIPT"' "$setup_hermes" || \
  fail "deployment does not pass the fixed OAuth helper into native setup"
grep -q '} | run_native_setup' "$setup_hermes" || \
  fail "GSF credentials are not supplied to native setup only on stdin"
grep -q 'exec bash "$EXAMPLE_DIR/hermes/setup.sh"' "$setup_hermes" || \
  fail "native setup is not launched by a fixed executable path"
if grep -q 'env.*NEMO_RETRIEVER_API_TOKEN=' "$setup_hermes"; then
  fail "Retriever credentials can enter a process argument list"
fi
grep -q '^complete_gsf_oauth_if_requested$' \
  "$EXAMPLE_DIR/hermes/setup.sh" || \
  fail "GSF OAuth is outside the native setup rollback transaction"

policy_mode="$(stat -c '%a' "$policy_file" 2>/dev/null || stat -f '%Lp' "$policy_file")"
[[ "$policy_mode" == 600 ]] || fail "GSF OAuth policy is not private"

policy_marker="$TEST_ROOT/query-claw-gsf-oauth-policy.applied.yaml"
policy_log="$TEST_ROOT/policy-command.log"
bash -c '
  source "$1"
  GSF_POLICY_FILE="$2"
  GSF_POLICY_MARKER="$3"
  GSF_PUBLIC_URL=https://gsf.example.test
  QUERY_CLAW_PRIVATE_HOST=query-claw.internal
  NEMOCLAW_SANDBOX_NAME=query-claw
  command_log="$4"
  nemohermes() { printf "%s\n" "$*" >"$command_log"; }
  gsf_policy_state() {
    [[ -s "$command_log" ]] && printf "%s\n" match || printf "%s\n" absent
  }
  apply_gsf_policy
' _ "$setup_hermes" "$policy_file" "$policy_marker" "$policy_log"
[[ "$(<"$policy_log")" == \
  'query-claw policy add --from-file '"$policy_marker"' --trusted-private-host query-claw.internal --yes' ]] || \
  fail "GSF OAuth policy was not applied through NemoClaw"
[[ -f "$policy_marker" ]] || fail "GSF OAuth policy receipt is missing"
marker_mode="$(stat -c '%a' "$policy_marker" 2>/dev/null || stat -f '%Lp' "$policy_marker")"
[[ "$marker_mode" == 600 ]] || fail "GSF OAuth policy receipt is not private"

unowned_log="$TEST_ROOT/unowned-policy-command.log"
if bash -c '
  source "$1"
  RUNTIME_DIR="$2"
  GSF_POLICY_FILE="$2/unowned.yaml"
  GSF_POLICY_MARKER="$2/unowned.applied"
  GSF_PUBLIC_URL=https://gsf.example.test
  QUERY_CLAW_PRIVATE_HOST=query-claw.internal
  NEMOCLAW_SANDBOX_NAME=query-claw
  command_log="$3"
  gsf_policy_state() { printf "%s\n" match; }
  nemohermes() { printf "%s\n" "$*" >>"$command_log"; }
  apply_gsf_policy
' _ "$setup_hermes" "$TEST_ROOT" "$unowned_log" \
  >"$TEST_ROOT/unowned-policy.out" 2>&1; then
  fail "setup accepted a preexisting unowned GSF policy"
fi
[[ ! -s "$unowned_log" ]] || fail "unowned GSF policy was mutated"

drift_log="$TEST_ROOT/drift-policy-command.log"
if bash -c '
  source "$1"
  RUNTIME_DIR="$2"
  GSF_POLICY_FILE="$2/drift.yaml"
  GSF_POLICY_MARKER="$2/drift.applied"
  GSF_PUBLIC_URL=https://gsf.example.test
  QUERY_CLAW_PRIVATE_HOST=query-claw.internal
  NEMOCLAW_SANDBOX_NAME=query-claw
  command_log="$3"
  write_gsf_policy "$GSF_POLICY_MARKER"
  gsf_policy_definitions_match() { return 0; }
  gsf_policy_state() { printf "%s\n" drift; }
  nemohermes() { printf "%s\n" "$*" >>"$command_log"; }
  apply_gsf_policy
' _ "$setup_hermes" "$TEST_ROOT" "$drift_log" \
  >"$TEST_ROOT/drift-policy.out" 2>&1; then
  fail "setup overwrote a drifted owned GSF policy"
fi
[[ ! -s "$drift_log" ]] || fail "drifted GSF policy was mutated"

rollback_log="$TEST_ROOT/rollback-policy-command.log"
bash -c '
  source "$1"
  GSF_POLICY_FILE="$2/rollback.yaml"
  GSF_POLICY_MARKER="$2/rollback.applied"
  GSF_PUBLIC_URL=https://gsf.example.test
  QUERY_CLAW_PRIVATE_HOST=query-claw.internal
  NEMOCLAW_SANDBOX_NAME=query-claw
  command_log="$3"
  fixture_policy_state=match
  write_gsf_policy "$GSF_POLICY_MARKER"
  cp "$GSF_POLICY_MARKER" "$GSF_POLICY_FILE"
  gsf_policy_state() { printf "%s\n" "$fixture_policy_state"; }
  nemohermes() {
    printf "%s\n" "$*" >>"$command_log"
    fixture_policy_state=absent
  }
  GSF_POLICY_ADDED=1
  GSF_POLICY_MARKER_CREATED=1
  rollback_gsf_policy
' _ "$setup_hermes" "$TEST_ROOT" "$rollback_log"
grep -q 'query-claw policy remove query-claw-gsf-oauth --yes' "$rollback_log" || \
  fail "failed setup did not roll back its new GSF policy"
[[ ! -e "$TEST_ROOT/rollback.applied" ]] || \
  fail "successful GSF policy rollback retained its receipt"

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
grep -q 'required v0.0.123' "$TEST_ROOT/cli-mismatch.out" || \
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
grep -q 'required commit f75f722bb4a1ec9642c8df36c8924e24500d78f0' \
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

grep -q '^readonly NEMOCLAW_VERSION=v0\.0\.123$' "$setup_hermes" || \
  fail "NemoClaw version is not a statically discoverable release literal"
grep -q '^readonly NEMOCLAW_COMMIT=f75f722bb4a1ec9642c8df36c8924e24500d78f0$' \
  "$setup_hermes" || fail "NemoClaw commit is not pinned for static analysis"
grep -q '^readonly HERMES_VERSION=0\.20\.6$' "$setup_hermes" || \
  fail "Hermes version is not a statically discoverable release literal"
grep -q '^  export NEMOCLAW_INSTALL_TAG=v0\.0\.123$' "$setup_hermes" || \
  fail "installer tag is not a static version literal"
grep -q 'git -C "$HOME/\.nemoclaw/source" rev-parse HEAD' "$setup_hermes" || \
  fail "installed source commit is not checked operationally"

printf 'PASS: Query Claw deployment release contracts\n'

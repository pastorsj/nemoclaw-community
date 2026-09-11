#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
initialize_deploy_env

compose build gsf gsf-frontend gsf-mcp

# Quiesce every catalog reader/writer before either schema migration or the
# destructive catalog refresh. This also makes a rerun safe when an older
# Query Claw deployment is still serving traffic.
compose stop gsf-mcp gsf-frontend ingestion-service gsf
compose up -d postgres

for _ in {1..60}; do
  if compose exec -T postgres pg_isready -U "$POSTGRES_USER" \
    -d "$POSTGRES_DATABASE" >/dev/null 2>&1; then
    break
  fi
  sleep 3
done
compose exec -T postgres pg_isready -U "$POSTGRES_USER" \
  -d "$POSTGRES_DATABASE" >/dev/null || die "PostgreSQL did not become ready"

# Current GSF has independent Alembic (public schema) and Prisma (frontend
# schema) migrations. Run both before either application tier starts.
compose run --rm --no-deps gsf-migrate
compose run --rm --no-deps frontend-migrate

configure_postgres_csv_database() {
  if ! compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='query_claw'" | grep -q 1; then
    compose exec -T postgres createdb -U "$POSTGRES_USER" query_claw
  fi
  compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" \
    -d query_claw <"$DEPLOY_DIR/load.sql"

  # The bundled sample keeps its dedicated transaction-read-only account. GSF
  # retains its owner credential for catalog maintenance, but generated SQL
  # cannot mutate the example dataset.
  compose exec -T postgres psql -v ON_ERROR_STOP=1 \
    -v owner_role="$POSTGRES_USER" -U "$POSTGRES_USER" \
    -d query_claw <<'SQL'
\getenv reader_password GSF_QUERY_PASSWORD
SELECT format('CREATE ROLE query_claw_reader LOGIN PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'query_claw_reader') \gexec
ALTER ROLE query_claw_reader PASSWORD :'reader_password';
ALTER ROLE query_claw_reader SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE query_claw TO query_claw_reader;
GRANT USAGE ON SCHEMA public TO query_claw_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO query_claw_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA public
  GRANT SELECT ON TABLES TO query_claw_reader;
SQL
}

set_semantic_compilation() {
  local enabled="$1"
  compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" \
    -d "$POSTGRES_DATABASE" -v enabled="$enabled" <<'SQL'
INSERT INTO frontend.configurations (key, value, updated_at)
VALUES ('semantic_compilation_enabled', :'enabled', NOW())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
SQL
}

wait_for_ingestion() {
  local run_since="$1" require_semantic="$2" ingest_id logs
  ingest_id="$(compose ps -q ingestion-service)"
  for _ in {1..240}; do
    logs="$(docker logs --since "$run_since" "$ingest_id" 2>&1 || true)"
    ! grep -q 'ingest: finished with errors' <<<"$logs" || \
      die "GSF Query Claw ingest failed"
    ! grep -q 'semantic: finished with errors' <<<"$logs" || \
      die "GSF semantic compilation failed"
    if grep -q 'ingest: finished successfully' <<<"$logs" && \
      { [[ "$require_semantic" != true ]] || \
        grep -q 'semantic: finished successfully' <<<"$logs"; }; then
      return
    fi
    sleep 5
  done
  logs="$(docker logs --since "$run_since" "$ingest_id" 2>&1 || true)"
  grep -q 'ingest: finished successfully' <<<"$logs" || \
    die "GSF Query Claw ingest did not complete"
  [[ "$require_semantic" != true ]] || \
    grep -q 'semantic: finished successfully' <<<"$logs" || \
    die "GSF semantic compilation did not complete"
}

# Query Claw owns this Compose project's GSF catalog. Resetting the whole store
# prevents a dataset switch from exposing stale catalog entries or global PQL
# examples from the previously active dataset.
compose run --rm --no-deps --entrypoint python gsf -c \
  'from gsf.dal.reset import delete_all_data; delete_all_data()'

run_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ "$QUERY_CLAW_DATABASE_ENGINE" == duckdb ]]; then
  database_name="$(python3 "$DEPLOY_DIR/configure_dataset.py" \
    "$QUERY_CLAW_ACTIVE_MANIFEST" field database-name)" || \
    die "could not read the active dataset database name"
  set_semantic_compilation false

  # Import the reviewed ontology into the empty catalog first. Ingestion then
  # refreshes physical metadata without generating a second semantic model.
  compose up -d --force-recreate --no-deps gsf
  wait_http http://127.0.0.1:3001/api/health "NVIDIA GSF API" 120
  python3 "$DEPLOY_DIR/configure_dataset.py" \
    "$QUERY_CLAW_ACTIVE_MANIFEST" import-ontology
  compose up -d --force-recreate --no-deps ingestion-service
  wait_for_ingestion "$run_since" false

  catalog_state="$(compose exec -T postgres psql -v ON_ERROR_STOP=1 \
    -v database_name="$database_name" -U "$POSTGRES_USER" \
    -d "$POSTGRES_DATABASE" -tA <<'SQL'
SELECT CASE WHEN count(DISTINCT d.id)=1
                 AND min(d.name)=:'database_name'
                 AND count(DISTINCT t.id)>0
                 AND count(DISTINCT c.id)>0
            THEN 'READY' ELSE 'WAIT' END
FROM catalog_database d
LEFT JOIN catalog_schema s ON s.database_id=d.id
LEFT JOIN catalog_table t ON t.schema_id=s.id
LEFT JOIN catalog_column c ON c.table_id=t.id;
SQL
  )"
  catalog_state="$(tr -d '[:space:]' <<<"$catalog_state")"
  [[ "$catalog_state" == READY ]] || \
    die "GSF catalog does not contain only the selected DuckDB dataset"

  semantic_state="$(curl --fail --silent --show-error --max-time 10 \
    http://127.0.0.1:3001/api/semantic-compilation/status | \
    python3 -c 'import json,sys; print("READY" if json.load(sys.stdin).get("calculated") is True else "WAIT")')"
  [[ "$semantic_state" == READY ]] || \
    die "GSF did not retain the selected dataset's reviewed ontology"
  python3 "$DEPLOY_DIR/configure_dataset.py" \
    "$QUERY_CLAW_ACTIVE_MANIFEST" seed-pql
elif [[ "$QUERY_CLAW_DATABASE_ENGINE" == postgres-csv ]]; then
  configure_postgres_csv_database
  # The bundled sample has no reviewed ontology artifact, so use GSF's native
  # compile-after-ingest behavior.
  set_semantic_compilation true
  compose up -d --force-recreate --no-deps gsf ingestion-service
  wait_http http://127.0.0.1:3001/api/health "NVIDIA GSF API" 120
  wait_for_ingestion "$run_since" true

  catalog_state="$(compose exec -T postgres psql -U "$POSTGRES_USER" \
    -d "$POSTGRES_DATABASE" -tAc "
SELECT CASE WHEN count(DISTINCT t.id)=6 AND count(DISTINCT c.id)=30
            THEN 'READY' ELSE 'WAIT' END
FROM catalog_database d
JOIN catalog_schema s ON s.database_id=d.id
JOIN catalog_table t ON t.schema_id=s.id
LEFT JOIN catalog_column c ON c.table_id=t.id
WHERE d.name='query_claw';" | tr -d '[:space:]')"
  [[ "$catalog_state" == READY ]] || \
    die "GSF catalog does not contain the expected six tables and 30 columns"

  semantic_state="$(compose exec -T postgres psql -U "$POSTGRES_USER" \
    -d "$POSTGRES_DATABASE" -tAc "
SELECT CASE WHEN count(DISTINCT t.id)=6
                 AND count(DISTINCT CASE WHEN term.id IS NOT NULL THEN t.id END)=6
            THEN 'READY' ELSE 'WAIT' END
FROM catalog_database d
JOIN catalog_schema s ON s.database_id=d.id
JOIN catalog_table t ON t.schema_id=s.id
LEFT JOIN table__term tt ON tt.table_id=t.id
LEFT JOIN term ON term.id=tt.term_id AND term.source='semantic'
WHERE d.name='query_claw';" | tr -d '[:space:]')"
  [[ "$semantic_state" == READY ]] || \
    die "GSF semantic terms do not cover all six Query Claw tables"
else
  die "active dataset has an unsupported database engine"
fi

compose up -d --no-deps gsf-frontend
wait_http http://127.0.0.1:3000/api/health "NVIDIA GSF UI and OAuth server" 120

# Caddy creates the private origin and CA before the official MCP server starts.
# Copy only its public root certificate to the host. The unprivileged GSF MCP
# process can trust that file without access to Caddy's private CA material.
compose up -d --no-deps --force-recreate mcp-ingress
CA_FILE="$RUNTIME_DIR/query-claw-mcp-root.crt"
for _ in {1..30}; do
  if compose cp mcp-ingress:/data/caddy/pki/authorities/local/root.crt \
    "$CA_FILE" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
[[ -s "$CA_FILE" ]] || die "Caddy root certificate was not created"
chmod 644 "$CA_FILE"
compose up -d --no-deps --force-recreate gsf-mcp

# The MCP server is the relevant OAuth consumer. Probe GSF's public metadata
# from that container so success also proves the deployed network path, not
# merely host access to the Brev URL.
oauth_ready=false
for _ in {1..24}; do
  if compose exec -T gsf-mcp /app/.venv/bin/python - \
    authorization-server "$GSF_PUBLIC_URL" \
    <"$DEPLOY_DIR/lib/verify_gsf_oauth.py" >/dev/null 2>&1; then
    oauth_ready=true
    break
  fi
  sleep 5
done
if [[ "$oauth_ready" != true ]]; then
  compose stop gsf-mcp >/dev/null 2>&1 || true
  die "public NVIDIA GSF OAuth metadata failed its consumer-side contract probe"
fi
compose exec -T gsf-mcp /app/.venv/bin/python - \
  authorization-server "$GSF_PUBLIC_URL" \
  <"$DEPLOY_DIR/lib/verify_gsf_oauth.py"

printf 'ready: Query Claw data in NVIDIA GSF\n'
printf 'ready: NVIDIA GSF OAuth authorization-server contract\n'
printf 'ready: official NVIDIA GSF MCP server on the private Query Claw origin\n'

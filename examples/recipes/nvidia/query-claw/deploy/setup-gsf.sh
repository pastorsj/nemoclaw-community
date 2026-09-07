#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$DEPLOY_DIR/lib/common.sh"
initialize_deploy_env

compose build gsf gsf-frontend
compose up -d postgres neo4j

for _ in {1..60}; do
  if compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" >/dev/null 2>&1; then
    break
  fi
  sleep 3
done
compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" >/dev/null || \
  die "PostgreSQL did not become ready"
neo4j_id="$(compose ps -q neo4j)"
for _ in {1..120}; do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' "$neo4j_id" 2>/dev/null)" == healthy ]] && break
  sleep 3
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' "$neo4j_id" 2>/dev/null)" == healthy ]] || \
  die "Neo4j did not become ready"

if ! compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='query_claw'" | grep -q 1; then
  compose exec -T postgres createdb -U "$POSTGRES_USER" query_claw
fi
compose run --rm --no-deps frontend-migrate
compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d query_claw \
  <"$DEPLOY_DIR/load.sql"

# The Ontology text-to-SQL path receives a dedicated, transaction-read-only
# account. Administrative loading and GSF metadata continue to use the owner
# credential, but model-generated SQL cannot mutate the Query Claw dataset.
compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" \
  -d query_claw <<'SQL'
\getenv reader_password GSF_QUERY_PASSWORD
SELECT format('CREATE ROLE query_claw_reader LOGIN PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'query_claw_reader') \gexec
ALTER ROLE query_claw_reader PASSWORD :'reader_password';
ALTER ROLE query_claw_reader SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE query_claw TO query_claw_reader;
GRANT USAGE ON SCHEMA public TO query_claw_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO query_claw_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT SELECT ON TABLES TO query_claw_reader;
SQL

# Disable semantic work while the refreshed source tables are being cataloged.
compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" <<'SQL'
INSERT INTO configurations (key, value, updated_at)
VALUES ('semantic_compilation_enabled', 'false', NOW())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
SQL

# Make repeat deployments prove a fresh Query Claw catalog and compilation.
# Resetting only semantic nodes leaves removed columns in the data graph.
compose run --rm --no-deps --entrypoint python ingestion-service -c \
  'from gsf.dal.reset import delete_all_data; delete_all_data("query_claw")'

ingest_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
compose up -d --force-recreate --no-deps ingestion-service
ingest_id="$(compose ps -q ingestion-service)"
for _ in {1..240}; do
  logs="$(docker logs --since "$ingest_since" "$ingest_id" 2>&1 || true)"
  ! grep -q 'ingest: failed for connection' <<<"$logs" || die "GSF Query Claw ingest failed"
  grep -q 'ingest: finished' <<<"$logs" && break
  sleep 5
done
docker logs --since "$ingest_since" "$ingest_id" 2>&1 | grep -q 'ingest: finished' || \
  die "GSF Query Claw ingest did not complete"

catalog_query="MATCH (:Database {name:'query_claw'})-[:CONTAINS]->(:Schema)-[:CONTAINS]->(t:Table) OPTIONAL MATCH (t)-[:CONTAINS]->(c:Column) WITH count(DISTINCT t) AS tables,count(DISTINCT c) AS columns RETURN CASE WHEN tables=6 AND columns=30 THEN 'READY' ELSE 'WAIT' END AS state;"
compose exec -T neo4j cypher-shell --format plain "$catalog_query" | \
  tail -n1 | tr -d '"\r' | grep -qx READY || \
  die "GSF catalog does not contain the expected six tables and 30 columns"

compose up -d --no-deps gsf
wait_http http://127.0.0.1:3001/api/health "NVIDIA Ontology API" 120

# Persist the same setting exposed by the GSF UI, then require a fresh,
# Query-Claw-specific six-table compilation rather than a global status bit.
compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" <<'SQL'
UPDATE configurations
SET value = 'true', updated_at = NOW()
WHERE key = 'semantic_compilation_enabled';
SQL
semantic_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
curl --fail --silent --show-error -X POST \
  http://127.0.0.1:3001/api/semantic-compilation/trigger >/dev/null

for _ in {1..240}; do
  logs="$(docker logs --since "$semantic_since" "$ingest_id" 2>&1 || true)"
  ! grep -q 'semantic: failed for database query_claw' <<<"$logs" || \
    die "GSF semantic compilation failed"
  grep -q 'Finished semantic compilation for database query_claw: 6 tables processed' <<<"$logs" && break
  sleep 5
done
docker logs --since "$semantic_since" "$ingest_id" 2>&1 | \
  grep -q 'Finished semantic compilation for database query_claw: 6 tables processed' || \
  die "GSF semantic compilation did not complete"

semantic_query="MATCH (:Database {name:'query_claw'})-[:CONTAINS]->(:Schema)-[:CONTAINS]->(t:Table) OPTIONAL MATCH (t)-[:REPRESENTS]->(term:Term {source:'semantic'}) WITH count(DISTINCT t) AS total,count(DISTINCT CASE WHEN term IS NOT NULL THEN t END) AS represented RETURN CASE WHEN total=6 AND represented=6 THEN 'READY' ELSE 'WAIT' END AS state;"
compose exec -T neo4j cypher-shell --format plain "$semantic_query" | \
  tail -n1 | tr -d '"\r' | grep -qx READY || \
  die "GSF semantic terms do not cover all six Query Claw tables"

compose up -d --no-deps gsf-frontend
wait_http http://127.0.0.1:3000/api/health "NVIDIA Ontology UI" 120

printf 'ready: Query Claw PostgreSQL data and NVIDIA Ontology\n'

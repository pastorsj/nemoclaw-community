// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The API's read-only view of a structured data source (live mode):
 *
 * - `GET  /v1/data_sources/{id}/schema`                  tables, views, columns, keys
 * - `GET  /v1/data_sources/{id}/preview?table=&limit=`   first rows of a table
 * - `POST /v1/data_sources/{id}/query` `{sql}`           one bounded read-only query
 *
 * The API bounds every result; errors come back as `{detail}`. Every pack's
 * structured source is one DuckDB database.
 *
 * Replay mode has no API. The recordings bundle's `database.json`, which
 * `demo-api record` writes from those same routes, holds each structured
 * source's schema, the first rows of each table and the result of each query
 * the recorded runs made; the replay loaders below serve those, and a query
 * that was not recorded fails with a note to use the live demo.
 */

export type Value = string | number | boolean | null

export interface DatabaseColumn {
  name: string
  dataType: string
  nullable?: boolean
  primaryKey?: boolean
}

export interface DatabaseTable {
  /** Bare table name */
  name: string
  schemaName: string
  /** How queries and previews name it: bare in `main`, `schema.table` elsewhere */
  qualifiedName: string
  tableType: string
  columns: DatabaseColumn[]
}

export interface DatabaseRelationship {
  /** `schema.table.column` of the foreign key */
  source: string
  /** `schema.table.column` it references */
  target: string
  sourceTable: string
  targetTable: string
}

export interface DatabaseSnapshot {
  sourceId: string
  databaseName: string
  provider: string
  schemas: Array<{ name: string; tables: DatabaseTable[] }>
  relationships: DatabaseRelationship[]
}

export interface QueryResult {
  columns: Array<{ name: string; dataType: string | null }>
  rows: Value[][]
  truncated: boolean
  durationMs: number
}

/** `GET /schema` as the API sends it. */
interface SchemaResponse {
  source_id: string
  database_name: string
  tables: Array<{
    name: string
    schema: string
    kind: string
    columns: Array<{ name: string; type: string; nullable?: boolean; primary_key?: boolean }>
  }>
  relationships: Array<{
    from_table: string
    from_column: string
    to_table: string
    to_column: string
  }>
}

/** `GET /preview` and `POST /query` as the API sends them. */
interface QueryResponse {
  columns: string[]
  types?: string[]
  rows: Value[][]
  truncated: boolean
  duration_ms: number
}

export const PREVIEW_ROWS = 8

const sourceUrl = (sourceId: string) => `/api/v1/data_sources/${encodeURIComponent(sourceId)}`

const readBody = async <T>(response: Response): Promise<T> => {
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail
    throw new Error(
      typeof detail === 'string' ? detail : `The request failed (${response.status}).`
    )
  }
  return body as T
}

const request = async <T>(url: string, init?: RequestInit): Promise<T> =>
  readBody<T>(await fetch(url, { cache: 'no-store', ...init }))

const toSnapshot = (schema: SchemaResponse): DatabaseSnapshot => {
  const schemas = new Map<string, DatabaseTable[]>()
  for (const table of schema.tables) {
    const tables = schemas.get(table.schema) ?? []
    tables.push({
      name: table.name.slice(table.name.lastIndexOf('.') + 1),
      schemaName: table.schema,
      qualifiedName: table.name,
      tableType: table.kind,
      columns: table.columns.map((column) => ({
        name: column.name,
        dataType: column.type,
        nullable: column.nullable,
        primaryKey: column.primary_key,
      })),
    })
    schemas.set(table.schema, tables)
  }
  // Relationships name each column in full: schema.table.column
  const fullName = new Map(
    [...schemas.values()]
      .flat()
      .map((table) => [table.qualifiedName, `${table.schemaName}.${table.name}`])
  )
  const column = (table: string, name: string): string => `${fullName.get(table) ?? table}.${name}`
  return {
    sourceId: schema.source_id,
    databaseName: schema.database_name,
    provider: 'duckdb',
    schemas: [...schemas].map(([name, tables]) => ({ name, tables })),
    relationships: schema.relationships.map((relationship) => ({
      source: column(relationship.from_table, relationship.from_column),
      target: column(relationship.to_table, relationship.to_column),
      sourceTable: relationship.from_table,
      targetTable: relationship.to_table,
    })),
  }
}

const toResult = (response: QueryResponse): QueryResult => ({
  columns: response.columns.map((name, index) => ({
    name,
    dataType: response.types?.[index] ?? null,
  })),
  rows: response.rows,
  truncated: response.truncated,
  durationMs: response.duration_ms,
})

export const getSnapshot = async (
  sourceId: string,
  signal?: AbortSignal
): Promise<DatabaseSnapshot> =>
  toSnapshot(await request<SchemaResponse>(`${sourceUrl(sourceId)}/schema`, { signal }))

export const getPreview = async (
  sourceId: string,
  table: string,
  signal?: AbortSignal
): Promise<QueryResult> =>
  toResult(
    await request<QueryResponse>(
      `${sourceUrl(sourceId)}/preview?table=${encodeURIComponent(table)}&limit=${PREVIEW_ROWS}`,
      { signal }
    )
  )

export const runQuery = async (
  sourceId: string,
  sql: string,
  signal?: AbortSignal
): Promise<QueryResult> =>
  toResult(
    await request<QueryResponse>(`${sourceUrl(sourceId)}/query`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ sql }),
      signal,
    })
  )

// ---------------------------------------------------------------- replay

/** A structured source of the replay bundle's `database.json`. */
interface ReplaySource {
  id: string
  name: string
  databaseName: string
  schema: SchemaResponse
  previews: Record<string, QueryResponse>
  queries: Array<{ sql: string; result: QueryResponse }>
}

export interface ReplayDatabase {
  schemaVersion: 1
  sources: ReplaySource[]
}

export const REPLAY_QUERY_ONLY =
  'Replay can rerun only the queries its recorded answers ran. Start the live demo to run your own SQL.'

let replayDatabase: Promise<ReplayDatabase | null> | null = null

/** The bundle's `database.json`, fetched once; null when the bundle has none. */
export const loadReplayDatabase = (): Promise<ReplayDatabase | null> => {
  replayDatabase ??= fetch('/api/recordings/database.json', { cache: 'no-store' })
    .then(async (response) => {
      if (!response.ok) return null
      const body = (await response.json()) as ReplayDatabase
      return body?.schemaVersion === 1 && Array.isArray(body.sources) ? body : null
    })
    .catch(() => {
      replayDatabase = null
      return null
    })
  return replayDatabase
}

/** For tests: forget the fetched bundle. */
export const resetReplayDatabase = (): void => {
  replayDatabase = null
}

const replaySource = async (sourceId: string): Promise<ReplaySource> => {
  const source = (await loadReplayDatabase())?.sources.find((item) => item.id === sourceId)
  if (!source) throw new Error('This recording has no copy of that database.')
  return source
}

/** Whitespace-insensitive, so a recorded query matches however it was reformatted. */
const normalizedSql = (sql: string): string => sql.replace(/;\s*$/, '').replace(/\s+/g, ' ').trim()

export const replaySnapshot = async (sourceId: string): Promise<DatabaseSnapshot> =>
  toSnapshot((await replaySource(sourceId)).schema)

export const replayPreview = async (sourceId: string, table: string): Promise<QueryResult> => {
  const preview = (await replaySource(sourceId)).previews[table]
  if (!preview) throw new Error('This recording has no preview of that table.')
  return toResult(preview)
}

export const replayQuery = async (sourceId: string, sql: string): Promise<QueryResult> => {
  const wanted = normalizedSql(sql)
  const recorded = (await replaySource(sourceId)).queries.find(
    (query) => normalizedSql(query.sql) === wanted
  )
  if (!recorded) throw new Error(REPLAY_QUERY_ONLY)
  return toResult(recorded.result)
}

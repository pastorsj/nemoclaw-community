// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Data Sources API Client
 *
 * Fetches the data sources of the active data pack: from the API
 * (`GET /v1/data_sources`) in live mode, or from the recordings bundle's
 * `pack.json` snapshot in replay mode.
 */

export interface DataSourceFromAPI {
  /** Unique identifier for the data source */
  id: string
  /** Display name for the source */
  name: string
  /** Brief description of the source */
  description?: string | null
  /** Whether the source starts enabled (defaults to true) */
  default_enabled?: boolean
  /** `structured` for the pack's market database, `documents` for a document collection */
  kind?: 'structured' | 'documents'
  /** The structured source's database; null for document sources */
  database_name?: string | null
}

/**
 * Get the available data sources.
 *
 * The API returns either a bare array or `{ data_sources: [...] }`.
 */
export const fetchDataSources = async (signal?: AbortSignal): Promise<DataSourceFromAPI[]> => {
  const response = await fetch('/api/v1/data_sources', { signal })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body?.error?.message || `Failed to fetch data sources: ${response.status}`)
  }
  const data = await response.json()
  return Array.isArray(data) ? data : (data.data_sources ?? [])
}

/**
 * The data sources in the replay bundle's `pack.json` snapshot (replay mode). As in the API, the
 * structured source carries the pack's database name.
 */
export const fetchRecordedDataSources = async (
  signal?: AbortSignal
): Promise<DataSourceFromAPI[]> => {
  const response = await fetch('/api/recordings/pack.json', { signal })
  if (!response.ok) throw new Error(`Failed to load the recorded data sources: ${response.status}`)
  const pack: {
    sources?: Pick<DataSourceFromAPI, 'id' | 'name' | 'description' | 'kind'>[]
    structured?: { source?: string; database_name?: string }
  } = await response.json()
  return (pack.sources ?? []).map(({ id, name, description, kind }) => ({
    id,
    name,
    description,
    kind,
    database_name: id === pack.structured?.source ? (pack.structured.database_name ?? null) : null,
  }))
}

// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The structured database behind a run: its tables, each table's
 * columns, keys, relationships and first rows, and read-only SQL, e.g. an
 * Auto Ontology query opened from its explorer. Loaded structures and
 * previews are cached for as long as the browser is open.
 */

'use client'

import Image from 'next/image'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { SelectShell } from '@/shared/components/SelectShell'
import type { StructuredQueryReceipt } from '../contract'
import styles from '../execution-workspace.module.css'
import type { ExecutionNodeDetail } from '../graph'
import {
  getPreview,
  getSnapshot,
  runQuery,
  type DatabaseSnapshot,
  type DatabaseTable,
  type QueryResult,
} from './database-client'
import { DatabaseQueryPanel } from './DatabaseQueryPanel'

/** A structured source the run can browse. */
export interface StructuredSource {
  id: string
  name: string
  databaseName: string
}

export interface DatabaseBrowserProps {
  detail: Pick<ExecutionNodeDetail, 'id'>
  /** The event cursor at the replay position */
  cursor: string
  /** The run's structured sources; empty when there is no API to read them (replay) */
  sources: readonly StructuredSource[]
  /** The run's Auto Ontology calls, to start a query from */
  receipts?: readonly StructuredQueryReceipt[]
  /** Opens in SQL mode with this call's query */
  initialReceipt?: StructuredQueryReceipt | null
  snapshotLoader?: (sourceId: string, signal?: AbortSignal) => Promise<DatabaseSnapshot>
  previewLoader?: (sourceId: string, table: string, signal?: AbortSignal) => Promise<QueryResult>
  queryRunner?: (sourceId: string, sql: string, signal?: AbortSignal) => Promise<QueryResult>
  onClose: () => void
}

const DUCKDB_MARK = { src: '/capability-assets/provider-duckdb.svg', alt: 'DuckDB' }

const quoted = (name: string): string => `"${name.replaceAll('"', '""')}"`

export const DatabaseBrowser = ({
  detail,
  cursor,
  sources,
  receipts = [],
  initialReceipt = null,
  snapshotLoader = getSnapshot,
  previewLoader = getPreview,
  queryRunner = runQuery,
  onClose,
}: DatabaseBrowserProps): ReactNode => {
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const snapshotCache = useRef(new Map<string, DatabaseSnapshot>())
  const previewCache = useRef(new Map<string, QueryResult>())
  const [chosenSourceId, setChosenSourceId] = useState(
    sources.find((source) => source.databaseName === initialReceipt?.content?.databaseName)?.id ??
      null
  )
  const selectedSource =
    sources.find((source) => source.id === chosenSourceId) ?? sources[0] ?? null
  const selectedSourceId = selectedSource?.id ?? ''
  const [mode, setMode] = useState<'browse' | 'query'>(initialReceipt ? 'query' : 'browse')
  const [queryOpened, setQueryOpened] = useState(Boolean(initialReceipt))
  const [snapshotState, setSnapshotState] = useState<{
    sourceId: string
    snapshot: DatabaseSnapshot | null
    error?: string
  }>({ sourceId: '', snapshot: null })
  const [chosenTable, setChosenTable] = useState('')
  const [previewState, setPreviewState] = useState<{
    key: string
    preview: QueryResult | null
    error?: string
  }>({ key: '', preview: null })

  useEffect(() => {
    closeButtonRef.current?.focus()
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  useEffect(() => {
    if (!selectedSourceId) return
    const cached = snapshotCache.current.get(selectedSourceId)
    if (cached) {
      setSnapshotState({ sourceId: selectedSourceId, snapshot: cached })
      return
    }
    const controller = new AbortController()
    snapshotLoader(selectedSourceId, controller.signal).then(
      (snapshot) => {
        if (controller.signal.aborted) return
        snapshotCache.current.set(selectedSourceId, snapshot)
        setSnapshotState({ sourceId: selectedSourceId, snapshot })
      },
      (error: unknown) => {
        if (controller.signal.aborted) return
        setSnapshotState({
          sourceId: selectedSourceId,
          snapshot: null,
          error: error instanceof Error ? error.message : 'Structured database metadata failed.',
        })
      }
    )
    return () => controller.abort()
  }, [selectedSourceId, snapshotLoader])

  const loaded = snapshotState.sourceId === selectedSourceId ? snapshotState : null
  const snapshot = loaded?.snapshot ?? null
  const tables = useMemo<DatabaseTable[]>(
    () => (snapshot?.schemas ?? []).flatMap((schema) => schema.tables),
    [snapshot]
  )
  const selectedTable =
    tables.find((table) => table.qualifiedName === chosenTable) ?? tables[0] ?? null
  const previewKey = selectedTable ? `${selectedSourceId}\u0000${selectedTable.qualifiedName}` : ''
  const previewTable = selectedTable?.qualifiedName ?? ''

  useEffect(() => {
    if (!selectedSourceId || !previewTable) return
    const cached = previewCache.current.get(previewKey)
    if (cached) {
      setPreviewState({ key: previewKey, preview: cached })
      return
    }
    const controller = new AbortController()
    previewLoader(selectedSourceId, previewTable, controller.signal).then(
      (preview) => {
        if (controller.signal.aborted) return
        previewCache.current.set(previewKey, preview)
        setPreviewState({ key: previewKey, preview })
      },
      (error: unknown) => {
        if (controller.signal.aborted) return
        setPreviewState({
          key: previewKey,
          preview: null,
          error: error instanceof Error ? error.message : 'Table preview failed.',
        })
      }
    )
    return () => controller.abort()
  }, [previewKey, previewLoader, previewTable, selectedSourceId])

  const previewLoaded = previewState.key === previewKey ? previewState : null
  const preview = previewLoaded?.preview ?? null
  const relationships = (snapshot?.relationships ?? []).filter(
    (relationship) =>
      relationship.sourceTable === previewTable || relationship.targetTable === previewTable
  )

  return (
    <section
      className={styles.capabilityExplorer}
      role="dialog"
      aria-modal="true"
      data-testid="capability-explorer"
      data-presentation="database"
      data-capability={detail.id}
      data-cursor={cursor}
      aria-label="Structured Database browser"
    >
      <header className={styles.capabilityExplorerHeader}>
        <div>
          <span className={styles.eyebrow}>Physical data source</span>
          <h3>Structured Database</h3>
          <p>Explore the database and run read-only SQL.</p>
        </div>
        <div className={styles.capabilityExplorerHeaderActions}>
          {snapshot && (
            <div className={styles.databaseViewModes} role="group" aria-label="Database view">
              <button
                type="button"
                className={styles.backButton}
                aria-pressed={mode === 'browse'}
                onClick={() => setMode('browse')}
              >
                Browse
              </button>
              <button
                type="button"
                className={styles.backButton}
                aria-pressed={mode === 'query'}
                onClick={() => {
                  setQueryOpened(true)
                  setMode('query')
                }}
              >
                SQL Query
              </button>
            </div>
          )}
          <span className={styles.capabilityCursor}>Replay step {cursor}</span>
          <button
            ref={closeButtonRef}
            type="button"
            className={styles.iconButton}
            onClick={onClose}
            aria-label="Close Structured Database browser"
          >
            ×
          </button>
        </div>
      </header>

      <div className={styles.databaseBrowserBody} data-testid="structured-database-browser">
        <section className={styles.databaseSourceBar} aria-label="Structured database source">
          <div className={styles.databaseSourceIdentity}>
            {selectedSource ? (
              <Image src={DUCKDB_MARK.src} alt={DUCKDB_MARK.alt} width={34} height={34} />
            ) : null}
            <div>
              <small>Database</small>
              {sources.length > 1 ? (
                <SelectShell>
                  <select
                    aria-label="Structured database"
                    value={selectedSourceId}
                    onChange={(event) => setChosenSourceId(event.target.value)}
                  >
                    {sources.map((source) => (
                      <option key={source.id} value={source.id}>
                        {source.name || source.databaseName}
                      </option>
                    ))}
                  </select>
                </SelectShell>
              ) : (
                <strong>
                  {selectedSource
                    ? selectedSource.name || selectedSource.databaseName
                    : 'No source selected'}
                </strong>
              )}
            </div>
          </div>
          <div className={styles.databaseSourceBadges}>
            <span>Read only</span>
            {snapshot ? <span>{snapshot.provider}</span> : null}
          </div>
        </section>

        {!sources.length ? (
          <div className={styles.databaseBrowserStatus} role="status">
            No run-scoped structured database is available for this execution.
          </div>
        ) : loaded?.error ? (
          <div className={styles.databaseBrowserStatus} role="alert">
            <strong>Database structure unavailable.</strong>
            <span>{loaded.error}</span>
          </div>
        ) : !snapshot ? (
          <div className={styles.databaseBrowserStatus} role="status">
            Loading the read-only database structure…
          </div>
        ) : (
          <div className={styles.databaseBrowserLayout}>
            <aside className={styles.databaseTableRail} aria-label="Database tables">
              <header>
                <small>Physical schema</small>
                <strong>
                  {tables.length} table{tables.length === 1 ? '' : 's'}
                </strong>
              </header>
              <div>
                {tables.map((table) => (
                  <button
                    type="button"
                    key={table.qualifiedName}
                    aria-pressed={previewTable === table.qualifiedName}
                    onClick={() => setChosenTable(table.qualifiedName)}
                  >
                    <small>{table.schemaName}</small>
                    <strong>{table.name}</strong>
                    <span>
                      {table.columns.length} column{table.columns.length === 1 ? '' : 's'}
                    </span>
                  </button>
                ))}
              </div>
            </aside>

            <main className={styles.databaseTableDetail}>
              {queryOpened && (
                <div hidden={mode !== 'query'}>
                  <DatabaseQueryPanel
                    key={`${selectedSourceId}:${cursor}`}
                    sourceId={selectedSourceId}
                    queryRunner={queryRunner}
                    receipts={receipts.filter(
                      (receipt) => receipt.content?.databaseName === snapshot.databaseName
                    )}
                    initialReceiptId={
                      initialReceipt?.content?.databaseName === snapshot.databaseName
                        ? initialReceipt.receiptId
                        : undefined
                    }
                    defaultSql={
                      selectedTable
                        ? `SELECT * FROM ${quoted(selectedTable.schemaName)}.${quoted(selectedTable.name)} LIMIT 25`
                        : ''
                    }
                  />
                </div>
              )}
              {mode !== 'query' &&
                (selectedTable ? (
                  <>
                    <header>
                      <div>
                        <small>{selectedTable.schemaName}</small>
                        <h4>{selectedTable.name}</h4>
                      </div>
                      <span>{selectedTable.tableType}</span>
                    </header>

                    <section className={styles.databaseColumns} aria-label="Table columns">
                      <h5>Columns</h5>
                      <div>
                        {selectedTable.columns.map((column) => (
                          <article key={column.name}>
                            <strong>{column.name}</strong>
                            <span>{column.dataType}</span>
                            <div>
                              {column.primaryKey ? <b>PK</b> : null}
                              {column.nullable === false ? <b>Required</b> : null}
                            </div>
                          </article>
                        ))}
                      </div>
                    </section>

                    {relationships.length ? (
                      <section
                        className={styles.databaseRelationships}
                        aria-label="Table relationships"
                      >
                        <h5>Relationships</h5>
                        <div>
                          {relationships.map((relationship) => (
                            <span key={`${relationship.source}:${relationship.target}`}>
                              {relationship.source} → {relationship.target}
                            </span>
                          ))}
                        </div>
                      </section>
                    ) : null}

                    <section className={styles.databasePreview} aria-label="Sample rows">
                      <header>
                        <div>
                          <h5>Sample rows</h5>
                          <p>Bounded, read-only data from the selected physical table.</p>
                        </div>
                        {preview ? (
                          <span>
                            {preview.rows.length} row{preview.rows.length === 1 ? '' : 's'}
                          </span>
                        ) : null}
                      </header>
                      {previewLoaded?.error ? (
                        <div className={styles.databasePreviewStatus} role="alert">
                          {previewLoaded.error}
                        </div>
                      ) : !preview ? (
                        <div className={styles.databasePreviewStatus} role="status">
                          Loading bounded sample rows…
                        </div>
                      ) : (
                        <div
                          className={styles.databasePreviewScroll}
                          data-testid="database-sample-rows"
                        >
                          {preview.rows.length ? (
                            <table>
                              <thead>
                                <tr>
                                  {preview.columns.map((column) => (
                                    <th scope="col" key={column.name}>
                                      <span>{column.name}</span>
                                      {column.dataType ? <small>{column.dataType}</small> : null}
                                    </th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {preview.rows.map((row, rowIndex) => (
                                  <tr key={rowIndex}>
                                    {row.map((cell, cellIndex) => (
                                      <td key={cellIndex}>{String(cell ?? '—')}</td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          ) : (
                            <p>No rows were returned from this table.</p>
                          )}
                        </div>
                      )}
                    </section>
                  </>
                ) : (
                  <div className={styles.databaseBrowserStatus}>No tables are available.</div>
                ))}
            </main>
          </div>
        )}
      </div>
    </section>
  )
}

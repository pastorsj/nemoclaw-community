// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Read-only SQL over the structured source: an editor with SQL highlighting,
 * the run's Auto Ontology queries to start from, and the bounded result.
 * Cmd/Ctrl+Enter runs the query.
 */

'use client'

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Button, Select } from '@/adapters/ui'
import type { StructuredQueryReceipt } from '../contract'
import { runQuery, type QueryResult } from './database-client'
import styles from './database-query.module.css'

const NEW_QUERY_VALUE = '__new_query__'
const SQL_KEYWORDS = new Set(
  'ALL AND AS ASC BETWEEN BY CASE CAST CROSS DESC DISTINCT ELSE END EXCEPT FALSE FILTER FROM FULL GROUP HAVING IN INNER INTERSECT INTERVAL IS JOIN LEFT LIKE LIMIT NOT NULL OFFSET ON OR ORDER OUTER OVER PARTITION QUALIFY RANGE RIGHT ROWS SELECT THEN TRUE UNION USING WHEN WHERE WITH'.split(
    ' '
  )
)
const SQL_TOKEN_PATTERN =
  /(--[^\n]*|\/\*[\s\S]*?(?:\*\/|$)|'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_$]*\b|<>|!=|<=|>=|::|[(),.;+\-*/%=<>])/g

const tokenKind = (sql: string, token: string, index: number): string => {
  if (token.startsWith('--') || token.startsWith('/*')) return 'comment'
  if (token.startsWith("'")) return 'string'
  if (token.startsWith('"') || token.startsWith('`')) return 'identifier'
  if (/^\d/.test(token)) return 'number'
  if (SQL_KEYWORDS.has(token.toUpperCase())) return 'keyword'
  if (
    /^[A-Za-z_]/.test(token) &&
    sql
      .slice(index + token.length)
      .trimStart()
      .startsWith('(')
  ) {
    return 'function'
  }
  if (/^[<>!=:+\-*/%(),.;]$/.test(token) || /^(?:<>|!=|<=|>=|::)$/.test(token)) return 'operator'
  return ''
}

const highlightedSql = (sql: string): ReactNode[] => {
  const output: ReactNode[] = []
  let cursor = 0
  for (const match of sql.matchAll(SQL_TOKEN_PATTERN)) {
    const index = match.index ?? 0
    const token = match[0]
    if (index > cursor) output.push(sql.slice(cursor, index))
    const kind = tokenKind(sql, token, index)
    output.push(
      kind ? (
        <span key={`${index}-${token}`} data-sql-token={kind} className={styles[`token${kind}`]}>
          {token}
        </span>
      ) : (
        token
      )
    )
    cursor = index + token.length
  }
  if (cursor < sql.length) output.push(sql.slice(cursor))
  if (sql.endsWith('\n')) output.push(' ')
  return output
}

export const DatabaseQueryPanel = ({
  sourceId,
  receipts,
  initialReceiptId,
  defaultSql = '',
  queryRunner = runQuery,
}: {
  sourceId: string
  /** The run's Auto Ontology calls on this database */
  receipts: readonly StructuredQueryReceipt[]
  initialReceiptId?: string
  defaultSql?: string
  queryRunner?: typeof runQuery
}): ReactNode => {
  const calls = receipts.filter((receipt) => receipt.content?.sql)
  const sqlOf = (receiptId: string): string =>
    calls.find((receipt) => receipt.receiptId === receiptId)?.content?.sql || defaultSql
  const initial = calls.find((receipt) => receipt.receiptId === initialReceiptId)
  const [callId, setCallId] = useState(initial?.receiptId ?? '')
  const [sql, setSql] = useState(initial?.content?.sql || defaultSql)
  const [result, setResult] = useState<QueryResult | null>(null)
  const [error, setError] = useState('')
  const [running, setRunning] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const highlightLayer = useRef<HTMLPreElement | null>(null)
  useEffect(() => () => controller.current?.abort(), [])

  const run = async () => {
    if (running || !sql.trim()) return
    const request = new AbortController()
    controller.current = request
    setRunning(true)
    setError('')
    setResult(null)
    try {
      const response = await queryRunner(sourceId, sql, request.signal)
      if (!request.signal.aborted) setResult(response)
    } catch (failure) {
      if (!request.signal.aborted) {
        setError(failure instanceof Error ? failure.message : 'Query failed.')
      }
    } finally {
      if (!request.signal.aborted) setRunning(false)
    }
  }

  return (
    <section className={styles.panel} aria-label="SQL Query">
      <label className={styles.editorLabel} htmlFor="database-sql-editor">
        SQL
      </label>
      <div className={styles.editorShell}>
        <pre
          ref={highlightLayer}
          className={styles.syntaxLayer}
          data-testid="sql-syntax-highlight"
          aria-hidden="true"
        >
          {highlightedSql(sql)}
        </pre>
        <textarea
          id="database-sql-editor"
          className={styles.editorInput}
          aria-label="SQL query"
          spellCheck={false}
          wrap="off"
          value={sql}
          maxLength={20_000}
          readOnly={running}
          aria-busy={running}
          onScroll={(event) => {
            if (!highlightLayer.current) return
            highlightLayer.current.scrollTop = event.currentTarget.scrollTop
            highlightLayer.current.scrollLeft = event.currentTarget.scrollLeft
          }}
          onChange={(event) => {
            setSql(event.target.value)
            setResult(null)
            setError('')
          }}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
              event.preventDefault()
              void run()
            }
          }}
        />
      </div>
      <div className={styles.queryToolbar} data-testid="database-query-toolbar">
        {calls.length > 0 && (
          <div className={styles.querySelect}>
            <Select
              aria-label="SQL query source"
              size="small"
              side="bottom"
              triggerKind="flat"
              value={callId || NEW_QUERY_VALUE}
              disabled={running}
              onValueChange={(value) => {
                const id = value === NEW_QUERY_VALUE ? '' : value
                setCallId(id)
                setSql(sqlOf(id))
                setResult(null)
                setError('')
              }}
              items={[
                { value: NEW_QUERY_VALUE, children: 'New query' },
                ...calls.map((receipt, index) => ({
                  value: receipt.receiptId,
                  children: `Ontology call ${index + 1}${
                    receipt.content?.query ? ` · ${receipt.content.query.slice(0, 70)}` : ''
                  }`,
                })),
              ]}
            />
          </div>
        )}
        <Button
          kind="primary"
          size="small"
          className={styles.runButton}
          disabled={running || !sql.trim()}
          onClick={() => void run()}
        >
          {running ? 'Running…' : 'Run query'}
        </Button>
      </div>
      {error && <p role="alert">{error}</p>}
      <div className="sr-only" role="status" aria-live="polite">
        {running
          ? 'Executing SQL…'
          : result
            ? `${result.rows.length} row${result.rows.length === 1 ? '' : 's'} · ${result.durationMs} ms${result.truncated ? ` · More rows available; showing the first ${result.rows.length}` : ''}`
            : ''}
      </div>
      {result && (
        <div className={styles.results} role="region" aria-label="SQL results">
          <table>
            <thead>
              <tr>
                {result.columns.map((column, index) => (
                  <th key={index} scope="col">
                    {column.name}
                    {column.dataType ? <small>{column.dataType}</small> : null}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row, index) => (
                <tr key={index}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex}>{cell === null ? 'NULL' : String(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {!result.rows.length && <p>The query returned no rows.</p>}
        </div>
      )}
    </section>
  )
}

// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * How Auto Ontology turned the question into SQL, from its receipts: the
 * phrases it resolved, the ontology objects they matched, the tables and
 * columns behind them, and the SQL. Only observed `ask_question` receipts
 * are shown.
 */

'use client'

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { SelectShell } from '@/shared/components/SelectShell'
import type { StructuredQueryReceipt } from '../contract'
import shellStyles from '../execution-workspace.module.css'
import type { ExecutionNodeDetail } from '../graph'
import styles from './ontology-lineage-inspector.module.css'

export interface OntologyLineageInspectorProps {
  detail: ExecutionNodeDetail
  /** The event cursor at the replay position */
  cursor: string
  question?: string | null
  receipts: readonly StructuredQueryReceipt[]
  loading?: boolean
  /** The call a citation points at */
  preferredInvocationId?: string | null
  onClose: () => void
  /** Opens the call's SQL in the data viewer; offered only for `queryDatabases` */
  onOpenQuery?: (receipt: StructuredQueryReceipt) => void
  queryDatabases?: readonly string[]
}

const unique = (values: readonly string[]): string[] => [
  ...new Set(values.map((value) => value.trim()).filter(Boolean)),
]

const compact = (value: string, limit = 72): string =>
  value.length <= limit ? value : `${value.slice(0, Math.max(0, limit - 1)).trimEnd()}…`

const LineageStage = ({
  number,
  kicker,
  title,
  items = [],
  empty,
  code,
  state,
}: {
  number: number
  kicker: string
  title: string
  items?: string[]
  empty: string
  code?: string | null
  state: StructuredQueryReceipt['status'] | 'unavailable'
}): ReactNode => (
  <section className={styles.stage} data-state={state} aria-label={title}>
    <div className={styles.stageRail} aria-hidden="true">
      <span>{number}</span>
    </div>
    <header className={styles.stageHeading}>
      <small>{kicker}</small>
      <h4>{title}</h4>
    </header>
    <div className={styles.stageEvidence}>
      {code ? (
        <pre data-language="sql">
          <code>{code}</code>
        </pre>
      ) : items.length ? (
        <ul>
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : (
        <p>{empty}</p>
      )}
    </div>
  </section>
)

export const OntologyLineageInspector = ({
  detail,
  cursor,
  question,
  receipts,
  loading = false,
  preferredInvocationId,
  onClose,
  onOpenQuery,
  queryDatabases = [],
}: OntologyLineageInspectorProps): ReactNode => {
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const [chosenId, setChosenId] = useState<string | null>(null)

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

  // The chosen call, else the cited one, else the latest (a later call may repair an earlier one)
  const selectedReceipt =
    receipts.find((receipt) => receipt.receiptId === chosenId) ??
    receipts.find((receipt) => receipt.invocationId === preferredInvocationId) ??
    receipts.at(-1)
  const selectedIndex = selectedReceipt ? receipts.indexOf(selectedReceipt) : -1
  const content = selectedReceipt?.content ?? null
  const lineage = content?.resolutionLineage ?? []
  const owningQuestion = content?.query || question
  const mappings = unique(lineage.map((entry) => `${entry.phrase} → ${entry.ontologyObject}`))
  const ontologyMatches = unique(lineage.map((entry) => entry.ontologyObject))
  const physicalBindings = unique(
    lineage.map((entry) => (entry.column ? `${entry.table}.${entry.column}` : entry.table))
  )
  const sql = content?.sql?.trim() || null
  const state = selectedReceipt?.status ?? 'unavailable'

  return (
    <section
      className={shellStyles.capabilityExplorer}
      role="dialog"
      aria-modal="true"
      data-testid="capability-explorer"
      data-presentation="lineage"
      data-capability={detail.id}
      data-cursor={cursor}
      aria-label={`${detail.label} text-to-SQL details`}
    >
      <header className={shellStyles.capabilityExplorerHeader}>
        <div>
          <span className={shellStyles.eyebrow}>Observed text-to-SQL lineage</span>
          <h3>{detail.label}</h3>
          <p>See how the question resolved through governed meaning into physical data and SQL.</p>
        </div>
        <div className={shellStyles.capabilityExplorerHeaderActions}>
          <span className={shellStyles.capabilityCursor}>Replay step {cursor}</span>
          <button
            ref={closeButtonRef}
            type="button"
            className={shellStyles.iconButton}
            onClick={onClose}
            aria-label={`Close ${detail.label} text-to-SQL details`}
          >
            ×
          </button>
        </div>
      </header>

      <div className={styles.body} data-testid="ontology-lineage-inspector">
        {receipts.length > 1 ? (
          <div className={styles.callSelector}>
            <label htmlFor="ontology-lineage-call">Text-to-SQL call</label>
            <SelectShell>
              <select
                id="ontology-lineage-call"
                value={selectedReceipt?.receiptId ?? ''}
                onChange={(event) => setChosenId(event.target.value)}
              >
                {receipts.map((receipt, index) => {
                  const callQuestion = receipt.content?.query || question
                  return (
                    <option value={receipt.receiptId} key={receipt.receiptId}>
                      {`Call ${index + 1} of ${receipts.length}${callQuestion ? ` · ${compact(callQuestion)}` : ''}`}
                    </option>
                  )
                })}
              </select>
            </SelectShell>
          </div>
        ) : null}

        {!selectedReceipt && question ? (
          <section className={styles.question} aria-label="Original question">
            <div>
              <small>Original question</small>
              <p>{question}</p>
            </div>
            <span data-state="unavailable">observing</span>
          </section>
        ) : null}

        {selectedReceipt ? (
          <>
            <section className={styles.question} aria-label="Original question">
              <div>
                <small>
                  {receipts.length > 1
                    ? `Text-to-SQL call ${selectedIndex + 1} of ${receipts.length}`
                    : 'Original question'}
                </small>
                <p>{owningQuestion || 'The recorded call did not retain its owning question.'}</p>
              </div>
              <span data-state={selectedReceipt.status}>{selectedReceipt.status}</span>
            </section>

            <div className={styles.lineage} data-testid="ontology-lineage-stages">
              <LineageStage
                number={1}
                kicker="Business language"
                title="Resolved phrases"
                items={mappings}
                empty="Typed phrase-resolution lineage was not retained for this call."
                state={mappings.length ? state : 'unavailable'}
              />
              <LineageStage
                number={2}
                kicker="Auto Ontology"
                title="Ontology matches"
                items={ontologyMatches}
                empty="No display-safe ontology matches were retained for this call."
                state={ontologyMatches.length ? state : 'unavailable'}
              />
              <LineageStage
                number={3}
                kicker="Structured database"
                title="Tables, columns & joins"
                items={physicalBindings}
                empty="No display-safe physical bindings were retained for this call."
                state={physicalBindings.length ? state : 'unavailable'}
              />
              <LineageStage
                number={4}
                kicker="Generated query"
                title="Generated SQL"
                code={sql}
                empty="SQL text was not retained for this call."
                state={sql ? state : 'unavailable'}
              />
              {sql && content && onOpenQuery && queryDatabases.includes(content.databaseName) && (
                <button
                  type="button"
                  className={shellStyles.backButton}
                  onClick={() => onOpenQuery(selectedReceipt)}
                >
                  Open in Data Viewer
                </button>
              )}
            </div>
          </>
        ) : loading ? (
          <div className={styles.message} role="status">
            Loading the recorded text-to-SQL lineage…
          </div>
        ) : (
          <div className={styles.message}>
            <strong>No text-to-SQL call was retained at this replay step.</strong>
            <p>The ontology inspector only shows observed structured retrieval evidence.</p>
          </div>
        )}
      </div>
    </section>
  )
}

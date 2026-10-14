// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The recorded calls behind a node, one card each: what ran, its statement
 * (SQL, PQL, the search query) and its bounded result. Used for retrieval,
 * Auto Ontology SQL, Kumo predictions and the agent itself. A flat list, so
 * no second workflow graph is nested inside the execution graph.
 */

'use client'

import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import type { ReceiptV2 } from '../contract'
import styles from '../execution-workspace.module.css'
import type { ExecutionNodeDetail } from '../graph'
import { OPERATION_LABELS, receiptOutputCount, summarizeReceipt } from '../receipt-summary'
import { toolFor } from '../registry'
import { ResultOutput } from './ResultOutput'

export interface EvidenceInspectorProps {
  detail: ExecutionNodeDetail
  /** The event cursor at the replay position */
  cursor: string
  question?: string | null
  receipts: readonly ReceiptV2[]
  /** The pack's database, which a Kumo prediction reads but its receipt does not name */
  databaseName?: string
  /** The data sources the question used */
  sourceIds?: readonly string[]
  /** The pack's structured sources, which name the question's database source */
  structuredSources?: ReadonlyArray<{ id: string; name: string }>
  loading?: boolean
  onClose: () => void
}

const callLabel = (receipt: ReceiptV2): string => {
  switch (receipt.artifactKind) {
    case 'structured_query':
      return 'Auto Ontology Text-to-SQL'
    case 'structured_prediction':
      return 'NVIDIA Kumo Prediction'
    case 'analytics_result':
      return receipt.content
        ? OPERATION_LABELS[receipt.content.operationId]
        : (toolFor(receipt.toolName)?.label ?? receipt.toolName)
    case 'retrieval_evidence':
      return 'Unstructured Retrieval'
  }
}

/** The logical sources a receipt read: the retrieval sources, or the database it queried. */
const receiptSources = (receipt: ReceiptV2, databaseName: string | undefined): string[] => {
  switch (receipt.artifactKind) {
    case 'retrieval_evidence':
      return receipt.content?.sourceIds ?? []
    case 'analytics_result':
    case 'structured_query':
      return receipt.content ? [receipt.content.databaseName] : []
    case 'structured_prediction':
      return databaseName ? [databaseName] : []
  }
}

const NO_SOURCE_IDS: readonly string[] = []
const NO_STRUCTURED_SOURCES: ReadonlyArray<{ id: string; name: string }> = []

export const EvidenceInspector = ({
  detail,
  cursor,
  question,
  receipts,
  databaseName,
  sourceIds = NO_SOURCE_IDS,
  structuredSources = NO_STRUCTURED_SOURCES,
  loading = false,
  onClose,
}: EvidenceInspectorProps): ReactNode => {
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const summaries = useMemo(
    () =>
      receipts.map((receipt) => ({
        receipt,
        summary: summarizeReceipt(receipt, { databaseName }),
      })),
    [databaseName, receipts]
  )
  // As the original showed them: the question's structured source by name, then what each receipt read
  // (a retrieval source by name when it is a structured one, else by id).
  const sourceNames = useMemo(() => {
    const nameOf = (id: string) => structuredSources.find((source) => source.id === id)?.name || id
    return [
      ...new Set(
        [
          ...sourceIds.flatMap((id) =>
            structuredSources.some((source) => source.id === id) ? [nameOf(id)] : []
          ),
          ...receipts.flatMap((receipt) =>
            receipt.artifactKind === 'retrieval_evidence'
              ? receiptSources(receipt, databaseName).map(nameOf)
              : receiptSources(receipt, databaseName)
          ),
        ].filter((name) => name.trim())
      ),
    ]
  }, [databaseName, receipts, sourceIds, structuredSources])

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

  return (
    <section
      className={styles.capabilityExplorer}
      role="dialog"
      aria-modal="true"
      tabIndex={-1}
      data-testid="capability-explorer"
      data-presentation="evidence"
      data-capability={detail.id}
      data-cursor={cursor}
      aria-label={`${detail.label} execution details`}
    >
      <header className={styles.capabilityExplorerHeader}>
        <div>
          <span className={styles.eyebrow}>Observed execution details</span>
          <h3>{detail.label}</h3>
          <p>{detail.subtitle}. Queries and display-safe results are shown directly below.</p>
        </div>
        <div className={styles.capabilityExplorerHeaderActions}>
          <span className={styles.capabilityCursor}>Replay step {cursor}</span>
          <button
            ref={closeButtonRef}
            type="button"
            className={styles.iconButton}
            onClick={onClose}
            aria-label={`Close ${detail.label} execution details`}
          >
            ×
          </button>
        </div>
      </header>

      <div className={styles.evidenceInspectorBody} data-testid="execution-evidence-inspector">
        {question ? (
          <section className={styles.evidenceQuestion} aria-label="Original question">
            <small>Original question</small>
            <p>{question}</p>
          </section>
        ) : null}

        {sourceNames.length ? (
          <section className={styles.evidenceSources} aria-label="Sources used">
            <small>{sourceNames.length === 1 ? 'Source used' : 'Sources used'}</small>
            <div>
              {sourceNames.map((source) => (
                <span key={source}>{source}</span>
              ))}
            </div>
          </section>
        ) : null}

        {summaries.length ? (
          <div className={styles.evidenceCallList}>
            {summaries.map(({ receipt, summary }, index) => (
              <article
                className={styles.evidenceCall}
                key={receipt.receiptId}
                data-capability={receipt.artifactKind}
                data-testid="execution-evidence-call"
              >
                <header className={styles.evidenceCallHeader}>
                  <div>
                    <small>
                      {summaries.length > 1
                        ? `Recorded call ${index + 1} of ${summaries.length}`
                        : 'Recorded call'}
                    </small>
                    <h4>{callLabel(receipt)}</h4>
                  </div>
                  <span data-state={receipt.status}>{receipt.status}</span>
                </header>

                <div className={styles.evidenceCallSummary}>
                  <strong>{summary.title}</strong>
                  <p>{summary.summary}</p>
                  {summary.details.length ? (
                    <div>
                      {summary.details.map((detailText) => (
                        <span key={detailText}>{detailText}</span>
                      ))}
                    </div>
                  ) : null}
                  {summary.notices?.length ? (
                    <p className={styles.evidenceNotice}>
                      <b>Notice:</b> {summary.notices.join(' · ')}
                    </p>
                  ) : null}
                </div>

                {summary.statement ? (
                  <section className={styles.evidenceStatement}>
                    <h5>{summary.statement.label}</h5>
                    <pre data-language={summary.statement.language}>
                      <code>{summary.statement.value}</code>
                    </pre>
                    {summary.statement.truncated ? (
                      <small>The recorded statement reached its display-safe size limit.</small>
                    ) : null}
                  </section>
                ) : null}

                {summary.output ? (
                  <section className={styles.evidenceOutput}>
                    <header>
                      <h5>{summary.output.label}</h5>
                      <span>{receiptOutputCount(summary.output)}</span>
                    </header>
                    <ResultOutput output={summary.output} testId="execution-evidence-output" />
                    {summary.output.truncated ? (
                      <small>Showing the bounded result retained in the execution receipt.</small>
                    ) : null}
                  </section>
                ) : null}
              </article>
            ))}
          </div>
        ) : loading ? (
          <div className={styles.evidenceEmpty} role="status">
            Loading the recorded query and result…
          </div>
        ) : (
          <div className={styles.evidenceEmpty}>
            <strong>No display-safe query result was retained at this replay step.</strong>
            {detail.invocations.length ? (
              <ul>
                {detail.invocations.map((invocation) => (
                  <li key={invocation.invocationId}>
                    <span>{toolFor(invocation.name)?.label ?? invocation.name}</span>
                    <small>{invocation.status}</small>
                  </li>
                ))}
              </ul>
            ) : (
              <p>This capability was not invoked in the selected replay prefix.</p>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

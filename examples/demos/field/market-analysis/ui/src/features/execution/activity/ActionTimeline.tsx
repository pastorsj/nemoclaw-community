// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The finished run as a chart: summary tiles, the selected action's timing and
 * recorded result, and one row per action. Hover previews an action, a click
 * pins it, Escape or a click on the empty chart clears it.
 */

'use client'

import {
  type CSSProperties,
  type FC,
  type MouseEvent as ReactMouseEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { selectPackDatabaseName, useLayoutStore } from '@/features/layout/store'
import type { ReceiptV2 } from '../contract'
import { ResultOutput } from '../explorers/ResultOutput'
import { receiptOutputCount, summarizeReceipt, type ReceiptOutput } from '../receipt-summary'
import { formatTimelineDuration, type TimelineItem, type TimelineModel } from './activity-model'
import styles from './execution-timeline.module.css'

export interface ActionTimelineProps {
  model: TimelineModel
  /** The run's receipts by id */
  receipts: Readonly<Record<string, ReceiptV2>>
  /** Whether the receipts are still loading (a live run's export) */
  receiptsLoading: boolean
  /** A failed call's Phoenix span page, when there is one */
  spanUrl: (spanId: string | undefined) => string | null
}

const TICKS = [0, 0.25, 0.5, 0.75, 1] as const

const formatCount = (value: number | null): string =>
  value === null ? 'Unavailable' : new Intl.NumberFormat('en-US').format(value)

const timelineStyle = (item: TimelineItem, model: TimelineModel): CSSProperties => {
  const left = Math.min(100, Math.max(0, (item.startOffsetMs / model.durationMs) * 100))
  const duration = item.durationMs ?? 0
  const width = Math.min(100 - left, Math.max(0, (duration / model.durationMs) * 100))
  return {
    '--timeline-left': `${left}%`,
    '--timeline-width': `${width}%`,
  } as CSSProperties
}

const itemTitle = (item: TimelineItem): string => {
  const timing =
    item.timing === 'point'
      ? 'Timing unavailable'
      : `${formatTimelineDuration(item.durationMs)} duration`
  return `${item.label} · ${item.service} · ${timing} · ${item.status}`
}

/** The first row, or the first cells of it, on one line. */
const compactOutput = (output: ReceiptOutput): string => {
  if (output.kind === 'passages') {
    const passage = output.passages[0]
    return passage ? `${passage.source}: ${passage.excerpt}` : 'No passages returned.'
  }
  const row = output.rows[0]
  if (!row) return 'No rows returned.'
  return output.columns
    .slice(0, 4)
    .map((column, index) => `${column.replaceAll('_', ' ')}: ${row[index] ?? '—'}`)
    .join(' · ')
}

export const ActionTimeline: FC<ActionTimelineProps> = ({
  model,
  receipts,
  receiptsLoading,
  spanUrl,
}) => {
  const [hoveredId, setHoveredId] = useState<string | null>(null)
  const [pinnedId, setPinnedId] = useState<string | null>(null)
  const [selectionCleared, setSelectionCleared] = useState(false)
  const [expandedStatementKey, setExpandedStatementKey] = useState<string | null>(null)
  const [expandedOutputKey, setExpandedOutputKey] = useState<string | null>(null)
  const hoverClearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Hover is a temporary preview even after a click pins another action.
  const selectedId =
    hoveredId ?? pinnedId ?? (selectionCleared ? null : (model.items[0]?.id ?? null))
  const selectedItem = useMemo(
    () => model.items.find((item) => item.id === selectedId),
    [model.items, selectedId]
  )
  const selectedSpanUrl = selectedItem?.status === 'failed' ? spanUrl(selectedItem.spanId) : null
  const fallbackInvocationCount = model.items.filter((item) => item.invocationId).length
  const tokenBreakdown = [
    model.metrics.inputTokens === null ? null : `${formatCount(model.metrics.inputTokens)} input`,
    model.metrics.outputTokens === null
      ? null
      : `${formatCount(model.metrics.outputTokens)} output`,
    model.metrics.reasoningTokens === null
      ? null
      : `${formatCount(model.metrics.reasoningTokens)} reasoning`,
  ].filter((part): part is string => Boolean(part))

  const resultKey = selectedItem?.receiptId ?? ''
  const receipt = resultKey ? receipts[resultKey] : undefined
  const resultStatus: 'available' | 'loading' | 'error' = receipt
    ? 'available'
    : receiptsLoading
      ? 'loading'
      : 'error'
  const databaseName = useLayoutStore(selectPackDatabaseName)
  const summary = useMemo(
    () => (receipt ? summarizeReceipt(receipt, { databaseName }) : undefined),
    [databaseName, receipt]
  )
  const statementExpanded = Boolean(resultKey && expandedStatementKey === resultKey)
  const outputExpanded = Boolean(resultKey && expandedOutputKey === resultKey)
  const inspectorExpanded = Boolean(expandedStatementKey || expandedOutputKey)
  const selectedDomId = selectedItem?.id.replace(/[^A-Za-z0-9_-]/g, '-') ?? 'none'

  const cancelScheduledHoverClear = (): void => {
    if (hoverClearTimerRef.current === null) return
    clearTimeout(hoverClearTimerRef.current)
    hoverClearTimerRef.current = null
  }

  const scheduleHoverClear = (): void => {
    cancelScheduledHoverClear()
    // Keep a failed preview mounted while the pointer crosses from its narrow
    // chart bar to the detail card, whose Phoenix link would otherwise vanish.
    hoverClearTimerRef.current = setTimeout(() => {
      setHoveredId(null)
      hoverClearTimerRef.current = null
    }, 350)
  }

  useEffect(
    () => () => {
      if (hoverClearTimerRef.current !== null) clearTimeout(hoverClearTimerRef.current)
    },
    []
  )

  const clearSelection = (): void => {
    setHoveredId(null)
    setPinnedId(null)
    setSelectionCleared(true)
    setExpandedStatementKey(null)
    setExpandedOutputKey(null)
  }

  const clearSelectionFromChart = (event: ReactMouseEvent<HTMLDivElement>): void => {
    const target = event.target as HTMLElement
    if (target.closest('[data-timeline-action]')) return
    clearSelection()
  }

  return (
    <section
      className={styles.timeline}
      aria-label="Execution action timeline"
      data-testid="execution-action-timeline"
      onKeyDown={(event) => {
        if (event.key !== 'Escape' || !pinnedId) return
        event.stopPropagation()
        clearSelection()
      }}
    >
      <div className={styles.summary} aria-label="Timeline summary">
        <div>
          <small>End-to-end</small>
          <strong>
            {formatTimelineDuration(model.metrics.wallDurationMs ?? model.durationMs)}
          </strong>
          <span>{model.status === 'failed' ? 'Run ended with an error' : 'Completed run'}</span>
        </div>
        <div>
          <small>{model.metrics.toolCallCount === null ? 'Timed invocations' : 'Tool calls'}</small>
          <strong>{formatCount(model.metrics.toolCallCount ?? fallbackInvocationCount)}</strong>
          <span>
            {model.metrics.knownToolDurationMs === null
              ? `${fallbackInvocationCount} observed invocation(s)`
              : `${formatTimelineDuration(model.metrics.knownToolDurationMs)} cumulative tool time`}
          </span>
        </div>
        <div>
          <small>Token usage</small>
          <strong>{formatCount(model.metrics.totalTokens)}</strong>
          <span>
            {model.metrics.totalTokens === null || tokenBreakdown.length === 0
              ? 'Aggregate usage unavailable'
              : tokenBreakdown.join(' · ')}
          </span>
        </div>
      </div>

      {selectedItem ? (
        <div
          className={styles.actionDetails}
          aria-live="polite"
          data-testid="timeline-action-details"
          data-expanded={inspectorExpanded || undefined}
          onMouseEnter={cancelScheduledHoverClear}
          onMouseLeave={() => {
            cancelScheduledHoverClear()
            setHoveredId(null)
          }}
        >
          <div className={styles.actionIdentity}>
            <small>{selectedItem.service}</small>
            <strong>{selectedItem.label}</strong>
            <span>{selectedItem.description}</span>
            {selectedItem.status === 'failed' ? (
              <div className={styles.failureDetail} data-testid="timeline-failure-detail">
                <b>Failure details</b>
                <span>
                  {selectedItem.failureDetail || 'This observed activity ended with a failure.'}
                </span>
                {selectedSpanUrl ? (
                  <a
                    href={selectedSpanUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    data-testid="timeline-failure-phoenix-link"
                  >
                    Open failed span in Phoenix
                  </a>
                ) : null}
              </div>
            ) : null}
          </div>
          <dl>
            <div>
              <dt>Started after request</dt>
              <dd>{formatTimelineDuration(selectedItem.startOffsetMs)}</dd>
            </div>
            <div>
              <dt>Duration</dt>
              <dd>
                {selectedItem.timing === 'point'
                  ? 'Unavailable'
                  : formatTimelineDuration(selectedItem.durationMs)}
              </dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{selectedItem.status}</dd>
            </div>
          </dl>
          <div
            className={styles.resultDetails}
            data-testid="timeline-result-details"
            data-state={selectedItem.receiptId ? resultStatus : 'none'}
            data-expanded={inspectorExpanded || undefined}
          >
            <div className={styles.resultHeading}>
              <small>Tool result</small>
              <strong>
                {!selectedItem.receiptId
                  ? 'No separate result'
                  : resultStatus === 'loading'
                    ? 'Loading result…'
                    : resultStatus === 'error'
                      ? 'Result unavailable'
                      : summary?.title || 'Result recorded'}
              </strong>
            </div>
            {selectedItem.receiptId && summary ? (
              <div className={styles.resultBody}>
                <p>{summary.summary}</p>
                {summary.details.length ? <span>{summary.details.join(' · ')}</span> : null}
                {summary.notices?.length ? (
                  <span>
                    <b>Notice:</b> {summary.notices.join(' · ')}
                  </span>
                ) : null}
                {summary.statement ? (
                  <div
                    className={styles.resultSection}
                    data-expanded={statementExpanded || undefined}
                  >
                    <div className={styles.resultSectionHeading}>
                      <b>{summary.statement.label}</b>
                    </div>
                    <div className={styles.resultSectionContent}>
                      <pre
                        id={`timeline-statement-${selectedDomId}`}
                        className={statementExpanded ? styles.statementExpanded : undefined}
                        title={summary.statement.value}
                      >
                        <code>{summary.statement.value}</code>
                      </pre>
                      <button
                        type="button"
                        aria-expanded={statementExpanded}
                        aria-controls={`timeline-statement-${selectedDomId}`}
                        aria-label={`${statementExpanded ? 'Collapse' : 'Expand'} ${summary.statement.label}`}
                        onClick={() =>
                          setExpandedStatementKey((current) =>
                            current === resultKey ? null : resultKey
                          )
                        }
                      >
                        {statementExpanded ? 'Collapse' : 'Expand'}
                      </button>
                    </div>
                    {statementExpanded && summary.statement.truncated ? (
                      <small>The recorded statement reached its display-safe size limit.</small>
                    ) : null}
                  </div>
                ) : null}
                {summary.output ? (
                  <div className={styles.resultSection} data-expanded={outputExpanded || undefined}>
                    <div className={styles.resultSectionHeading}>
                      <b>{summary.output.label}</b>
                      <span>{receiptOutputCount(summary.output)}</span>
                    </div>
                    <div className={styles.resultSectionContent}>
                      <div id={`timeline-output-${selectedDomId}`}>
                        {outputExpanded ? (
                          <ResultOutput output={summary.output} testId="timeline-result-output" />
                        ) : (
                          <span className={styles.compactOutput}>
                            {compactOutput(summary.output)}
                          </span>
                        )}
                      </div>
                      <button
                        type="button"
                        aria-expanded={outputExpanded}
                        aria-controls={`timeline-output-${selectedDomId}`}
                        aria-label={`${outputExpanded ? 'Collapse' : 'Expand'} ${summary.output.label}`}
                        onClick={() =>
                          setExpandedOutputKey((current) =>
                            current === resultKey ? null : resultKey
                          )
                        }
                      >
                        {outputExpanded ? 'Collapse' : 'Expand'}
                      </button>
                    </div>
                    {outputExpanded && summary.output.truncated ? (
                      <small>Showing the bounded result retained in the execution receipt.</small>
                    ) : null}
                  </div>
                ) : null}
              </div>
            ) : selectedItem.receiptId && resultStatus === 'error' ? (
              <p className={styles.resultNotice}>
                This action has no available display-safe result summary.
              </p>
            ) : !selectedItem.receiptId ? (
              <p className={styles.resultNotice}>
                This action does not produce a separate inspectable tool result.
              </p>
            ) : null}
          </div>
        </div>
      ) : (
        <div
          className={`${styles.actionDetails} ${styles.emptyActionDetails}`}
          data-testid="timeline-action-details"
          data-empty="true"
        >
          <p>Select an action to inspect its timing, generated query, and recorded result.</p>
        </div>
      )}

      <div
        className={styles.chartScroll}
        data-testid="execution-timeline-scroll"
        onClick={clearSelectionFromChart}
      >
        <div className={styles.chart}>
          <div className={styles.axisRow}>
            <div className={styles.axisLabel}>Action</div>
            <div className={styles.axis} aria-hidden="true">
              {TICKS.map((tick) => (
                <span
                  key={tick}
                  data-edge={tick === 0 ? 'start' : tick === 1 ? 'end' : undefined}
                  style={{ left: `${tick * 100}%` }}
                >
                  {formatTimelineDuration(model.durationMs * tick)}
                </span>
              ))}
            </div>
          </div>

          <div className={styles.rows} role="list" aria-label="Observed execution actions">
            {model.items.map((item) => {
              const selected = item.id === selectedItem?.id
              const title = itemTitle(item)
              return (
                <div
                  key={item.id}
                  className={styles.row}
                  role="listitem"
                  data-status={item.status}
                  data-selected={selected || undefined}
                  data-timeline-item={item.id}
                  data-start-ms={item.startMs}
                  data-end-ms={item.endMs}
                >
                  <div className={styles.rowLabel}>
                    <strong>{item.label}</strong>
                    <span>{item.service}</span>
                  </div>
                  <div className={styles.track}>
                    <button
                      type="button"
                      className={item.kind === 'milestone' ? styles.milestone : styles.bar}
                      data-component={item.component}
                      data-status={item.status}
                      data-timing={item.timing}
                      data-timeline-action="true"
                      style={timelineStyle(item, model)}
                      aria-label={title}
                      aria-pressed={pinnedId === item.id}
                      title={title}
                      onMouseEnter={() => {
                        cancelScheduledHoverClear()
                        if (!inspectorExpanded) {
                          setSelectionCleared(false)
                          setHoveredId(item.id)
                        }
                      }}
                      onMouseLeave={() => {
                        if (item.status === 'failed') scheduleHoverClear()
                        else setHoveredId(null)
                      }}
                      onFocus={() => {
                        cancelScheduledHoverClear()
                        if (!inspectorExpanded) {
                          setSelectionCleared(false)
                          setHoveredId(item.id)
                        }
                      }}
                      onBlur={() => setHoveredId(null)}
                      onClick={() => {
                        setSelectionCleared(false)
                        setPinnedId((current) => (current === item.id ? null : item.id))
                        setExpandedStatementKey(null)
                        setExpandedOutputKey(null)
                      }}
                    >
                      <span className="sr-only">{title}</span>
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </section>
  )
}

// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The explorer of a market analytics node: its header, a selector over the
 * node's calls, and the chosen call's receipt and result. A dialog over the
 * graph: Escape closes it and Tab stays inside it.
 */

'use client'

import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { SelectShell } from '@/shared/components/SelectShell'
import type { AnalyticsResultReceipt } from '../contract'
import styles from '../execution-workspace.module.css'
import { gpuAccelerationForReceipt } from '../graph'
import { MarketToolInspector } from './MarketToolInspector'

export interface MarketToolExplorerProps {
  nodeId: string
  title: string
  /** The event cursor at the replay position */
  cursor: string
  receipts: readonly AnalyticsResultReceipt[]
  /** The call a citation points at; else the first GPU call, else the first */
  preferredInvocationId?: string | null
  loading?: boolean
  onClose: () => void
}

const SUBTITLE =
  'See what ran, where it ran, how long it took, and the display-safe result returned by each call.'

const FOCUSABLE_SELECTOR =
  'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'

const focusableWithin = (element: HTMLElement | null): HTMLElement[] =>
  element
    ? [...element.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)].filter(
        (candidate) =>
          !candidate.hasAttribute('inert') && candidate.getAttribute('aria-hidden') !== 'true'
      )
    : []

const initialReceipt = (
  receipts: readonly AnalyticsResultReceipt[],
  preferredInvocationId: string | null | undefined
): AnalyticsResultReceipt | undefined =>
  receipts.find((receipt) => receipt.invocationId === preferredInvocationId) ??
  receipts.find((receipt) => gpuAccelerationForReceipt(receipt)) ??
  receipts[0]

export const MarketToolExplorer = ({
  nodeId,
  title,
  cursor,
  receipts,
  preferredInvocationId,
  loading = false,
  onClose,
}: MarketToolExplorerProps): ReactNode => {
  const explorerRef = useRef<HTMLElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const selectId = `${useId().replace(/[^a-zA-Z0-9_-]/g, '')}-view-select`
  const [chosenId, setChosenId] = useState<string | null>(null)
  const receipt =
    receipts.find((candidate) => candidate.receiptId === chosenId) ??
    initialReceipt(receipts, preferredInvocationId) ??
    null
  const viewLabel = (index: number): string => `${title} Call ${index + 1}`
  const selectorLabel = `${title} call`

  useEffect(() => {
    closeButtonRef.current?.focus()
  }, [nodeId])

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopImmediatePropagation()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const dialog = explorerRef.current
      const focusable = focusableWithin(dialog)
      if (!focusable.length) {
        event.preventDefault()
        dialog?.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !dialog?.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !dialog?.contains(active))) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  return (
    <section
      ref={explorerRef}
      className={styles.capabilityExplorer}
      role="dialog"
      aria-modal="true"
      tabIndex={-1}
      data-testid="capability-explorer"
      data-capability={nodeId}
      data-cursor={cursor}
      aria-label={`${title} explorer`}
    >
      <header className={styles.capabilityExplorerHeader}>
        <div>
          <span className={styles.eyebrow}>Market Analytics</span>
          <h3>{title}</h3>
          <p>{loading ? `${SUBTITLE} Loading display-safe receipts…` : SUBTITLE}</p>
        </div>
        <div className={styles.capabilityExplorerHeaderActions}>
          <span className={styles.capabilityCursor}>Replay cursor {cursor}</span>
          <button
            ref={closeButtonRef}
            type="button"
            className={styles.iconButton}
            onClick={onClose}
            aria-label={`Close ${title} explorer`}
          >
            ×
          </button>
        </div>
      </header>

      {receipts.length > 1 ? (
        <label className={styles.capabilityViewSelector} htmlFor={selectId}>
          <span>{selectorLabel}</span>
          <SelectShell>
            <select
              id={selectId}
              value={receipt?.receiptId ?? ''}
              aria-label={selectorLabel}
              onChange={(event) => setChosenId(event.currentTarget.value)}
            >
              {receipts.map((candidate, index) => (
                <option key={candidate.receiptId} value={candidate.receiptId}>
                  {viewLabel(index)}
                </option>
              ))}
            </select>
          </SelectShell>
          <small>
            {receipt ? receipts.indexOf(receipt) + 1 : 1} of {receipts.length} available
          </small>
        </label>
      ) : (
        <div className={styles.capabilityViewSelector} data-testid="capability-view-value">
          <span>{selectorLabel}</span>
          <strong className={styles.capabilityViewValue}>
            {receipt ? viewLabel(0) : `No ${title} call yet`}
          </strong>
          <small>1 call available</small>
        </div>
      )}

      <div
        className={styles.capabilityExplorerBody}
        role="region"
        aria-label={`${title} Receipt`}
        data-focused
        data-market-tool
      >
        <MarketToolInspector toolTitle={title} receipt={receipt} />
      </div>
    </section>
  )
}

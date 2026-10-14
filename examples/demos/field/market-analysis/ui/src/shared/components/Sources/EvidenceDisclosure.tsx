// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client'

import { type ReactNode, useState } from 'react'
import type { SourceEvidence, SourceRef } from './types'

interface EvidenceDisclosureProps {
  source: SourceRef & { evidence: SourceEvidence }
  children: ReactNode
  /** Opens the evidence in the execution view; omitted when there is no execution view */
  onOpen?: (evidence: SourceEvidence) => void
}

/**
 * Compact, native disclosure for evidence captured during the run. The
 * canonical identifiers stay available without leaking into the answer's
 * human-readable source list.
 */
export function EvidenceDisclosure({
  source,
  children,
  onOpen,
}: EvidenceDisclosureProps): ReactNode {
  const [isOpen, setIsOpen] = useState(false)

  return (
    <details className="min-w-0 flex-1" onToggle={(e) => setIsOpen(e.currentTarget.open)}>
      <summary className="flex cursor-pointer list-none items-baseline gap-2 rounded-sm outline-none transition-colors hover:text-[color:var(--color-green-500)] focus-visible:ring-2 focus-visible:ring-[color:var(--color-green-500)] [&::-webkit-details-marker]:hidden">
        {children}
        <span className="text-subtle shrink-0 text-[0.65rem] font-medium uppercase tracking-wide">
          {isOpen ? 'Close' : 'Inspect'}
        </span>
      </summary>
      <div className="bg-surface-sunken border-base mt-1.5 rounded-md border px-3 py-2 text-xs">
        <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1">
          <dt className="text-subtle">Source</dt>
          <dd className="text-primary min-w-0 break-words">{source.title}</dd>
          <dt className="text-subtle">Evidence</dt>
          <dd className="text-primary min-w-0 break-all font-mono">
            {source.evidence.referenceId}
          </dd>
        </dl>
        {onOpen ? (
          <button
            type="button"
            className="mt-2 cursor-pointer font-medium text-[color:var(--color-green-500)] hover:underline"
            onClick={() => onOpen(source.evidence)}
          >
            Open this run in the execution view
          </button>
        ) : null}
      </div>
    </details>
  )
}

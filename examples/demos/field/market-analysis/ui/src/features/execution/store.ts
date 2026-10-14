// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Execution runs by job id: the `execution.v2` events in stream order and the
 * receipts they reference. Live SSE records, job exports and recorded turns
 * all land here. Nothing is persisted; a reload refetches the export (or the
 * recording).
 */

import { create } from 'zustand'
import {
  toBenchmark,
  toExecutionEvent,
  toReceipt,
  toRetrievalBenchmark,
  type Benchmark,
  type ExecutionEventV2,
  type ReceiptV2,
  type RetrievalBenchmark,
} from './contract'

/** Events kept per run. The stream replays from the start, so it can only repeat, not grow. */
const MAX_EVENTS = 5000

export interface ExecutionRun {
  jobId: string
  /** Unique by eventId, in the order the stream sent them */
  events: ExecutionEventV2[]
  /** By receiptId */
  receipts: Record<string, ReceiptV2>
  /** The job's latest status (`running`, `success`, `failure`, `interrupted`, …); null until known */
  jobStatus: string | null
  /** Evidence (receipt) ids the answer cites; null until its report is known */
  citedEvidenceIds: string[] | null
  /** The CPU/GPU comparison of its market calls, once one has run (the Benchmark tab) */
  benchmark: Benchmark | null
  /** The Milvus CPU/GPU index comparison that applies to its retrieval calls, once known */
  retrievalBenchmark: RetrievalBenchmark | null
  /** From the data pack's recordings: no live job behind it, in either mode */
  recorded: boolean
  /** The recordings archive it came from (`archiveId`), when known */
  archive: string | null
}

/** A job's execution record as the API exports it and a recording stores it. */
export interface ExecutionRecord {
  jobId: string
  events: unknown[]
  receipts: unknown[]
  status?: string
  report?: { citations: unknown[] } | null
  benchmark?: unknown
  retrievalBenchmark?: unknown
  /** Loaded from the data pack's recordings */
  recorded?: boolean
  /** The recordings archive it came from */
  archive?: string | null
}

interface ExecutionState {
  runs: Record<string, ExecutionRun>
  /** Records that failed the contract guard; they are never shown. */
  dropped: number
  addEvent: (jobId: string, value: unknown, cursor?: string | null) => void
  addRecord: (record: ExecutionRecord) => void
  setJobStatus: (jobId: string, status: string) => void
  setBenchmark: (jobId: string, benchmark: Benchmark) => void
  setRetrievalBenchmark: (jobId: string, benchmark: RetrievalBenchmark) => void
}

const emptyRun = (jobId: string): ExecutionRun => ({
  jobId,
  events: [],
  receipts: {},
  jobStatus: null,
  citedEvidenceIds: null,
  benchmark: null,
  retrievalBenchmark: null,
  recorded: false,
  archive: null,
})

/** The evidence ids of a report's citations (`{evidenceId, …}`), each once. */
const citedEvidence = (report: ExecutionRecord['report']): string[] | null =>
  report
    ? [
        ...new Set(
          report.citations.flatMap((citation) =>
            typeof citation === 'object' &&
            citation !== null &&
            'evidenceId' in citation &&
            typeof citation.evidenceId === 'string'
              ? [citation.evidenceId]
              : []
          )
        ),
      ]
    : null

const merge = (
  run: ExecutionRun,
  events: ExecutionEventV2[],
  receipts: ReceiptV2[]
): ExecutionRun => {
  const seen = new Set(run.events.map((event) => event.eventId))
  const fresh: ExecutionEventV2[] = []
  for (const event of events) {
    if (seen.has(event.eventId)) continue
    seen.add(event.eventId)
    fresh.push(event)
  }
  return {
    ...run,
    events: fresh.length ? [...run.events, ...fresh].slice(0, MAX_EVENTS) : run.events,
    receipts: receipts.length
      ? { ...run.receipts, ...Object.fromEntries(receipts.map((r) => [r.receiptId, r])) }
      : run.receipts,
  }
}

export const useExecutionStore = create<ExecutionState>()((set) => ({
  runs: {},
  dropped: 0,

  addEvent: (jobId, value, cursor = null) => {
    const event = toExecutionEvent(value, cursor)
    set((state) =>
      event
        ? {
            runs: {
              ...state.runs,
              [jobId]: merge(state.runs[jobId] ?? emptyRun(jobId), [event], []),
            },
          }
        : { dropped: state.dropped + 1 }
    )
  },

  addRecord: ({
    jobId,
    events,
    receipts,
    status,
    report,
    benchmark,
    retrievalBenchmark,
    recorded,
    archive,
  }) => {
    const validEvents = events.map((event) => toExecutionEvent(event)).filter((e) => e !== null)
    const validReceipts = receipts.map(toReceipt).filter((r) => r !== null)
    const dropped = events.length + receipts.length - validEvents.length - validReceipts.length
    set((state) => {
      const run = merge(state.runs[jobId] ?? emptyRun(jobId), validEvents, validReceipts)
      return {
        runs: {
          ...state.runs,
          [jobId]: {
            ...run,
            jobStatus: status ?? run.jobStatus,
            citedEvidenceIds: citedEvidence(report) ?? run.citedEvidenceIds,
            benchmark: toBenchmark(benchmark) ?? run.benchmark,
            retrievalBenchmark: toRetrievalBenchmark(retrievalBenchmark) ?? run.retrievalBenchmark,
            recorded: recorded ?? run.recorded,
            archive: archive ?? run.archive,
          },
        },
        dropped: state.dropped + dropped,
      }
    })
  },

  setBenchmark: (jobId, benchmark) =>
    set((state) => ({
      runs: {
        ...state.runs,
        [jobId]: { ...(state.runs[jobId] ?? emptyRun(jobId)), benchmark },
      },
    })),

  setRetrievalBenchmark: (jobId, retrievalBenchmark) =>
    set((state) => ({
      runs: {
        ...state.runs,
        [jobId]: { ...(state.runs[jobId] ?? emptyRun(jobId)), retrievalBenchmark },
      },
    })),

  setJobStatus: (jobId, status) =>
    set((state) => ({
      runs: {
        ...state.runs,
        [jobId]: { ...(state.runs[jobId] ?? emptyRun(jobId)), jobStatus: status },
      },
    })),
}))

export const useExecutionRun = (jobId: string | null): ExecutionRun | undefined =>
  useExecutionStore((state) => (jobId ? state.runs[jobId] : undefined))

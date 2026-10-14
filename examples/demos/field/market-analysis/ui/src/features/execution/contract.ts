// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The execution contract, as the UI sees it.
 *
 * The types are generated from the API's Pydantic models (`ui/src/generated`).
 * The API validates and bounds every event and receipt before it stores them,
 * so the UI only checks the shape it relies on and drops anything else.
 */

import type { Benchmark } from '@/generated/benchmark'
import type { ExecutionEventV2 } from '@/generated/execution-event'
import type { ReceiptV2 } from '@/generated/receipt'
import type { RetrievalBenchmark } from '@/generated/retrieval-benchmark'

export type { Benchmark, BenchmarkStage, EngineTrials } from '@/generated/benchmark'
export type {
  RetrievalBackend,
  RetrievalBenchmark,
  RetrievalProfile,
} from '@/generated/retrieval-benchmark'
export type { ExecutionEventV2 } from '@/generated/execution-event'
export type {
  AnalyticsResult,
  AnalyticsResultReceipt,
  ReceiptV2,
  RetrievalEvidence,
  RetrievalEvidenceReceipt,
  StructuredPrediction,
  StructuredPredictionReceipt,
  StructuredQuery,
  StructuredQueryReceipt,
} from '@/generated/receipt'

export type ArtifactKind = ReceiptV2['artifactKind']
export type EventState = ExecutionEventV2['state']

// Records keyed by the generated unions, so TypeScript flags a new state or
// receipt kind here too; otherwise its events or receipts would be dropped.
const EVENT_STATES: readonly string[] = Object.keys({
  started: true,
  progress: true,
  completed: true,
  failed: true,
  cancelled: true,
} satisfies Record<EventState, true>)
const ARTIFACT_KINDS: readonly string[] = Object.keys({
  retrieval_evidence: true,
  analytics_result: true,
  structured_query: true,
  structured_prediction: true,
} satisfies Record<ArtifactKind, true>)

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const isString = (value: unknown): value is string => typeof value === 'string' && value !== ''

/**
 * Returns the event when `value` is an `execution.v2` event, else null.
 * The SSE frame carries the cursor as its `id`, not in the data; pass it as `cursor`.
 */
export const toExecutionEvent = (
  value: unknown,
  cursor: string | null = null
): ExecutionEventV2 | null => {
  if (!isRecord(value) || value.schemaVersion !== '2') return null
  const { display, provenance } = value
  const valid =
    isString(value.eventId) &&
    isString(value.jobId) &&
    isString(value.eventKind) &&
    isString(value.componentId) &&
    isString(value.occurredAt) &&
    EVENT_STATES.includes(value.state as string) &&
    Array.isArray(value.artifactRefs) &&
    isRecord(display) &&
    typeof display.label === 'string' &&
    isRecord(display.attributes) &&
    isRecord(provenance)
  if (!valid) return null
  const ownCursor = typeof value.cursor === 'number' ? value.cursor : null
  return {
    ...(value as unknown as ExecutionEventV2),
    cursor: ownCursor ?? (cursor === null ? null : Number(cursor)),
  }
}

/** Returns the receipt when `value` is a `ReceiptV2`, else null. */
export const toReceipt = (value: unknown): ReceiptV2 | null => {
  if (!isRecord(value) || value.schemaVersion !== '2') return null
  const valid =
    ARTIFACT_KINDS.includes(value.artifactKind as string) &&
    isString(value.receiptId) &&
    isString(value.jobId) &&
    isString(value.invocationId) &&
    isString(value.toolName) &&
    (value.status === 'completed' || value.status === 'failed') &&
    (value.content === null || isRecord(value.content))
  return valid ? (value as unknown as ReceiptV2) : null
}

const isEngineTrials = (value: unknown): boolean =>
  value === null ||
  (isRecord(value) &&
    (value.device === 'cpu' || value.device === 'gpu') &&
    typeof value.library === 'string' &&
    Array.isArray(value.trialsMs) &&
    value.trialsMs.every((ms) => typeof ms === 'number' && Number.isFinite(ms)))

/** Returns the comparison when `value` is a `Benchmark`, else null. */
export const toBenchmark = (value: unknown): Benchmark | null => {
  if (!isRecord(value) || value.schemaVersion !== '1') return null
  const valid =
    isString(value.jobId) &&
    (value.status === 'completed' || value.status === 'unavailable' || value.status === 'failed') &&
    Array.isArray(value.stages) &&
    value.stages.every(
      (stage) =>
        isRecord(stage) &&
        isString(stage.invocationId) &&
        isString(stage.toolName) &&
        isString(stage.outcome) &&
        typeof stage.qualified === 'boolean' &&
        isEngineTrials(stage.cpu) &&
        isEngineTrials(stage.gpu)
    )
  return valid ? (value as unknown as Benchmark) : null
}

const isNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value)

const isRetrievalBackend = (value: unknown, role: 'cpu' | 'gpu'): boolean =>
  isRecord(value) &&
  value.role === role &&
  isString(value.indexType) &&
  (value.status === 'completed' || value.status === 'failed') &&
  [value.p50Ms, value.p95Ms, value.vectorSearchMs, value.vectorQps].every(isNumber)

/** Returns the Milvus index comparison when `value` is a `RetrievalBenchmark`, else null. */
export const toRetrievalBenchmark = (value: unknown): RetrievalBenchmark | null => {
  if (!isRecord(value) || value.schemaVersion !== '1') return null
  const valid =
    isString(value.collectionVersion) &&
    isNumber(value.queryCount) &&
    Array.isArray(value.profiles) &&
    value.profiles.length > 0 &&
    value.profiles.every(
      (profile) =>
        isRecord(profile) &&
        isString(profile.profileId) &&
        isString(profile.executionMode) &&
        isNumber(profile.topK) &&
        isRetrievalBackend(profile.cpu, 'cpu') &&
        isRetrievalBackend(profile.gpu, 'gpu') &&
        isRecord(profile.quality) &&
        typeof profile.quality.passed === 'boolean' &&
        isRecord(profile.claim) &&
        isString(profile.claim.decision)
    )
  return valid ? (value as unknown as RetrievalBenchmark) : null
}

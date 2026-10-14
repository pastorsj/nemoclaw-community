// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Reduces a run's `execution.v2` events to what the views show: the run
 * status, one entry per tool call and one per model call. The same events
 * always give the same projection, so replay at a cursor is just the
 * projection of the events before it.
 *
 * The API can end a job without a `run.*` event (a cancel, the deadline, an
 * API restart), and can fail a job whose run completed (its receipts never
 * arrived). So a final job status ends a run its events left open, and a
 * failed job always fails it.
 */

import type { ExecutionEventV2 } from './contract'
import { formatCount, plural } from './format'
import { toolFor, type Family, type Tool } from './registry'

export type RunStatus = 'waiting' | 'running' | 'completed' | 'failed' | 'cancelled'
export type CallState = 'running' | 'completed' | 'failed'
export type Tier = 'efficient' | 'capable'

export interface ToolCall {
  invocationId: string
  /** The MCP tool name the event reported */
  name: string
  /** Its registry entry; undefined for a tool the registry does not know */
  tool: Tool | undefined
  label: string
  family: Family | null
  state: CallState
  startedAt: string
  endedAt: string | null
  /** Receipts (evidence) the call produced */
  receiptIds: string[]
}

/** One model call routed by Switchyard (`llm.call`). */
export interface ModelCall {
  invocationId: string
  servedModel: string | null
  tier: Tier | null
  inputTokens: number | null
  outputTokens: number | null
  occurredAt: string
}

export interface RunProjection {
  status: RunStatus
  startedAt: string | null
  endedAt: string | null
  toolCalls: ToolCall[]
  modelCalls: ModelCall[]
  inputTokens: number
  outputTokens: number
}

const RUN_STATUS: Record<ExecutionEventV2['state'], RunStatus> = {
  started: 'running',
  progress: 'running',
  completed: 'completed',
  failed: 'failed',
  cancelled: 'cancelled',
}

/** Final job statuses (`GET /v1/jobs/async/job/{id}`) as run statuses. */
const JOB_END = new Map<string, RunStatus>([
  ['success', 'completed'],
  ['failure', 'failed'],
  ['interrupted', 'cancelled'],
])

export const runEnded = (status: RunStatus): boolean => status !== 'waiting' && status !== 'running'

const numberOrNull = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null

const callState = (event: ExecutionEventV2): CallState => {
  if (event.state === 'failed' || event.state === 'cancelled') return 'failed'
  if (event.state !== 'completed') return 'running'
  return event.display.attributes.reported_error === true ? 'failed' : 'completed'
}

const newToolCall = (event: ExecutionEventV2, invocationId: string): ToolCall => {
  const tool = toolFor(event.toolName)
  return {
    invocationId,
    name: event.toolName ?? 'unknown',
    tool,
    label: tool?.label ?? event.toolName ?? event.display.label,
    family: tool?.family ?? null,
    state: 'running',
    startedAt: event.occurredAt,
    endedAt: null,
    receiptIds: [],
  }
}

/** `jobStatus` is the job's own status (`success`, `failure`, …), when known. */
export const projectRun = (
  events: readonly ExecutionEventV2[],
  jobStatus: string | null = null
): RunProjection => {
  const projection: RunProjection = {
    status: events.length ? 'running' : 'waiting',
    startedAt: events[0]?.occurredAt ?? null,
    endedAt: null,
    toolCalls: [],
    modelCalls: [],
    inputTokens: 0,
    outputTokens: 0,
  }
  const calls = new Map<string, ToolCall>()
  let runTokens: { input: number | null; output: number | null } | null = null

  for (const event of events) {
    const kind = event.eventKind
    const attributes = event.display.attributes
    const id = event.invocationId ?? event.eventId

    if (kind.startsWith('run.')) {
      projection.status = RUN_STATUS[event.state]
      projection.endedAt = projection.status === 'running' ? null : event.occurredAt
      if (event.state === 'completed') {
        runTokens = {
          input: numberOrNull(attributes.input_tokens),
          output: numberOrNull(attributes.output_tokens),
        }
      }
    } else if (kind === 'llm.call') {
      projection.modelCalls.push({
        invocationId: id,
        servedModel: attributes.served_model ?? null,
        tier: attributes.tier ?? null,
        inputTokens: numberOrNull(attributes.input_tokens),
        outputTokens: numberOrNull(attributes.output_tokens),
        occurredAt: event.occurredAt,
      })
    } else if (kind.startsWith('tool.') || kind.startsWith('artifact.')) {
      const call = calls.get(id) ?? newToolCall(event, id)
      calls.set(id, call)
      if (kind.startsWith('tool.')) {
        // A failed receipt (`tool.observed`) outlives the call's own `tool.completed`
        if (call.state !== 'failed') call.state = callState(event)
        call.endedAt = call.state === 'running' ? null : event.occurredAt
      }
      for (const ref of event.artifactRefs) {
        if (!call.receiptIds.includes(ref)) call.receiptIds.push(ref)
      }
    }
  }

  const jobEnd = jobStatus ? JOB_END.get(jobStatus) : undefined
  if (jobEnd && (!runEnded(projection.status) || jobEnd === 'failed')) {
    projection.status = jobEnd
    projection.endedAt = events.at(-1)?.occurredAt ?? null
  }
  projection.toolCalls = [...calls.values()]
  // A call cut off by the end of the run did not finish
  if (runEnded(projection.status)) {
    for (const call of projection.toolCalls.filter((call) => call.state === 'running')) {
      call.state = 'failed'
      call.endedAt = projection.endedAt
    }
  }
  const sum = (pick: (call: ModelCall) => number | null) =>
    projection.modelCalls.reduce((total, call) => total + (pick(call) ?? 0), 0)
  projection.inputTokens = runTokens?.input ?? sum((call) => call.inputTokens)
  projection.outputTokens = runTokens?.output ?? sum((call) => call.outputTokens)
  return projection
}

/** One line for headers: "completed · 2 tool calls · 3 model calls · 9,532 tokens". */
export const describeRun = (run: RunProjection): string =>
  [
    run.status,
    plural(run.toolCalls.length, 'tool call'),
    plural(run.modelCalls.length, 'model call'),
    run.inputTokens + run.outputTokens > 0 &&
      `${formatCount(run.inputTokens + run.outputTokens)} tokens`,
  ]
    .filter(Boolean)
    .join(' · ')

/** What the API records when it publishes the answer (`report.metrics`), for runs recorded with it. */
export interface PublishedMetrics {
  /** The model the run asked Hermes for */
  runtimeProfile: string | null
  wallDurationMs: number | null
  toolCallCount: number | null
  knownToolDurationMs: number | null
}

const metricCount = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.round(value) : null

/** The run's published metrics, or null for a run recorded before the API published them. */
export const publishedMetrics = (events: readonly ExecutionEventV2[]): PublishedMetrics | null => {
  const attributes = [...events].reverse().find((event) => event.eventKind === 'report.metrics')
    ?.display.attributes
  if (!attributes) return null
  const profile = attributes.runtime_profile
  return {
    runtimeProfile: typeof profile === 'string' && profile.trim() ? profile : null,
    wallDurationMs: metricCount(attributes.wall_duration_ms),
    toolCallCount: metricCount(attributes.tool_call_count),
    knownToolDurationMs: metricCount(attributes.known_tool_duration_ms),
  }
}

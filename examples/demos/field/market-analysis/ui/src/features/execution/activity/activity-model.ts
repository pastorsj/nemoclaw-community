// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * What the Agent Activity panel shows of a run, built from its `execution.v2`
 * events alone:
 *
 * - Thinking: one display-safe milestone per step, in stream order (the request,
 *   each tool call, "evaluating evidence" between them, the answer);
 * - Timeline: the finished run as a chart of observed spans and milestones, with
 *   its duration, tool calls and token use.
 *
 * Neither ever shows model text, prompts or tool arguments. A span is drawn only
 * from an observed start and end; a start without an end is marked incomplete
 * and an end without a start is a point.
 */

import type { ExecutionEventV2 } from '../contract'
import { publishedMetrics } from '../projection'
import { toolFor, type Family } from '../registry'

export type ActivityComponent = Family | 'agent' | 'tool' | 'synthesis'

/** Labels of the market operations, as the Thinking and Timeline views name them. */
const MARKET_OPERATION_LABELS: Readonly<Record<string, string>> = {
  market_scan: 'Market Scan',
  market_anomaly_scan: 'Market Anomaly Scan',
  price_context: 'Price Context',
  sentiment_timeline: 'Sentiment Timeline',
  analyze_news_price_relationship: 'News and Price Relationship',
  analyze_market_relationships: 'Market Relationship Analysis',
  intraday_scan: 'Intraday Scan',
}

/** Hermes' own tools, which the tool registry does not list. */
const HERMES_TOOL_LABELS: Readonly<Record<string, string>> = {
  skill_view: 'Reading skill',
  skills_list: 'Listing skills',
  tool_search: 'Finding tools',
  tool_describe: 'Inspecting tool definitions',
  tool_call: 'Calling selected tool',
  // As the original activity named the call (the tool registry's label is the node's, "Auto Ontology")
  ask_question: 'Auto Ontology Text-to-SQL',
}

const COMPONENT_LABELS: Record<ActivityComponent, string> = {
  agent: 'Reasoning over the request',
  structured_retrieval: 'Retrieving structured data',
  structured_prediction: 'Running structured prediction',
  unstructured_retrieval: 'Retrieving document evidence',
  market_analytics: 'Analyzing market data',
  tool: 'Using a connected tool',
  synthesis: 'Preparing the answer',
}

const COMPONENT_DESCRIPTIONS: Record<ActivityComponent, string> = {
  agent: 'Planning the work and evaluating the available evidence.',
  structured_retrieval: 'Generating, validating, and running a structured query.',
  structured_prediction: 'Grounding a predictive query and invoking NVIDIA Kumo.',
  unstructured_retrieval: 'Searching, ranking, and selecting supporting passages.',
  market_analytics: 'Running a registered market operation against an observed snapshot.',
  tool: 'Calling one of the tools enabled for this run.',
  synthesis: 'Combining verified evidence into the final response.',
}

const LIFECYCLE_SUFFIX = /\s+(?:started|completed|failed)$/i

/** The capability behind an event: a registered tool's family, a Hermes tool, or the agent. */
export const activityComponent = (event: ExecutionEventV2): ActivityComponent => {
  const tool = toolFor(event.toolName)
  if (tool) return tool.family
  return event.toolName ? 'tool' : 'agent'
}

/** One consistent, display-safe action label for Thinking and Timeline. */
export const activityLabel = (
  component: ActivityComponent,
  name: string | null,
  displayLabel?: string,
  detail?: string | null
): string => {
  if (name === 'skill_view') return detail ? `Reading skill: ${detail}` : 'Reading a skill'
  if (component === 'market_analytics' && name && MARKET_OPERATION_LABELS[name]) {
    return MARKET_OPERATION_LABELS[name]
  }
  if (name && HERMES_TOOL_LABELS[name]) return HERMES_TOOL_LABELS[name]
  const tool = toolFor(name)
  if (tool) return tool.label
  if (displayLabel) return displayLabel.replace(LIFECYCLE_SUFFIX, '')
  return COMPONENT_LABELS[component]
}

/** Bounded explanatory copy, never the tool's input, output or reasoning. */
export const activityDescription = (
  component: ActivityComponent,
  name: string | null,
  detail?: string | null
): string => {
  if (name === 'skill_view') {
    return detail
      ? `Loading the ${detail} workflow instructions for this run.`
      : 'Loading workflow instructions relevant to this request.'
  }
  if (name === 'skills_list') return 'Reviewing the skills available to this agent.'
  if (name === 'tool_search') return 'Finding tools that can provide the required evidence.'
  if (name === 'tool_describe') return 'Loading the callable definitions for selected tools.'
  if (name === 'tool_call') return 'Invoking a selected tool through the Hermes tool catalog.'
  if (component === 'market_analytics' && name && MARKET_OPERATION_LABELS[name]) {
    return `Running the registered ${MARKET_OPERATION_LABELS[name]} operation.`
  }
  return COMPONENT_DESCRIPTIONS[component]
}

/** The service a Timeline row names under its action. */
export const activityService = (component: ActivityComponent, name: string | null): string => {
  if (component === 'structured_retrieval') return 'Auto Ontology'
  if (component === 'structured_prediction') return 'NVIDIA Kumo'
  if (component === 'unstructured_retrieval') return 'Milvus + NVIDIA Nemotron'
  if (component === 'market_analytics') return 'Market Analytics'
  if (component === 'synthesis') return 'Response pipeline'
  if (component === 'agent') return 'Hermes Agent'
  if (name === 'skill_view' || name === 'skills_list') return 'Hermes skills'
  if (name === 'tool_search' || name === 'tool_describe' || name === 'tool_call') {
    return 'Hermes tool catalog'
  }
  return 'Connected tool'
}

// ---------------------------------------------------------------- events

const isToolLifecycle = (event: ExecutionEventV2): boolean =>
  Boolean(event.invocationId) && event.eventKind.startsWith('tool.')

const lifecycleFailed = (event: ExecutionEventV2): boolean =>
  isToolLifecycle(event) &&
  (event.state === 'failed' ||
    event.state === 'cancelled' ||
    event.display.attributes.reported_error === true)

const lifecycleEnded = (event: ExecutionEventV2): boolean =>
  isToolLifecycle(event) && event.state !== 'started' && event.state !== 'progress'

const isRunFailure = (event: ExecutionEventV2): boolean =>
  event.eventKind.startsWith('run.') && (event.state === 'failed' || event.state === 'cancelled')

/**
 * The events that mark the answer: `report.completed` when the run has one, else
 * the first completed `run.*` event (a Hermes run ends with the answer).
 */
const answerEvents = (events: readonly ExecutionEventV2[]): Set<ExecutionEventV2> => {
  const reports = events.filter((event) => event.eventKind === 'report.completed')
  if (reports.length) return new Set(reports.slice(0, 1))
  const completed = events.find(
    (event) => event.eventKind.startsWith('run.') && event.state === 'completed'
  )
  return new Set(completed ? [completed] : [])
}

const eventMs = (event: ExecutionEventV2): number => Date.parse(event.occurredAt)

/** Stream order: by cursor where both have one, else as received. */
const inStreamOrder = (events: readonly ExecutionEventV2[]): ExecutionEventV2[] =>
  events
    .map((event, index) => ({ event, index }))
    .sort(
      (left, right) =>
        (left.event.cursor !== null && right.event.cursor !== null
          ? left.event.cursor - right.event.cursor
          : 0) || left.index - right.index
    )
    .map(({ event }) => event)

const identity = (history: readonly ExecutionEventV2[]) => {
  const named = history.find((event) => event.toolName) ?? history[0]!
  const component = activityComponent(named)
  const name = named.toolName
  const started = history.find((event) => event.eventKind === 'tool.started')
  // A start's summary names what it works on (a skill); a failure's is its error, never a label
  const detail = started?.display.summary?.trim() || undefined
  return {
    component,
    name,
    detail: detail ?? null,
    label: activityLabel(component, name, (started ?? named).display.label, detail),
    description: activityDescription(component, name, detail),
  }
}

// ---------------------------------------------------------------- Thinking

export type ThinkingStatus = 'running' | 'completed' | 'failed'

export interface ThinkingItem {
  id: string
  label: string
  description: string
  status: ThinkingStatus
  occurredAt: string
  /** Bounded detail of a failure, from the display-safe event; never a raw provider error */
  failureDetail?: string
  /** The Phoenix span of a failed tool call, when its receipt has one */
  spanId?: string
}

const numberRepeated = <T extends { id: string; label: string }>(
  items: T[],
  repeatable: ReadonlySet<string>,
  extra?: (item: T, ordinal: number, total: number) => Partial<T>
): T[] => {
  const totals = new Map<string, number>()
  for (const item of items) {
    if (repeatable.has(item.id)) totals.set(item.label, (totals.get(item.label) ?? 0) + 1)
  }
  const ordinals = new Map<string, number>()
  return items.map((item) => {
    const total = totals.get(item.label) ?? 0
    if (!repeatable.has(item.id) || total < 2) return item
    const ordinal = (ordinals.get(item.label) ?? 0) + 1
    ordinals.set(item.label, ordinal)
    return { ...item, label: `${item.label} · Call ${ordinal}`, ...extra?.(item, ordinal, total) }
  })
}

const invocationHistories = (
  ordered: readonly ExecutionEventV2[]
): Map<string, ExecutionEventV2[]> => {
  const histories = new Map<string, ExecutionEventV2[]>()
  for (const event of ordered) {
    if (
      !event.invocationId ||
      !(isToolLifecycle(event) || event.eventKind === 'artifact.available')
    )
      continue
    const history = histories.get(event.invocationId) ?? []
    history.push(event)
    histories.set(event.invocationId, history)
  }
  return histories
}

const failureDetail = (history: readonly ExecutionEventV2[]): string | undefined =>
  [...history]
    .reverse()
    .find((event) => lifecycleFailed(event) && event.display.summary?.trim())
    ?.display.summary?.trim()

/**
 * The Thinking list. `callStates` holds each tool call's state as the run's
 * projection sees it (a call cut off by the end of the run failed).
 */
export const buildThinkingActivity = (
  events: readonly ExecutionEventV2[],
  callStates: ReadonlyMap<string, ThinkingStatus> = new Map(),
  spanIds: ReadonlyMap<string, string> = new Map()
): ThinkingItem[] => {
  const ordered = inStreamOrder(events)
  const histories = invocationHistories(ordered)
  const answers = answerEvents(ordered)
  const items: ThinkingItem[] = []
  const invocationItems = new Set<string>()
  const placed = new Set<string>()
  let reasoningSinceLastAction = false
  let runFailed = false

  for (const event of ordered) {
    const kind = event.eventKind
    if (kind === 'run.heartbeat' || kind === 'llm.call') continue

    if (kind === 'run.created') {
      items.push({
        id: event.eventId,
        label: 'Request accepted',
        description: 'Hermes started a new run for this question.',
        status: ordered.length > 1 ? 'completed' : 'running',
        occurredAt: event.occurredAt,
      })
      continue
    }

    if (kind === 'reasoning.available') {
      if (!reasoningSinceLastAction) {
        items.push({
          id: event.eventId,
          label: 'Evaluating evidence',
          description: 'Reviewing observed results and deciding the next useful step.',
          status: 'completed',
          occurredAt: event.occurredAt,
        })
        reasoningSinceLastAction = true
      }
      continue
    }

    if (event.invocationId && histories.has(event.invocationId) && isToolLifecycle(event)) {
      reasoningSinceLastAction = false
      if (placed.has(event.invocationId)) continue
      placed.add(event.invocationId)
      const history = histories.get(event.invocationId)!
      const who = identity(history)
      const status =
        callStates.get(event.invocationId) ??
        (history.some(lifecycleFailed)
          ? 'failed'
          : history.some(lifecycleEnded)
            ? 'completed'
            : 'running')
      const id = `invocation:${event.invocationId}`
      invocationItems.add(id)
      const spanId = spanIds.get(event.invocationId)
      items.push({
        id,
        label: who.label,
        description: who.description,
        status,
        occurredAt: event.occurredAt,
        ...(status === 'failed'
          ? {
              failureDetail:
                failureDetail(history) || 'This observed activity ended with a failure.',
              ...(spanId ? { spanId } : {}),
            }
          : {}),
      })
      continue
    }

    if (answers.has(event)) {
      items.push({
        id: event.eventId,
        label: 'Answer ready',
        description: 'The grounded response is available in the main conversation.',
        status: 'completed',
        occurredAt: event.occurredAt,
      })
      continue
    }

    if (isRunFailure(event) && !runFailed) {
      runFailed = true
      const detail = event.display.summary?.trim()
      items.push({
        id: event.eventId,
        label: 'Run stopped',
        description: 'The run ended before a complete answer was available.',
        status: 'failed',
        occurredAt: event.occurredAt,
        failureDetail: detail || 'The run ended before a complete answer was available.',
      })
    }
  }

  return numberRepeated(items, invocationItems)
}

// ---------------------------------------------------------------- Timeline

export type TimelineStatus = 'completed' | 'failed' | 'incomplete'

export interface TimelineItem {
  id: string
  invocationId?: string
  kind: 'span' | 'milestone'
  component: ActivityComponent
  label: string
  description: string
  service: string
  status: TimelineStatus
  startMs: number
  endMs: number
  startOffsetMs: number
  durationMs: number | null
  timing: 'observed' | 'incomplete' | 'point'
  callIndex?: number
  callCount?: number
  /** The receipt (evidence) the call produced */
  receiptId?: string
  failureDetail?: string
  spanId?: string
}

export interface TimelineMetrics {
  wallDurationMs: number | null
  knownToolDurationMs: number | null
  toolCallCount: number | null
  totalTokens: number | null
  inputTokens: number | null
  outputTokens: number | null
  reasoningTokens: number | null
}

export interface TimelineModel {
  startedAt: string
  endedAt: string
  startMs: number
  endMs: number
  durationMs: number
  status: 'completed' | 'failed'
  items: TimelineItem[]
  metrics: TimelineMetrics
}

const compareEvents = (left: ExecutionEventV2, right: ExecutionEventV2): number =>
  eventMs(left) - eventMs(right) ||
  (left.cursor ?? 0) - (right.cursor ?? 0) ||
  left.eventId.localeCompare(right.eventId)

const statusForTerminal = (history: readonly ExecutionEventV2[]): TimelineStatus => {
  if (history.some(lifecycleFailed)) return 'failed'
  if (history.some(lifecycleEnded)) return 'completed'
  return 'incomplete'
}

const count = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.round(value) : null

const tokenMetrics = (events: readonly ExecutionEventV2[]) => {
  const run = [...events]
    .reverse()
    .find((event) => event.eventKind.startsWith('run.') && event.state === 'completed')
  const attributes = run?.display.attributes ?? {}
  const input = count(attributes.input_tokens)
  const output = count(attributes.output_tokens)
  const total =
    count(attributes.total_tokens) ?? (input !== null && output !== null ? input + output : null)
  return {
    totalTokens: total,
    inputTokens: input,
    outputTokens: output,
    reasoningTokens: count(attributes.reasoning_tokens),
  }
}

const milestoneFor = (
  event: ExecutionEventV2,
  startMs: number,
  answers: ReadonlySet<ExecutionEventV2>
): TimelineItem | null => {
  let label: string
  let description: string
  let component: ActivityComponent
  if (event.eventKind === 'reasoning.available') {
    label = 'Evaluating evidence'
    description = 'The agent evaluated observed results before choosing its next action.'
    component = 'agent'
  } else if (answers.has(event)) {
    label = 'Answer ready'
    description = 'The grounded response became available in the conversation.'
    component = 'synthesis'
  } else if (event.eventKind === 'report.reference_resolution') {
    label = 'Citations resolved'
    description = 'Evidence references were resolved for the published response.'
    component = 'synthesis'
  } else {
    return null
  }
  const timestamp = eventMs(event)
  return {
    id: `milestone:${event.eventId}`,
    kind: 'milestone',
    component,
    label,
    description,
    service: activityService(component, null),
    status: 'completed',
    startMs: timestamp,
    endMs: timestamp,
    startOffsetMs: Math.max(0, timestamp - startMs),
    durationMs: 0,
    timing: 'point',
  }
}

/**
 * The finished run's timeline, or null without timestamps. `spanIds` holds the
 * Phoenix span of each tool call whose receipt has one.
 */
export const buildActionTimeline = (
  events: readonly ExecutionEventV2[],
  spanIds: ReadonlyMap<string, string> = new Map()
): TimelineModel | null => {
  const ordered = events.filter((event) => Number.isFinite(eventMs(event))).sort(compareEvents)
  if (!ordered.length) return null

  const firstMs = eventMs(ordered[0]!)
  const lastMs = eventMs(ordered[ordered.length - 1]!)
  const created = ordered.find((event) => event.eventKind === 'run.created')
  const startMs = created ? eventMs(created) : firstMs
  const published = publishedMetrics(ordered)
  const wallEndMs = published?.wallDurationMs == null ? startMs : startMs + published.wallDurationMs
  const endMs = Math.max(startMs, lastMs, wallEndMs)
  const answers = answerEvents(inStreamOrder(events))

  const items: TimelineItem[] = []
  for (const [invocationId, unordered] of invocationHistories(ordered)) {
    const history = [...unordered].sort(compareEvents)
    const lifecycle = history.filter(isToolLifecycle)
    if (!lifecycle.length) continue
    const started = lifecycle.find((event) => event.eventKind === 'tool.started')
    const terminal = [...lifecycle].reverse().find(lifecycleEnded)
    const observedStartMs = started ? eventMs(started) : terminal ? eventMs(terminal) : startMs
    const observedEndMs = terminal ? eventMs(terminal) : endMs
    const observedSpan = Boolean(started && terminal && observedEndMs >= observedStartMs)
    const incomplete = Boolean(started && !terminal)
    const who = identity(history)
    const receiptId = history.find(
      (event) => event.eventKind === 'artifact.available' && event.artifactRefs.length > 0
    )?.artifactRefs[0]
    const status = statusForTerminal(lifecycle)
    const spanId = spanIds.get(invocationId)
    items.push({
      id: `invocation:${invocationId}`,
      invocationId,
      kind: observedSpan || incomplete ? 'span' : 'milestone',
      component: who.component,
      label: who.label,
      description: who.description,
      service: activityService(who.component, who.name),
      status,
      startMs: observedStartMs,
      endMs: Math.max(observedStartMs, observedEndMs),
      startOffsetMs: Math.max(0, observedStartMs - startMs),
      durationMs: observedSpan
        ? Math.max(0, observedEndMs - observedStartMs)
        : incomplete
          ? Math.max(0, endMs - observedStartMs)
          : null,
      timing: observedSpan ? 'observed' : incomplete ? 'incomplete' : 'point',
      ...(receiptId ? { receiptId } : {}),
      ...(status === 'failed'
        ? {
            failureDetail:
              failureDetail(lifecycle) ?? 'This observed activity ended with a failure.',
            ...(spanId ? { spanId } : {}),
          }
        : {}),
    })
  }
  for (const event of ordered) {
    const milestone = milestoneFor(event, startMs, answers)
    if (milestone) items.push(milestone)
  }

  const toolItems = items.filter((item) => item.invocationId)
  const knownTool = toolItems.filter((item) => item.timing === 'observed')
  const actions = numberRepeated(
    items.sort((left, right) => left.startMs - right.startMs || left.id.localeCompare(right.id)),
    new Set(toolItems.map((item) => item.id)),
    (_item, callIndex, callCount) => ({ callIndex, callCount })
  )
  return {
    startedAt: new Date(startMs).toISOString(),
    endedAt: new Date(endMs).toISOString(),
    startMs,
    endMs,
    durationMs: Math.max(1, endMs - startMs),
    status: ordered.some(isRunFailure) ? 'failed' : 'completed',
    items: actions,
    metrics: {
      wallDurationMs: published?.wallDurationMs ?? null,
      knownToolDurationMs:
        published?.knownToolDurationMs ??
        (knownTool.length
          ? knownTool.reduce((total, item) => total + (item.durationMs ?? 0), 0)
          : null),
      toolCallCount: published?.toolCallCount ?? toolItems.length,
      ...tokenMetrics(ordered),
    },
  }
}

/** Compact elapsed time for chart axes and action statistics. */
export const formatTimelineDuration = (durationMs: number | null): string => {
  if (durationMs === null) return 'Unavailable'
  if (durationMs <= 0) return '0 s'
  if (durationMs < 1_000) return '<1 s'
  const seconds = Math.round(durationMs / 1_000)
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return `${minutes}m ${String(remainder).padStart(2, '0')}s`
}

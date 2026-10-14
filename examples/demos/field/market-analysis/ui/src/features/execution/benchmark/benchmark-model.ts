// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The Benchmark tab's view of a run: its tool calls as the Timeline observed
 * them, each market call that was replayed on both engines paired with its CPU
 * and NVIDIA GPU medians (`Benchmark`, from the API). Every other call is
 * shared: it is the same in both paths, so it keeps its one observed timing.
 * Nothing here is simulated; a speedup is claimed only for a stage the API
 * qualified.
 */

import type { Benchmark, BenchmarkStage, ExecutionEventV2 } from '../contract'
import type { ActivityComponent, TimelineItem, TimelineModel } from '../activity/activity-model'
import { toolFor } from '../registry'

export type BenchmarkEngine = 'cpu' | 'gpu'
export type LaneState = 'pending' | 'running' | 'completed' | 'failed' | 'mismatch' | 'unavailable'

export interface BenchmarkLane {
  engine: BenchmarkEngine
  state: LaneState
  /** Median of the engine's timed trials; null until measured */
  durationMs: number | null
  /** The library that did the work, e.g. cudf.pandas or pandas */
  library: string | null
}

export interface BenchmarkStep {
  id: string
  invocationId?: string
  kind: 'shared' | 'measured'
  component: ActivityComponent
  label: string
  service: string
  source: TimelineItem
  stage: BenchmarkStage | null
  cpu: BenchmarkLane
  gpu: BenchmarkLane
}

export interface BenchmarkView {
  steps: BenchmarkStep[]
}

export interface BenchmarkEligibility {
  /** The run called a market analytics tool that returned a result */
  market: boolean
  /** The run retrieved documents */
  retrieval: boolean
  /** The run has ended */
  completed: boolean
  marketCallCount: number
}

/** Which comparisons a run's events allow. The API checks its receipts again. */
export const benchmarkEligibility = (events: readonly ExecutionEventV2[]): BenchmarkEligibility => {
  const market = new Set<string>()
  const retrieval = new Set<string>()
  for (const event of events) {
    if (event.eventKind !== 'artifact.available' || !event.invocationId) continue
    if (!event.artifactRefs.length) continue
    const family = toolFor(event.toolName)?.family
    if (family === 'market_analytics') market.add(event.invocationId)
    if (family === 'unstructured_retrieval') retrieval.add(event.invocationId)
  }
  return {
    market: market.size > 0,
    retrieval: retrieval.size > 0,
    completed: events.some(
      (event) =>
        event.eventKind.startsWith('run.') &&
        event.state !== 'started' &&
        event.state !== 'progress'
    ),
    marketCallCount: market.size,
  }
}

const TOOL_COMPONENTS = new Set<ActivityComponent>([
  'structured_retrieval',
  'structured_prediction',
  'unstructured_retrieval',
  'market_analytics',
  'tool',
])

/** A tool call the chart lists: everything but the agent's own milestones. */
export const isToolCall = (step: BenchmarkStep): boolean =>
  Boolean(step.invocationId) && TOOL_COMPONENTS.has(step.component)

const lane = (
  engine: BenchmarkEngine,
  stage: BenchmarkStage | null,
  running: boolean
): BenchmarkLane => {
  const trials = stage ? stage[engine] : null
  const state: LaneState = !stage
    ? running
      ? 'running'
      : 'pending'
    : stage.outcome === 'completed'
      ? 'completed'
      : stage.outcome
  return {
    engine,
    state: trials?.medianMs == null && state === 'completed' ? 'unavailable' : state,
    durationMs: trials?.medianMs ?? null,
    library: trials?.library ?? null,
  }
}

/**
 * Join the run's timeline to its comparison. While `running`, the market calls
 * are drawn as measured rows still waiting for their trials.
 */
export const buildBenchmarkView = (
  timeline: TimelineModel,
  benchmark: Benchmark | null,
  marketInvocations: ReadonlySet<string>,
  running = false
): BenchmarkView => {
  const stages = new Map(benchmark?.stages.map((stage) => [stage.invocationId, stage]) ?? [])
  return {
    steps: timeline.items.map((source): BenchmarkStep => {
      const stage = source.invocationId ? (stages.get(source.invocationId) ?? null) : null
      const measured =
        stage !== null ||
        (running && Boolean(source.invocationId) && marketInvocations.has(source.invocationId!))
      return {
        id: source.id,
        ...(source.invocationId ? { invocationId: source.invocationId } : {}),
        kind: measured ? 'measured' : 'shared',
        component: source.component,
        label: source.label,
        service: source.service,
        source,
        stage,
        cpu: lane('cpu', stage, running),
        gpu: lane('gpu', stage, running),
      }
    }),
  }
}

export const formatBenchmarkDuration = (milliseconds: number | null): string => {
  if (milliseconds === null) return '—'
  if (milliseconds < 1) return '<1 ms'
  if (milliseconds < 1_000) return `${Math.round(milliseconds)} ms`
  return `${(milliseconds / 1_000).toFixed(milliseconds < 10_000 ? 2 : 1)} s`
}

const pairCount = (stage: BenchmarkStage): number =>
  Math.min(stage.cpu?.trialsMs.length ?? 0, stage.gpu?.trialsMs.length ?? 0)

/** What a measured row claims, under its label: `value · label`, with `detail` as its tooltip. */
export const stageClaim = (
  stage: BenchmarkStage | null
): { label: string; value: string; detail: string } => {
  if (!stage) {
    return {
      label: 'Observed comparison',
      value: 'In progress',
      detail: 'Results appear once the matched CPU and GPU trials complete.',
    }
  }
  const terminal: Partial<Record<BenchmarkStage['outcome'], string>> = {
    mismatch: 'Output mismatch',
    unavailable: 'Unavailable',
    failed: 'Failed',
  }
  if (terminal[stage.outcome]) {
    return {
      label: 'Comparison outcome',
      value: terminal[stage.outcome]!,
      detail: stage.reason || 'The matched comparison did not complete.',
    }
  }
  const pairs = pairCount(stage)
  if (stage.qualified && stage.speedup && stage.speedupRange) {
    return {
      label: 'Qualified speedup',
      value: `${stage.speedup.toFixed(1)}×`,
      detail: `The NVIDIA GPU was faster in each of ${pairs} matched pairs and returned the same results; pair ratios ${stage.speedupRange.low.toFixed(1)}–${stage.speedupRange.high.toFixed(1)}×.`,
    }
  }
  return {
    label: 'Unqualified sample',
    value: 'No speedup claim',
    detail:
      stage.parity === true
        ? `Both engines returned the same results, but the NVIDIA GPU was not faster in every one of ${pairs} matched pairs, or fewer than 5 pairs ran.`
        : 'Observed timings are available, but the results were not compared.',
  }
}

// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The run's tool calls with their CPU and NVIDIA GPU timings: the accelerated
 * total, each replayed market call's two medians on one millisecond scale, and
 * each shared call once, as observed.
 */

'use client'

import type { CSSProperties, FC } from 'react'
import {
  formatBenchmarkDuration,
  isToolCall,
  stageClaim,
  type BenchmarkEngine,
  type BenchmarkStep,
  type BenchmarkView,
} from './benchmark-model'
import styles from './benchmark-journey-timeline.module.css'

const TICKS = [0, 0.25, 0.5, 0.75, 1] as const

interface ToolTime {
  callCount: number
  gpuDurationMs: number
  cpuDurationMs: number
  maximumDurationMs: number
}

interface Outcome {
  status: 'gpu_faster' | 'unqualified' | 'equal' | 'pending'
  headline: string
  detail: string
}

const engineName = (engine: BenchmarkEngine): string => (engine === 'gpu' ? 'NVIDIA GPU' : 'CPU')

const measuredBoth = (step: BenchmarkStep): boolean =>
  step.gpu.durationMs !== null && step.cpu.durationMs !== null

const toolTime = (steps: readonly BenchmarkStep[]): ToolTime | null => {
  if (!steps.length || !steps.every(measuredBoth)) return null
  const gpuDurationMs = steps.reduce((total, step) => total + step.gpu.durationMs!, 0)
  const cpuDurationMs = steps.reduce((total, step) => total + step.cpu.durationMs!, 0)
  return {
    callCount: steps.length,
    gpuDurationMs,
    cpuDurationMs,
    maximumDurationMs: Math.max(1, gpuDurationMs, cpuDurationMs),
  }
}

const difference = (summary: ToolTime): string => {
  const delta = Math.abs(summary.cpuDurationMs - summary.gpuDurationMs)
  if (delta === 0) return 'No observed cumulative difference'
  const lower = summary.gpuDurationMs < summary.cpuDurationMs ? 'NVIDIA GPU' : 'CPU'
  return `${formatBenchmarkDuration(delta)} lower on ${lower}`
}

const ratioLabel = (ratio: number): string =>
  `${ratio.toFixed(ratio < 2 ? 2 : 1).replace(/\.0+$/, '')}× faster`

const outcomeOf = (
  summary: ToolTime | null,
  steps: readonly BenchmarkStep[],
  running: boolean
): Outcome => {
  if (!summary) {
    return running
      ? { status: 'pending', headline: 'Measuring…', detail: 'Waiting for all matched tool calls' }
      : {
          status: 'unqualified',
          headline: 'No speedup claim',
          detail: 'No call completed on both engines',
        }
  }
  if (summary.cpuDurationMs === summary.gpuDurationMs) {
    return { status: 'equal', headline: 'Same observed time', detail: '0 ms difference' }
  }
  const qualified = steps.every((step) => step.stage?.qualified === true)
  if (summary.gpuDurationMs >= summary.cpuDurationMs || !qualified || summary.gpuDurationMs <= 0) {
    return {
      status: 'unqualified',
      headline: 'No speedup claim',
      detail: `Observed sample · ${difference(summary)}`,
    }
  }
  return {
    status: 'gpu_faster',
    headline: ratioLabel(summary.cpuDurationMs / summary.gpuDurationMs),
    detail: `Observed total · ${formatBenchmarkDuration(summary.cpuDurationMs - summary.gpuDurationMs)} less on NVIDIA GPU`,
  }
}

const barWidth = (durationMs: number, maximumDurationMs: number): string =>
  `${Math.min(100, (durationMs / Math.max(1, maximumDurationMs)) * 100)}%`

const axisLabel = (durationMs: number): string =>
  durationMs === 0 ? '0 ms' : formatBenchmarkDuration(durationMs)

const knownMaximum = (steps: readonly BenchmarkStep[]): number =>
  Math.max(
    1,
    ...steps.flatMap((step) =>
      [step.gpu.durationMs, step.cpu.durationMs].filter((value): value is number => value !== null)
    )
  )

const ToolLane: FC<{ step: BenchmarkStep; engine: BenchmarkEngine; maximumDurationMs: number }> = ({
  step,
  engine,
  maximumDurationMs,
}) => {
  const lane = engine === 'gpu' ? step.gpu : step.cpu
  const duration = lane.durationMs
  const runtime = lane.library || engineName(engine)
  const description = `${step.label} · ${engineName(engine)} · ${runtime} · ${formatBenchmarkDuration(duration)}`
  return (
    <div
      className={styles.laneCell}
      data-lane={engine}
      data-basis={duration === null ? 'pending' : 'measured_replay'}
      data-duration-ms={duration ?? undefined}
      data-state={lane.state}
      aria-label={description}
      title={description}
    >
      <div className={styles.track}>
        {duration === null ? (
          <span className={styles.pendingMarker} />
        ) : (
          <span
            className={styles.bar}
            style={{ '--journey-width': barWidth(duration, maximumDurationMs) } as CSSProperties}
          />
        )}
      </div>
      <div className={styles.laneMeta}>
        <span>{runtime}</span>
        <small>{duration === null ? lane.state : formatBenchmarkDuration(duration)}</small>
      </div>
    </div>
  )
}

const TotalLane: FC<{ engine: BenchmarkEngine; durationMs: number; maximumDurationMs: number }> = ({
  engine,
  durationMs,
  maximumDurationMs,
}) => (
  <div
    className={styles.laneCell}
    data-engine={engine}
    data-duration-ms={durationMs}
    data-total="true"
  >
    <div className={styles.track}>
      <span
        className={styles.bar}
        data-testid={`benchmark-tool-time-fill-${engine}`}
        style={{ '--journey-width': barWidth(durationMs, maximumDurationMs) } as CSSProperties}
      />
    </div>
    <div className={styles.laneMeta}>
      <span>Summed median</span>
      <small>{formatBenchmarkDuration(durationMs)}</small>
    </div>
  </div>
)

const SharedToolCall: FC<{ step: BenchmarkStep }> = ({ step }) => {
  const duration = step.source.durationMs
  const timing = duration === null ? 'Timing unavailable' : formatBenchmarkDuration(duration)
  const status =
    step.source.status === 'completed' ? 'Shared · unchanged' : `Shared · ${step.source.status}`
  const description = `${step.label}; ${status.toLowerCase()} in NVIDIA GPU and CPU paths; observed ${timing}`
  return (
    <div
      className={styles.sharedCell}
      data-shared-tool="true"
      data-basis={duration === null ? 'unavailable' : 'source_observed'}
      data-duration-ms={duration ?? undefined}
      data-state={step.source.status}
      role="group"
      aria-label={description}
      title={description}
    >
      <div className={styles.sharedPath} aria-hidden="true">
        <span className={styles.sharedEndpoint} />
        <span className={styles.sharedRail} />
        <span className={styles.sharedEndpoint} />
      </div>
      <div className={styles.sharedMeta}>
        <strong>{status}</strong>
        <span>
          {step.service} · {timing} observed
        </span>
      </div>
    </div>
  )
}

export const BenchmarkChart: FC<{ view: BenchmarkView; running: boolean }> = ({
  view,
  running,
}) => {
  const toolSteps = view.steps.filter(isToolCall)
  const acceleratedSteps = toolSteps.filter((step) => step.kind === 'measured')
  const timedSteps = acceleratedSteps.filter(measuredBoth)
  const summary = toolTime(running ? acceleratedSteps : timedSteps)
  const maximumDurationMs = summary?.maximumDurationMs ?? knownMaximum(acceleratedSteps)
  const outcome = outcomeOf(summary, running ? acceleratedSteps : timedSteps, running)

  return (
    <section
      className={styles.journey}
      aria-label="CPU and NVIDIA GPU tool-call timing comparison"
      data-testid="benchmark-journey-timeline"
      data-scale-duration-ms={maximumDurationMs}
      data-accelerated-tool-count={acceleratedSteps.length}
      data-shared-tool-count={toolSteps.length - acceleratedSteps.length}
    >
      <div className={styles.intro}>
        <div>
          <strong>Tool-call execution</strong>
          <span>Accelerated calls are compared; shared calls remain unchanged in both paths.</span>
        </div>
        <div className={styles.outcome} data-status={outcome.status} aria-live="polite">
          <strong data-testid="benchmark-tool-time-ratio">{outcome.headline}</strong>
          <small data-testid="benchmark-tool-time-difference">{outcome.detail}</small>
        </div>
      </div>

      <div className={styles.chartScroll} data-testid="benchmark-journey-scroll">
        <div className={styles.chart}>
          <div className={styles.headerRow}>
            <div className={styles.actionHeader}>Tool call</div>
            {(['gpu', 'cpu'] as const).map((engine) => {
              const total = summary
                ? engine === 'gpu'
                  ? summary.gpuDurationMs
                  : summary.cpuDurationMs
                : null
              return (
                <div className={styles.laneHeader} data-engine={engine} key={engine}>
                  <div>
                    <span className={styles.engineDot} />
                    <strong>{engineName(engine)}</strong>
                    <small>
                      {total === null
                        ? 'Measuring…'
                        : `${formatBenchmarkDuration(total)} accelerated`}
                    </small>
                  </div>
                  <div className={styles.axis} aria-hidden="true">
                    {TICKS.map((tick) => (
                      <span
                        key={tick}
                        data-edge={tick === 0 ? 'start' : tick === 1 ? 'end' : undefined}
                        style={{ left: `${tick * 100}%` }}
                      >
                        {axisLabel(maximumDurationMs * tick)}
                      </span>
                    ))}
                  </div>
                </div>
              )
            })}
          </div>

          <div className={styles.rows} role="list" aria-label="Tool-call timings">
            {summary ? (
              <div
                className={`${styles.row} ${styles.totalRow}`}
                role="listitem"
                data-testid="benchmark-tool-time-summary"
                data-scope="accelerated-only"
              >
                <div className={styles.rowLabel}>
                  <strong>Accelerated tool-call time</strong>
                  <span>
                    {summary.callCount} matched call{summary.callCount === 1 ? '' : 's'}
                  </span>
                </div>
                <TotalLane
                  engine="gpu"
                  durationMs={summary.gpuDurationMs}
                  maximumDurationMs={maximumDurationMs}
                />
                <TotalLane
                  engine="cpu"
                  durationMs={summary.cpuDurationMs}
                  maximumDurationMs={maximumDurationMs}
                />
              </div>
            ) : null}

            {toolSteps.map((step) => {
              const claim = step.kind === 'measured' ? stageClaim(step.stage) : null
              return (
                <div
                  className={styles.row}
                  role="listitem"
                  key={step.id}
                  data-benchmark-row="true"
                  data-tool-kind={step.kind === 'measured' ? 'accelerated' : 'shared'}
                  data-invocation-id={step.invocationId}
                >
                  <div className={styles.rowLabel} title={claim?.detail}>
                    <strong>{step.label}</strong>
                    <span data-claim={claim?.label}>
                      {claim ? `${claim.value} · ${claim.label}` : `${step.service} · shared tool`}
                    </span>
                  </div>
                  {step.kind === 'measured' ? (
                    <>
                      <ToolLane step={step} engine="gpu" maximumDurationMs={maximumDurationMs} />
                      <ToolLane step={step} engine="cpu" maximumDurationMs={maximumDurationMs} />
                    </>
                  ) : (
                    <SharedToolCall step={step} />
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </div>

      <p className={styles.disclaimer}>
        Speedup, totals, and the millisecond scale include accelerated calls only. Shared rows show
        the original observed call once because it is identical in both paths.
      </p>
    </section>
  )
}

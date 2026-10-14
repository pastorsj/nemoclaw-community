// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Benchmark: the run's market analytics calls replayed on the CPU and the
 * NVIDIA GPU, with the same arguments. Live, the API runs the comparison on
 * request (`POST /v1/jobs/async/job/{id}/benchmark`) and keeps it; a recording
 * carries the comparison made when it was recorded, if any. The agent is never
 * rerun.
 *
 * A run that searched documents also shows the Milvus comparison that applies
 * to it (`GET /v1/jobs/async/job/{id}/retrieval-benchmark`): the CPU index
 * against an NVIDIA GPU index of the same vectors, measured once per index
 * build on a GPU host. A CPU-only stack has none.
 */

'use client'

import { type FC, type ReactNode, useEffect, useMemo, useState } from 'react'
import { Button, Flex, Spinner, Text } from '@/adapters/ui'
import { ChartFlow, Warning } from '@/adapters/ui/icons'
import type { TimelineModel } from '../activity/activity-model'
import type { Benchmark, ExecutionEventV2, RetrievalBenchmark } from '../contract'
import { toBenchmark, toRetrievalBenchmark } from '../contract'
import { toolFor } from '../registry'
import { useExecutionStore } from '../store'
import { BenchmarkChart } from './BenchmarkChart'
import { benchmarkEligibility, buildBenchmarkView } from './benchmark-model'
import { RetrievalBenchmarkPanel } from './RetrievalBenchmarkPanel'
import styles from './benchmark-tab.module.css'

interface BenchmarkTabProps {
  jobId: string | null
  events: readonly ExecutionEventV2[]
  timeline: TimelineModel | null
  /** The comparison already known for this run (a recording's, or one run earlier) */
  benchmark: Benchmark | null
  /** The Milvus comparison already known for this run (a recording's, or one loaded earlier) */
  retrievalBenchmark: RetrievalBenchmark | null
  /** Replay: the comparison comes only from the recording */
  recorded: boolean
}

type Phase = 'idle' | 'restoring' | 'running' | 'completed' | 'unavailable' | 'error'

const RETRIEVAL_UNAVAILABLE =
  'Retrieval comparison is unavailable: Milvus runs its CPU index only in this stack, so there is no GPU index to compare.'

const benchmarkUrl = (jobId: string) =>
  `/api/v1/jobs/async/job/${encodeURIComponent(jobId)}/benchmark`

const retrievalBenchmarkUrl = (jobId: string) =>
  `/api/v1/jobs/async/job/${encodeURIComponent(jobId)}/retrieval-benchmark`

const errorDetail = async (response: Response): Promise<string> => {
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null
  return typeof body?.detail === 'string'
    ? body.detail
    : `The comparison request failed (${response.status}).`
}

const EmptyState: FC<{
  icon?: 'spinner' | 'warning'
  title: string
  description: string
  action?: ReactNode
}> = ({ icon, title, description, action }) => (
  <Flex
    direction="col"
    align="center"
    justify="center"
    className={styles.empty}
    data-testid="benchmark-empty-state"
  >
    {icon === 'spinner' ? (
      <Spinner size="medium" aria-label={title} />
    ) : icon === 'warning' ? (
      <Warning className="text-secondary h-9 w-9" aria-hidden="true" />
    ) : (
      <ChartFlow className="h-10 w-10 text-[#76b900]" aria-hidden="true" />
    )}
    <Text kind="label/semibold/md" className="text-primary mt-4">
      {title}
    </Text>
    <Text kind="body/regular/sm" className="text-secondary mt-2 max-w-md text-center">
      {description}
    </Text>
    {action ? <div className="mt-5">{action}</div> : null}
  </Flex>
)

type RetrievalPhase = 'idle' | 'loading' | 'ready' | 'unavailable'

/**
 * The Milvus comparison for a finished run that searched documents: the one
 * already known, else (live) the API's, else why there is none.
 */
const useRetrievalComparison = ({
  jobId,
  eligible,
  recorded,
  known,
}: {
  jobId: string | null
  eligible: boolean
  recorded: boolean
  known: RetrievalBenchmark | null
}): { phase: RetrievalPhase; message: string } => {
  const setRetrievalBenchmark = useExecutionStore((state) => state.setRetrievalBenchmark)
  const [state, setState] = useState<{ phase: RetrievalPhase; message: string }>({
    phase: 'idle',
    message: RETRIEVAL_UNAVAILABLE,
  })

  useEffect(() => {
    if (known || !eligible) {
      setState({ phase: 'idle', message: RETRIEVAL_UNAVAILABLE })
      return
    }
    if (recorded || !jobId) {
      setState({ phase: 'unavailable', message: RETRIEVAL_UNAVAILABLE })
      return
    }
    const controller = new AbortController()
    setState({ phase: 'loading', message: RETRIEVAL_UNAVAILABLE })
    fetch(retrievalBenchmarkUrl(jobId), { cache: 'no-store', signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          setState({ phase: 'unavailable', message: await errorDetail(response) })
          return
        }
        const result = toRetrievalBenchmark(await response.json())
        if (result) setRetrievalBenchmark(jobId, result)
        else setState({ phase: 'unavailable', message: RETRIEVAL_UNAVAILABLE })
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setState({ phase: 'unavailable', message: RETRIEVAL_UNAVAILABLE })
        }
      })
    return () => controller.abort()
  }, [eligible, jobId, known, recorded, setRetrievalBenchmark])

  return known ? { phase: 'ready', message: '' } : state
}

export const BenchmarkTab: FC<BenchmarkTabProps> = ({
  jobId,
  events,
  timeline,
  benchmark,
  retrievalBenchmark,
  recorded,
}) => {
  const eligibility = useMemo(() => benchmarkEligibility(events), [events])
  const retrieval = useRetrievalComparison({
    jobId,
    eligible: eligibility.retrieval && eligibility.completed,
    recorded,
    known: retrievalBenchmark,
  })
  const setBenchmark = useExecutionStore((state) => state.setBenchmark)
  const [phase, setPhase] = useState<Phase>(benchmark ? 'completed' : 'idle')
  const [message, setMessage] = useState<string | null>(null)
  const canCompare = !recorded && Boolean(jobId) && eligibility.market && eligibility.completed

  // Restore a comparison the API already holds for this run
  useEffect(() => {
    if (benchmark) {
      setPhase('completed')
      return
    }
    setPhase('idle')
    setMessage(null)
    if (!canCompare || !jobId) return
    const controller = new AbortController()
    setPhase('restoring')
    fetch(benchmarkUrl(jobId), { cache: 'no-store', signal: controller.signal })
      .then(async (response) => {
        const stored = response.ok ? toBenchmark(await response.json()) : null
        if (stored) setBenchmark(jobId, stored)
        else setPhase('idle')
      })
      .catch(() => {
        if (!controller.signal.aborted) setPhase('idle')
      })
    return () => controller.abort()
  }, [benchmark, canCompare, jobId, setBenchmark])

  const start = async (): Promise<void> => {
    if (!jobId) return
    setPhase('running')
    setMessage(null)
    try {
      const response = await fetch(benchmarkUrl(jobId), { method: 'POST', cache: 'no-store' })
      if (!response.ok) {
        setMessage(await errorDetail(response))
        setPhase(response.status === 422 || response.status === 409 ? 'unavailable' : 'error')
        return
      }
      const result = toBenchmark(await response.json())
      if (!result) {
        setMessage('The comparison failed its display contract.')
        setPhase('error')
      } else if (result.status === 'unavailable') {
        setMessage(result.reason)
        setPhase('unavailable')
      } else {
        setBenchmark(jobId, result)
      }
    } catch {
      setMessage('The API could not be reached.')
      setPhase('error')
    }
  }

  const marketInvocations = useMemo(
    () =>
      new Set(
        events.flatMap((event) =>
          event.eventKind === 'artifact.available' &&
          event.invocationId &&
          toolFor(event.toolName)?.family === 'market_analytics'
            ? [event.invocationId]
            : []
        )
      ),
    [events]
  )
  const running = phase === 'running'
  const view = useMemo(
    () => (timeline ? buildBenchmarkView(timeline, benchmark, marketInvocations, running) : null),
    [benchmark, marketInvocations, running, timeline]
  )

  let retrievalSection: ReactNode = null
  if (eligibility.retrieval && retrieval.phase === 'ready' && retrievalBenchmark) {
    retrievalSection = <RetrievalBenchmarkPanel benchmark={retrievalBenchmark} />
  } else if (eligibility.retrieval && retrieval.phase === 'loading') {
    retrievalSection = (
      <div className={styles.retrievalStatus} role="status">
        <Spinner size="small" aria-label="Loading retrieval benchmark" />
        Loading the release-qualified Milvus index comparison…
      </div>
    )
  } else if (eligibility.retrieval) {
    retrievalSection = (
      <div className={styles.retrievalStatus} data-status="warning" role="status">
        <Warning className="h-4 w-4" aria-hidden="true" />
        {retrieval.message}
      </div>
    )
  }

  if (!recorded && !jobId && events.length === 0) {
    return (
      <EmptyState
        title="GPU comparison available after a qualifying run"
        description="This view populates after a completed question calls a benchmark-eligible NVIDIA GPU analytics tool."
      />
    )
  }
  if (!eligibility.completed) {
    return (
      <EmptyState
        icon="spinner"
        title={
          eligibility.market || eligibility.retrieval
            ? 'Benchmark available after completion'
            : 'Watching for GPU analytics'
        }
        description={
          eligibility.market || eligibility.retrieval
            ? 'The agent is still assembling its answer. Its market operations can be compared once the run completes.'
            : 'If this question calls a benchmark-eligible NVIDIA GPU analytics tool, the comparison will become available after the answer completes.'
        }
      />
    )
  }
  if (!eligibility.market && !eligibility.retrieval) {
    return (
      <EmptyState
        title="No GPU analytics to compare"
        description="This run did not call a benchmark-eligible NVIDIA GPU analytics tool, so no CPU/GPU comparison is available."
      />
    )
  }
  if (!eligibility.market) {
    return <div className={styles.workspace}>{retrievalSection}</div>
  }
  if (recorded && !benchmark) {
    return (
      <EmptyState
        title="No recorded benchmark"
        description="This recorded answer does not include an observed CPU/GPU comparison. Start a new live question and use a benchmark-eligible GPU analytics tool to create one."
      />
    )
  }
  if (phase === 'idle') {
    return (
      <div className={styles.workspace}>
        {retrievalSection}
        <EmptyState
          title="Compare CPU and NVIDIA GPU"
          description={`${eligibility.marketCallCount} observed market operation${eligibility.marketCallCount === 1 ? '' : 's'} can be replayed against the same immutable inputs. Timings are measured live; nothing is simulated.`}
          action={
            <Button
              kind="primary"
              size="small"
              onClick={() => void start()}
              data-testid="benchmark-start"
            >
              Run comparison
            </Button>
          }
        />
      </div>
    )
  }
  if (phase === 'restoring') {
    return (
      <div className={styles.workspace}>
        {retrievalSection}
        <EmptyState
          icon="spinner"
          title="Restoring comparison…"
          description="Loading the comparison already measured for this run."
        />
      </div>
    )
  }
  if (phase === 'unavailable' || phase === 'error') {
    return (
      <div className={styles.workspace}>
        {retrievalSection}
        <EmptyState
          icon="warning"
          title={
            phase === 'unavailable'
              ? 'Benchmark service unavailable'
              : 'Benchmark could not continue'
          }
          description={message || 'The answer and its original execution record remain available.'}
        />
      </div>
    )
  }

  return (
    <div className={styles.workspace} data-testid="benchmark-comparison">
      <div className={styles.toolbar}>
        <div>
          <small>{recorded ? 'Recorded benchmark' : 'Observed benchmark'}</small>
          <strong>NVIDIA GPU and CPU</strong>
          <span>
            {running
              ? 'Running matched trials live'
              : benchmark?.status === 'completed'
                ? 'Completed from durable trial evidence'
                : `Finished: ${benchmark?.status ?? 'unknown'}`}
          </span>
        </div>
      </div>
      {view ? <BenchmarkChart view={view} running={running} /> : null}
      {retrievalSection}
    </div>
  )
}

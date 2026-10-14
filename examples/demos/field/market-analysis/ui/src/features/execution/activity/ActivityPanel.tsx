// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The Agent Activity panel's tabs: Thinking (the run as it happens), Timeline
 * (the finished run's timing) and Benchmark (its market calls on the CPU and
 * the NVIDIA GPU). In live mode what the store lacks loads from the job's
 * export (`useJobHistory`); recorded runs are already in the store.
 */

'use client'

import { type FC, useEffect, useMemo, useState } from 'react'
import { Flex, Spinner, Text } from '@/adapters/ui'
import { ThinkingReasoning } from '@/adapters/ui/icons'
import { useAppConfig, type ActivityPanelProps } from '@/shared/context'
import type { ExecutionEventV2 } from '../contract'
import { BenchmarkTab } from '../benchmark/BenchmarkTab'
import { projectRun, runEnded } from '../projection'
import { loadJobExport } from '../replay/sources'
import { useExecutionRun, type ExecutionRun } from '../store'
import { phoenixSpanUrl, useTraceUrl } from '../trace-link'
import { ActionTimeline } from './ActionTimeline'
import { buildActionTimeline, buildThinkingActivity, type ThinkingStatus } from './activity-model'
import { ThinkingTab, type ActivityLoadState, type FailureLinks } from './ThinkingTab'

type ActivityTab = 'thinking' | 'timeline' | 'benchmark'

const ACTIVITY_TABS = ['thinking', 'timeline', 'benchmark'] as const
const TAB_LABELS: Record<ActivityTab, string> = {
  thinking: 'Thinking',
  timeline: 'Timeline',
  benchmark: 'Benchmark',
}
const NO_EVENTS: ExecutionEventV2[] = []

const TimelineEmptyState: FC<{ loading?: boolean; title: string; description: string }> = ({
  loading = false,
  title,
  description,
}) => (
  <Flex
    direction="col"
    align="center"
    justify="center"
    className="flex-1 px-8 py-12 text-center"
    data-testid="timeline-empty-state"
  >
    {loading ? (
      <Spinner size="medium" aria-label="Loading timeline" />
    ) : (
      <ThinkingReasoning className="text-secondary h-9 w-9" />
    )}
    <Text kind="label/semibold/md" className="text-primary mt-4">
      {title}
    </Text>
    <Text kind="body/regular/sm" className="text-secondary mt-2 max-w-sm">
      {description}
    </Text>
  </Flex>
)

/**
 * Live mode loads a job's export (`loadJobExport`) when the store lacks what the panel shows: the
 * whole run of an answer this page did not stream (a reopened session), or, once a streamed run has
 * ended, the receipts its events point at (the SSE stream carries events only). Each need loads once.
 */
const useJobHistory = (
  jobId: string | null,
  run: ExecutionRun | undefined,
  finished: boolean,
  wanted: boolean
): { runLoad: ActivityLoadState; receiptsLoading: boolean } => {
  const missingReceipts = useMemo(
    () => Boolean(run?.events.some((event) => event.artifactRefs.some((id) => !run.receipts[id]))),
    [run]
  )
  const need =
    !wanted || !jobId ? null : !run ? 'run' : finished && missingReceipts ? 'receipts' : null
  const key = need && jobId ? `${jobId}:${need}` : null
  const [load, setLoad] = useState<{ key: string | null; state: ActivityLoadState }>({
    key: null,
    state: 'idle',
  })
  useEffect(() => {
    if (!key || !jobId) return
    let active = true
    setLoad({ key, state: 'loading' })
    loadJobExport(jobId).then(
      () => active && setLoad({ key, state: 'idle' }),
      () => active && setLoad({ key, state: 'error' })
    )
    return () => {
      active = false
    }
  }, [jobId, key])
  const current = key !== null && load.key === key ? load.state : 'idle'
  return {
    runLoad: need === 'run' ? current : 'idle',
    receiptsLoading: need === 'receipts' && current === 'loading',
  }
}

export const ActivityPanel: FC<ActivityPanelProps> = ({ jobId, streaming, open }) => {
  const [activeTab, setActiveTab] = useState<ActivityTab>('thinking')
  const { mode, phoenixUrl } = useAppConfig()
  const run = useExecutionRun(jobId)
  // A recorded run (every run in replay mode) has no live job behind it
  const live = mode === 'live' && !run?.recorded
  const events = run?.events ?? NO_EVENTS
  const jobStatus = run?.jobStatus ?? null
  const receipts = run?.receipts

  useEffect(() => {
    if (streaming) setActiveTab('thinking')
  }, [streaming])

  const projection = useMemo(() => projectRun(events, jobStatus), [events, jobStatus])
  const finished = runEnded(projection.status)
  const { runLoad: loadState, receiptsLoading } = useJobHistory(
    jobId,
    run,
    finished,
    open && live && !streaming
  )
  const spanIds = useMemo(
    () =>
      new Map(
        Object.values(receipts ?? {}).flatMap((receipt) =>
          receipt.spanId ? [[receipt.invocationId, receipt.spanId] as const] : []
        )
      ),
    [receipts]
  )
  const callStates = useMemo(
    () =>
      new Map<string, ThinkingStatus>(
        projection.toolCalls.map((call) => [call.invocationId, call.state])
      ),
    [projection]
  )
  const thinking = useMemo(
    () => (jobId ? buildThinkingActivity(events, callStates, spanIds) : []),
    [callStates, events, jobId, spanIds]
  )
  const terminal = events.some(
    (event) =>
      event.eventKind.startsWith('run.') && event.state !== 'started' && event.state !== 'progress'
  )
  const timeline = useMemo(
    () => (terminal ? buildActionTimeline(events, spanIds) : null),
    [events, spanIds, terminal]
  )
  const traceUrl = useTraceUrl(jobId ?? '', live && jobId ? phoenixUrl : null, finished)
  const links: FailureLinks = useMemo(
    () => ({
      spanUrl: (spanId) => (phoenixUrl && spanId ? phoenixSpanUrl(phoenixUrl, spanId) : null),
      traceUrl,
      traceLoading: Boolean(phoenixUrl) && live && !finished,
    }),
    [finished, live, phoenixUrl, traceUrl]
  )

  let timelineContent
  if (!jobId) {
    timelineContent = (
      <TimelineEmptyState
        title="No completed run"
        description="Ask a question, then return here to inspect its action timing."
      />
    )
  } else if (loadState === 'loading') {
    timelineContent = (
      <TimelineEmptyState
        loading
        title="Loading timeline…"
        description="Restoring the durable execution record for this answer."
      />
    )
  } else if (loadState === 'error') {
    timelineContent = (
      <TimelineEmptyState
        title="Timeline unavailable"
        description="The answer remains available, but its execution record could not be restored."
      />
    )
  } else if (streaming || !terminal) {
    timelineContent = (
      <TimelineEmptyState
        title="Timeline available after completion"
        description="The live narrative remains in Thinking. This chart is assembled from the final durable trace."
      />
    )
  } else if (!timeline) {
    timelineContent = (
      <TimelineEmptyState
        title="No timing data"
        description="This run completed without enough display-safe timestamp data to draw a timeline."
      />
    )
  } else {
    timelineContent = (
      <ActionTimeline
        model={timeline}
        receipts={receipts ?? {}}
        receiptsLoading={receiptsLoading}
        spanUrl={links.spanUrl}
      />
    )
  }

  return (
    <>
      <div
        role="tablist"
        aria-label="Agent activity views"
        className="border-base bg-surface-base flex shrink-0 gap-6 border-b px-6"
        onKeyDown={(event) => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
          const tabs = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
          const currentIndex = Math.max(
            0,
            tabs.indexOf(document.activeElement as HTMLButtonElement)
          )
          const nextIndex =
            event.key === 'Home'
              ? 0
              : event.key === 'End'
                ? tabs.length - 1
                : event.key === 'ArrowLeft'
                  ? (currentIndex - 1 + tabs.length) % tabs.length
                  : (currentIndex + 1) % tabs.length
          event.preventDefault()
          tabs[nextIndex]?.focus()
          tabs[nextIndex]?.click()
        }}
      >
        {ACTIVITY_TABS.map((tab) => (
          <button
            key={tab}
            id={`activity-${tab}-tab`}
            type="button"
            role="tab"
            aria-controls={`activity-${tab}-panel`}
            aria-selected={activeTab === tab}
            tabIndex={activeTab === tab ? 0 : -1}
            onClick={() => setActiveTab(tab)}
            className={`relative cursor-pointer py-3 text-sm font-semibold capitalize transition-colors after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:content-[''] ${
              activeTab === tab
                ? 'text-primary after:bg-[#76b900]'
                : 'text-secondary hover:text-primary after:bg-transparent'
            }`}
          >
            {TAB_LABELS[tab]}
          </button>
        ))}
      </div>

      <div
        id="activity-thinking-panel"
        role="tabpanel"
        aria-labelledby="activity-thinking-tab"
        hidden={activeTab !== 'thinking'}
        className="flex min-h-0 flex-1 flex-col overflow-hidden py-4 pl-6 pr-8"
      >
        {open && activeTab === 'thinking' ? (
          <ThinkingTab
            items={thinking}
            loadState={loadState}
            streaming={streaming}
            failed={projection.status === 'failed' || projection.status === 'cancelled'}
            completed={projection.status === 'completed'}
            links={links}
          />
        ) : null}
      </div>
      <div
        id="activity-timeline-panel"
        role="tabpanel"
        aria-labelledby="activity-timeline-tab"
        hidden={activeTab !== 'timeline'}
        className="flex min-h-0 flex-1 flex-col overflow-hidden py-4 pl-6 pr-8"
      >
        {open && activeTab === 'timeline' ? timelineContent : null}
      </div>
      <div
        id="activity-benchmark-panel"
        role="tabpanel"
        aria-labelledby="activity-benchmark-tab"
        hidden={activeTab !== 'benchmark'}
        className="flex min-h-0 flex-1 flex-col overflow-hidden py-4 pl-6 pr-8"
      >
        {open && activeTab === 'benchmark' ? (
          <BenchmarkTab
            key={jobId ?? 'none'}
            jobId={jobId}
            events={events}
            timeline={timeline}
            benchmark={run?.benchmark ?? null}
            retrievalBenchmark={run?.retrievalBenchmark ?? null}
            recorded={!live}
          />
        ) : null}
      </div>
    </>
  )
}

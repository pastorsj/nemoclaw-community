// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * useDeepResearch Hook
 *
 * Follows the SSE stream of the current job. Connects when a job starts (or
 * is reconnected after a reload), forwards every record to the execution
 * view, keeps the final answer, and settles the answer message when the job
 * reaches a terminal status.
 *
 * A stream that breaks says nothing about the job, which runs on the API: the
 * page may be unloading (a reload or a navigation closes it), or a proxy may
 * have dropped it. So the hook asks the API for the job's status before
 * reporting anything: it follows the job again while it runs, settles it if it
 * ended, and reports a lost connection only if the API stays unreachable.
 */

'use client'

import { useEffect } from 'react'
import { createDeepResearchClient, getJobStatus } from '@/adapters/api'
import { useLayoutStore } from '@/features/layout/store'
import { useExecutionFeature } from '@/shared/context'
import { useChatStore } from '../store'

/** Waits before each new status check while the API cannot be reached after the stream broke */
const STATUS_RETRY_DELAYS_MS = [1_000, 2_000, 4_000]
/** How many times one job's broken stream is opened again while the job still runs */
const MAX_STREAM_RECONNECTS = 3

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))

export const useDeepResearch = (): void => {
  const { onJobEvent, ActivityPanel } = useExecutionFeature()
  const jobId = useChatStore((s) => s.deepResearchJobId)
  const isStreaming = useChatStore((s) => s.isDeepResearchStreaming)

  useEffect(() => {
    if (!jobId || !isStreaming) return

    const store = useChatStore.getState()
    let disposed = false
    let client: ReturnType<typeof createDeepResearchClient> | null = null
    let reconnects = 0

    // Events of a job the user navigated away from must not touch the store.
    const isCurrentJob = (): boolean => {
      const state = useChatStore.getState()
      return !disposed && state.isDeepResearchStreaming && state.deepResearchJobId === jobId
    }

    const follow = (): void => {
      client = createDeepResearchClient({
        jobId,
        callbacks: {
          onEvent: onJobEvent,
          onStreamStart: () => {
            if (isCurrentJob()) store.setCurrentStatus('researching')
          },
          onJobStatus: (status, error) => {
            if (!isCurrentJob()) return
            if (status === 'submitted' || status === 'running') {
              store.updateDeepResearchStatus(status)
            } else {
              store.finishDeepResearch(status, error)
            }
          },
          onFinalReport: (content) => {
            if (!isCurrentJob()) return
            store.setReportContent(content)
            store.setCurrentStatus('writing')
          },
          onError: (error) => void recover(error.message),
          onDisconnect: () => void recover('the stream was closed'),
        },
      })
      client.connect()
    }

    /** The stream broke: ask the API what became of the job before reporting anything. */
    const recover = async (reason: string): Promise<void> => {
      client = null
      for (const delay of [0, ...STATUS_RETRY_DELAYS_MS]) {
        if (delay) await sleep(delay)
        if (!isCurrentJob()) return
        let job: Awaited<ReturnType<typeof getJobStatus>>
        try {
          job = await getJobStatus(jobId)
        } catch {
          continue // The API, or the network of a page being left, cannot answer now
        }
        if (!isCurrentJob()) return
        if (job.status === 'submitted' || job.status === 'running') {
          if (reconnects++ >= MAX_STREAM_RECONNECTS) break
          // The stream replays from the start; the execution view drops what it already has.
          follow()
          return
        }
        store.finishDeepResearch(job.status, job.error)
        return
      }
      if (isCurrentJob())
        store.finishDeepResearch('failure', `Lost connection to the run: ${reason}`)
    }

    // Defer the connection so React StrictMode's mount/unmount/mount cycle
    // opens only one stream.
    const connectTimer = setTimeout(() => {
      follow()
      if (ActivityPanel) useLayoutStore.getState().openRightPanel('research')
    }, 50)

    return () => {
      disposed = true
      clearTimeout(connectTimer)
      client?.disconnect()
    }
  }, [jobId, isStreaming, onJobEvent, ActivityPanel])
}

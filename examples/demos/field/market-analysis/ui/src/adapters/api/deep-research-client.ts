// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Async Job Client
 *
 * Every question runs as a durable job on the API (`/v1/jobs/async/...`).
 * This module submits jobs, follows their Server-Sent Events stream, and
 * wraps the small REST surface (status, report, cancel). The browser always
 * calls the same-origin proxy under `/api/v1`, which forwards to the API.
 */

import type { JobStreamEvent } from '@/shared/context'

const JOBS_BASE = '/api/v1/jobs/async'

/** Job status values */
export type DeepResearchJobStatus = 'submitted' | 'running' | 'success' | 'failure' | 'interrupted'

/**
 * SSE event names the client subscribes to. `EventSource` only delivers
 * named events that have a listener, so a new event type must be added here
 * before anything (including the execution view) can see it.
 */
const JOB_STREAM_EVENTS = [
  'stream.start',
  'stream.mode',
  'job.status',
  'job.heartbeat',
  'artifact.update',
  'execution.v2',
] as const

/** Callbacks for one job's SSE stream */
interface DeepResearchCallbacks {
  /** Every record, before any typed callback below. Feeds the execution view. */
  onEvent?: (event: JobStreamEvent) => void
  /** The stream is connected and replaying or following the job */
  onStreamStart?: () => void
  /** Job status changed; terminal statuses close the stream */
  onJobStatus?: (status: DeepResearchJobStatus, error?: string) => void
  /** The job is alive during a long operation */
  onHeartbeat?: () => void
  /** The job published its final answer (markdown) */
  onFinalReport?: (content: string) => void
  /** Transport failure after reconnection attempts are exhausted */
  onError?: (error: Error) => void
  /** The browser gave up the connection */
  onDisconnect?: () => void
}

interface DeepResearchStreamOptions {
  /** Job ID to stream */
  jobId: string
  /** Event callbacks */
  callbacks: DeepResearchCallbacks
  /** Resume after this event ID instead of replaying from the start */
  lastEventId?: string
}

interface DeepResearchClient {
  /** Connect to the SSE stream */
  connect: () => void
  /** Disconnect from the SSE stream */
  disconnect: () => void
  /** Check if connected */
  isConnected: () => boolean
  /** Get the last received event ID (for reconnection) */
  getLastEventId: () => string | null
}

/** Max consecutive reconnection failures before surfacing an error to the caller */
const MAX_RECONNECT_ATTEMPTS = 5

const TERMINAL_STATUSES: readonly DeepResearchJobStatus[] = ['success', 'failure', 'interrupted']

const parseEventData = (data: string): unknown => {
  try {
    return JSON.parse(data)
  } catch {
    return data
  }
}

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' ? (value as Record<string, unknown>) : {}

/** `job.status` payloads arrive either wrapped in `data` or flat. */
const readJobStatus = (payload: unknown): { status?: DeepResearchJobStatus; error?: string } => {
  const record = asRecord(payload)
  const body = asRecord(record.data ?? record)
  return {
    status: body.status as DeepResearchJobStatus | undefined,
    error: typeof body.error === 'string' ? body.error : undefined,
  }
}

/** The final answer is an `artifact.update` output with `output_category: final_report`. */
const readFinalReport = (payload: unknown): string | null => {
  const record = asRecord(payload)
  const artifact = asRecord(record.data ?? record)
  return artifact.type === 'output' &&
    artifact.output_category === 'final_report' &&
    typeof artifact.content === 'string'
    ? artifact.content
    : null
}

/**
 * Create an SSE client for one job.
 *
 * Uses native EventSource, which reconnects on its own and resends the last
 * event ID. Terminal job statuses close the stream so it does not reconnect.
 */
export const createDeepResearchClient = (
  options: DeepResearchStreamOptions
): DeepResearchClient => {
  const { jobId, callbacks, lastEventId } = options

  let eventSource: EventSource | null = null
  let lastReceivedEventId: string | null = lastEventId || null
  let isTerminated = false
  let reconnectAttempts = 0

  const buildStreamUrl = (): string => {
    const url = `${JOBS_BASE}/job/${encodeURIComponent(jobId)}/stream`
    return lastReceivedEventId ? `${url}/${encodeURIComponent(lastReceivedEventId)}` : url
  }

  const close = (): void => {
    isTerminated = true
    eventSource?.close()
    eventSource = null
  }

  const handleMessage = (event: MessageEvent, type: string): void => {
    if (event.lastEventId) {
      lastReceivedEventId = event.lastEventId
    }
    const data = parseEventData(event.data)

    // A consumer failure must never interrupt the user-facing stream.
    try {
      callbacks.onEvent?.({ jobId, cursor: event.lastEventId || null, type, data })
    } catch (error) {
      console.warn('[SSE] Job event consumer failed', error)
    }

    switch (type) {
      case 'stream.start':
        callbacks.onStreamStart?.()
        break
      case 'job.heartbeat':
        callbacks.onHeartbeat?.()
        break
      case 'job.status': {
        const { status, error } = readJobStatus(data)
        if (!status) break
        callbacks.onJobStatus?.(status, error)
        // Close on terminal states so EventSource does not reconnect forever.
        if (TERMINAL_STATUSES.includes(status)) close()
        break
      }
      case 'artifact.update': {
        const report = readFinalReport(data)
        if (report !== null) callbacks.onFinalReport?.(report)
        break
      }
    }
  }

  const connect = (): void => {
    if (eventSource) return

    isTerminated = false
    eventSource = new EventSource(buildStreamUrl())

    eventSource.onopen = () => {
      reconnectAttempts = 0
    }
    eventSource.onmessage = (event) => handleMessage(event, 'message')
    for (const type of JOB_STREAM_EVENTS) {
      eventSource.addEventListener(type, (event) => handleMessage(event as MessageEvent, type))
    }

    // EventSource fires onerror on any connection issue and then reconnects on
    // its own (readyState CONNECTING). Only surface an error once the browser
    // gives up (CLOSED) or reconnection keeps failing.
    eventSource.onerror = () => {
      if (isTerminated) {
        close()
        return
      }
      if (eventSource?.readyState === EventSource.CLOSED) {
        eventSource = null
        callbacks.onDisconnect?.()
        return
      }
      reconnectAttempts++
      if (reconnectAttempts <= MAX_RECONNECT_ATTEMPTS) return
      close()
      callbacks.onError?.(
        new Error(`SSE connection failed after ${MAX_RECONNECT_ATTEMPTS} reconnection attempts`)
      )
    }
  }

  return {
    connect,
    disconnect: close,
    isConnected: () => eventSource !== null && eventSource.readyState === EventSource.OPEN,
    getLastEventId: () => lastReceivedEventId,
  }
}

// ============================================================
// REST API Functions
// ============================================================

/** Input accepted by the job submission route. */
interface SubmitJobRequest {
  input: string
  conversationId: string
  dataSources: string[]
  /** Caller-chosen job ID, persisted before submission so a reload can recover the run */
  jobId: string
}

interface SubmitJobResponse {
  job_id: string
  status: DeepResearchJobStatus
}

/** An HTTP error answer to a job request. */
export class ApiRequestError extends Error {
  readonly status: number
  /** Whether the API itself answered (a `detail` body), rather than the UI proxy or one in front of it */
  readonly fromApi: boolean

  constructor(message: string, status: number, fromApi: boolean) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
    this.fromApi = fromApi
  }
}

const errorDetails = async (response: Response): Promise<{ details: string; fromApi: boolean }> => {
  const text = await response.text().catch(() => '')
  try {
    const body = JSON.parse(text) as { error?: { message?: unknown }; detail?: unknown }
    const message = typeof body.error?.message === 'string' ? body.error.message : body.detail
    return {
      details: typeof message === 'string' ? message : text,
      fromApi: body.detail !== undefined,
    }
  } catch {
    return { details: text, fromApi: false } // Not JSON; use the raw text.
  }
}

const request = async <T>(path: string, context: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(`${JOBS_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    const { details, fromApi } = await errorDetails(response)
    throw new ApiRequestError(
      `${context}: ${response.status}${details ? ` - ${details}` : ''}`,
      response.status,
      fromApi
    )
  }
  return response.json() as Promise<T>
}

/** Submit one question to the agent as a durable job. */
export const submitJob = ({
  input,
  conversationId,
  dataSources,
  jobId,
}: SubmitJobRequest): Promise<SubmitJobResponse> =>
  request('/submit', 'Failed to start research', {
    method: 'POST',
    headers: { 'conversation-id': conversationId },
    // Admission can outlive a navigation. The caller persists the job ID
    // first, so a reloaded page can recover the run without this response.
    keepalive: true,
    body: JSON.stringify({ agent_type: 'hermes', input, data_sources: dataSources, job_id: jobId }),
  })

/** Get job status */
export const getJobStatus = (
  jobId: string
): Promise<{ job_id: string; status: DeepResearchJobStatus; error: string | null }> =>
  request(`/job/${encodeURIComponent(jobId)}`, 'Failed to get job status')

/** Get the job's final report */
export const getJobReport = (
  jobId: string
): Promise<{ job_id: string; has_report: boolean; report: string | null }> =>
  request(`/job/${encodeURIComponent(jobId)}/report`, 'Failed to get job report')

/** Cancel a running job */
export const cancelJob = (jobId: string): Promise<{ cancelled: boolean }> =>
  request(`/job/${encodeURIComponent(jobId)}/cancel`, 'Failed to cancel job', { method: 'POST' })

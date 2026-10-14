// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Links into the private Phoenix (PHOENIX_URL). Phoenix redirects a trace or
 * span id to its page. The API resolves a job's trace id server-side
 * (`GET /v1/jobs/async/job/{id}/trace` → `{trace_id}`); receipts carry their
 * own span id.
 */

import { useEffect, useState } from 'react'

const TRACE_ID = /^[0-9a-f]{32}$/
const SPAN_ID = /^[0-9a-f]{16}$/

const redirect = (phoenixUrl: string, kind: 'traces' | 'spans', id: string) =>
  `${phoenixUrl.replace(/\/+$/, '')}/redirects/${kind}/${id}`

export const phoenixTraceUrl = (phoenixUrl: string, traceId: string | null): string | null =>
  traceId && TRACE_ID.test(traceId) ? redirect(phoenixUrl, 'traces', traceId) : null

export const phoenixSpanUrl = (phoenixUrl: string, spanId: string | null): string | null =>
  spanId && SPAN_ID.test(spanId) ? redirect(phoenixUrl, 'spans', spanId) : null

export const fetchTraceId = async (jobId: string, signal?: AbortSignal): Promise<string | null> => {
  const response = await fetch(`/api/v1/jobs/async/job/${encodeURIComponent(jobId)}/trace`, {
    signal,
  })
  if (!response.ok) return null
  const body: unknown = await response.json()
  const traceId = (body as { trace_id?: unknown } | null)?.trace_id
  return typeof traceId === 'string' ? traceId : null
}

/** The job's Phoenix trace URL, looked up once the run has finished; null until then. */
export const useTraceUrl = (
  jobId: string,
  phoenixUrl: string | null,
  finished: boolean
): string | null => {
  const [traceId, setTraceId] = useState<string | null>(null)
  useEffect(() => {
    if (!phoenixUrl || !finished) return
    const controller = new AbortController()
    fetchTraceId(jobId, controller.signal)
      .then(setTraceId)
      .catch(() => setTraceId(null))
    return () => controller.abort()
  }, [jobId, phoenixUrl, finished])
  return phoenixUrl ? phoenixTraceUrl(phoenixUrl, traceId) : null
}

// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The execution feature, plugged into the base UI through its one slot
 * (`ExecutionFeature`, wired in app/providers.tsx).
 */

import type { ExecutionFeature } from '@/shared/context'
import { ExecutionWorkspace } from './ExecutionWorkspace'
import { recordings } from './replay/sources'
import { useExecutionStore } from './store'
import { ActivityPanel } from './activity/ActivityPanel'

const asRecord = (value: unknown): Record<string, unknown> =>
  typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {}

/** A `job.status` frame's status; the payload is flat or wrapped in `data`. */
const statusOf = (data: unknown): string | null => {
  const frame = asRecord(data)
  const { status } = asRecord(frame.data ?? frame)
  return typeof status === 'string' ? status : null
}

export const executionFeature: ExecutionFeature = {
  onJobEvent: ({ jobId, type, data, cursor }) => {
    const store = useExecutionStore.getState()
    if (type === 'execution.v2') store.addEvent(jobId, data, cursor)
    if (type === 'job.status') {
      const status = statusOf(data)
      if (status) store.setJobStatus(jobId, status)
    }
  },
  Workspace: ExecutionWorkspace,
  ActivityPanel,
  recordings,
}

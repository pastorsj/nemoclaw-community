// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Deep Research Session Storage
 *
 * Persists in-progress job metadata to sessionStorage, keyed by job ID,
 * when the user leaves the conversation that owns a running job.
 */

import type { DeepResearchJobStatus } from '../types'

const STORAGE_KEY_PREFIX = 'aiq-deep-research-'

const getStorageKey = (jobId: string): string => `${STORAGE_KEY_PREFIX}${jobId}`

/** Lightweight job metadata; the job's events are replayed from the SSE stream. */
export interface DeepResearchSessionState {
  jobId: string
  ownerConversationId: string | null
  activeMessageId: string | null
  status: DeepResearchJobStatus | null
}

/**
 * Save job state to sessionStorage (keyed by jobId)
 */
export const saveDeepResearchToSession = (state: DeepResearchSessionState): void => {
  try {
    sessionStorage.setItem(
      getStorageKey(state.jobId),
      JSON.stringify({ ...state, timestamp: Date.now() })
    )
  } catch (error) {
    console.warn('Failed to save deep research state to sessionStorage:', error)
  }
}

/**
 * Clear job state from sessionStorage for a specific job
 */
export const clearDeepResearchSession = (jobId: string): void => {
  try {
    sessionStorage.removeItem(getStorageKey(jobId))
  } catch (error) {
    console.warn('Failed to clear deep research state from sessionStorage:', error)
  }
}

/**
 * Clear all job state from sessionStorage
 */
export const clearAllDeepResearchSessions = (): void => {
  try {
    const keysToRemove: string[] = []
    for (let i = 0; i < sessionStorage.length; i++) {
      const key = sessionStorage.key(i)
      if (key?.startsWith(STORAGE_KEY_PREFIX)) {
        keysToRemove.push(key)
      }
    }
    keysToRemove.forEach((key) => sessionStorage.removeItem(key))
  } catch (error) {
    console.warn('Failed to clear all deep research sessions:', error)
  }
}

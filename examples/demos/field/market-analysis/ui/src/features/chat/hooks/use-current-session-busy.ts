// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * useIsCurrentSessionBusy Hook
 *
 * Whether the CURRENT session is submitting a question or has a running job.
 * Used to disable data source changes and session management meanwhile.
 *
 * Checks BOTH ephemeral state (fast path for normal operation) AND persisted
 * message history (safety net for page refresh recovery).
 *
 * For per-session checks (e.g., session deletion), use store.isSessionBusy() instead.
 */

'use client'

import { useChatStore } from '../store'
import { hasActiveDeepResearchJob } from '../lib/session-activity'

export const useIsCurrentSessionBusy = (): boolean => {
  const isStreaming = useChatStore((state) => state.isStreaming)
  const isDeepResearchStreaming = useChatStore((state) => state.isDeepResearchStreaming)
  const deepResearchStatus = useChatStore((state) => state.deepResearchStatus)

  // Job state is global in the store, so scope it to the owning session.
  const ownsDeepResearch = useChatStore(
    (state) =>
      state.deepResearchOwnerConversationId !== null &&
      state.deepResearchOwnerConversationId === state.currentConversation?.id
  )

  // Returns a boolean, so Zustand only re-renders when the value changes.
  const hasActiveJobInHistory = useChatStore((state) =>
    state.currentConversation ? hasActiveDeepResearchJob(state.currentConversation.messages) : false
  )

  return (
    isStreaming ||
    (ownsDeepResearch && isDeepResearchStreaming) ||
    (ownsDeepResearch &&
      (deepResearchStatus === 'submitted' || deepResearchStatus === 'running')) ||
    hasActiveJobInHistory
  )
}

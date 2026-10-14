// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Session Activity Utilities
 *
 * Pure functions to derive session activity state from PERSISTED data.
 * These survive page refresh because they read from localStorage-backed
 * conversation message history rather than ephemeral store fields.
 */

import type { ChatMessage, Conversation, DeepResearchJobStatus } from '../types'

/** Non-terminal job statuses that indicate an active server-side job */
const ACTIVE_JOB_STATUSES: readonly DeepResearchJobStatus[] = ['submitted', 'running']

/** Epoch ms of the most recent user-typed message, or null when the session has none. */
export const lastUserMessageTime = (messages: ChatMessage[]): number | null => {
  for (let i = messages.length - 1; i >= 0; i--) {
    const messageType =
      messages[i].messageType || (messages[i].role === 'user' ? 'user' : 'assistant')
    if (messageType === 'user') {
      return new Date(messages[i].timestamp).getTime()
    }
  }
  return null
}

/** Epoch ms used to order the session list: the last user query, falling back to last update or creation. */
const sessionRecency = (conversation: Conversation): number => {
  const userTime = lastUserMessageTime(conversation.messages)
  if (userTime !== null) return userTime
  return new Date(conversation.updatedAt ?? conversation.createdAt).getTime()
}

/** Sessions ordered most-recent first by the timestamp of their last user query (stable for ties). */
export const sortConversationsByLastUserMessage = <T extends Conversation>(
  conversations: readonly T[]
): T[] => [...conversations].sort((a, b) => sessionRecency(b) - sessionRecency(a))

const getLatestJobMessage = (messages: ChatMessage[]): ChatMessage | null => {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i]
    if (message.messageType === 'agent_response' && message.deepResearchJobId) {
      return message
    }
  }
  return null
}

/**
 * Check if a conversation's latest job is still running.
 *
 * Scans messages in reverse (most recent first); O(1) in practice since job
 * messages are always near the end.
 */
export const hasActiveDeepResearchJob = (messages: ChatMessage[]): boolean => {
  const status = getLatestJobMessage(messages)?.deepResearchJobStatus
  return Boolean(status && ACTIVE_JOB_STATUSES.includes(status))
}

/** Whether a conversation's latest job finished with an answer. */
export const hasCompletedDeepResearchReport = (messages: ChatMessage[]): boolean => {
  const message = getLatestJobMessage(messages)
  return Boolean(message?.deepResearchJobStatus === 'success' && message.content.trim())
}

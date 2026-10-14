// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Chat Feature Types
 *
 * Type definitions for chat messages, conversations, and state.
 */

import type { DeepResearchJobStatus } from '@/adapters/api'
import type { RecordedSession } from '@/shared/context'

export type { DeepResearchJobStatus }

/** Message role types */
export type MessageRole = 'user' | 'assistant'

/** Message types for different display purposes */
export type MessageType = 'user' | 'agent_response' | 'error' | 'deep_research_banner'

/**
 * Deep research banner types for status notifications.
 * 'expired': the API no longer knows the job (404/410).
 */
export type DeepResearchBannerType = 'starting' | 'failure' | 'cancelled' | 'expired'

/** Status of the current run, shown while it is active */
export type StatusType = 'thinking' | 'researching' | 'writing' | 'complete' | 'error'

/** Error codes using dot-notation for extensibility */
export type ErrorCode =
  // Agent errors
  | 'agent.response_failed'
  | 'agent.response_interrupted'
  | 'agent.deep_research_failed'
  // System errors
  | 'system.unknown'

/** Error card data for error messages */
export interface ErrorCardData {
  errorCode: ErrorCode
  errorMessage?: string
  errorDetails?: string
}

/** Deep research banner data for status notifications */
export interface DeepResearchBannerData {
  /** Type of banner: a running job, or one that ended without an answer */
  bannerType: DeepResearchBannerType
  /** Job ID for identification */
  jobId: string
}

/** Individual chat message */
export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  timestamp: Date
  /** Type of message for routing to correct display */
  messageType?: MessageType
  /** Error card data for error messages */
  errorData?: ErrorCardData
  /** Deep research banner data for status notifications */
  deepResearchBannerData?: DeepResearchBannerData
  /** Completion timestamp of a job's answer (start timestamp stays in `timestamp`) */
  responseCompletedAt?: Date

  // Job persistence fields (for session restoration across tab close/reopen)

  /** Job that produced (or is producing) this answer */
  deepResearchJobId?: string
  /** Job status at time of save */
  deepResearchJobStatus?: DeepResearchJobStatus
  /** Whether this message's job is still running */
  isDeepResearchActive?: boolean
  /** Data sources that were enabled when this message was sent */
  enabledDataSources?: string[]
}

/** Conversation/Session */
export interface Conversation {
  id: string
  /** Owner of this session - used to filter sessions by user */
  userId: string
  title: string
  messages: ChatMessage[]
  createdAt: Date
  updatedAt: Date
  /** Per-session enabled data source IDs (persisted across refresh) */
  enabledDataSourceIds?: string[]
  /** True for recorded sessions shown in replay mode: never persisted or continued */
  readOnly?: boolean
}

/** Chat state for Zustand store */
export interface ChatState {
  /** Current user ID - used for filtering sessions */
  currentUserId: string | null
  /** Current active conversation */
  currentConversation: Conversation | null
  /** All conversations for the sessions sidebar (includes all users) */
  conversations: Conversation[]
  /** Whether a question is being submitted */
  isStreaming: boolean
  /** Whether we're waiting for the job to be admitted */
  isLoading: boolean
  /** ID of the latest user message (the chat scrolls to it when it changes) */
  currentUserMessageId: string | null
  /** Current status type (for status indicators) */
  currentStatus: StatusType | null

  // Job SSE state
  /** Current job ID (null when not active) */
  deepResearchJobId: string | null
  /** Whether the job's SSE stream is currently open */
  isDeepResearchStreaming: boolean
  /** Current job status */
  deepResearchStatus: DeepResearchJobStatus | null
  /** Conversation ID that owns the current job stream (for session isolation) */
  deepResearchOwnerConversationId: string | null
  /** Message ID of the answer placeholder (patched on completion) */
  activeDeepResearchMessageId: string | null
  /** Final answer received for the current job */
  reportContent: string
  /**
   * Jobs whose submission this page has sent and not yet seen answered. The API
   * does not know such a job yet, so nothing asks for its status meanwhile.
   */
  submittingJobIds: string[]
}

/** Chat actions for Zustand store */
export interface ChatActions {
  /** Set the current user ID */
  setCurrentUser: (userId: string | null) => void
  /** Start a new unsaved session draft; persisted only after first interaction. */
  startNewSessionDraft: () => void
  /** Ensure a persisted session exists, creating one if needed. Returns its ID. */
  ensureSession: () => string | undefined
  /** Select a conversation by ID */
  selectConversation: (conversationId: string) => void
  /** Show a recorded session (replay mode) as a read-only conversation */
  openRecordedSession: (session: RecordedSession) => void
  /** Add a user message to the current conversation */
  addUserMessage: (content: string, metadata?: { enabledDataSources?: string[] }) => ChatMessage
  /** Set loading state */
  setLoading: (loading: boolean) => void
  /** Set streaming state */
  setStreaming: (streaming: boolean) => void
  /** Delete a conversation (cancels its running job) */
  deleteConversation: (conversationId: string) => void
  /** Delete all conversations of the current user */
  deleteAllConversations: () => void
  /** Rename a conversation */
  updateConversationTitle: (conversationId: string, title: string) => void
  /** Save the enabled data sources to the current conversation */
  saveDataSourcesToConversation: (ids: string[]) => void
  /** Set the current run status */
  setCurrentStatus: (status: StatusType | null) => void
  /** Add an answer placeholder with job metadata; returns its message ID */
  addAgentResponseWithMeta: (content: string, meta: Partial<ChatMessage>) => string
  /** Patch one message of a conversation */
  patchConversationMessage: (
    conversationId: string,
    messageId: string,
    patch: Partial<ChatMessage>
  ) => void
  /** Add an error card to the current conversation */
  addErrorCard: (code: ErrorCode, message?: string, details?: string) => void
  /** Remove an error card */
  dismissErrorCard: (messageId: string) => void
  /** Replace the status banner of a job (null removes it) */
  addDeepResearchBanner: (
    bannerType: DeepResearchBannerType | null,
    jobId: string,
    conversationId?: string
  ) => void
  /** Record that a job's submission was sent and its answer is awaited */
  beginJobSubmission: (jobId: string) => void
  /** Record that a job's submission was answered (admitted or refused) */
  endJobSubmission: (jobId: string) => void
  /** Start following a submitted job */
  startDeepResearch: (jobId: string, messageId: string) => void
  /** Update the status of a job that is still running */
  updateDeepResearchStatus: (status: DeepResearchJobStatus) => void
  /** Store the final answer of the current job */
  setReportContent: (content: string) => void
  /** Record the current job's terminal status and stop following it */
  finishDeepResearch: (status: DeepResearchJobStatus, error?: string | null) => void
  /** Persist the active job to sessionStorage before leaving its conversation */
  persistDeepResearchToSession: () => void
  /** Resume following a job that was running when the page was left */
  reconnectToActiveJob: () => Promise<void>
  /** Resolve 'starting' banners whose job finished while the page was closed */
  cleanupOrphanedStartingBanners: () => Promise<void>
  /** Refresh the status of every conversation's latest job */
  refreshDeepResearchSessionStatuses: () => Promise<void>
  /** Cancel the current job; the stream then delivers the terminal status */
  cancelActiveDeepResearchJob: () => Promise<void>
  /** Restore ephemeral state for a conversation after load or switch */
  restoreSessionState: (conversation: Conversation) => void
  /** Whether a conversation has a running job */
  isSessionBusy: (conversationId: string) => boolean
  /** Whether any conversation has a running job */
  hasAnyBusySession: () => boolean
}

/** Combined chat store type */
export type ChatStore = ChatState & ChatActions

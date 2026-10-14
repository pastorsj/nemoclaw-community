// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Chat Store
 *
 * Zustand store for managing chat state including messages, conversations,
 * and the lifecycle of the job that answers the current question.
 */

import { create } from 'zustand'
import {
  devtools,
  persist,
  createJSONStorage,
  type StorageValue,
  type PersistStorage,
} from 'zustand/middleware'
import { v4 as uuidv4 } from 'uuid'
import { cancelJob, getJobReport, getJobStatus } from '@/adapters/api'
import type { RecordedSession } from '@/shared/context'
import type {
  ChatStore,
  ChatState,
  Conversation,
  ChatMessage,
  ErrorCode,
  DeepResearchJobStatus,
  DeepResearchBannerType,
} from './types'
import { getErrorMeta } from './lib/error-registry'
import {
  saveDeepResearchToSession,
  clearDeepResearchSession,
  clearAllDeepResearchSessions,
} from './lib/deep-research-session-storage'
import { isUnavailableDeepResearchJobError } from './lib/deep-research-errors'
import { hasActiveDeepResearchJob } from './lib/session-activity'
import {
  logStorageWrite,
  logQuotaExceededPruning,
  logCriticalSessionsClear,
  logStorageAvailability,
  logExternalStorageEvent,
  logStoreHydration,
} from './lib/storage-logger'
import { ensureStorageCapacity } from './lib/storage-manager'
import { useLayoutStore } from '@/features/layout/store'

/** If the stream does not deliver a terminal status this long after a cancel, settle locally. */
const CANCEL_FALLBACK_TIMEOUT_MS = 5000

const TERMINAL_STATUSES: readonly DeepResearchJobStatus[] = ['success', 'failure', 'interrupted']

/**
 * The API knows a job only once it has admitted its submission, and a page can ask for it before:
 * a reload while the submission (sent with keepalive) is still on its way. So for this long after
 * its question, a job the API does not know (404) may still be admitted, not gone.
 */
const ADMISSION_GRACE_MS = 10_000
/** While a job may still be admitted, its status is asked for again after these delays. */
const ADMISSION_RETRY_DELAYS_MS = [250, 500, 1_000, 2_000, 3_000, 4_000]

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))

/** Whether the job of an answer placeholder was submitted moments ago (its submission may be pending). */
const mayStillBeAdmitted = (message: ChatMessage): boolean =>
  Math.abs(Date.now() - new Date(message.timestamp).getTime()) < ADMISSION_GRACE_MS

/**
 * A job's status, waiting out its admission if it was submitted moments ago. Null if `wanted`
 * turns false meanwhile; a 404 once the job is no longer that recent is thrown, as is any other
 * error.
 */
const getStatusOnceAdmitted = async (
  message: ChatMessage,
  wanted: () => boolean
): Promise<Awaited<ReturnType<typeof getJobStatus>> | null> => {
  const jobId = message.deepResearchJobId!
  for (const delay of ADMISSION_RETRY_DELAYS_MS) {
    try {
      return await getJobStatus(jobId)
    } catch (error) {
      if (!isUnavailableDeepResearchJobError(error) || !mayStillBeAdmitted(message)) throw error
    }
    await sleep(delay)
    if (!wanted()) return null
  }
  return getJobStatus(jobId)
}

const isQuotaExceededError = (error: unknown): boolean => {
  if (!(error instanceof Error)) return false
  if (error.name === 'QuotaExceededError') return true
  return /quota|exceeded|storage/i.test(error.message)
}

type PersistedChatState = {
  currentUserId: ChatState['currentUserId']
  conversations: ChatState['conversations']
  currentConversation: ChatState['currentConversation']
}

type PersistedChatStorageValue = StorageValue<PersistedChatState>

/**
 * Store only the current conversation's ID — the full object already lives in
 * conversations[]. On read, getItem reconstructs it by ID. A read-only
 * recorded session is not in conversations[], so it is never restored.
 */
const toStoredValue = (value: PersistedChatStorageValue): PersistedChatStorageValue => ({
  ...value,
  state: {
    currentUserId: value.state.currentUserId ?? null,
    conversations: value.state.conversations ?? [],
    currentConversation: (value.state.currentConversation?.id ??
      null) as unknown as Conversation | null,
  },
})

const createResilientStorage = (): PersistStorage<PersistedChatState> | undefined => {
  const base = createJSONStorage<PersistedChatState>(() => localStorage)
  if (!base) {
    logStorageAvailability(false)
    return undefined
  }

  return {
    getItem: async (name: string): Promise<PersistedChatStorageValue | null> => {
      const raw = await base.getItem(name)
      if (!raw) return null

      const storedId = raw.state.currentConversation as unknown as string | null
      raw.state.currentConversation = storedId
        ? ((raw.state.conversations ?? []).find((c) => c.id === storedId) ?? null)
        : null

      return raw
    },
    removeItem: base.removeItem,
    setItem: (name: string, value: PersistedChatStorageValue) => {
      const storedValue = toStoredValue(value)
      const serializedValue = JSON.stringify(storedValue)

      try {
        if (localStorage.getItem(name) === serializedValue) return

        localStorage.setItem(name, serializedValue)
        logStorageWrite(storedValue.state.conversations, storedValue.state.currentUserId)
      } catch (error) {
        if (!isQuotaExceededError(error)) {
          throw error
        }

        const conversations = storedValue.state.conversations
        const sizeKB = Math.round((JSON.stringify(conversations).length * 2) / 1024)
        logQuotaExceededPruning(conversations.length, conversations.length, sizeKB, sizeKB)

        // Last resort: clear all conversations
        try {
          base.removeItem(name)
          base.setItem(name, {
            ...value,
            state: {
              currentUserId: value.state.currentUserId ?? null,
              conversations: [],
              currentConversation: null,
            },
          })
          logCriticalSessionsClear(
            value.state.currentUserId ?? null,
            conversations.map((c) => c.id),
            error
          )
        } catch (finalError) {
          console.error('[SessionsStore] Failed to clear sessions', {
            error: finalError instanceof Error ? finalError.message : String(finalError),
          })
        }
      }
    },
  }
}

/** Job state that belongs to one conversation and is cleared when leaving it. */
const clearedJobState: Pick<
  ChatState,
  | 'deepResearchJobId'
  | 'isDeepResearchStreaming'
  | 'deepResearchStatus'
  | 'deepResearchOwnerConversationId'
  | 'activeDeepResearchMessageId'
  | 'reportContent'
> = {
  deepResearchJobId: null,
  isDeepResearchStreaming: false,
  deepResearchStatus: null,
  deepResearchOwnerConversationId: null,
  activeDeepResearchMessageId: null,
  reportContent: '',
}

const initialState: ChatState = {
  currentUserId: null,
  currentConversation: null,
  conversations: [],
  isStreaming: false,
  isLoading: false,
  currentUserMessageId: null,
  currentStatus: null,
  ...clearedJobState,
  submittingJobIds: [],
}

/**
 * Create a new conversation with default values
 * @param userId - The user ID who owns this conversation
 */
const createNewConversation = (userId: string): Conversation => ({
  id: `s_${uuidv4().replace(/-/g, '_')}`,
  userId,
  title: '',
  messages: [],
  createdAt: new Date(),
  updatedAt: new Date(),
})

/**
 * Generate a title from the first user message
 */
const generateTitle = (content: string): string => {
  const maxLength = 50
  const trimmed = content.trim()
  if (trimmed.length <= maxLength) {
    return trimmed
  }
  return trimmed.substring(0, maxLength) + '...'
}

/**
 * Helper to update conversation in list
 */
const updateConversationInList = (
  conversations: Conversation[],
  updatedConversation: Conversation
): Conversation[] => {
  return conversations.map((c) => (c.id === updatedConversation.id ? updatedConversation : c))
}

const findLatestJobMessage = (messages: ChatMessage[]): ChatMessage | undefined =>
  [...messages].reverse().find((m) => m.messageType === 'agent_response' && m.deepResearchJobId)

const isActiveJobMessage = (message: ChatMessage | undefined): message is ChatMessage =>
  Boolean(
    message?.deepResearchJobId &&
    message.deepResearchJobStatus &&
    !TERMINAL_STATUSES.includes(message.deepResearchJobStatus)
  )

/**
 * Replace a job's banner. Terminal jobs that produced an answer need no
 * banner: the answer itself shows the outcome.
 */
const withDeepResearchBanner = (
  conversation: Conversation,
  bannerType: DeepResearchBannerType | null,
  jobId: string
): Conversation => {
  const messages = conversation.messages.filter(
    (message) =>
      !(
        message.messageType === 'deep_research_banner' &&
        message.deepResearchBannerData?.jobId === jobId
      )
  )
  if (bannerType) {
    messages.push({
      id: uuidv4(),
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      messageType: 'deep_research_banner',
      deepResearchBannerData: { bannerType, jobId },
    })
  }
  return { ...conversation, messages, updatedAt: new Date() }
}

/** Banner for a job that ended: none after an answer, otherwise failure or cancellation. */
const terminalBannerType = (
  status: DeepResearchJobStatus,
  hasAnswer: boolean,
  error?: string | null
): DeepResearchBannerType | null => {
  if (hasAnswer) return null
  return status === 'interrupted' && error?.toLowerCase().includes('cancelled by user')
    ? 'cancelled'
    : 'failure'
}

const getDefaultEnabledDataSourceIds = (): string[] =>
  (useLayoutStore.getState().availableDataSources ?? [])
    .filter((source) => source.default_enabled !== false)
    .map((source) => source.id)

const restoreConversationDataSources = (conversation: Conversation): void => {
  const layoutStore = useLayoutStore.getState()

  if (conversation.enabledDataSourceIds) {
    // Only restore sources that are still available.
    const availableIds = new Set((layoutStore.availableDataSources ?? []).map((s) => s.id))
    layoutStore.setEnabledDataSources(
      conversation.enabledDataSourceIds.filter((id) => availableIds.has(id))
    )
    return
  }

  layoutStore.setEnabledDataSources(getDefaultEnabledDataSourceIds())
}

/** A recorded session as a read-only conversation: each turn becomes a question and its answer. */
const toRecordedConversation = (session: RecordedSession, userId: string): Conversation => {
  const recordedAt = new Date(session.recordedAt)
  const messages = session.turns.flatMap((turn, index): ChatMessage[] => {
    const id = `${session.id}:${index}`
    const question: ChatMessage = {
      id: `${id}:question`,
      role: 'user',
      messageType: 'user',
      content: turn.question,
      timestamp: recordedAt,
      enabledDataSources: turn.sourceIds,
    }
    const answer: ChatMessage = turn.answer
      ? {
          id: `${id}:answer`,
          role: 'assistant',
          messageType: 'agent_response',
          content: turn.answer,
          timestamp: recordedAt,
          deepResearchJobId: turn.jobId,
          deepResearchJobStatus: 'success',
        }
      : {
          id: `${id}:error`,
          role: 'assistant',
          messageType: 'error',
          content: 'This recorded run did not return an answer.',
          timestamp: recordedAt,
          deepResearchJobId: turn.jobId,
          errorData: {
            errorCode: 'agent.response_failed',
            errorMessage: 'This recorded run did not return an answer.',
          },
        }
    return [question, answer]
  })

  return {
    id: session.id,
    userId,
    title: session.title,
    messages,
    createdAt: recordedAt,
    updatedAt: recordedAt,
    // The data sources of its last question, for the composer's counter
    enabledDataSourceIds: session.turns.at(-1)?.sourceIds ?? [],
    readOnly: true,
  }
}

let cancelFallbackTimer: ReturnType<typeof setTimeout> | null = null

export const useChatStore = create<ChatStore>()(
  devtools(
    persist(
      (set, get) => {
        /** Apply `update` to one conversation in the list and, if current, to the current one. */
        const updateConversation = (
          conversationId: string,
          update: (conversation: Conversation) => Conversation,
          action: string
        ): void => {
          const { conversations, currentConversation } = get()
          const target = conversations.find((c) => c.id === conversationId)
          if (!target) return
          const updated = update(target)
          set(
            {
              conversations: updateConversationInList(conversations, updated),
              currentConversation:
                currentConversation?.id === conversationId ? updated : currentConversation,
            },
            false,
            action
          )
        }

        /** Append a message to a conversation (the current one by default). */
        const appendMessage = (
          message: ChatMessage,
          action: string,
          conversationId = get().currentConversation?.id
        ): void => {
          if (!conversationId) return
          updateConversation(
            conversationId,
            (conversation) => ({
              ...conversation,
              messages: [...conversation.messages, message],
              updatedAt: new Date(),
            }),
            action
          )
        }

        const errorCard = (code: ErrorCode, message?: string, details?: string): ChatMessage => ({
          id: uuidv4(),
          role: 'assistant',
          content: message || getErrorMeta(code).defaultMessage,
          timestamp: new Date(),
          messageType: 'error',
          errorData: { errorCode: code, errorMessage: message, errorDetails: details },
        })

        /**
         * Record a job's terminal state on its answer message: the answer
         * itself on success, otherwise a failure or cancellation banner
         * (or the given `banner`).
         */
        const settleJob = (
          conversationId: string,
          messageId: string,
          jobId: string,
          status: DeepResearchJobStatus,
          answer: string,
          error?: string | null,
          banner?: DeepResearchBannerType
        ): void => {
          clearDeepResearchSession(jobId)
          get().patchConversationMessage(conversationId, messageId, {
            content: answer,
            deepResearchJobStatus: status,
            isDeepResearchActive: false,
            responseCompletedAt: new Date(),
          })
          const bannerType = banner ?? terminalBannerType(status, Boolean(answer.trim()), error)
          get().addDeepResearchBanner(bannerType, jobId, conversationId)
          if (bannerType === 'failure' && error) {
            appendMessage(
              errorCard('agent.deep_research_failed', error),
              'settleJob',
              conversationId
            )
          }
        }

        /** Settle a job whose answer was not streamed, fetching its report if it succeeded. */
        const settleJobFromReport = async (
          conversationId: string,
          messageId: string,
          jobId: string,
          status: DeepResearchJobStatus,
          error?: string | null
        ): Promise<void> => {
          const answer =
            status === 'success'
              ? await getJobReport(jobId)
                  .then((response) => response.report ?? '')
                  .catch(() => '')
              : ''
          settleJob(conversationId, messageId, jobId, status, answer, error)
        }

        /** Settle a job the API no longer knows (404/410): its result is gone. */
        const settleUnavailableJob = (
          conversationId: string,
          messageId: string,
          jobId: string
        ): void => settleJob(conversationId, messageId, jobId, 'failure', '', null, 'expired')

        return {
          ...initialState,

          setCurrentUser: (userId: string | null) => {
            const { conversations, currentConversation } = get()

            // Clear the current conversation when the user changes.
            const shouldClearCurrent =
              currentConversation && (userId === null || currentConversation.userId !== userId)
            const userConversations = userId ? conversations.filter((c) => c.userId === userId) : []
            const newCurrentConversation = shouldClearCurrent
              ? userConversations[0] || null
              : currentConversation

            set(
              { currentUserId: userId, currentConversation: newCurrentConversation },
              false,
              'setCurrentUser'
            )

            if (newCurrentConversation) {
              get().restoreSessionState(newCurrentConversation)
              restoreConversationDataSources(newCurrentConversation)
            } else {
              set({ currentStatus: null, ...clearedJobState }, false, 'setCurrentUser:clearState')
            }
          },

          startNewSessionDraft: () => {
            useLayoutStore.getState().setEnabledDataSources(getDefaultEnabledDataSourceIds())
            set(
              {
                currentConversation: null,
                isStreaming: false,
                isLoading: false,
                currentUserMessageId: null,
                currentStatus: null,
                ...clearedJobState,
              },
              false,
              'startNewSessionDraft'
            )
          },

          ensureSession: () => {
            const { currentConversation, currentUserId } = get()

            if (currentConversation?.id) {
              return currentConversation.id
            }
            if (!currentUserId) {
              return undefined
            }

            ensureStorageCapacity(null, currentUserId)

            const newConversation: Conversation = {
              ...createNewConversation(currentUserId),
              enabledDataSourceIds: [...useLayoutStore.getState().enabledDataSourceIds],
            }
            set(
              (state) => ({
                conversations: [newConversation, ...state.conversations],
                currentConversation: newConversation,
                currentStatus: null,
                ...clearedJobState,
              }),
              false,
              'ensureSession'
            )
            return newConversation.id
          },

          selectConversation: (conversationId: string) => {
            const { conversations, currentUserId, currentConversation } = get()
            const conversation = conversations.find((c) => c.id === conversationId)
            if (!conversation || conversation.userId !== currentUserId) return

            if (currentConversation?.id !== conversationId) {
              ensureStorageCapacity(conversationId, currentUserId)
              // Keep a running job recoverable after leaving its conversation.
              get().persistDeepResearchToSession()
            }

            useLayoutStore.getState().closeRightPanel()
            set(
              { currentConversation: conversation, ...clearedJobState },
              false,
              'selectConversation'
            )
            get().restoreSessionState(conversation)
            restoreConversationDataSources(conversation)
          },

          openRecordedSession: (session: RecordedSession) => {
            const { currentUserId } = get()
            if (!currentUserId) return
            const conversation = toRecordedConversation(session, currentUserId)
            set(
              {
                currentConversation: conversation,
                isStreaming: false,
                isLoading: false,
                currentStatus: null,
                ...clearedJobState,
              },
              false,
              'openRecordedSession'
            )
            restoreConversationDataSources(conversation)
          },

          addUserMessage: (content, metadata) => {
            const { currentConversation, conversations, currentUserId } = get()

            let conversation = currentConversation
            if (!conversation) {
              if (!currentUserId) {
                throw new Error('Cannot create conversation without a user')
              }
              conversation = {
                ...createNewConversation(currentUserId),
                enabledDataSourceIds: [...useLayoutStore.getState().enabledDataSourceIds],
              }
            }

            const newMessage: ChatMessage = {
              id: uuidv4(),
              role: 'user',
              content,
              timestamp: new Date(),
              messageType: 'user',
              enabledDataSources: metadata?.enabledDataSources,
            }
            const isFirstQuestion = !conversation.messages.some((m) => m.messageType === 'user')

            const updatedConversation: Conversation = {
              ...conversation,
              title: isFirstQuestion ? generateTitle(content) : conversation.title,
              messages: [...conversation.messages, newMessage],
              updatedAt: new Date(),
            }

            const exists = conversations.some((c) => c.id === updatedConversation.id)
            set(
              {
                currentConversation: updatedConversation,
                conversations: exists
                  ? updateConversationInList(conversations, updatedConversation)
                  : [updatedConversation, ...conversations],
                isLoading: true,
                currentUserMessageId: newMessage.id,
              },
              false,
              'addUserMessage'
            )

            return newMessage
          },

          setLoading: (isLoading: boolean) => {
            set({ isLoading }, false, 'setLoading')
          },

          setStreaming: (isStreaming: boolean) => {
            set({ isStreaming }, false, 'setStreaming')
          },

          deleteConversation: (conversationId: string) => {
            const {
              currentConversation,
              conversations,
              deepResearchJobId,
              isDeepResearchStreaming,
            } = get()
            const conversation = conversations.find((c) => c.id === conversationId)
            const isCurrentWithActiveJob =
              currentConversation?.id === conversationId && isDeepResearchStreaming

            // Cancel the conversation's running job (fire and forget).
            const latestJob = conversation && findLatestJobMessage(conversation.messages)
            const jobIdToCancel = isCurrentWithActiveJob
              ? deepResearchJobId
              : isActiveJobMessage(latestJob)
                ? latestJob.deepResearchJobId
                : null
            if (jobIdToCancel) {
              cancelJob(jobIdToCancel).catch((err) => {
                console.warn('Failed to cancel job on session delete:', err)
              })
            }

            set(
              {
                conversations: conversations.filter((c) => c.id !== conversationId),
                currentConversation:
                  currentConversation?.id === conversationId ? null : currentConversation,
                ...(isCurrentWithActiveJob && clearedJobState),
              },
              false,
              'deleteConversation'
            )
          },

          deleteAllConversations: () => {
            const { conversations, currentUserId, currentConversation, deepResearchJobId } = get()
            if (!currentUserId) return

            const userConversations = conversations.filter((c) => c.userId === currentUserId)
            const jobIdsToCancel = new Set<string>(deepResearchJobId ? [deepResearchJobId] : [])
            for (const conversation of userConversations) {
              const latestJob = findLatestJobMessage(conversation.messages)
              if (isActiveJobMessage(latestJob)) jobIdsToCancel.add(latestJob.deepResearchJobId!)
            }
            for (const jobId of jobIdsToCancel) {
              cancelJob(jobId).catch((err) => {
                console.warn('Failed to cancel job on delete all sessions:', jobId, err)
              })
            }

            clearAllDeepResearchSessions()

            set(
              {
                conversations: conversations.filter((c) => c.userId !== currentUserId),
                currentConversation:
                  currentConversation?.userId === currentUserId ? null : currentConversation,
                currentStatus: null,
                ...clearedJobState,
              },
              false,
              'deleteAllConversations'
            )
          },

          updateConversationTitle: (conversationId: string, title: string) => {
            updateConversation(
              conversationId,
              (conversation) => ({ ...conversation, title, updatedAt: new Date() }),
              'updateConversationTitle'
            )
          },

          saveDataSourcesToConversation: (ids: string[]) => {
            const conversationId = get().ensureSession()
            if (!conversationId) return
            updateConversation(
              conversationId,
              (conversation) => ({ ...conversation, enabledDataSourceIds: ids }),
              'saveDataSourcesToConversation'
            )
          },

          setCurrentStatus: (status) => {
            set({ currentStatus: status }, false, 'setCurrentStatus')
          },

          addAgentResponseWithMeta: (content, meta) => {
            const messageId = uuidv4()
            appendMessage(
              {
                id: messageId,
                role: 'assistant',
                content,
                timestamp: new Date(),
                messageType: 'agent_response',
                ...meta,
              },
              'addAgentResponseWithMeta'
            )
            return messageId
          },

          patchConversationMessage: (conversationId, messageId, patch) => {
            updateConversation(
              conversationId,
              (conversation) => ({
                ...conversation,
                messages: conversation.messages.map((msg) =>
                  msg.id === messageId ? { ...msg, ...patch } : msg
                ),
                updatedAt: new Date(),
              }),
              'patchConversationMessage'
            )
          },

          addErrorCard: (code: ErrorCode, message?: string, details?: string) => {
            appendMessage(errorCard(code, message, details), 'addErrorCard')
          },

          dismissErrorCard: (messageId: string) => {
            const { currentConversation } = get()
            if (!currentConversation) return
            updateConversation(
              currentConversation.id,
              (conversation) => ({
                ...conversation,
                messages: conversation.messages.filter((msg) => msg.id !== messageId),
                updatedAt: new Date(),
              }),
              'dismissErrorCard'
            )
          },

          addDeepResearchBanner: (bannerType, jobId, conversationId) => {
            const targetId = conversationId ?? get().currentConversation?.id
            if (!targetId) return
            updateConversation(
              targetId,
              (conversation) => withDeepResearchBanner(conversation, bannerType, jobId),
              'addDeepResearchBanner'
            )
          },

          beginJobSubmission: (jobId: string) => {
            set(
              (state) => ({ submittingJobIds: [...state.submittingJobIds, jobId] }),
              false,
              'beginJobSubmission'
            )
          },

          endJobSubmission: (jobId: string) => {
            set(
              (state) => ({
                submittingJobIds: state.submittingJobIds.filter((id) => id !== jobId),
              }),
              false,
              'endJobSubmission'
            )
          },

          startDeepResearch: (jobId: string, messageId: string) => {
            set(
              {
                ...clearedJobState,
                deepResearchJobId: jobId,
                isDeepResearchStreaming: true,
                deepResearchStatus: 'submitted',
                deepResearchOwnerConversationId: get().currentConversation?.id || null,
                activeDeepResearchMessageId: messageId,
              },
              false,
              'startDeepResearch'
            )
          },

          updateDeepResearchStatus: (status: DeepResearchJobStatus) => {
            set({ deepResearchStatus: status }, false, 'updateDeepResearchStatus')
          },

          setReportContent: (content: string) => {
            set({ reportContent: content }, false, 'setReportContent')
          },

          finishDeepResearch: (status, error) => {
            const {
              deepResearchJobId: jobId,
              deepResearchOwnerConversationId: ownerId,
              activeDeepResearchMessageId: messageId,
              reportContent,
            } = get()
            if (!jobId) return
            if (cancelFallbackTimer) {
              clearTimeout(cancelFallbackTimer)
              cancelFallbackTimer = null
            }
            if (ownerId && messageId) {
              if (status === 'success' && !reportContent.trim()) {
                void settleJobFromReport(ownerId, messageId, jobId, status, error)
              } else {
                settleJob(ownerId, messageId, jobId, status, reportContent, error)
              }
            }
            set(
              {
                deepResearchStatus: status,
                isDeepResearchStreaming: false,
                isStreaming: false,
                isLoading: false,
                currentStatus: status === 'success' ? 'complete' : 'error',
              },
              false,
              'finishDeepResearch'
            )
          },

          persistDeepResearchToSession: () => {
            const {
              deepResearchJobId,
              deepResearchOwnerConversationId,
              activeDeepResearchMessageId,
              deepResearchStatus,
              isDeepResearchStreaming,
            } = get()

            if (!deepResearchJobId || !isDeepResearchStreaming) return

            saveDeepResearchToSession({
              jobId: deepResearchJobId,
              ownerConversationId: deepResearchOwnerConversationId,
              activeMessageId: activeDeepResearchMessageId,
              status: deepResearchStatus,
            })
          },

          reconnectToActiveJob: async () => {
            const { currentConversation, isDeepResearchStreaming } = get()
            if (!currentConversation || isDeepResearchStreaming) return

            const conversationId = currentConversation.id
            const message = findLatestJobMessage(currentConversation.messages)
            if (!isActiveJobMessage(message) || !message.isDeepResearchActive) return

            const jobId = message.deepResearchJobId!
            // Submitted from this page and not answered yet: the API does not know it yet, and
            // the page follows it once it is admitted.
            if (get().submittingJobIds.includes(jobId)) return
            const isStillCurrent = (): boolean =>
              get().currentConversation?.id === conversationId && !get().isDeepResearchStreaming

            try {
              const job = await getStatusOnceAdmitted(message, isStillCurrent)
              if (!job || !isStillCurrent()) return
              const { status, error } = job

              if (status === 'running' || status === 'submitted') {
                // Replay the stream from the beginning: the execution view
                // rebuilds its state from the full event history.
                set(
                  {
                    ...clearedJobState,
                    deepResearchJobId: jobId,
                    isDeepResearchStreaming: true,
                    deepResearchStatus: status,
                    deepResearchOwnerConversationId: conversationId,
                    activeDeepResearchMessageId: message.id,
                    currentStatus: 'researching',
                  },
                  false,
                  'reconnectToActiveJob'
                )
                return
              }
              await settleJobFromReport(conversationId, message.id, jobId, status, error)
            } catch (error) {
              console.warn('Failed to reconnect to active job:', error)
              if (isUnavailableDeepResearchJobError(error)) {
                settleUnavailableJob(conversationId, message.id, jobId)
              } else {
                // Mark as inactive to prevent retry loops
                get().patchConversationMessage(conversationId, message.id, {
                  isDeepResearchActive: false,
                })
              }
            }
          },

          cleanupOrphanedStartingBanners: async () => {
            const { currentConversation } = get()
            if (!currentConversation) return

            const conversationId = currentConversation.id
            const startingJobIds = currentConversation.messages
              .filter((m) => m.deepResearchBannerData?.bannerType === 'starting')
              .map((m) => m.deepResearchBannerData!.jobId)

            for (const jobId of startingJobIds) {
              if (get().currentConversation?.id !== conversationId) return
              if (get().isDeepResearchStreaming && get().deepResearchJobId === jobId) continue
              if (get().submittingJobIds.includes(jobId)) continue
              const message = currentConversation.messages.find(
                (m) => m.messageType === 'agent_response' && m.deepResearchJobId === jobId
              )
              if (!message) continue
              try {
                const { status, error } = await getJobStatus(jobId)
                if (TERMINAL_STATUSES.includes(status)) {
                  await settleJobFromReport(conversationId, message.id, jobId, status, error)
                }
              } catch (error) {
                if (isUnavailableDeepResearchJobError(error) && !mayStillBeAdmitted(message)) {
                  settleUnavailableJob(conversationId, message.id, jobId)
                }
                // Other failures are likely transient, and a job submitted moments ago may not
                // be admitted yet; leave the banner as-is.
              }
            }
          },

          refreshDeepResearchSessionStatuses: async () => {
            const { currentUserId, conversations } = get()
            if (!currentUserId) return

            for (const conversation of conversations) {
              if (conversation.userId !== currentUserId) continue
              const message = findLatestJobMessage(conversation.messages)
              if (!isActiveJobMessage(message)) continue
              const jobId = message.deepResearchJobId!
              // The followed job settles through its stream, and one being submitted is not known yet.
              if (get().isDeepResearchStreaming && get().deepResearchJobId === jobId) continue
              if (get().submittingJobIds.includes(jobId)) continue
              try {
                const { status, error } = await getJobStatus(jobId)
                if (TERMINAL_STATUSES.includes(status)) {
                  await settleJobFromReport(conversation.id, message.id, jobId, status, error)
                }
              } catch (error) {
                // A job submitted moments ago may not be admitted yet: not gone.
                if (isUnavailableDeepResearchJobError(error) && !mayStillBeAdmitted(message)) {
                  settleUnavailableJob(conversation.id, message.id, jobId)
                }
              }
            }
          },

          cancelActiveDeepResearchJob: async () => {
            const { deepResearchJobId: jobId, isDeepResearchStreaming } = get()
            if (!jobId || !isDeepResearchStreaming) return

            await cancelJob(jobId)

            // The stream normally delivers the terminal `interrupted` status.
            // If it is broken, settle locally so the UI never stays busy.
            if (cancelFallbackTimer) clearTimeout(cancelFallbackTimer)
            cancelFallbackTimer = setTimeout(() => {
              cancelFallbackTimer = null
              const state = get()
              if (state.isDeepResearchStreaming && state.deepResearchJobId === jobId) {
                state.finishDeepResearch('interrupted', 'cancelled by user')
              }
            }, CANCEL_FALLBACK_TIMEOUT_MS)
          },

          restoreSessionState: (conversation: Conversation) => {
            const latestJob = findLatestJobMessage(conversation.messages)

            set(
              {
                ...clearedJobState,
                // In-progress jobs reconnect via reconnectToActiveJob.
                isStreaming: false,
                isLoading: false,
                currentStatus: null,
                deepResearchJobId: latestJob?.deepResearchJobId || null,
                activeDeepResearchMessageId: latestJob?.id || null,
                deepResearchOwnerConversationId: conversation.id,
              },
              false,
              'restoreSessionState'
            )

            // A question with no answer, banner or error was interrupted by a
            // reload before its job was created.
            const last = conversation.messages.at(-1)
            if (last?.messageType === 'user') {
              get().addErrorCard(
                'agent.response_interrupted',
                'Your previous request was not completed. Please resend your message.'
              )
            }
          },

          isSessionBusy: (conversationId: string) => {
            const state = get()
            if (state.currentConversation?.id === conversationId && state.isStreaming) {
              return true
            }
            if (
              state.deepResearchOwnerConversationId === conversationId &&
              state.isDeepResearchStreaming
            ) {
              return true
            }
            // Jobs of other sessions are only visible in their message history.
            const conversation = state.conversations.find((c) => c.id === conversationId)
            return Boolean(conversation && hasActiveDeepResearchJob(conversation.messages))
          },

          hasAnyBusySession: () => {
            const state = get()
            return state.conversations.some((conv) => state.isSessionBusy(conv.id))
          },
        }
      },
      {
        name: 'aiq-chat-store',
        storage: typeof window === 'undefined' ? undefined : createResilientStorage(),
        partialize: (state) => ({
          // Persist conversations and user context, not streaming state
          currentUserId: state.currentUserId,
          conversations: state.conversations,
          currentConversation: state.currentConversation,
        }),
      }
    ),
    { name: 'ChatStore' }
  )
)

// ============================================================
// Storage Event Monitoring (for debugging session clearing)
// ============================================================

if (typeof window !== 'undefined') {
  const hydratedState = useChatStore.getState()
  logStoreHydration(true, hydratedState.conversations?.length ?? 0, hydratedState.currentUserId)

  // Monitor storage events from other tabs or browser extensions
  window.addEventListener('storage', (event) => {
    if (event.key !== 'aiq-chat-store') return
    logExternalStorageEvent(event.key, event.oldValue, event.newValue)
    if (event.oldValue !== null && event.newValue === null) {
      console.error(
        '[SessionsStore] CRITICAL: Storage cleared by external source (browser extension, dev tools, or another tab)'
      )
    }
  })
}

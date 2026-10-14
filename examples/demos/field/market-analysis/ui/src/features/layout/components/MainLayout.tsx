// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * MainLayout Component
 *
 * The main application layout container that orchestrates:
 * - AppBar (top)
 * - SessionsPanel (left, collapsible push rail)
 * - ChatArea + InputArea (center, responsive width)
 * - ResearchPanel (right, pushes content)
 * - DataSourcesPanel (right, push panel)
 * - the execution workspace, when a run is opened from an answer
 *
 * The data pack's recordings are listed under "Recorded", beside the browser's
 * own sessions ("My sessions"), as in the original demo UI. A recorded session
 * is read only: its composer and data source selection are disabled. Replay
 * mode has the recordings only.
 */

'use client'

import { type FC, useCallback, useEffect, useMemo, useRef } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { Flex } from '@/adapters/ui'
import { useAppConfig, useExecutionFeature } from '@/shared/context'
import { cn } from '@/shared/lib/cn'
import { AppBar } from './AppBar'
import { SessionsPanel, type RecordedCollection } from './SessionsPanel'
import { ChatArea } from './ChatArea'
import { InputArea } from './InputArea'
import { ResearchPanel } from './ResearchPanel'
import { DataSourcesPanel } from './DataSourcesPanel'
import { useChatStore, useDeepResearch, NoSourcesBanner, type ChatMessage } from '@/features/chat'
import {
  hasActiveDeepResearchJob,
  hasCompletedDeepResearchReport,
  sortConversationsByLastUserMessage,
} from '@/features/chat/lib/session-activity'
import { useLayoutStore } from '../store'
import { useRecordedSessions } from '../use-recorded-sessions'
import type { DemoScenario } from '../scenarios'
import { useSearchParams } from 'next/navigation'
import { useSessionUrl } from '@/hooks/use-session-url'

/** A question to place in the composer, e.g. a featured question from the landing page. */
export interface InitialQuestion {
  question: string
  sourceIds: string[]
}

interface MainLayoutProps {
  initialQuestion?: InitialQuestion | null
  /** The active data pack's questions, offered by the composer's demo scenario picker */
  demoScenarios?: DemoScenario[]
}

/** The question, and its data sources, that started a job in this conversation. */
const turnOfJob = (
  messages: readonly ChatMessage[] | undefined,
  jobId: string | undefined
): { question: string; sourceIds: string[] } | null => {
  const answer = messages?.findIndex((message) => message.deepResearchJobId === jobId) ?? -1
  if (!messages || !jobId || answer < 0) return null
  const asked = messages.slice(0, answer).findLast((message) => message.role === 'user')
  return asked ? { question: asked.content, sourceIds: asked.enabledDataSources ?? [] } : null
}

/**
 * Main application layout with all panels and regions.
 * Chat state is managed via the useChatStore.
 */
export const MainLayout: FC<MainLayoutProps> = ({ initialQuestion = null, demoScenarios }) => {
  const { mode } = useAppConfig()
  const isReplay = mode === 'replay'
  const { Workspace } = useExecutionFeature()

  const {
    currentConversation,
    conversations,
    isStreaming,
    isDeepResearchStreaming,
    deepResearchOwnerConversationId,
    currentUserId,
  } = useChatStore(
    useShallow((s) => ({
      currentConversation: s.currentConversation,
      conversations: s.conversations,
      isStreaming: s.isStreaming,
      isDeepResearchStreaming: s.isDeepResearchStreaming,
      deepResearchOwnerConversationId: s.deepResearchOwnerConversationId,
      currentUserId: s.currentUserId,
    }))
  )

  const selectConversation = useChatStore((s) => s.selectConversation)
  const startNewSessionDraft = useChatStore((s) => s.startNewSessionDraft)
  const deleteConversation = useChatStore((s) => s.deleteConversation)
  const deleteAllConversations = useChatStore((s) => s.deleteAllConversations)
  const updateConversationTitle = useChatStore((s) => s.updateConversationTitle)

  const openRightPanel = useLayoutStore((s) => s.openRightPanel)
  const execution = useLayoutStore((s) => s.execution)
  const closeExecution = useLayoutStore((s) => s.closeExecution)
  const availableDataSources = useLayoutStore((s) => s.availableDataSources)
  const executionOpen = Boolean(Workspace && execution)
  const executionTurn = useMemo(
    () => turnOfJob(currentConversation?.messages, execution?.jobId),
    [currentConversation?.messages, execution?.jobId]
  )

  // Follows the current job's SSE stream
  useDeepResearch()

  const {
    sessions: recordedSessions,
    status: recordedStatus,
    error: recordedError,
    loadingId: recordedLoadingId,
    open: openRecordedSession,
    retry: retryRecordedSessions,
  } = useRecordedSessions()

  // Sync saved sessions with the ?session= query parameter
  const { updateSessionUrl, clearSessionUrl } = useSessionUrl({ enabled: !isReplay })

  // Place a featured question in a fresh session, with its data sources,
  // once the data sources are known. The question leaves the URL then, so a
  // reload reopens the session it starts. A URL that also names one of this
  // browser's sessions (one saved before the question left the URL) reopens
  // that session instead.
  const searchParams = useSearchParams()
  const sessionAtLoad = useRef(searchParams?.get('session') ?? null)
  const initialQuestionApplied = useRef(false)
  useEffect(() => {
    if (!initialQuestion || initialQuestionApplied.current || availableDataSources === null) return
    if (!currentUserId) return
    initialQuestionApplied.current = true
    const saved = sessionAtLoad.current
    if (saved && conversations.some((c) => c.id === saved && c.userId === currentUserId)) {
      updateSessionUrl(saved)
      return
    }
    startNewSessionDraft()
    const available = new Set(availableDataSources.map((source) => source.id))
    const layout = useLayoutStore.getState()
    layout.setEnabledDataSources(initialQuestion.sourceIds.filter((id) => available.has(id)))
    layout.setPromptDraft(initialQuestion.question)
    clearSessionUrl()
  }, [
    initialQuestion,
    availableDataSources,
    currentUserId,
    conversations,
    startNewSessionDraft,
    updateSessionUrl,
    clearSessionUrl,
  ])

  // The live session that was open before a recording, to go back to
  const liveSessionBeforeRecording = useRef<string | null>(null)
  const handleSelectRecordedSession = useCallback(
    (sessionId: string) => {
      const current = useChatStore.getState().currentConversation
      if (current && !current.readOnly) liveSessionBeforeRecording.current = current.id
      closeExecution()
      clearSessionUrl()
      void openRecordedSession(sessionId)
    },
    [clearSessionUrl, closeExecution, openRecordedSession]
  )

  const handleSelectSession = useCallback(
    (sessionId: string) => {
      if (isReplay) {
        handleSelectRecordedSession(sessionId)
        return
      }
      closeExecution()
      selectConversation(sessionId)
      updateSessionUrl(sessionId)
    },
    [closeExecution, handleSelectRecordedSession, isReplay, selectConversation, updateSessionUrl]
  )

  // A recorded session restored with the page: load its run again, since the
  // execution records are not kept across reloads.
  const reopenedRecording = useRef(false)
  useEffect(() => {
    if (reopenedRecording.current || recordedStatus !== 'ready') return
    reopenedRecording.current = true
    const restored = useChatStore.getState().currentConversation
    if (restored?.readOnly && recordedSessions.some((session) => session.id === restored.id)) {
      void openRecordedSession(restored.id)
    }
  }, [openRecordedSession, recordedSessions, recordedStatus])

  // Start a new unsaved draft session and clear URL until first interaction.
  // Open Data Sources panel so it stays visible (default panel for new sessions).
  const handleNewSession = useCallback(() => {
    startNewSessionDraft()
    clearSessionUrl()
    openRightPanel('data-sources')
  }, [startNewSessionDraft, clearSessionUrl, openRightPanel])

  // "My sessions" while a recording is open: back to the live session it
  // replaced, or to a new one.
  const handleLeaveRecordedSession = useCallback(() => {
    if (!useChatStore.getState().currentConversation?.readOnly) return
    const previous = liveSessionBeforeRecording.current
    if (previous && conversations.some((c) => c.id === previous)) {
      handleSelectSession(previous)
    } else {
      handleNewSession()
    }
  }, [conversations, handleNewSession, handleSelectSession])

  // Wrap deleteConversation to clear URL if deleting current session
  const handleDeleteSession = useCallback(
    (sessionId: string) => {
      const wasCurrentSession = currentConversation?.id === sessionId
      deleteConversation(sessionId)
      if (wasCurrentSession) {
        clearSessionUrl()
      }
    },
    [deleteConversation, currentConversation?.id, clearSessionUrl]
  )

  // Delete all sessions for the current user
  const handleDeleteAllSessions = useCallback(() => {
    deleteAllConversations()
    clearSessionUrl()
  }, [deleteAllConversations, clearSessionUrl])

  const sessions = useMemo(() => {
    // Replay mode has no sessions of its own: its recordings are under Recorded
    if (isReplay) return []
    return sortConversationsByLastUserMessage(
      currentUserId ? conversations.filter((c) => c.userId === currentUserId) : []
    ).map((conv) => ({
      id: conv.id,
      title: conv.title,
      date: conv.updatedAt,
      hasActiveDeepResearch:
        hasActiveDeepResearchJob(conv.messages) ||
        (isDeepResearchStreaming && deepResearchOwnerConversationId === conv.id),
      hasCompletedReport: hasCompletedDeepResearchReport(conv.messages),
    }))
  }, [
    isReplay,
    conversations,
    currentUserId,
    isDeepResearchStreaming,
    deepResearchOwnerConversationId,
  ])

  const isRecordedSession = isReplay || currentConversation?.readOnly === true
  const recordedConversationId = currentConversation?.readOnly ? currentConversation.id : null
  // Live mode lists the recordings once they have loaded, if the pack has any.
  const showRecorded = isReplay || (recordedStatus === 'ready' && recordedSessions.length > 0)
  const recorded = useMemo<RecordedCollection | undefined>(
    () =>
      showRecorded
        ? {
            sessions: recordedSessions,
            status: recordedStatus,
            error: recordedError,
            selectedId: recordedConversationId,
            loadingId: recordedLoadingId,
            onSelect: handleSelectRecordedSession,
            onRetry: retryRecordedSessions,
            onLeave: isReplay ? undefined : handleLeaveRecordedSession,
          }
        : undefined,
    [
      showRecorded,
      recordedSessions,
      recordedStatus,
      recordedError,
      recordedConversationId,
      recordedLoadingId,
      handleSelectRecordedSession,
      retryRecordedSessions,
      isReplay,
      handleLeaveRecordedSession,
    ]
  )

  return (
    <Flex direction="col" className="h-screen min-w-[768px] overflow-x-auto overflow-y-hidden">
      {/* AppBar - Fixed at top */}
      <AppBar
        sessionTitle={currentConversation?.title}
        onNewSession={executionOpen ? closeExecution : handleNewSession}
        newSessionActionLabel={executionOpen ? 'Back to answer' : 'Create new session'}
        isNewSessionDisabled={executionOpen ? false : isReplay || isStreaming}
        isDataSourceSelectionDisabled={isRecordedSession}
        isRecordedSession={isRecordedSession}
      />

      {/* Main content area: in-flow panels reflow the center column (push, not overlay) */}
      <div className="relative flex flex-1 overflow-hidden">
        {!executionOpen && (
          <SessionsPanel
            sessions={sessions}
            selectedSessionId={currentConversation?.id}
            onSelectSession={handleSelectSession}
            onNewSession={handleNewSession}
            onDeleteSession={handleDeleteSession}
            onDeleteAllSessions={handleDeleteAllSessions}
            onRenameSession={updateConversationTitle}
            recorded={recorded}
            readOnly={isReplay}
          />
        )}

        {/* Center Content: Chat + Input */}
        <div
          className={cn(
            'flex min-w-0 flex-col overflow-hidden',
            executionOpen
              ? 'border-base w-[clamp(20rem,27vw,28rem)] flex-none border-r 2xl:w-[30rem]'
              : 'flex-1'
          )}
        >
          <ChatArea compact={executionOpen} />
          {!isReplay && <NoSourcesBanner />}
          <InputArea scenarios={demoScenarios} showDemoScenarios={!executionOpen} />
        </div>

        {Workspace && execution ? (
          <div className="min-w-0 flex-1 overflow-hidden">
            <Workspace
              key={execution.jobId}
              jobId={execution.jobId}
              focus={execution.focus}
              question={executionTurn?.question ?? null}
              sourceIds={executionTurn?.sourceIds}
              onClose={closeExecution}
            />
          </div>
        ) : (
          <>
            {!isRecordedSession && <DataSourcesPanel />}
            <ResearchPanel />
          </>
        )}
      </div>
    </Flex>
  )
}

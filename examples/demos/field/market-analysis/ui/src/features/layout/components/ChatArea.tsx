// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * ChatArea Component
 *
 * Main chat display area showing messages between user and assistant.
 * Includes the message list and is positioned in the center of the layout.
 */

'use client'

import { type FC, type ReactNode, memo, useRef, useEffect, useMemo } from 'react'
import { motion } from 'motion/react'
import { Button, Flex, Text } from '@/adapters/ui'
import { ChartFlow } from '@/adapters/ui/icons'
import { useShallow } from 'zustand/react/shallow'
import {
  useChatStore,
  AgentResponse,
  ErrorBanner,
  DeepResearchBanner,
  UserMessage,
} from '@/features/chat'
import type { ChatMessage } from '@/features/chat'
import { useExecutionFeature } from '@/shared/context'
import type { SourceEvidence } from '@/shared/components/Sources/types'
import { StarfieldAnimation } from '@/shared/components/StarfieldAnimation'
import { cn } from '@/shared/lib/cn'
import { isPinnedToBottom } from '@/shared/lib/scroll'
import { useLayoutStore } from '../store'

interface ChatAreaProps {
  /** Narrow rendering beside the execution workspace */
  compact?: boolean
}

/** A user message plus the assistant messages that answer it. */
interface ConversationTurn {
  id: string
  user?: ChatMessage
  assistant: ChatMessage[]
}

/**
 * Main chat area container with scrollable message list.
 * Shows welcome state when no messages exist.
 */
export const ChatArea: FC<ChatAreaProps> = memo(function ChatArea({ compact = false }) {
  const { currentConversation, currentUserMessageId, deepResearchJobId, isDeepResearchStreaming } =
    useChatStore(
      useShallow((s) => ({
        currentConversation: s.currentConversation,
        currentUserMessageId: s.currentUserMessageId,
        deepResearchJobId: s.deepResearchJobId,
        isDeepResearchStreaming: s.isDeepResearchStreaming,
      }))
    )
  const dismissErrorCard = useChatStore((s) => s.dismissErrorCard)
  const openExecution = useLayoutStore((s) => s.openExecution)
  const selectedExecutionJobId = useLayoutStore((s) => s.execution?.jobId ?? null)
  const { Workspace } = useExecutionFeature()

  /** The scrollable viewport that holds the message list. */
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  /** The growing content wrapper inside the viewport (observed for size changes). */
  const contentRef = useRef<HTMLDivElement>(null)
  /**
   * Whether the user is currently pinned to the bottom. Kept in a ref (not
   * state) so reading/updating it from scroll + ResizeObserver handlers never
   * triggers a re-render. Defaults to true so a freshly opened conversation
   * lands at the latest message.
   */
  const stickToBottomRef = useRef(true)

  const conversationId = currentConversation?.id ?? null
  const messages = currentConversation?.messages
  const isEmpty = !messages || messages.length === 0

  // Group the flat message list into turns: each user message starts a turn and
  // the assistant messages that follow it belong to that turn.
  const turns = useMemo<ConversationTurn[]>(() => {
    const groupedTurns: ConversationTurn[] = []
    let activeTurn: ConversationTurn | null = null

    for (const message of messages ?? []) {
      if (message.messageType === 'user' || message.role === 'user') {
        activeTurn = { id: message.id, user: message, assistant: [] }
        groupedTurns.push(activeTurn)
        continue
      }
      if (!activeTurn) {
        activeTurn = { id: `assistant-${message.id}`, assistant: [] }
        groupedTurns.push(activeTurn)
      }
      activeTurn.assistant.push(message)
    }

    return groupedTurns
  }, [messages])

  /**
   * Track whether the user is pinned to the bottom. Updated on every scroll so
   * the auto-follow below knows whether to keep up with new content or leave a
   * user who scrolled up to read history exactly where they are.
   */
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    const onScroll = (): void => {
      stickToBottomRef.current = isPinnedToBottom(el.scrollTop, el.scrollHeight, el.clientHeight)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  /**
   * Auto-follow content growth. A ResizeObserver on the content wrapper catches
   * every height change (new answers, banners, appended turns) and pins to the
   * bottom only when the user already was, so it never yanks someone reading
   * earlier history.
   */
  useEffect(() => {
    const el = scrollContainerRef.current
    const content = contentRef.current
    if (!el || !content) return
    const followIfPinned = (): void => {
      if (stickToBottomRef.current) el.scrollTop = el.scrollHeight
    }
    const observer = new ResizeObserver(followIfPinned)
    observer.observe(content)
    return () => observer.disconnect()
  }, [isEmpty])

  // On conversation switch, re-pin to the latest message and jump to the bottom.
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    stickToBottomRef.current = true
    el.scrollTop = el.scrollHeight
  }, [conversationId])

  /**
   * On sending a new message, always re-pin and jump to the bottom, even if the
   * user had scrolled up to read history, so their new message and the incoming
   * response are visible. Deferred a frame so the new turn is in the DOM.
   */
  useEffect(() => {
    if (!currentUserMessageId) return
    stickToBottomRef.current = true
    const el = scrollContainerRef.current
    if (el) el.scrollTop = el.scrollHeight
    const raf = requestAnimationFrame(() => {
      const node = scrollContainerRef.current
      if (node) node.scrollTop = node.scrollHeight
    })
    return () => cancelAnimationFrame(raf)
  }, [currentUserMessageId])

  return (
    <Flex
      ref={scrollContainerRef}
      direction="col"
      className="scrollbar-hide flex-1 overflow-y-auto"
      role="log"
      aria-live="polite"
      aria-relevant="additions text"
      aria-label="Chat messages"
    >
      {isEmpty ? (
        <WelcomeState />
      ) : (
        <Flex
          ref={contentRef}
          direction="col"
          gap="8"
          className={cn(
            'mx-auto w-full pt-6',
            compact ? 'max-w-none px-4 pb-6' : 'max-w-4xl px-6 pb-28'
          )}
        >
          {turns.map((turn) => {
            const jobId = turn.assistant.find((m) => m.deepResearchJobId)?.deepResearchJobId
            const isActive = Boolean(
              jobId && isDeepResearchStreaming && jobId === deepResearchJobId
            )
            const openEvidence =
              Workspace && jobId
                ? (evidence: SourceEvidence) => openExecution(jobId, evidence)
                : undefined

            return (
              <motion.div
                key={turn.id}
                layout
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
                className="flex flex-col gap-3"
              >
                {turn.user && (
                  <UserMessage content={turn.user.content} timestamp={turn.user.timestamp} />
                )}

                {turn.assistant.length > 0 && (
                  <AssistantRun isActive={isActive}>
                    {!compact && Workspace && jobId && (
                      <Flex justify="end" className="sticky top-2 z-20 w-full pb-1">
                        <Button
                          kind={selectedExecutionJobId === jobId ? 'secondary' : 'primary'}
                          size="small"
                          onClick={() => openExecution(jobId)}
                          aria-label="View execution for this response"
                          aria-pressed={selectedExecutionJobId === jobId}
                        >
                          <Flex align="center" gap="1.5">
                            <ChartFlow className="h-4 w-4" />
                            <Text kind="label/semibold/sm">View Execution</Text>
                          </Flex>
                        </Button>
                      </Flex>
                    )}

                    {turn.assistant.map((message) => (
                      <MessageRenderer
                        key={message.id}
                        message={message}
                        onErrorDismiss={dismissErrorCard}
                        onOpenEvidence={openEvidence}
                      />
                    ))}
                  </AssistantRun>
                )}
              </motion.div>
            )
          })}
        </Flex>
      )}
    </Flex>
  )
})

/**
 * Wraps an assistant turn in the "run lane": a quiet left spine that lights up
 * while the turn's job is running.
 */
const AssistantRun: FC<{ children: ReactNode; isActive?: boolean }> = ({
  children,
  isActive = false,
}) => (
  <Flex justify="start" className="w-full">
    <div
      className={cn('assistant-turn flex w-full max-w-[88%]', isActive && 'assistant-turn-active')}
    >
      <Flex
        direction="col"
        gap="3.5"
        className="assistant-lane-glow min-w-0 flex-1 rounded-[var(--radius-card)] px-3 py-2"
      >
        {children}
      </Flex>
    </div>
  </Flex>
)

/**
 * Renders one assistant-side message by type.
 */
interface MessageRendererProps {
  message: ChatMessage
  onErrorDismiss: (messageId: string) => void
  onOpenEvidence?: (evidence: SourceEvidence) => void
}

const MessageRenderer: FC<MessageRendererProps> = ({ message, onErrorDismiss, onOpenEvidence }) => {
  switch (message.messageType) {
    case 'agent_response':
      return (
        <AgentResponse
          content={message.content}
          timestamp={message.responseCompletedAt ?? message.timestamp}
          onOpenEvidence={onOpenEvidence}
          variant="inline"
        />
      )

    case 'error':
      if (!message.errorData) return null
      return (
        <ErrorBanner
          code={message.errorData.errorCode}
          message={message.errorData.errorMessage}
          details={message.errorData.errorDetails}
          onDismiss={() => onErrorDismiss(message.id)}
        />
      )

    case 'deep_research_banner':
      if (!message.deepResearchBannerData) return null
      return (
        <DeepResearchBanner
          bannerType={message.deepResearchBannerData.bannerType}
          jobId={message.deepResearchBannerData.jobId}
        />
      )

    default:
      return null
  }
}

/**
 * Welcome state shown when no messages exist
 */
const WelcomeState: FC = () => (
  <Flex direction="col" align="center" justify="center" className="relative flex-1 p-8">
    {/* Ambient starfield backdrop */}
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center opacity-30">
      <div className="h-[500px] w-[500px]">
        <StarfieldAnimation particleCount={300} maxRadius={220} rotationSpeed={0.001} />
      </div>
    </div>

    {/* Ready-to-chat prompt */}
    <Flex direction="col" align="center" gap="5" className="relative z-10 max-w-xl text-center">
      <Text kind="title/lg" className="text-primary">
        What do you want to know?
      </Text>
      <Text kind="body/regular/md" className="text-subtle">
        Ask a question about your connected data sources, or commission a deep research report.
      </Text>
    </Flex>
  </Flex>
)

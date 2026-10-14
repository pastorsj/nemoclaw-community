// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * AgentResponse Component
 *
 * Displays a completed agent response in the chat area.
 * Left-aligned with distinct styling from user messages.
 */

'use client'

import { type FC, useMemo } from 'react'
import { Flex, Text } from '@/adapters/ui'
import { MarkdownRenderer } from '@/shared/components/MarkdownRenderer'
import { SourceList } from '@/shared/components/Sources/SourceList'
import {
  splitReferences,
  tabularizeEntityLines,
} from '@/shared/components/Sources/parse-references'
import type { SourceEvidence } from '@/shared/components/Sources/types'
import { CopyButton } from '@/shared/components/Actions/CopyButton'
import { formatTime } from '@/shared/utils/format-time'

export interface AgentResponseProps {
  /** Response content from the agent */
  content: string
  /** Timestamp of the response (Date or ISO string from persisted state) */
  timestamp?: Date | string
  /** Display variant - 'default' has box styling, 'inline' has no box (for use inside containers) */
  variant?: 'default' | 'inline'
  /** Opens cited run evidence in the execution view */
  onOpenEvidence?: (evidence: SourceEvidence) => void
}

/**
 * Agent response bubble component for completed responses
 */
export const AgentResponse: FC<AgentResponseProps> = ({
  content,
  timestamp,
  variant = 'default',
  onOpenEvidence,
}) => {
  const { body, sources } = useMemo(() => {
    const split = splitReferences(content ?? '')
    return { body: tabularizeEntityLines(split.body), sources: split.sources }
  }, [content])

  if (!content || !content.trim() || content === 'null') {
    return null
  }

  const answer = (
    <>
      <MarkdownRenderer
        content={body}
        variant="answer"
        sources={sources}
        className="answer-reveal"
      />
      {sources.length > 0 && <SourceList sources={sources} onOpenEvidence={onOpenEvidence} />}
    </>
  )

  const meta = (
    <Flex align="center" gap="2" className="agent-final-meta mt-1.5">
      <CopyButton text={content} label="Copy answer" />
      {timestamp && (
        <Text kind="body/regular/xs" className="mono-meta text-subtle">
          {formatTime(timestamp)}
        </Text>
      )}
    </Flex>
  )

  if (variant === 'inline') {
    return (
      <Flex
        direction="col"
        gap="2"
        className="agent-final-response w-full overflow-hidden break-words pl-2"
      >
        {answer}
        {meta}
      </Flex>
    )
  }

  return (
    <Flex justify="start" className="w-full">
      <Flex direction="col" className="agent-final-response w-full max-w-[85%]">
        <Flex direction="col" gap="2" className="overflow-hidden break-words pl-2">
          {answer}
        </Flex>
        {meta}
      </Flex>
    </Flex>
  )
}

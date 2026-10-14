// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Thinking: the run as display-safe milestones, in stream order, while it runs
 * and after. It never shows model text, prompts, tool inputs or tool outputs;
 * the execution workspace is the deeper graph and replay view.
 */

'use client'

import type { FC, ReactNode } from 'react'
import { Flex, Spinner, Text, Tooltip } from '@/adapters/ui'
import { Check, Error, ThinkingReasoning } from '@/adapters/ui/icons'
import { cn } from '@/shared/lib/cn'
import type { ThinkingItem, ThinkingStatus } from './activity-model'

export type ActivityLoadState = 'idle' | 'loading' | 'error'

/** Where a failed item's Phoenix link points, if anywhere. */
export interface FailureLinks {
  /** The failed call's span page */
  spanUrl: (spanId: string | undefined) => string | null
  /** The run's trace page, once the trace is known */
  traceUrl: string | null
  /** Whether the trace is still being looked up */
  traceLoading: boolean
}

interface ThinkingTabProps {
  items: ThinkingItem[]
  loadState: ActivityLoadState
  streaming: boolean
  failed: boolean
  completed: boolean
  links: FailureLinks
}

const statusLabel = (status: ThinkingStatus): string => {
  if (status === 'completed') return 'Complete'
  if (status === 'failed') return 'Failed'
  return 'In progress'
}

const ActivityIcon: FC<{ status: ThinkingStatus }> = ({ status }) => {
  if (status === 'completed') {
    return (
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[rgba(118,185,0,0.16)] text-[#76b900]">
        <Check className="h-4 w-4" aria-hidden="true" />
      </span>
    )
  }
  if (status === 'failed') {
    return (
      <span className="text-error flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-red-500/15">
        <Error className="h-4 w-4" aria-hidden="true" />
      </span>
    )
  }
  return (
    <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[rgba(118,185,0,0.16)]">
      <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-[#76b900]" aria-hidden="true" />
    </span>
  )
}

const FailureTooltipContent: FC<{ item: ThinkingItem; links: FailureLinks }> = ({
  item,
  links,
}) => {
  const spanUrl = links.spanUrl(item.spanId)
  const href = spanUrl ?? links.traceUrl
  return (
    <Flex
      direction="col"
      gap="2"
      className="max-w-80"
      data-testid="thinking-failure-tooltip-content"
    >
      <Text kind="label/semibold/sm">Failure details</Text>
      <Text kind="body/regular/sm">
        {item.failureDetail || 'This observed activity ended with a failure.'}
      </Text>
      {href ? (
        <Flex direction="col" gap="1">
          {item.spanId && spanUrl ? (
            <Text kind="body/regular/xs" className="break-all font-mono">
              Span {item.spanId}
            </Text>
          ) : null}
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`Open ${item.label} ${spanUrl ? 'failed span' : 'trace'} in Phoenix`}
            data-testid="thinking-failure-phoenix-link"
            className="text-success hover:underline focus-visible:underline focus-visible:outline-none"
          >
            {spanUrl ? 'Open failed span in Phoenix' : 'Open trace in Phoenix'}
          </a>
        </Flex>
      ) : (
        <Text kind="body/regular/xs">
          {links.traceLoading
            ? 'Locating the Phoenix trace…'
            : 'A Phoenix trace is not available for this run.'}
        </Text>
      )}
    </Flex>
  )
}

/** The Thinking view of the current run. */
export const ThinkingTab: FC<ThinkingTabProps> = ({
  items,
  loadState,
  streaming,
  failed,
  completed,
  links,
}) => {
  const isFailed = failed || items.some((item) => item.label === 'Run stopped')
  return (
    <Flex direction="col" gap="4" className="h-full min-h-0" data-testid="thinking-timeline">
      <Flex direction="col" gap="1" className="shrink-0">
        <Text kind="body/regular/sm" className="text-secondary">
          Display-safe milestones and observed tool activity from the current Hermes run.
        </Text>
      </Flex>

      {items.length === 0 ? (
        <Flex
          direction="col"
          align="center"
          justify="center"
          className="flex-1 px-8 py-12 text-center"
        >
          {streaming || loadState === 'loading' ? (
            <Spinner size="medium" aria-label="Thinking" />
          ) : (
            <ThinkingReasoning className="text-secondary h-9 w-9" />
          )}
          <Text kind="label/semibold/md" className="text-primary mt-4">
            {streaming
              ? 'Starting the run…'
              : loadState === 'loading'
                ? 'Loading activity…'
                : loadState === 'error'
                  ? 'Activity unavailable'
                  : 'No active run'}
          </Text>
          <Text kind="body/regular/sm" className="text-secondary mt-2 max-w-sm">
            {streaming
              ? 'The first observed activity will appear here.'
              : loadState === 'loading'
                ? 'Restoring the display-safe execution history for this answer.'
                : loadState === 'error'
                  ? 'The answer remains available in chat, but its activity could not be restored.'
                  : 'Ask a question to follow the agent as it reasons and uses tools.'}
          </Text>
        </Flex>
      ) : (
        <ol className="min-h-0 flex-1 overflow-y-auto pr-2" aria-label="Hermes thinking activity">
          {items.map((item, index): ReactNode => {
            const row = (
              <li
                key={item.id}
                className={cn(
                  'relative flex gap-3 pb-5',
                  item.status === 'failed' &&
                    'cursor-help rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500'
                )}
                data-testid="thinking-activity-item"
                data-status={item.status}
                tabIndex={item.status === 'failed' ? 0 : undefined}
                aria-label={
                  item.status === 'failed' ? `Failure details for ${item.label}` : undefined
                }
              >
                {index < items.length - 1 ? (
                  <span
                    className="border-base absolute bottom-0 left-[13px] top-7 border-l"
                    aria-hidden="true"
                  />
                ) : null}
                <ActivityIcon status={item.status} />
                <Flex direction="col" gap="1" className="min-w-0 flex-1 pt-0.5">
                  <Flex align="center" justify="between" gap="3">
                    <Text kind="label/semibold/sm" className="text-primary">
                      {item.label}
                    </Text>
                    <Text
                      kind="label/regular/xs"
                      className={cn(
                        'shrink-0',
                        item.status === 'failed' ? 'text-error' : 'text-secondary'
                      )}
                    >
                      {statusLabel(item.status)}
                    </Text>
                  </Flex>
                  <Text kind="body/regular/sm" className="text-secondary max-w-2xl">
                    {item.description}
                  </Text>
                </Flex>
              </li>
            )

            return item.status === 'failed' ? (
              <Tooltip
                key={item.id}
                side="left"
                align="start"
                openDelayDuration={200}
                className="max-w-80"
                slotContent={<FailureTooltipContent item={item} links={links} />}
              >
                {row}
              </Tooltip>
            ) : (
              row
            )
          })}
          {streaming && !items.some((item) => item.status === 'running') ? (
            <li className="flex gap-3 pb-2" data-testid="thinking-between-steps">
              <ActivityIcon status="running" />
              <Flex direction="col" gap="1" className="pt-0.5">
                <Text kind="label/semibold/sm" className="text-primary">
                  Continuing the analysis
                </Text>
                <Text kind="body/regular/sm" className="text-secondary">
                  Evaluating the latest result and selecting the next useful step.
                </Text>
              </Flex>
            </li>
          ) : null}
        </ol>
      )}

      {items.length > 0 ? (
        <Flex align="center" gap="2" className="border-base shrink-0 border-t pt-3">
          <span
            className={cn(
              'h-2 w-2 rounded-full',
              streaming ? 'animate-pulse bg-[#76b900]' : isFailed ? 'bg-red-500' : 'bg-[#76b900]'
            )}
            aria-hidden="true"
          />
          <Text kind="label/regular/xs" className="text-secondary">
            {streaming
              ? 'Working live'
              : isFailed
                ? 'Run stopped'
                : completed || items.some((item) => item.label === 'Answer ready')
                  ? 'Run complete'
                  : 'Activity loaded'}
          </Text>
        </Flex>
      ) : null}
    </Flex>
  )
}

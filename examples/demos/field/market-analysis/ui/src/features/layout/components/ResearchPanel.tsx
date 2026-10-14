// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * ResearchPanel Component
 *
 * The right-side "Agent Activity" panel: its rail, a resizable width, the
 * header with Stop and Close, and the execution feature's tabs (Thinking,
 * Timeline, Benchmark). Without an execution feature the panel is not rendered.
 *
 * This panel PUSHES the chat area (60% of the width by default) rather than overlaying it.
 */

'use client'

import {
  type FC,
  type KeyboardEvent as ReactKeyboardEvent,
  memo,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { Button, Flex, Spinner, Text } from '@/adapters/ui'
import { Close, Generate, StopCircle, ThinkingReasoning } from '@/adapters/ui/icons'
import { selectActivityJobId, useChatStore } from '@/features/chat'
import { useReducedMotion } from '@/hooks/use-reduced-motion'
import { useExecutionFeature } from '@/shared/context'
import { useLayoutStore } from '../store'
import {
  ACTIVITY_PANEL_COLLAPSED_WIDTH,
  ACTIVITY_PANEL_DEFAULT_OFFSET,
  ACTIVITY_PANEL_DEFAULT_RATIO,
  ACTIVITY_PANEL_FALLBACK_CONTAINER_WIDTH,
  ACTIVITY_PANEL_KEYBOARD_STEP,
  ACTIVITY_PANEL_MIN_WIDTH,
  ACTIVITY_PANEL_REMAINDER_MIN,
  activityPanelWidthBounds,
  clampActivityPanelWidth,
  defaultActivityPanelWidth,
} from './activity-panel-resize'

type ResizeDrag = {
  pointerId: number
  startX: number
  startWidth: number
}

/** The default width before the first measurement: 60% of the row plus the rail. */
const DEFAULT_WIDTH = `clamp(${ACTIVITY_PANEL_MIN_WIDTH}px, calc(${ACTIVITY_PANEL_DEFAULT_RATIO * 100}% + ${ACTIVITY_PANEL_DEFAULT_OFFSET}px), calc(100% - ${ACTIVITY_PANEL_REMAINDER_MIN}px))`

export const ResearchPanel: FC = memo(function ResearchPanel() {
  const { ActivityPanel } = useExecutionFeature()
  const isOpen = useLayoutStore((state) => state.rightPanel === 'research')
  const closeRightPanel = useLayoutStore((state) => state.closeRightPanel)
  const openRightPanel = useLayoutStore((state) => state.openRightPanel)
  const isDeepResearchStreaming = useChatStore((state) => state.isDeepResearchStreaming)
  const jobId = useChatStore(selectActivityJobId)
  const prefersReducedMotion = useReducedMotion()
  const [containerWidth, setContainerWidth] = useState<number | null>(null)
  const [desiredWidth, setDesiredWidth] = useState<number | null>(null)
  const [isResizing, setIsResizing] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)
  const resizeDragRef = useRef<ResizeDrag | null>(null)

  const resolvedContainerWidth = containerWidth ?? ACTIVITY_PANEL_FALLBACK_CONTAINER_WIDTH
  const widthBounds = useMemo(
    () => activityPanelWidthBounds(resolvedContainerWidth),
    [resolvedContainerWidth]
  )
  const openWidth =
    desiredWidth === null
      ? defaultActivityPanelWidth(resolvedContainerWidth)
      : clampActivityPanelWidth(desiredWidth, widthBounds)
  const panelWidth = isOpen ? openWidth : ACTIVITY_PANEL_COLLAPSED_WIDTH
  const unmeasured = isOpen && containerWidth === null && desiredWidth === null

  const currentContainerWidth = useCallback(
    () => panelRef.current?.parentElement?.clientWidth || resolvedContainerWidth,
    [resolvedContainerWidth]
  )

  const currentPanelWidth = useCallback(
    () => panelRef.current?.getBoundingClientRect().width || openWidth,
    [openWidth]
  )

  const applyWidth = useCallback(
    (width: number) => {
      setDesiredWidth(
        clampActivityPanelWidth(width, activityPanelWidthBounds(currentContainerWidth()))
      )
    },
    [currentContainerWidth]
  )

  useEffect(() => {
    const container = panelRef.current?.parentElement
    if (!container) return
    const updateWidth = () => {
      if (container.clientWidth > 0) setContainerWidth(container.clientWidth)
    }
    updateWidth()
    const observer = new ResizeObserver(updateWidth)
    observer.observe(container)
    return () => observer.disconnect()
  }, [ActivityPanel])

  useEffect(() => {
    if (!isResizing) return
    document.body.classList.add('activity-panel-resizing')
    return () => document.body.classList.remove('activity-panel-resizing')
  }, [isResizing])

  useEffect(() => {
    if (isOpen) return
    resizeDragRef.current = null
    setIsResizing(false)
  }, [isOpen])

  const handleStop = useCallback(() => {
    useChatStore
      .getState()
      .cancelActiveDeepResearchJob()
      .catch((error: unknown) => console.error('Failed to stop run:', error))
  }, [])

  const handleToggle = useCallback(() => {
    if (isOpen) closeRightPanel()
    else openRightPanel('research')
  }, [closeRightPanel, isOpen, openRightPanel])

  const handleResizeStart = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return
      resizeDragRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startWidth: currentPanelWidth(),
      }
      event.currentTarget.setPointerCapture?.(event.pointerId)
      setIsResizing(true)
      event.preventDefault()
    },
    [currentPanelWidth]
  )

  const handleResizeMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const drag = resizeDragRef.current
      if (!drag || drag.pointerId !== event.pointerId) return
      applyWidth(drag.startWidth + drag.startX - event.clientX)
    },
    [applyWidth]
  )

  const finishResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = resizeDragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    resizeDragRef.current = null
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture?.(event.pointerId)
    }
    setIsResizing(false)
  }, [])

  const handleResizeKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      const bounds = activityPanelWidthBounds(currentContainerWidth())
      const current = currentPanelWidth()
      const step = event.shiftKey ? ACTIVITY_PANEL_KEYBOARD_STEP * 2 : ACTIVITY_PANEL_KEYBOARD_STEP
      let next: number | null = null
      if (event.key === 'ArrowLeft') next = current + step
      else if (event.key === 'ArrowRight') next = current - step
      else if (event.key === 'Home') next = bounds.min
      else if (event.key === 'End') next = bounds.max
      else if (event.key === 'Enter' || event.key === '0') {
        event.preventDefault()
        setDesiredWidth(null)
        return
      }
      if (next === null) return
      event.preventDefault()
      applyWidth(next)
    },
    [applyWidth, currentContainerWidth, currentPanelWidth]
  )

  if (!ActivityPanel) return null

  return (
    <div
      ref={panelRef}
      data-testid="research-panel-shell"
      data-resizing={isResizing ? 'true' : 'false'}
      className="relative flex h-full"
      style={{
        width: unmeasured ? DEFAULT_WIDTH : `${panelWidth}px`,
        minWidth: unmeasured ? DEFAULT_WIDTH : `${panelWidth}px`,
        transition:
          prefersReducedMotion || isResizing
            ? 'none'
            : 'width 600ms ease-in-out, min-width 600ms ease-in-out',
      }}
    >
      {isOpen ? (
        <div
          role="separator"
          tabIndex={0}
          aria-label="Resize agent activity panel"
          aria-orientation="vertical"
          aria-controls="agent-activity-panel"
          aria-valuemin={widthBounds.min}
          aria-valuemax={widthBounds.max}
          aria-valuenow={Math.round(openWidth)}
          aria-valuetext={`${Math.round(openWidth)} pixels wide`}
          title="Drag to resize Agent Activity · Double-click to reset"
          className="activity-panel-resizer"
          data-testid="research-panel-resizer"
          data-resizing={isResizing ? 'true' : 'false'}
          onPointerDown={handleResizeStart}
          onPointerMove={handleResizeMove}
          onPointerUp={finishResize}
          onPointerCancel={finishResize}
          onLostPointerCapture={() => {
            resizeDragRef.current = null
            setIsResizing(false)
          }}
          onDoubleClick={() => setDesiredWidth(null)}
          onKeyDown={handleResizeKeyDown}
        />
      ) : null}
      <button
        onClick={handleToggle}
        className="research-panel-toggle border-base bg-surface-base relative z-10 flex w-10 shrink-0 cursor-pointer items-center justify-center self-start overflow-hidden rounded-bl-lg border-b border-l border-r border-t transition-colors hover:border-[#76B900]"
        style={{
          height: 'calc(var(--spacing) * 38)',
          backgroundColor: 'var(--background-color-surface-base)',
        }}
        aria-label={isOpen ? 'Close agent activity panel' : 'Open agent activity panel'}
        aria-expanded={isOpen}
        title={isOpen ? 'Close agent activity panel' : 'Open agent activity panel'}
        data-testid="research-panel-toggle"
      >
        <span
          className="absolute left-1/2 flex -translate-x-1/2 items-center justify-center"
          style={{
            top: 'calc(var(--spacing) * 3)',
            width: 'calc(var(--spacing) * 6)',
            height: 'calc(var(--spacing) * 6)',
          }}
        >
          {isDeepResearchStreaming ? (
            <Spinner size="small" aria-label="Thinking" />
          ) : (
            <Generate className="h-[calc(var(--spacing)*6)] w-[calc(var(--spacing)*6)]" />
          )}
        </span>
        <Text
          kind="label/semibold/sm"
          className="text-primary absolute left-1/2 -translate-x-1/2 -rotate-90 whitespace-nowrap"
          style={{ top: 'calc(var(--spacing) * 21)' }}
        >
          Show Activity
        </Text>
      </button>

      <div
        id="agent-activity-panel"
        className={`border-base bg-surface-base -ml-px h-full flex-1 overflow-hidden rounded-bl-xl ${
          isOpen ? 'border-l' : ''
        }`}
        aria-hidden={!isOpen}
      >
        <Flex
          direction="col"
          className="h-full w-full"
          style={{
            visibility: isOpen ? 'visible' : 'hidden',
            opacity: isOpen ? 1 : 0,
            transition: prefersReducedMotion
              ? 'none'
              : isOpen
                ? 'opacity 100ms ease-in-out, visibility 0ms'
                : 'opacity 100ms ease-in-out 500ms, visibility 0ms 600ms',
          }}
        >
          <Flex
            align="center"
            justify="between"
            className="border-base shrink-0 border-b py-4 pl-6 pr-8"
          >
            <Flex align="center" gap="3">
              <ThinkingReasoning className="h-6 w-6 text-[#76b900]" />
              <Flex direction="col" gap="0.5">
                <Text kind="label/semibold/lg" className="text-primary">
                  Agent Activity
                </Text>
                <Text kind="body/regular/xs" className="text-secondary">
                  Live narrative and completed timing
                </Text>
              </Flex>
            </Flex>

            <Flex align="center" gap="2">
              {isDeepResearchStreaming ? (
                <Button
                  kind="tertiary"
                  size="small"
                  onClick={handleStop}
                  aria-label="Stop run"
                  title="Stop run"
                  data-testid="research-panel-stop"
                >
                  <StopCircle className="mr-2 h-4 w-4" aria-hidden="true" />
                  Stop
                </Button>
              ) : null}
              <Button
                kind="tertiary"
                size="small"
                onClick={closeRightPanel}
                aria-label="Close agent activity panel"
                title="Close agent activity panel"
                data-testid="research-panel-close"
              >
                <Close className="h-4 w-4" aria-hidden="true" />
              </Button>
            </Flex>
          </Flex>

          <ActivityPanel jobId={jobId} streaming={isDeepResearchStreaming} open={isOpen} />
        </Flex>
      </div>
    </div>
  )
})

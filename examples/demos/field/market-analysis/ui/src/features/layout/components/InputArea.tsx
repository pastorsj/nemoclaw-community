// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * InputArea Component
 *
 * Chat input area at the bottom of the chat view: the demo scenario picker
 * (the active data pack's examples, five rows at a time), the question, the
 * microphone when voice input is on, the data source indicator, and send (or
 * stop while a run is in progress). A recorded session shows it read only, as
 * the original demo UI did; in replay mode that includes the original's
 * microphone, disabled.
 */

'use client'

import {
  type CSSProperties,
  type FC,
  memo,
  useState,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  type KeyboardEvent,
} from 'react'
import { Banner, Flex, Text, Button, Select, TextArea } from '@/adapters/ui'
import { useHermesChat, useChatStore, useIsCurrentSessionBusy } from '@/features/chat'
import {
  getSpeechInputStatusMessage,
  SpeechInputButton,
  useSpeechInput,
} from '@/features/speech-input'
import { useAppConfig } from '@/shared/context'
import { useLayoutStore } from '../store'
import { ToolPills } from '@/shared/components/ToolPills'
import { getActiveDemoScenario, getAvailableDemoScenarios, type DemoScenario } from '../scenarios'
import { ChartFlow, Globe, Paperplane, StopCircle } from '@/adapters/ui/icons'
import { VISIBLE_EXAMPLE_ROWS, visibleRowsHeight } from './visible-rows'

const NO_SCENARIOS: DemoScenario[] = []
const EXAMPLE_LIST = 'demo-scenario-list'
/**
 * Run turns into Stop in place, so the second click of a double click on Run
 * would stop the run it just started: Stop ignores clicks this soon after Run.
 */
const STOP_AFTER_RUN_GUARD_MS = 600

interface InputAreaProps {
  /** Placeholder text */
  placeholder?: string
  /** The active data pack's examples, offered as demo scenarios */
  scenarios?: DemoScenario[]
  /** Whether the demo scenario picker shows (not beside the execution view) */
  showDemoScenarios?: boolean
}

/**
 * Chat input component with text area and action buttons.
 * Positioned at the bottom of the chat area.
 */
export const InputArea: FC<InputAreaProps> = memo(function InputArea({
  placeholder = 'Check data sources and ask a research question...',
  scenarios = NO_SCENARIOS,
  showDemoScenarios = true,
}) {
  const [message, setMessage] = useState('')
  // The latest draft, for a transcript that arrives after the user typed more
  const messageRef = useRef('')
  const textAreaRef = useRef<HTMLTextAreaElement>(null)
  const speechInsertionRef = useRef({ start: 0, end: 0 })
  const { sendMessage, stop } = useHermesChat()
  const sentAtRef = useRef(Number.NEGATIVE_INFINITY)
  const { mode, speechInput: speechInputConfig } = useAppConfig()

  // A running job in this session pauses the composer; it can be stopped.
  const isBusy = useIsCurrentSessionBusy()

  const currentConversation = useChatStore((state) => state.currentConversation)
  const isRecordedSession = mode === 'replay' || currentConversation?.readOnly === true
  const disabled = isBusy || isRecordedSession

  // A recorded session's read-only composer shows no draft of a live question.
  useEffect(() => {
    if (!isRecordedSession) return
    messageRef.current = ''
    setMessage('')
  }, [isRecordedSession])
  const ensureSession = useChatStore((state) => state.ensureSession)
  const saveDataSourcesToConversation = useChatStore((state) => state.saveDataSourcesToConversation)

  const enabledDataSourceIds = useLayoutStore((s) => s.enabledDataSourceIds)
  const availableDataSources = useLayoutStore((s) => s.availableDataSources)
  const setEnabledDataSources = useLayoutStore((s) => s.setEnabledDataSources)
  const promptDraft = useLayoutStore((s) => s.promptDraft)
  const setPromptDraft = useLayoutStore((s) => s.setPromptDraft)

  const availableDemoScenarios = useMemo(
    () =>
      getAvailableDemoScenarios(
        scenarios,
        (availableDataSources ?? []).map((source) => source.id)
      ),
    [availableDataSources, scenarios]
  )
  const activeDemoScenario = getActiveDemoScenario(
    message,
    enabledDataSourceIds,
    availableDemoScenarios
  )

  // The open list shows five examples and scrolls for the rest. Their height is measured once the
  // list is laid out (it renders when the picker opens), and kept for the next opening. The list is
  // never taller than the space it has; that is the select's own limit without the 12 px its opening
  // slide keeps back, which would cut the fifth row below the composer of a 900 px high window.
  const [exampleListHeight, setExampleListHeight] = useState<number | null>(null)
  const handlePickerOpenChange = useCallback((open: boolean) => {
    if (!open) return
    window.requestAnimationFrame(() => {
      const list = document.querySelector<HTMLElement>(`[data-testid="${EXAMPLE_LIST}"]`)
      if (list) setExampleListHeight(visibleRowsHeight(list, VISIBLE_EXAMPLE_ROWS))
    })
  }, [])
  const exampleListStyle = useMemo<CSSProperties | undefined>(
    () =>
      exampleListHeight === null
        ? undefined
        : {
            maxHeight: `min(${exampleListHeight}px, var(--max-height))`,
          },
    [exampleListHeight]
  )

  const handleScenarioChange = useCallback(
    (scenarioId: string) => {
      const scenario = availableDemoScenarios.find((candidate) => candidate.id === scenarioId)
      if (!scenario || disabled) return
      // Session creation restores its default source set, so it must happen before
      // applying and persisting the scenario's exact connections.
      if (!currentConversation) ensureSession()
      setEnabledDataSources([...scenario.sourceIds])
      saveDataSourcesToConversation([...scenario.sourceIds])
      messageRef.current = scenario.question
      setMessage(scenario.question)
    },
    [
      availableDemoScenarios,
      currentConversation,
      disabled,
      ensureSession,
      saveDataSourcesToConversation,
      setEnabledDataSources,
    ]
  )

  // Prefill the composer from a staged prompt (e.g. a featured question)
  useEffect(() => {
    if (promptDraft) {
      messageRef.current = promptDraft
      setMessage(promptDraft)
      setPromptDraft(null)
    }
  }, [promptDraft, setPromptDraft])

  const handleSubmit = useCallback(() => {
    if (!message.trim() || disabled) return
    // Session creation needs the user ID, which is set at startup.
    if (!ensureSession()) return
    messageRef.current = ''
    setMessage('')
    sentAtRef.current = performance.now()
    sendMessage(message)
  }, [message, disabled, ensureSession, sendMessage])

  const handleStop = useCallback(() => {
    if (performance.now() - sentAtRef.current < STOP_AFTER_RUN_GUARD_MS) return
    stop()
  }, [stop])

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        handleSubmit()
      }
    },
    [handleSubmit]
  )

  const handleValueChange = useCallback(
    (value: string) => {
      // Persist a session as soon as the user starts typing. This keeps
      // logo-triggered "new session" drafts out of history until touched.
      if (!currentConversation && value.trim().length > 0) {
        ensureSession()
      }
      messageRef.current = value
      setMessage(value)
    },
    [currentConversation, ensureSession]
  )

  const rememberSpeechInsertionPoint = useCallback(() => {
    const textArea = textAreaRef.current
    if (!textArea) {
      speechInsertionRef.current = { start: message.length, end: message.length }
      return
    }
    speechInsertionRef.current = {
      start: textArea.selectionStart ?? message.length,
      end: textArea.selectionEnd ?? message.length,
    }
  }, [message.length])

  const insertSpeechTranscript = useCallback(
    (transcript: string) => {
      const spokenText = transcript.trim()
      if (!spokenText) return
      if (!currentConversation) ensureSession()

      // Transcription is asynchronous and the composer stays editable while it
      // runs: insert into the latest draft so a late result never overwrites
      // text typed meanwhile.
      const currentMessage = messageRef.current
      const start = Math.min(currentMessage.length, Math.max(0, speechInsertionRef.current.start))
      const end = Math.min(currentMessage.length, Math.max(start, speechInsertionRef.current.end))
      const before = currentMessage.slice(0, start)
      const after = currentMessage.slice(end)
      const leadingSpace = before.length > 0 && !/\s$/.test(before) ? ' ' : ''
      const trailingSpace = after.length > 0 && !/^\s/.test(after) ? ' ' : ''
      const nextMessage = `${before}${leadingSpace}${spokenText}${trailingSpace}${after}`
      const nextCursor = before.length + leadingSpace.length + spokenText.length

      messageRef.current = nextMessage
      setMessage(nextMessage)
      speechInsertionRef.current = { start: nextCursor, end: nextCursor }
      window.requestAnimationFrame(() => {
        textAreaRef.current?.focus()
        textAreaRef.current?.setSelectionRange(nextCursor, nextCursor)
      })
    },
    [currentConversation, ensureSession]
  )

  const speechInput = useSpeechInput({
    enabled: speechInputConfig.enabled && !disabled,
    maxSeconds: speechInputConfig.maxSeconds,
    onTranscript: insertSpeechTranscript,
  })

  const handleSpeechToggle = useCallback(() => {
    if (speechInput.state === 'recording') {
      void speechInput.stop()
      return
    }
    void speechInput.start()
  }, [speechInput])

  // The counter counts database connections, as the original UI's did (its one registered
  // connection was the market database): each pack has one, its structured source. The document
  // collections beside it are listed in the Data Sources panel.
  const databaseIds = new Set(
    (availableDataSources ?? [])
      .filter((source) => source.kind === 'structured')
      .map((source) => source.id)
  )
  const enabledDatabaseCount = enabledDataSourceIds.filter((id) => databaseIds.has(id)).length
  // Replay keeps the original's microphone in the read-only composer; it never records there.
  const showMicrophone = speechInputConfig.enabled || mode === 'replay'

  const toggleDataSources = useCallback(() => {
    const { rightPanel, closeRightPanel, openRightPanel } = useLayoutStore.getState()
    if (rightPanel === 'data-sources') {
      closeRightPanel()
    } else {
      openRightPanel('data-sources')
    }
  }, [])

  return (
    <Flex direction="col" className="mx-auto w-full max-w-4xl px-6 py-4">
      <Flex
        direction="col"
        className="composer-surface relative rounded-[var(--radius-composer)] border p-3.5 transition-colors"
      >
        {showDemoScenarios && !isRecordedSession && availableDemoScenarios.length > 0 && (
          <div
            className="border-base mb-2 grid grid-cols-1 gap-1.5 border-b pb-2 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-center sm:gap-2"
            data-testid="demo-scenario-control"
          >
            <Flex align="center" gap="1.5" className="shrink-0">
              <ChartFlow className="text-brand h-4 w-4" />
              <Text kind="label/semibold/sm" className="text-secondary">
                Demo scenario
              </Text>
            </Flex>
            <div className="w-full min-w-0 flex-1 sm:max-w-sm sm:justify-self-end">
              <Select
                aria-label="Choose a demo scenario"
                placeholder="Choose an example"
                size="small"
                side="bottom"
                triggerKind="flat"
                value={activeDemoScenario?.id ?? ''}
                onValueChange={handleScenarioChange}
                onOpenChange={handlePickerOpenChange}
                disabled={disabled}
                attributes={{
                  SelectTrigger: { 'data-testid': 'demo-scenario-select' },
                  SelectContent: { 'data-testid': EXAMPLE_LIST, style: exampleListStyle },
                }}
                items={availableDemoScenarios.map((scenario) => ({
                  value: scenario.id,
                  children: scenario.label,
                  slotRight: (
                    <ToolPills
                      pills={scenario.tools.map((pill) => ({ pill }))}
                      className="justify-end"
                    />
                  ),
                  attributes: {
                    SelectItem: {
                      title: scenario.description,
                      'data-scenario-id': scenario.id,
                    },
                  },
                }))}
              />
            </div>
            <span className="sr-only" role="status" aria-live="polite">
              {activeDemoScenario
                ? `${activeDemoScenario.label} loaded; ${activeDemoScenario.sourceIds.length} connection selected.`
                : ''}
            </span>
          </div>
        )}
        {isRecordedSession && (
          <Text kind="label/semibold/xs" className="text-subtle mb-2 px-1">
            Recorded test session · read only
          </Text>
        )}
        {/* Text Input */}
        <div onKeyDown={handleKeyDown}>
          <TextArea
            ref={textAreaRef}
            className="composer-textarea border-0 bg-transparent"
            value={message}
            onValueChange={handleValueChange}
            onSelect={rememberSpeechInsertionPoint}
            placeholder={
              isRecordedSession
                ? 'Recorded test sessions are read only'
                : isBusy
                  ? 'Please wait...'
                  : placeholder
            }
            disabled={disabled}
            resizeable="auto"
            size="medium"
            aria-label="Chat message input"
            slotRight={
              showMicrophone ? (
                <SpeechInputButton
                  state={speechInput.state}
                  disabled={disabled}
                  onBeforeToggle={rememberSpeechInsertionPoint}
                  onToggle={handleSpeechToggle}
                />
              ) : undefined
            }
          />
        </div>

        {speechInputConfig.enabled && (
          <span className="sr-only" role="status" aria-live="polite">
            {getSpeechInputStatusMessage(speechInput.state)}
          </span>
        )}

        {speechInput.error && (
          <Banner kind="inline" status="error" onClose={speechInput.clearError} className="mt-2">
            {speechInput.error}
          </Banner>
        )}

        {/* Bottom Actions Bar */}
        <Flex align="center" justify="end" gap="1.5" className="border-base mt-3 border-t pt-3">
          {/* Sources indicator - clickable to toggle data connections */}
          <Button
            kind="tertiary"
            size="tiny"
            onClick={toggleDataSources}
            disabled={isRecordedSession}
            tabIndex={-1}
            aria-label="Toggle data sources connections"
            title="Selected data connections"
          >
            <Flex align="center" gap="1">
              <Globe className="h-3 w-3" />
              <Text kind="label/bold/sm">
                {enabledDatabaseCount}/{databaseIds.size}
              </Text>
            </Flex>
          </Button>

          {isBusy ? (
            <Button
              kind="secondary"
              size="small"
              color="danger"
              onClick={handleStop}
              aria-label="Stop generating"
              title="Stop generating"
            >
              <StopCircle className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              kind="primary"
              size="small"
              color={!message.trim() || disabled ? undefined : 'brand'}
              onClick={handleSubmit}
              disabled={!message.trim() || disabled}
              aria-label="Send message"
              title="Send query"
            >
              <Paperplane className="h-4 w-4" />
            </Button>
          )}
        </Flex>
      </Flex>
    </Flex>
  )
})

// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client'

import type { FC, PointerEvent } from 'react'
import { Button } from '@/adapters/ui'
import { LoadingSpinner, Microphone, StopCircle } from '@/adapters/ui/icons'
import type { SpeechInputState } from './use-speech-input'

interface SpeechInputButtonProps {
  state: SpeechInputState
  disabled?: boolean
  onBeforeToggle?: () => void
  onToggle: () => void
}

const labelForState = (state: SpeechInputState): string => {
  switch (state) {
    case 'requesting-permission':
      return 'Requesting microphone permission'
    case 'recording':
      return 'Stop voice recording'
    case 'transcribing':
      return 'Transcribing speech'
    default:
      return 'Start voice input'
  }
}

export const SpeechInputButton: FC<SpeechInputButtonProps> = ({
  state,
  disabled = false,
  onBeforeToggle,
  onToggle,
}) => {
  const isRecording = state === 'recording'
  const isWaiting = state === 'requesting-permission' || state === 'transcribing'
  const label = labelForState(state)

  const handlePointerDown = (event: PointerEvent<HTMLButtonElement>): void => {
    if (event.button === 0 && !disabled && !isWaiting) onBeforeToggle?.()
  }

  return (
    <span className="speech-input-button-slot">
      <Button
        type="button"
        kind="tertiary"
        size="small"
        color={isRecording ? 'danger' : undefined}
        className={`mr-1 ${isRecording ? 'animate-pulse' : ''}`}
        onPointerDown={handlePointerDown}
        onClick={(event) => {
          event.stopPropagation()
          onToggle()
        }}
        disabled={disabled || isWaiting}
        aria-label={label}
        aria-pressed={isRecording}
        title={label}
        data-testid="speech-input-button"
      >
        {isWaiting ? (
          <LoadingSpinner aria-label={label} />
        ) : isRecording ? (
          <StopCircle className="h-4 w-4" />
        ) : (
          <Microphone className="h-4 w-4" />
        )}
      </Button>
    </span>
  )
}

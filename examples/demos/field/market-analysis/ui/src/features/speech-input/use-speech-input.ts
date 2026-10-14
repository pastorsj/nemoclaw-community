// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { transcribeSpeech, type SpeechTranscriptionError } from '@/adapters/api/speech-client'
import { createBrowserSpeechRecorder, type SpeechRecorder } from './browser-recorder'

export type SpeechInputState =
  | 'idle'
  | 'requesting-permission'
  | 'recording'
  | 'transcribing'
  | 'error'

interface UseSpeechInputOptions {
  enabled: boolean
  maxSeconds: number
  onTranscript: (text: string) => void
  createRecorder?: (maxSeconds: number) => SpeechRecorder
  transcribe?: typeof transcribeSpeech
}

interface SpeechInputController {
  state: SpeechInputState
  error: string | null
  start: () => Promise<void>
  stop: () => Promise<void>
  clearError: () => void
}

const userFacingError = (error: unknown): string => {
  if (error instanceof DOMException) {
    switch (error.name) {
      case 'NotAllowedError':
        return 'Microphone permission was denied. Allow microphone access and try again.'
      case 'NotFoundError':
        return 'No microphone was found. Connect a microphone and try again.'
      case 'NotReadableError':
        return 'The microphone is already in use or unavailable.'
      case 'SecurityError':
        return 'Microphone access requires a secure connection.'
      case 'NotSupportedError':
        return 'Voice input is not supported by this browser.'
    }
  }

  if (error && typeof error === 'object' && 'userMessage' in error) {
    return String((error as SpeechTranscriptionError).userMessage)
  }
  if (error instanceof Error && error.message === 'No microphone audio was captured') {
    return 'No speech was captured. Try recording again.'
  }
  return 'Voice input could not be completed. Try again.'
}

export const getSpeechInputStatusMessage = (state: SpeechInputState): string => {
  switch (state) {
    case 'requesting-permission':
      return 'Requesting microphone permission'
    case 'recording':
      return 'Recording. Activate the microphone button to stop.'
    case 'transcribing':
      return 'Transcribing speech with NVIDIA Nemotron'
    case 'error':
      return 'Voice input failed'
    default:
      return 'Voice input ready'
  }
}

export const useSpeechInput = ({
  enabled,
  maxSeconds,
  onTranscript,
  createRecorder = createBrowserSpeechRecorder,
  transcribe = transcribeSpeech,
}: UseSpeechInputOptions): SpeechInputController => {
  const [state, setState] = useState<SpeechInputState>('idle')
  const [error, setError] = useState<string | null>(null)
  const recorderRef = useRef<SpeechRecorder | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const durationTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)

  const clearDurationTimer = useCallback(() => {
    if (durationTimerRef.current) clearTimeout(durationTimerRef.current)
    durationTimerRef.current = null
  }, [])

  const stop = useCallback(async () => {
    const recorder = recorderRef.current
    if (!recorder) return
    recorderRef.current = null
    clearDurationTimer()
    if (mountedRef.current) setState('transcribing')

    const abortController = new AbortController()
    abortRef.current = abortController
    try {
      const recording = await recorder.stop()
      const transcript = await transcribe(recording, { signal: abortController.signal })
      if (!mountedRef.current || abortController.signal.aborted) return
      onTranscript(transcript.text)
      setError(null)
      setState('idle')
    } catch (caught) {
      if (!mountedRef.current || abortController.signal.aborted) return
      setError(userFacingError(caught))
      setState('error')
    } finally {
      if (abortRef.current === abortController) abortRef.current = null
    }
  }, [clearDurationTimer, onTranscript, transcribe])

  const stopRef = useRef(stop)
  useEffect(() => {
    stopRef.current = stop
  }, [stop])

  const start = useCallback(async () => {
    if (
      !enabled ||
      recorderRef.current ||
      state === 'requesting-permission' ||
      state === 'transcribing'
    ) {
      return
    }
    setError(null)
    setState('requesting-permission')
    let recorder: SpeechRecorder | null = null

    try {
      recorder = createRecorder(maxSeconds)
      // Reserve the recorder before the permission promise resolves. This
      // prevents a rapid second activation from opening another device stream.
      recorderRef.current = recorder
      await recorder.start()
      if (!mountedRef.current || recorderRef.current !== recorder) {
        await recorder.cancel()
        return
      }
      setState('recording')
      durationTimerRef.current = setTimeout(() => void stopRef.current(), maxSeconds * 1000)
    } catch (caught) {
      const isCurrentOperation = recorder === null || recorderRef.current === recorder
      if (recorderRef.current === recorder) recorderRef.current = null
      await recorder?.cancel().catch(() => undefined)
      if (!mountedRef.current || !isCurrentOperation) return
      setError(userFacingError(caught))
      setState('error')
    }
  }, [createRecorder, enabled, maxSeconds, state])

  const clearError = useCallback(() => {
    setError(null)
    setState((current) => (current === 'error' ? 'idle' : current))
  }, [])

  useEffect(() => {
    if (enabled) return
    clearDurationTimer()
    abortRef.current?.abort()
    abortRef.current = null
    const recorder = recorderRef.current
    recorderRef.current = null
    if (recorder) void recorder.cancel()
    setError(null)
    setState('idle')
  }, [clearDurationTimer, enabled])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      clearDurationTimer()
      abortRef.current?.abort()
      const recorder = recorderRef.current
      recorderRef.current = null
      if (recorder) void recorder.cancel()
    }
  }, [clearDurationTimer])

  return { state, error, start, stop, clearError }
}

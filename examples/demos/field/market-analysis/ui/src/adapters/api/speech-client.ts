// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Voice input: one WAV recording (16 kHz, mono, PCM16) to the API's
 * transcription route (`POST /v1/speech/transcriptions`, NVIDIA Nemotron ASR),
 * through the UI's same-origin proxy.
 */

const TRANSCRIPTION_URL = '/api/v1/speech/transcriptions'

export interface SpeechTranscription {
  text: string
}

export interface TranscribeSpeechOptions {
  signal?: AbortSignal
}

export class SpeechTranscriptionError extends Error {
  readonly userMessage: string
  readonly status?: number

  constructor(userMessage: string, status?: number) {
    super(userMessage)
    this.name = 'SpeechTranscriptionError'
    this.userMessage = userMessage
    this.status = status
  }
}

const messageForStatus = (status: number): string => {
  switch (status) {
    case 400:
      return 'The recording could not be processed. Try recording again.'
    case 401:
    case 403:
      return 'Voice input is not authorized for this session.'
    case 413:
      return 'The recording was too long. Record a shorter question and try again.'
    case 429:
      return 'Voice transcription is busy. Wait a moment and try again.'
    case 503:
      return 'Voice transcription is temporarily unavailable.'
    case 504:
      return 'Voice transcription timed out. Try again.'
    default:
      return 'Voice transcription could not be completed. Try again.'
  }
}

/** Send an in-memory PCM WAV to the same-origin server transcription boundary. */
export const transcribeSpeech = async (
  recording: Blob,
  options: TranscribeSpeechOptions = {}
): Promise<SpeechTranscription> => {
  if (recording.size <= 44) {
    throw new SpeechTranscriptionError('No speech was captured. Try recording again.')
  }

  const response = await fetch(TRANSCRIPTION_URL, {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'audio/wav',
    },
    body: recording,
    signal: options.signal,
  })

  if (!response.ok) {
    throw new SpeechTranscriptionError(messageForStatus(response.status), response.status)
  }

  const body: unknown = await response.json().catch(() => null)
  if (
    !body ||
    typeof body !== 'object' ||
    !('text' in body) ||
    typeof body.text !== 'string' ||
    body.text.trim().length === 0
  ) {
    throw new SpeechTranscriptionError('No speech was recognized. Try recording again.')
  }

  return { text: body.text.trim() }
}

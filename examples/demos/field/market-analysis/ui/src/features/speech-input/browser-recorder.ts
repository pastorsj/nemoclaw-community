// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  combineAudioChunks,
  encodePcm16Wav,
  NEMOTRON_SAMPLE_RATE,
  resampleMonoAudio,
} from './audio'

export interface SpeechRecorder {
  start(): Promise<void>
  stop(): Promise<Blob>
  cancel(): Promise<void>
}

interface RecorderDependencies {
  mediaDevices: Pick<MediaDevices, 'getUserMedia'>
  createAudioContext: () => AudioContext
}

const resolveDependencies = (): RecorderDependencies => {
  if (typeof window === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
    throw new DOMException('Microphone capture is not supported', 'NotSupportedError')
  }

  const AudioContextConstructor =
    window.AudioContext ??
    (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!AudioContextConstructor) {
    throw new DOMException('Browser audio processing is not supported', 'NotSupportedError')
  }

  return {
    mediaDevices: navigator.mediaDevices,
    createAudioContext: () => new AudioContextConstructor(),
  }
}

/**
 * Short-lived Web Audio recorder that retains audio only in browser memory.
 * The processor output is muted, while remaining connected so browsers keep
 * delivering capture frames while the tab is active.
 */
export class BrowserSpeechRecorder implements SpeechRecorder {
  private readonly dependencies?: RecorderDependencies
  private readonly maxSeconds: number
  private stream: MediaStream | null = null
  private context: AudioContext | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private processor: ScriptProcessorNode | null = null
  private mute: GainNode | null = null
  private chunks: Float32Array[] = []
  private sourceSampleRate = 0
  private capturedSamples = 0

  constructor(dependencies?: RecorderDependencies, maxSeconds = 60) {
    this.dependencies = dependencies
    this.maxSeconds = maxSeconds
  }

  async start(): Promise<void> {
    if (this.stream) throw new Error('A microphone recording is already active')
    const dependencies = this.dependencies ?? resolveDependencies()

    const stream = await dependencies.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    })
    this.stream = stream
    this.chunks = []
    this.capturedSamples = 0

    try {
      const context = dependencies.createAudioContext()
      this.context = context
      if (context.state === 'suspended') await context.resume()

      const source = context.createMediaStreamSource(stream)
      this.source = source
      const processor = context.createScriptProcessor(4096, 1, 1)
      this.processor = processor
      const mute = context.createGain()
      this.mute = mute
      mute.gain.value = 0
      processor.onaudioprocess = (event) => {
        // Browser timers can fire late when a tab is busy. Bound the samples
        // themselves so an automatic stop at the configured limit still
        // produces audio the backend will accept.
        const remaining = Math.max(
          0,
          Math.floor(this.maxSeconds * context.sampleRate) - this.capturedSamples
        )
        if (remaining === 0) return
        const input = event.inputBuffer.getChannelData(0)
        const chunk = input.slice(0, Math.min(input.length, remaining))
        this.chunks.push(chunk)
        this.capturedSamples += chunk.length
      }

      source.connect(processor)
      processor.connect(mute)
      mute.connect(context.destination)

      this.sourceSampleRate = context.sampleRate
    } catch (error) {
      await this.releaseResources()
      throw error
    }
  }

  async stop(): Promise<Blob> {
    if (!this.stream || !this.context) throw new Error('No microphone recording is active')

    const chunks = this.chunks
    const sampleRate = this.sourceSampleRate
    await this.releaseResources()
    this.chunks = []
    this.capturedSamples = 0

    const samples = combineAudioChunks(chunks)
    if (samples.length === 0) throw new Error('No microphone audio was captured')
    return encodePcm16Wav(resampleMonoAudio(samples, sampleRate), NEMOTRON_SAMPLE_RATE)
  }

  async cancel(): Promise<void> {
    await this.releaseResources()
    this.chunks = []
    this.capturedSamples = 0
  }

  private async releaseResources(): Promise<void> {
    if (this.processor) this.processor.onaudioprocess = null
    this.source?.disconnect()
    this.processor?.disconnect()
    this.mute?.disconnect()
    this.stream?.getTracks().forEach((track) => track.stop())

    const context = this.context
    this.stream = null
    this.context = null
    this.source = null
    this.processor = null
    this.mute = null
    this.sourceSampleRate = 0

    if (context && context.state !== 'closed') {
      await context.close().catch(() => undefined)
    }
  }
}

export const createBrowserSpeechRecorder = (maxSeconds = 60): SpeechRecorder =>
  new BrowserSpeechRecorder(undefined, maxSeconds)

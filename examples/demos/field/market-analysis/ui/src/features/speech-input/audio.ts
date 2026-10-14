// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

const TARGET_SAMPLE_RATE = 16_000

/**
 * Resample mono floating-point browser audio for the NVIDIA ASR input
 * contract. Browser audio contexts commonly capture at 44.1 or 48 kHz.
 */
export const resampleMonoAudio = (
  samples: Float32Array,
  sourceSampleRate: number,
  targetSampleRate = TARGET_SAMPLE_RATE
): Float32Array => {
  if (sourceSampleRate <= 0 || targetSampleRate <= 0) {
    throw new Error('Audio sample rates must be positive')
  }
  if (samples.length === 0) return new Float32Array()
  if (sourceSampleRate === targetSampleRate) return samples.slice()

  const outputLength = Math.max(
    1,
    Math.round((samples.length * targetSampleRate) / sourceSampleRate)
  )
  const output = new Float32Array(outputLength)
  const ratio = sourceSampleRate / targetSampleRate

  if (ratio > 1) {
    // Averaging each source window provides a small anti-aliasing improvement
    // over simply dropping samples during the common 48 kHz -> 16 kHz path.
    for (let index = 0; index < outputLength; index += 1) {
      const start = Math.floor(index * ratio)
      const end = Math.max(start + 1, Math.min(samples.length, Math.floor((index + 1) * ratio)))
      let total = 0
      for (let sourceIndex = start; sourceIndex < end; sourceIndex += 1) {
        total += samples[sourceIndex]
      }
      output[index] = total / (end - start)
    }
    return output
  }

  // Linear interpolation also keeps the helper correct for uncommon devices
  // whose native sample rate is below the configured ASR rate.
  for (let index = 0; index < outputLength; index += 1) {
    const position = index * ratio
    const before = Math.floor(position)
    const after = Math.min(samples.length - 1, before + 1)
    const fraction = position - before
    output[index] = samples[before] * (1 - fraction) + samples[after] * fraction
  }
  return output
}

const writeAscii = (view: DataView, offset: number, value: string): void => {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index))
  }
}

/** Encode normalized mono samples as a 16-bit PCM WAV accepted by NVIDIA ASR. */
export const encodePcm16Wav = (samples: Float32Array, sampleRate = TARGET_SAMPLE_RATE): Blob => {
  const bytesPerSample = 2
  const dataLength = samples.length * bytesPerSample
  const buffer = new ArrayBuffer(44 + dataLength)
  const view = new DataView(buffer)

  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + dataLength, true)
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * bytesPerSample, true)
  view.setUint16(32, bytesPerSample, true)
  view.setUint16(34, 16, true)
  writeAscii(view, 36, 'data')
  view.setUint32(40, dataLength, true)

  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]))
    view.setInt16(44 + index * bytesPerSample, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true)
  }

  return new Blob([buffer], { type: 'audio/wav' })
}

export const combineAudioChunks = (chunks: readonly Float32Array[]): Float32Array => {
  const length = chunks.reduce((total, chunk) => total + chunk.length, 0)
  const combined = new Float32Array(length)
  let offset = 0
  for (const chunk of chunks) {
    combined.set(chunk, offset)
    offset += chunk.length
  }
  return combined
}

export const NEMOTRON_SAMPLE_RATE = TARGET_SAMPLE_RATE

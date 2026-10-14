// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The replay cursor: how many of the run's events are shown. By default it
 * follows the latest event, so a live run keeps growing; play, pause and seek
 * step through the events one at a time, at the chosen speed.
 */

import { useEffect, useState } from 'react'

/** Milliseconds per event at 1× */
const STEP_MS = 900

export type ReplaySpeed = 0.5 | 1 | 1.5 | 2

export interface Replay {
  /** Events shown: 0..total */
  step: number
  total: number
  playing: boolean
  speed: ReplaySpeed
  play: () => void
  pause: () => void
  seek: (step: number) => void
  /** Plays the whole run again from before its first event */
  restart: () => void
  setSpeed: (speed: ReplaySpeed) => void
}

export const useReplay = (total: number): Replay => {
  // null follows the latest event
  const [pinned, setPinned] = useState<number | null>(null)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<ReplaySpeed>(1)
  const step = Math.min(pinned ?? total, total)

  useEffect(() => {
    if (!playing) return
    if (step >= total) {
      setPlaying(false)
      setPinned(null)
      return
    }
    const timer = setTimeout(() => setPinned(step + 1), STEP_MS / speed)
    return () => clearTimeout(timer)
  }, [playing, speed, step, total])

  return {
    step,
    total,
    playing,
    speed,
    play: () => {
      if (step >= total) setPinned(0)
      setPlaying(true)
    },
    pause: () => setPlaying(false),
    seek: (next) => {
      setPlaying(false)
      setPinned(next >= total ? null : Math.max(0, next))
    },
    restart: () => {
      setPinned(0)
      setPlaying(true)
    },
    setSpeed,
  }
}

// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client'

import type { ChangeEvent, ReactNode } from 'react'
import { SelectShell } from '@/shared/components/SelectShell'
import styles from '../execution-workspace.module.css'
import type { Replay, ReplaySpeed } from './use-replay'

interface ReplayControlsProps {
  replay: Replay
  /** What the event at the cursor was */
  currentLabel?: string
}

export const ReplayControls = ({ replay, currentLabel }: ReplayControlsProps): ReactNode => {
  const { step, total, playing, speed } = replay
  const clampStep = (next: number): number => Math.max(0, Math.min(total, next))
  const handleRangeChange = (event: ChangeEvent<HTMLInputElement>): void => {
    replay.seek(clampStep(Number(event.target.value)))
  }

  return (
    <div className={styles.replayControls} aria-label="Execution replay controls">
      <div className={styles.replaySummary} aria-live="polite">
        <span className={styles.stepCount}>
          Step {step} of {total}
        </span>
        {currentLabel && <span className={styles.currentEventLabel}>{currentLabel}</span>}
      </div>

      <input
        className={styles.replayRange}
        type="range"
        min={0}
        max={Math.max(0, total)}
        step={1}
        value={clampStep(step)}
        disabled={total === 0}
        onChange={handleRangeChange}
        aria-label="Replay position"
      />

      <div className={styles.replayActions}>
        <button
          type="button"
          className={styles.iconButton}
          disabled={step === 0}
          onClick={() => replay.seek(clampStep(step - 1))}
          aria-label="Previous execution step"
        >
          <span aria-hidden="true">‹</span>
        </button>
        <button
          type="button"
          className={styles.iconButton}
          disabled={total === 0}
          onClick={playing ? replay.pause : replay.play}
          aria-label={playing ? 'Pause execution replay' : 'Play execution replay'}
        >
          <span aria-hidden="true">{playing ? 'Ⅱ' : '▶'}</span>
        </button>
        <button
          type="button"
          className={styles.iconButton}
          disabled={step >= total}
          onClick={() => replay.seek(clampStep(step + 1))}
          aria-label="Next execution step"
        >
          <span aria-hidden="true">›</span>
        </button>

        <label className={styles.speedControl}>
          <span className={styles.visuallyHidden}>Replay speed</span>
          <SelectShell>
            <select
              value={speed}
              disabled={total === 0}
              onChange={(event) => replay.setSpeed(Number(event.target.value) as ReplaySpeed)}
              aria-label="Replay speed"
            >
              <option value={0.5}>0.5×</option>
              <option value={1}>1×</option>
              <option value={1.5}>1.5×</option>
              <option value={2}>2×</option>
            </select>
          </SelectShell>
        </label>

        <button
          type="button"
          className={styles.replayButton}
          disabled={total === 0}
          onClick={replay.restart}
        >
          Replay Trace
        </button>
      </div>
    </div>
  )
}

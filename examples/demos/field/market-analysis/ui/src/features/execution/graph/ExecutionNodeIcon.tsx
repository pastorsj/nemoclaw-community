// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { FC } from 'react'

type ExecutionNodeIconProps = {
  kind:
    | 'question'
    | 'router'
    | 'model'
    | 'agent'
    | 'tools'
    | 'prediction'
    | 'ontology'
    | 'database'
    | 'table'
    | 'retrieval'
    | 'analytics'
    | 'synthesis'
    | 'report'
    | 'answer'
}

const Dot = ({ cx, cy, r = 2.2 }: { cx: number; cy: number; r?: number }) => (
  <circle cx={cx} cy={cy} r={r} fill="currentColor" />
)

export const ExecutionNodeIcon: FC<ExecutionNodeIconProps> = ({ kind }) => {
  if (kind === 'database') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <ellipse cx="12" cy="5" rx="7.5" ry="3" />
        <path d="M4.5 5v6c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3V5M4.5 11v6c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3v-6" />
      </svg>
    )
  }

  if (kind === 'model' || kind === 'prediction') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M6 7.5 12 4l6 3.5v7L12 20l-6-5.5zM6 7.5l6 3.5 6-3.5M12 11v9" />
        <Dot cx={6} cy={7.5} />
        <Dot cx={18} cy={7.5} />
        <Dot cx={12} cy={4} />
        <Dot cx={12} cy={11} />
        <Dot cx={12} cy={20} />
      </svg>
    )
  }

  if (kind === 'ontology') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="m6 6 6 5 6-5M6 18l6-7 6 7M6 6v12M18 6v12" />
        <Dot cx={6} cy={6} r={2.5} />
        <Dot cx={18} cy={6} r={2.5} />
        <Dot cx={12} cy={11} r={2.5} />
        <Dot cx={6} cy={18} r={2.5} />
        <Dot cx={18} cy={18} r={2.5} />
      </svg>
    )
  }

  if (kind === 'router') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M5 5v4c0 2 1 3 3 3h8M12 7l4 5-4 5M5 19v-3c0-2 1-4 4-4" />
        <Dot cx={5} cy={5} />
        <Dot cx={5} cy={19} />
      </svg>
    )
  }

  if (kind === 'table') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <rect x="4" y="5" width="16" height="14" rx="2" />
        <path d="M4 10h16M10 5v14" />
      </svg>
    )
  }

  if (kind === 'retrieval') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="10.5" cy="10.5" r="6" />
        <path d="m15 15 5 5M8 9h5M8 12h3" />
      </svg>
    )
  }

  if (kind === 'analytics') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 19h16M6 16v-4M11 16V8M16 16V5M5 9l5-3 5 1 4-4" />
        <Dot cx={5} cy={9} />
        <Dot cx={10} cy={6} />
        <Dot cx={15} cy={7} />
        <Dot cx={19} cy={3} />
      </svg>
    )
  }

  if (kind === 'tools') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M14 6a5 5 0 0 0-6.7 6.7L3.5 16.5a2.1 2.1 0 0 0 3 3l3.8-3.8A5 5 0 0 0 17 9l-3 3-2-2z" />
      </svg>
    )
  }

  if (kind === 'report') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6" />
      </svg>
    )
  }

  if (kind === 'question') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M9.7 9a2.5 2.5 0 1 1 3.4 2.35c-.72.32-1.1.8-1.1 1.65v.25M12 17h.01" />
      </svg>
    )
  }

  if (kind === 'answer') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="m12 3 1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7zM18 16l.8 2.2L21 19l-2.2.8L18 22l-.8-2.2L15 19l2.2-.8z" />
      </svg>
    )
  }

  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="8" />
      <path d="M8 13h8M9 9h6M10 17h4" />
    </svg>
  )
}

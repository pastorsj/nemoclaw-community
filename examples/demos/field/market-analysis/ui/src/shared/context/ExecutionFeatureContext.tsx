// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Execution Feature Slot
 *
 * The execution view (graph, capability explorers, timeline, replay) lives in
 * `features/execution` and plugs into the base UI through this one typed
 * interface. The base UI works without it: `noExecutionFeature` renders
 * nothing and ignores job events.
 *
 * The implementation is wired once, in `app/providers.tsx`.
 */

'use client'

import { createContext, useContext, type ComponentType, type ReactNode } from 'react'
import type { ToolPillUse } from '@/shared/components/ToolPills'

/** One record of a job's SSE stream (`GET /v1/jobs/async/job/{id}/stream`), as the API sent it. */
export interface JobStreamEvent {
  jobId: string
  /** SSE `id` (the API's event cursor); null when the record has none. */
  cursor: string | null
  /** SSE event name, e.g. `execution.v2`, `job.status` or `artifact.update`. */
  type: string
  /** Parsed JSON `data`, or the raw string when it is not JSON. Untrusted input. */
  data: unknown
}

/** Evidence to select when the workspace opens from a cited source. */
export interface ExecutionFocus {
  referenceId?: string
  invocationId?: string
}

export interface ExecutionWorkspaceProps {
  jobId: string
  focus: ExecutionFocus | null
  /** The question that started the job, when the conversation has it */
  question?: string | null
  /** Data sources selected for that question */
  sourceIds?: string[]
  onClose: () => void
}

export interface ActivityPanelProps {
  /** The running job, or the latest job of the current conversation; null when there is none. */
  jobId: string | null
  /** Whether that job is running now */
  streaming: boolean
  /** Whether the panel is open; a closed panel keeps its tabs but renders no content */
  open: boolean
}

/** One question and its recorded answer. */
export interface RecordedTurn {
  question: string
  /** Final answer markdown; null when the recorded run produced no answer. */
  answer: string | null
  jobId: string
  sourceIds: string[]
}

/** A conversation recorded for the active data pack (replay mode). */
export interface RecordedSession {
  id: string
  title: string
  /** ISO 8601 timestamp */
  recordedAt: string
  turns: RecordedTurn[]
}

/** A recorded session as the sessions list shows it, before it is loaded. */
export interface RecordedSessionSummary extends Pick<
  RecordedSession,
  'id' | 'title' | 'recordedAt'
> {
  /** Its questions, one per turn */
  questions: string[]
  /** The tools its runs used, as pills */
  tools: ToolPillUse[]
}

export interface RecordingsSource {
  list: () => Promise<RecordedSessionSummary[]>
  load: (sessionId: string) => Promise<RecordedSession>
}

export interface ExecutionFeature {
  /** Receives every record of a live job's SSE stream, in order. */
  onJobEvent: (event: JobStreamEvent) => void
  /** Full view opened from an answer's "View Execution" action. */
  Workspace: ComponentType<ExecutionWorkspaceProps> | null
  /** Content of the Agent Activity side panel: its tabs and their views. */
  ActivityPanel: ComponentType<ActivityPanelProps> | null
  /** Recorded sessions listed in replay mode. */
  recordings: RecordingsSource | null
}

/** The base UI without an execution view. */
export const noExecutionFeature: ExecutionFeature = {
  onJobEvent: () => {},
  Workspace: null,
  ActivityPanel: null,
  recordings: null,
}

const ExecutionFeatureContext = createContext<ExecutionFeature>(noExecutionFeature)

interface ExecutionFeatureProviderProps {
  feature: ExecutionFeature
  children: ReactNode
}

export const ExecutionFeatureProvider = ({
  feature,
  children,
}: ExecutionFeatureProviderProps): ReactNode => (
  <ExecutionFeatureContext.Provider value={feature}>{children}</ExecutionFeatureContext.Provider>
)

export const useExecutionFeature = (): ExecutionFeature => useContext(ExecutionFeatureContext)

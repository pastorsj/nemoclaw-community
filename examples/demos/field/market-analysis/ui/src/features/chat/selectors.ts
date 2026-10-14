// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { ChatStore } from './types'

/**
 * The job the Agent Activity panel follows: the running job, else the latest job of the
 * current conversation. A reopened session, recorded or saved, has no running job, so its
 * activity is its last answer's.
 */
export const selectActivityJobId = (state: Pick<ChatStore, 'deepResearchJobId' | 'currentConversation'>): string | null => {
  if (state.deepResearchJobId) return state.deepResearchJobId
  const messages = state.currentConversation?.messages ?? []
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const jobId = messages[index]?.deepResearchJobId
    if (jobId) return jobId
  }
  return null
}

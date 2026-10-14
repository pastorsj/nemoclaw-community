// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Error Registry
 *
 * Centralized metadata for all error types used in the chat UI.
 * This registry makes it easy to maintain consistent error messages,
 * icons, and retry behavior across the application.
 */

import type { ErrorCode } from '../types'

/** Metadata for each error type */
export interface ErrorMeta {
  /** KUI Banner status */
  status: 'error' | 'warning' | 'info'
  /** Human-readable title */
  title: string
  /** Default message if none provided */
  defaultMessage: string
}

/**
 * Registry of all error types with their metadata.
 * Add new errors here to maintain consistency across the UI.
 */
const ERROR_REGISTRY: Record<ErrorCode, ErrorMeta> = {
  // ============================================================
  // Agent Errors
  // ============================================================
  'agent.response_failed': {
    status: 'error',
    title: 'Response Failed',
    defaultMessage: 'The assistant encountered an error generating a response.',
  },
  'agent.response_interrupted': {
    status: 'warning',
    title: 'Response Interrupted',
    defaultMessage: 'Your previous request was not completed. Please resend your message.',
  },
  'agent.deep_research_failed': {
    status: 'error',
    title: 'Deep Research Failed',
    defaultMessage: 'The deep research process encountered an error.',
  },
  // ============================================================
  // System Errors
  // ============================================================
  'system.unknown': {
    status: 'error',
    title: 'Something Went Wrong',
    defaultMessage: 'An unexpected error occurred. Please try again.',
  },
}

/**
 * Get error metadata by code.
 * Falls back to system.unknown if code not found.
 */
export const getErrorMeta = (code: ErrorCode): ErrorMeta => {
  return ERROR_REGISTRY[code] || ERROR_REGISTRY['system.unknown']
}

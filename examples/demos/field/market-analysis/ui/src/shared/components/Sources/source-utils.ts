// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { SourceKind } from './types'

/**
 * Infer the {@link SourceKind} from a source reference (a URL or a file ref).
 * Web URLs map to `web`; everything else (files, tool refs, bare text) to `doc`.
 */
export function inferSourceKind(ref: string | undefined): SourceKind {
  if (!ref) return 'doc'
  if (/^https?:\/\//i.test(ref)) return 'web'
  return 'doc'
}

/**
 * Reduce a URL to a clean, scannable domain (`www.` and protocol stripped),
 * falling back to the raw ref when it is not a parseable URL.
 */
export function prettyDomain(ref: string | undefined): string {
  if (!ref) return ''
  try {
    return new URL(ref).hostname.replace(/^www\./, '')
  } catch {
    return ref.replace(/^[a-z]+:\/\//i, '').split('/')[0]
  }
}

/**
 * Build the footer label shown on a source card: the domain for web sources,
 * a friendly document label otherwise.
 */
export function sourceLabel(ref: string | undefined, kind: SourceKind): string {
  if (kind === 'web') return prettyDomain(ref)
  return prettyDomain(ref) || 'Document'
}

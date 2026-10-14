// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { SourceKind } from '@/shared/components/Sources/types'

/**
 * Maps a data source to a typed icon kind so each connection shows a glyph that
 * matches what it does (a globe for web/search sources) instead of a single
 * generic icon for every source.
 */
export function getDataSourceKind(id: string): SourceKind {
  const name = id.toLowerCase()
  if (name.includes('web') || name.includes('search') || name.includes('glean')) {
    return 'web'
  }
  return 'doc'
}

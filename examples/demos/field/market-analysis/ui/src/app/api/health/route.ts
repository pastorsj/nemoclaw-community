// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Health Route
 *
 * Liveness of the UI server itself (used by the container healthcheck). It
 * does not call the API, so replay mode is healthy without one.
 */

import { readUiMode } from '@/shared/config/env'

export const dynamic = 'force-dynamic'

export function GET(): Response {
  return Response.json({ status: 'ok', mode: readUiMode() })
}

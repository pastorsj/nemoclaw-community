// SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * API Adapters (browser)
 *
 * Features import API functionality from '@/adapters/api' only. The
 * server-only pack client is imported directly from './pack-client'.
 */

export {
  fetchDataSources,
  fetchRecordedDataSources,
  type DataSourceFromAPI,
} from './data-sources-client'

export {
  ApiRequestError,
  cancelJob,
  createDeepResearchClient,
  getJobReport,
  getJobStatus,
  submitJob,
  type DeepResearchJobStatus,
} from './deep-research-client'

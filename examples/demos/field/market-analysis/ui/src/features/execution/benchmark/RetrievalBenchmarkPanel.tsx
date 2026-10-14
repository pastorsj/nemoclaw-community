// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The Milvus comparison, as the original demo UI drew it: the pack's held-out
 * queries searched on the CPU index and on an NVIDIA GPU (cuVS) index of the
 * same vectors, one workload profile at a time. Only the vector search is
 * timed; embedding and reranking happen once and are left out.
 */

'use client'

import { type FC, useMemo, useState } from 'react'
import type { RetrievalBackend, RetrievalBenchmark, RetrievalProfile } from '../contract'
import { formatBenchmarkDuration } from './benchmark-model'
import styles from './benchmark-tab.module.css'

const profileLabel = (profile: RetrievalProfile): string => {
  if (profile.executionMode === 'concurrency') return `Concurrent ×${profile.concurrency}`
  if (profile.executionMode === 'batch') return `Batch ×${profile.batchSize}`
  return 'Single query'
}

const claimLabel = (profile: RetrievalProfile): string => {
  if (!profile.quality.passed) return 'Quality gate did not pass'
  if (profile.claim.decision === 'gpu_speedup' && profile.claim.gpuSpeedupFactor) {
    return `${profile.claim.gpuSpeedupFactor.toFixed(1)}× faster vector search`
  }
  if (profile.claim.decision === 'cpu_faster_or_equal') {
    return (profile.claim.observedCpuOverGpuRatio ?? 0) > 1
      ? 'No material GPU advantage'
      : 'CPU faster or equal for this workload'
  }
  return 'No acceleration claim'
}

const BackendLane: FC<{ backend: RetrievalBackend; maximum: number }> = ({ backend, maximum }) => {
  const measured = backend.status === 'completed' && backend.vectorSearchMs > 0
  const width = measured ? Math.max(2, (backend.vectorSearchMs / maximum) * 100) : 0
  const engine = backend.role === 'gpu' ? 'NVIDIA GPU' : 'CPU'
  const progressProps = measured
    ? {
        'aria-valuemin': 0,
        'aria-valuemax': Math.round(maximum),
        'aria-valuenow': Math.round(backend.vectorSearchMs),
      }
    : {}
  return (
    <div
      className={styles.retrievalLane}
      data-engine={backend.role}
      data-status={backend.status}
      data-testid={`retrieval-benchmark-${backend.role}`}
    >
      <div className={styles.retrievalLaneHeading}>
        <div>
          <span className={styles.engineDot} />
          <strong>{engine}</strong>
        </div>
        <code>
          {backend.indexType}
          {backend.role === 'gpu' ? ' · NVIDIA cuVS' : ''}
        </code>
      </div>
      <div
        className={styles.activityProgress}
        role="progressbar"
        aria-label={`${engine} Milvus vector search`}
        aria-valuetext={
          measured
            ? `${formatBenchmarkDuration(backend.vectorSearchMs)} aggregate vector search`
            : backend.status
        }
        {...progressProps}
      >
        <span style={{ width: `${width}%` }} />
      </div>
      <div className={styles.retrievalMetrics}>
        <strong>
          {measured ? formatBenchmarkDuration(backend.vectorSearchMs) : 'Unavailable'}
        </strong>
        <span>total vector search</span>
        {measured ? (
          <>
            <span>p50 {formatBenchmarkDuration(backend.p50Ms)}</span>
            <span>p95 {formatBenchmarkDuration(backend.p95Ms)}</span>
            <span>{Math.round(backend.vectorQps).toLocaleString()} vectors/s</span>
          </>
        ) : null}
      </div>
    </div>
  )
}

export const RetrievalBenchmarkPanel: FC<{ benchmark: RetrievalBenchmark }> = ({ benchmark }) => {
  const initialProfile =
    benchmark.profiles.find((profile) => profile.executionMode === 'concurrency') ??
    benchmark.profiles[0]
  const [profileId, setProfileId] = useState(initialProfile.profileId)
  const profile =
    benchmark.profiles.find((candidate) => candidate.profileId === profileId) ?? initialProfile
  const maximum = useMemo(
    () => Math.max(profile.cpu.vectorSearchMs, profile.gpu.vectorSearchMs, 1),
    [profile]
  )
  const recall = Math.min(profile.quality.cpuRecallAtK ?? 0, profile.quality.gpuRecallAtK ?? 0)

  return (
    <section
      className={styles.retrievalPanel}
      aria-label="Milvus CPU and NVIDIA GPU vector-search comparison"
      data-testid="retrieval-benchmark-panel"
    >
      <div className={styles.retrievalHeader}>
        <div>
          <small>Release-qualified index workload</small>
          <strong>Milvus Vector Search</strong>
          <span>{claimLabel(profile)}</span>
        </div>
        <div
          className={styles.profileSelector}
          role="group"
          aria-label="Retrieval benchmark profile"
        >
          {benchmark.profiles.map((candidate) => (
            <button
              type="button"
              key={candidate.profileId}
              aria-pressed={candidate.profileId === profile.profileId}
              onClick={() => setProfileId(candidate.profileId)}
            >
              {profileLabel(candidate)}
            </button>
          ))}
        </div>
      </div>

      <div className={styles.sharedStage}>
        <strong>NVIDIA Nemotron Embed</strong>
        <span>Shared once · excluded from timing</span>
      </div>
      <div className={styles.retrievalComparison}>
        <BackendLane backend={profile.gpu} maximum={maximum} />
        <BackendLane backend={profile.cpu} maximum={maximum} />
      </div>
      <div className={styles.sharedStage}>
        <strong>NVIDIA Nemotron Rerank + Evidence</strong>
        <span>Shared once · excluded from timing</span>
      </div>

      <div
        className={styles.retrievalQuality}
        data-quality={profile.quality.passed ? 'passed' : 'failed'}
      >
        <span>{profile.quality.passed ? 'Quality passed' : 'No timing claim'}</span>
        <span>
          Recall@{profile.topK} {(recall * 100).toFixed(0)}%
        </span>
        <span>CPU/GPU overlap {((profile.quality.cpuGpuOverlapAtK ?? 0) * 100).toFixed(0)}%</span>
        <span>{benchmark.queryCount} held-out queries</span>
      </div>
      <p className={styles.retrievalScopeNote}>
        Measures Milvus vector search only over the same normalized embeddings and source filters.
        The original agent request is not rerun.
      </p>
    </section>
  )
}

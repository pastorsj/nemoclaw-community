// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from 'react'
import Image from 'next/image'
import Link from 'next/link'
import { Logo } from '@/adapters/ui'
import type { PackQuestion } from '@/adapters/api/pack-client'
import styles from './ecosystem-landing.module.css'

interface EcosystemLandingProps {
  /** Featured questions of the active data pack; each opens the research view */
  featuredQuestions: PackQuestion[]
  /** The data pack's disclaimer, shown under the introduction */
  disclaimer: string | null
}

const NvidiaMark = ({ size = 'medium' }: { size?: 'small' | 'medium' }): ReactNode => (
  <span
    className={`${styles.nvidiaMark} ${size === 'small' ? styles.nvidiaMarkSmall : ''}`}
    data-brand="NVIDIA"
    aria-hidden="true"
  >
    <Logo kind="logo-only" size="small" />
  </span>
)

/** Logos of the third-party technologies the demo runs, served from `public/ecosystem-logos`. */
const LOGOS = {
  duckdb: '/ecosystem-logos/duckdb.svg',
  fastapi: '/ecosystem-logos/fastapi.png',
  langchain: '/ecosystem-logos/langchain.svg',
  milvus: '/ecosystem-logos/milvus.svg',
  nextjs: '/ecosystem-logos/nextjs.svg',
  nim: '/ecosystem-logos/nim.png',
  nous: '/ecosystem-logos/nous.png',
  opentelemetry: '/ecosystem-logos/opentelemetry.png',
  phoenix: '/ecosystem-logos/phoenix.png',
  react: '/ecosystem-logos/react.svg',
} as const

const MARK_SIZES = {
  small: styles.brandMarkSmall,
  large: styles.brandMarkLarge,
  wide: styles.brandMarkWide,
}

/** A decorative logo; the technology's name is always written next to it. */
const BrandMark = ({
  brand,
  src,
  size = 'small',
  treatment = 'color',
}: {
  brand: string
  src: string
  size?: keyof typeof MARK_SIZES
  treatment?: 'color' | 'monochrome' | 'light-tile' | 'circle'
}): ReactNode => (
  <span
    className={[
      styles.brandMark,
      MARK_SIZES[size],
      treatment === 'monochrome' ? styles.brandMarkMonochrome : '',
      treatment === 'light-tile' ? styles.brandMarkLightTile : '',
      treatment === 'circle' ? styles.brandMarkCircle : '',
    ]
      .filter(Boolean)
      .join(' ')}
    data-brand={brand}
    aria-hidden="true"
  >
    <Image className={styles.brandImage} src={src} alt="" width={40} height={40} unoptimized />
  </span>
)

const NimMark = (): ReactNode => (
  <BrandMark brand="NVIDIA NIM" src={LOGOS.nim} treatment="monochrome" />
)

/** A technology chip: its mark, then its name. */
const Technology = ({ children, mark }: { children: ReactNode; mark: ReactNode }): ReactNode => (
  <span className={styles.technology}>
    {mark}
    <strong>{children}</strong>
  </span>
)

export const EcosystemLanding = ({
  featuredQuestions,
  disclaimer,
}: EcosystemLandingProps): ReactNode => (
  <main className={styles.page}>
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.brand}>
          <NvidiaMark />
          <span>NVIDIA</span>
        </div>
        <div className={styles.context}>
          <span className={styles.statusDot} aria-hidden="true" />
          Architecture overview · Market analysis
        </div>
      </header>

      <div className={styles.content}>
        <section className={styles.intro} aria-labelledby="ecosystem-title">
          <div>
            <p className={styles.eyebrow}>One agent · NVIDIA AI models · GPU acceleration</p>
            <h1 id="ecosystem-title">From market question to grounded decision.</h1>
          </div>
          <div>
            <p className={styles.introCopy}>
              A connected intelligence stack for structured data, unstructured public knowledge, and
              accelerated market analysis.
            </p>
            {disclaimer && <p className={styles.disclaimer}>{disclaimer}</p>}
          </div>
        </section>

        <section
          className={styles.architecture}
          aria-label="Market analysis technology architecture"
        >
          <article
            className={`${styles.node} ${styles.terminal} ${styles.question}`}
            data-testid="ecosystem-node"
          >
            <span className={styles.terminalIcon} aria-hidden="true">
              ?
            </span>
            <p className={styles.nodeKicker}>Natural language</p>
            <h2>Market question</h2>
            <p className={styles.nodeNote}>Ask what happened, why, and what may happen next.</p>
          </article>

          <article className={`${styles.node} ${styles.hermes}`} data-testid="ecosystem-node">
            <div className={styles.hermesHeader}>
              <BrandMark
                brand="Nous Research"
                src={LOGOS.nous}
                size="large"
                treatment="light-tile"
              />
              <div>
                <p className={styles.nodeKicker}>Nous Research</p>
                <h2>Hermes Agent</h2>
              </div>
            </div>
            <div className={styles.agentSteps} aria-label="Hermes responsibilities">
              <span>Plan</span>
              <span>Call tools</span>
              <span>Synthesize</span>
            </div>
            <div className={styles.technologyRow} aria-label="Agent runtime">
              <Technology mark={<NvidiaMark size="small" />}>OpenShell</Technology>
              <Technology mark={<NvidiaMark size="small" />}>Switchyard</Technology>
            </div>
            <div className={styles.inference}>
              <NvidiaMark size="small" />
              <span>
                <strong>NVIDIA Inference API</strong>
                <br />
                Model execution
              </span>
            </div>
          </article>

          <div className={styles.capabilities} aria-label="Agent capabilities">
            <article className={`${styles.node} ${styles.capability}`} data-testid="ecosystem-node">
              <div className={styles.capabilityHeader}>
                <span className={styles.capabilityTitle}>Structured Data</span>
                <span className={styles.role}>Retrieve · Predict</span>
              </div>
              <div className={styles.technologyRow}>
                <Technology mark={<NvidiaMark size="small" />}>Auto Ontology</Technology>
                <Technology mark={<NvidiaMark size="small" />}>NVIDIA Kumo</Technology>
                <Technology mark={<BrandMark brand="DuckDB" src={LOGOS.duckdb} />}>
                  DuckDB
                </Technology>
              </div>
            </article>

            <article className={`${styles.node} ${styles.capability}`} data-testid="ecosystem-node">
              <div className={styles.capabilityHeader}>
                <span className={styles.capabilityTitle}>Public Knowledge</span>
                <span className={styles.role}>Search</span>
              </div>
              <div className={styles.technologyGroups}>
                <div className={styles.technologyGroup}>
                  <p className={styles.groupLabel}>Retrieval orchestration</p>
                  <div className={styles.technologyRow}>
                    <Technology mark={<BrandMark brand="LangChain" src={LOGOS.langchain} />}>
                      LangChain
                    </Technology>
                    <Technology mark={<BrandMark brand="Milvus" src={LOGOS.milvus} />}>
                      Milvus
                    </Technology>
                  </div>
                </div>
                <div className={styles.technologyGroup}>
                  <p className={styles.groupLabel}>
                    <NvidiaMark size="small" />
                    NVIDIA AI models
                  </p>
                  <div className={styles.technologyRow}>
                    <Technology mark={<NimMark />}>Nemotron Embed</Technology>
                    <Technology mark={<NimMark />}>Nemotron Rerank</Technology>
                  </div>
                </div>
              </div>
            </article>

            <article className={`${styles.node} ${styles.capability}`} data-testid="ecosystem-node">
              <div className={styles.capabilityHeader}>
                <span className={styles.capabilityTitle}>
                  <NvidiaMark size="small" />
                  Market Analytics (using CUDA-X Libraries)
                </span>
                <span className={styles.role}>Analyze</span>
              </div>
              <div className={styles.technologyRow}>
                <Technology mark={<NvidiaMark size="small" />}>cuDF</Technology>
                <Technology mark={<NvidiaMark size="small" />}>cuGraph</Technology>
                <Technology mark={<NvidiaMark size="small" />}>cuML</Technology>
              </div>
            </article>
          </div>

          <article
            className={`${styles.node} ${styles.terminal} ${styles.answer}`}
            data-testid="ecosystem-node"
          >
            <span className={styles.terminalIcon} aria-hidden="true">
              ✦
            </span>
            <p className={styles.nodeKicker}>Inspectable output</p>
            <h2>Grounded decision</h2>
            <ul className={styles.answerList}>
              <li>Cited answers</li>
              <li>Predictions</li>
              <li>GPU analysis</li>
              <li>Replayable evidence</li>
            </ul>
          </article>

          <div className={styles.gpuFoundation} aria-label="NVIDIA GPU foundation">
            <div className={styles.gpuIdentity}>
              <NvidiaMark />
              <span>
                <strong>NVIDIA GPU foundation</strong>
                <br />
                Accelerated compute across the intelligence stack
              </span>
            </div>
            <div className={styles.gpuScope} aria-label="Accelerated workloads">
              <span>Model inference</span>
              <span>Tabular analytics</span>
              <span>Graph analytics</span>
            </div>
          </div>
        </section>

        {featuredQuestions.length > 0 && (
          <section className={styles.featured} aria-labelledby="featured-title">
            <h2 id="featured-title" className={styles.featuredTitle}>
              Featured questions
            </h2>
            <ul className={styles.featuredList}>
              {featuredQuestions.map((question) => (
                <li key={question.id}>
                  <Link
                    className={styles.featuredQuestion}
                    href={`/research?${new URLSearchParams({ question: question.id })}`}
                    title={question.question}
                  >
                    <strong>{question.label}</strong>
                    <span className={styles.featuredText}>{question.question}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        )}

        <footer className={styles.footer}>
          <section className={styles.observability} aria-label="Observability flow">
            <strong>Observable by design</strong>
            <span className={styles.observabilityItem}>
              <NvidiaMark size="small" />
              NeMo Relay
            </span>
            <span className={styles.arrow} aria-hidden="true">
              →
            </span>
            <span className={styles.observabilityItem}>
              <BrandMark brand="OpenTelemetry" src={LOGOS.opentelemetry} />
              OpenTelemetry
            </span>
            <span className={styles.arrow} aria-hidden="true">
              →
            </span>
            <span className={styles.observabilityItem}>
              <BrandMark brand="Phoenix" src={LOGOS.phoenix} treatment="light-tile" />
              Phoenix
            </span>
          </section>
          <div className={styles.application} aria-label="Application technology">
            <strong>Application</strong>
            <span>
              <BrandMark brand="React" src={LOGOS.react} />
              React
            </span>
            <span>
              <BrandMark brand="Next.js" src={LOGOS.nextjs} treatment="monochrome" />
              Next.js
            </span>
            <span>
              <BrandMark brand="FastAPI" src={LOGOS.fastapi} treatment="circle" />
              FastAPI
            </span>
          </div>
          <Link className={styles.enterLink} href="/research">
            Enter market analysis <span aria-hidden="true">→</span>
          </Link>
        </footer>
      </div>
    </div>
  </main>
)

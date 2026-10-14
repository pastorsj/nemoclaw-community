// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/** A receipt's bounded output: a table of rows, or a list of retrieved passages. */

import type { ReactNode } from 'react'
import type { ReceiptOutput } from '../receipt-summary'
import styles from './result-output.module.css'

export const ResultOutput = ({
  output,
  testId = 'result-output',
}: {
  output: ReceiptOutput
  testId?: string
}): ReactNode => {
  if (output.kind === 'passages') {
    return (
      <div className={styles.passageList} data-testid={testId}>
        {output.passages.length ? (
          output.passages.map((passage, index) => (
            <article key={`${passage.source}-${index}`}>
              <div>
                <strong>{passage.source}</strong>
                {passage.metadata.length ? <span>{passage.metadata.join(' · ')}</span> : null}
              </div>
              <p>{passage.excerpt}</p>
            </article>
          ))
        ) : (
          <p>No passages returned.</p>
        )}
      </div>
    )
  }

  return (
    <div className={styles.resultTableScroll} data-testid={testId}>
      {output.rows.length && output.columns.length ? (
        <table>
          <thead>
            <tr>
              {output.columns.map((column) => (
                <th key={column} scope="col">
                  {column.replaceAll('_', ' ')}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {output.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {output.columns.map((column, columnIndex) => (
                  <td key={`${column}-${columnIndex}`}>{row[columnIndex] ?? '—'}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p>No rows returned.</p>
      )}
    </div>
  )
}

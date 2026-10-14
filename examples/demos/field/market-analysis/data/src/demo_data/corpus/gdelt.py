# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""GDELT news headlines, read in place from an external dataset -> one document per headline.

Corpus (`format: gdelt-parquet`, `files`: a glob inside the dataset): Parquet files with GDELT's article columns
`id`, `date` (YYYYMMDDHHMMSS, UTC), `source` (the site), `string` (the headline), `url`, `tone` and
`cluster_label`. A precomputed `embedding` column is never read: retrieval embeds every document with its own
model. The headlines are world news, not linked to tickers, and only https links are kept.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from demo_data.corpus.common import Document
from demo_data.corpus.common import normalize_text

SELECT = """
SELECT id, date, source, string AS headline, url, tone, cluster_label
FROM read_parquet(?) WHERE string IS NOT NULL AND trim(string) <> ''
ORDER BY id
"""


def documents(source_id: str, files: list[Path]) -> list[Document]:
    if not files:
        return []
    cursor = duckdb.execute(SELECT, [[str(path) for path in files]])
    columns = [column[0] for column in cursor.description]
    return [document(source_id, dict(zip(columns, row, strict=True))) for row in cursor.fetchall()]


def document(source_id: str, row: dict[str, Any]) -> Document:
    published = datetime.strptime(str(row["date"]), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    url = row["url"]
    return Document(
        document_id=f"gdelt:{row['id']}",
        source_id=source_id,
        title=normalize_text(row["headline"]),
        text=normalize_text(row["headline"]),
        url=url if url and url.startswith("https://") else None,
        published_at=published.isoformat().replace("+00:00", "Z"),
        metadata={
            "citation": f"{row['source']}, {published:%Y-%m-%d %H:%M} UTC (GDELT)",
            "source_domain": row["source"],
            "tone": row["tone"],
            "topic": row["cluster_label"],
        },
    )

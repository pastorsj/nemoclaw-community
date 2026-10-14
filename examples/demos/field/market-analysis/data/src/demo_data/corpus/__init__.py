# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build the corpus part of a pack: corpus/documents.jsonl, one whole document per line.

Each corpus `format` in pack.yaml names a builder that turns a source into Document rows. A pinned corpus has a
`manifest` in the pack: its builder is `documents(source_id, manifest_path, downloads)`. An in-place corpus has
`files`, a glob inside the external dataset its origin names: its builder is `documents(source_id, files)`, and it
reads only files the dataset's manifest lists and `fetch` verified. Chunking and embedding belong to retrieval
ingestion.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from demo_data import external
from demo_data.corpus import ecfr
from demo_data.corpus import edgar
from demo_data.corpus import federal_register
from demo_data.corpus import gdelt
from demo_data.corpus import markdown
from demo_data.corpus.common import CorpusError
from demo_data.corpus.common import Document
from demo_data.corpus.common import Downloads
from demo_data.pack import SCHEMAS
from demo_data.pack import Pack

PINNED: dict[str, Callable[[str, Path, Downloads], list[Document]]] = {
    "ecfr-xml": ecfr.documents,
    "edgar-filings": edgar.documents,
    "federal-register-xml": federal_register.documents,
    "markdown": markdown.documents,
}
IN_PLACE: dict[str, Callable[[str, list[Path]], list[Document]]] = {
    "gdelt-parquet": gdelt.documents,
}


def build(
    pack: Pack, corpora: list[dict[str, Any]], downloads: Downloads, out: Path, *, sources_dir: Path
) -> dict[str, int]:
    """Write out/corpus/documents.jsonl for `corpora`; returns the document count per source."""
    documents: list[Document] = []
    for corpus in corpora:
        if "files" in corpus:
            documents += IN_PLACE[corpus["format"]](corpus["source"], dataset_files(pack, corpus, sources_dir))
        else:
            documents += PINNED[corpus["format"]](corpus["source"], pack.path(corpus["manifest"]), downloads)
    documents.sort(key=lambda document: (document.source_id, document.document_id))
    rows = [asdict(document) for document in documents]
    errors = document_errors(rows)
    if errors:
        raise CorpusError("; ".join(errors[:5]))
    path = out / "corpus" / "documents.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return dict(Counter(document.source_id for document in documents))


def dataset_files(pack: Pack, corpus: dict[str, Any], sources_dir: Path) -> list[Path]:
    """The files of the corpus's external dataset that match its glob, once `fetch` has verified them."""
    dataset = external.datasets(pack.manifest, sources_dir)[corpus["origin"]]
    listed = [entry["path"] for entry in external.require_verified(dataset)]
    files = [dataset.root / path for path in sorted(listed) if fnmatch(path, corpus["files"])]
    if not files:
        raise CorpusError(f"{corpus['source']}: no file in {dataset.id} matches {corpus['files']}")
    return files


def document_errors(rows: list[dict[str, Any]]) -> list[str]:
    """Rows that break schemas/documents.schema.json, plus duplicate document ids."""
    validator = Draft202012Validator(json.loads((SCHEMAS / "documents.schema.json").read_text(encoding="utf-8")))
    errors = [
        f"{row.get('document_id')}: {'/'.join(map(str, error.absolute_path)) or '(root)'}: {error.message}"
        for row in rows
        for error in validator.iter_errors(row)
    ]
    counts = Counter(row["document_id"] for row in rows)
    return errors + [f"{document_id}: duplicate document_id" for document_id, count in counts.items() if count > 1]

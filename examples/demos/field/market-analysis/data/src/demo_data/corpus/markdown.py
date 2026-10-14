# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Markdown files committed in the pack -> one document per file.

Manifest (`format: markdown`):
    {"source_id", "files": ["<file next to the manifest>.md", ...]}

Each file starts with YAML front matter (document_id, published_at, ...) that becomes metadata, never text.
The first `# ` heading is the title. Bullet lines written as `key=value` in backticks, such as
"- `asset_id=ACME`", also become metadata, so a document can be filtered by the entity it describes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from demo_data.corpus.common import CorpusError
from demo_data.corpus.common import Document
from demo_data.corpus.common import Downloads

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
HEADING = re.compile(r"^# (.+)$", re.MULTILINE)
REFERENCE = re.compile(r"^- `([a-z_]+)=([^`]+)`$", re.MULTILINE)


def documents(source_id: str, manifest_path: Path, _downloads: Downloads) -> list[Document]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [parse(manifest_path.parent / name, source_id) for name in manifest["files"]]


def parse(path: Path, source_id: str) -> Document:
    content = path.read_text(encoding="utf-8")
    front = FRONT_MATTER.match(content)
    heading = HEADING.search(content)
    if front is None or heading is None:
        raise CorpusError(f"{path.name} needs YAML front matter and a '# ' title")
    fields: dict[str, Any] = yaml.safe_load(front[1])
    body = content[front.end() :].strip()
    title = heading[1].strip()
    top_level = ("document_id", "published_at")
    metadata = {key: _scalar(value) for key, value in fields.items() if key not in top_level}
    references = {key: value for key, value in REFERENCE.findall(body) if key not in top_level}
    return Document(
        document_id=fields["document_id"],
        source_id=source_id,
        title=title,
        text=body,
        url=None,
        published_at=_scalar(fields.get("published_at")),
        metadata={"citation": title, **metadata, **references},
    )


def _scalar(value: Any) -> Any:
    """YAML parses timestamps into datetimes; documents carry ISO strings."""
    return value.isoformat() if isinstance(value, datetime) else value

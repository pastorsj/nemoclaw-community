# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""What every corpus builder shares: the document row, text normalization, and pinned downloads."""

from __future__ import annotations

import gzip
import hashlib
import http.client
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Whitespace, plus the other control characters that PDF extraction leaves in some filings (such as \x03).
SEPARATORS = re.compile(r"[\s\x00-\x1f\x7f]+")


class CorpusError(Exception):
    """A corpus could not be fetched or parsed."""


@dataclass(frozen=True)
class Document:
    """One line of corpus/documents.jsonl (schemas/documents.schema.json)."""

    document_id: str
    source_id: str
    title: str
    text: str
    url: str | None
    published_at: str | None
    metadata: dict[str, Any]


def normalize_text(value: str) -> str:
    """Collapse whitespace and control characters (including non-breaking spaces) without rewording anything."""
    return SEPARATORS.sub(" ", value.replace(" ", " ")).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class Downloads:
    """A content-addressed cache of pinned public files: <root>/sha256/<digest>.

    A file is fetched once, checked against its pinned SHA-256 and then reused by every later build.
    """

    def __init__(self, root: Path, *, attempts: int = 4, timeout: float = 60.0) -> None:
        self.root = root / "sha256"
        self.attempts = attempts
        self.timeout = timeout

    def path(self, sha256: str) -> Path:
        return self.root / sha256

    def fetch(self, url: str, sha256: str, *, user_agent: str, max_bytes: int, pause: float = 0.0) -> Path:
        """The cached file for `sha256`, downloading it from `url` first if needed.

        `pause` is slept after a network fetch, to stay under a source's fair-access rate.
        """
        target = self.path(sha256)
        if target.is_file():
            return target
        if not url.startswith("https://"):
            raise CorpusError(f"refusing a non-HTTPS download: {url}")
        self.root.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"})
        for attempt in range(1, self.attempts + 1):
            try:
                self._download(request, target, sha256, max_bytes)
                break
            except (ConnectionError, TimeoutError, http.client.IncompleteRead, urllib.error.URLError) as error:
                if attempt == self.attempts:
                    raise CorpusError(f"could not download {url}: {error}") from error
                time.sleep(0.5 * 2**attempt)
        time.sleep(pause)
        return target

    def _download(self, request: urllib.request.Request, target: Path, sha256: str, max_bytes: int) -> None:
        digest, size = hashlib.sha256(), 0
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as temporary:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    gzipped = response.headers.get("Content-Encoding") == "gzip"
                    stream = gzip.GzipFile(fileobj=response) if gzipped else response
                    while block := stream.read(1 << 20):
                        size += len(block)
                        if size > max_bytes:
                            raise CorpusError(f"{request.full_url} is larger than {max_bytes:,} bytes")
                        digest.update(block)
                        temporary.write(block)
                if digest.hexdigest() != sha256:
                    raise CorpusError(f"{request.full_url} has sha256 {digest.hexdigest()}, pinned {sha256}")
                temporary.close()
                os.chmod(temporary.name, 0o644)
                os.replace(temporary.name, target)
            finally:
                Path(temporary.name).unlink(missing_ok=True)

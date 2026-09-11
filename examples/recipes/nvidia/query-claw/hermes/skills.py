#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Hash Hermes skills and maintain Query Claw's ownership receipt."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
HERMES_SKILL_ROOT = Path("/sandbox/.hermes/skills")
SKILL_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
MANAGED_SKILLS = {
    "query-claw",
    "query-claw-predictive",
    "query-claw-structured",
    "retriever-mcp",
}


def _skill_name(value: str) -> str:
    if (
        not SKILL_NAME.fullmatch(value)
        or value in {".", ".."}
        or value not in MANAGED_SKILLS
    ):
        raise ValueError(f"invalid skill name: {value!r}")
    return value


def tree_hash(root: Path) -> str:
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
        raise ValueError(f"skill root is not a regular directory: {root}")

    files: list[Path] = []
    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, names, filenames in os.walk(
        root, followlinks=False, onerror=raise_walk_error
    ):
        current = Path(directory)
        names[:] = sorted(names)
        for name in names:
            if (current / name).is_symlink():
                raise ValueError(f"skill contains a symbolic link: {current / name}")
        for name in sorted(filenames):
            candidate = current / name
            mode = candidate.lstat().st_mode
            if candidate.is_symlink() or not stat.S_ISREG(mode):
                raise ValueError(f"skill contains an unsupported path: {candidate}")
            files.append(candidate)

    if not files or not (root / "SKILL.md").is_file():
        raise ValueError(f"skill is missing SKILL.md: {root}")

    digest = hashlib.sha256()
    for path in sorted(files, key=lambda value: value.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _load_receipt(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise ValueError("skill ownership receipt is not a regular file")
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError("skill ownership receipt is not a regular file")
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("skill ownership receipt has an unsupported schema")
    skills = payload.get("skills")
    if not isinstance(skills, dict):
        raise ValueError("skill ownership receipt is missing its skills map")
    result: dict[str, str] = {}
    for raw_name, raw_digest in skills.items():
        if not isinstance(raw_name, str) or not isinstance(raw_digest, str):
            raise ValueError("skill ownership receipt entries must be strings")
        name = _skill_name(raw_name)
        if not SHA256.fullmatch(raw_digest):
            raise ValueError(f"skill ownership receipt has an invalid digest for {name}")
        result[name] = raw_digest
    return result


def _write_receipt(path: Path, skills: dict[str, str]) -> None:
    if not skills:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(
                {"schema_version": SCHEMA_VERSION, "skills": dict(sorted(skills.items()))},
                stream,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
        temporary.replace(path)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def main(arguments: list[str]) -> int:
    command, *values = arguments
    if command == "hash" and len(values) == 1:
        print(tree_hash(Path(values[0])))
    elif command == "state" and len(values) == 1:
        target = HERMES_SKILL_ROOT / _skill_name(values[0])
        if not target.exists() and not target.is_symlink():
            print("absent")
        else:
            print(tree_hash(target))
    elif command == "receipt-validate" and len(values) == 1:
        _load_receipt(Path(values[0]))
    elif command == "receipt-get" and len(values) == 2:
        print(_load_receipt(Path(values[0])).get(_skill_name(values[1]), "unowned"))
    elif command == "receipt-set" and len(values) == 3:
        path, name, digest = Path(values[0]), _skill_name(values[1]), values[2]
        if not SHA256.fullmatch(digest):
            raise ValueError("invalid skill digest")
        skills = _load_receipt(path)
        skills[name] = digest
        _write_receipt(path, skills)
    elif command == "receipt-delete" and len(values) == 2:
        path, name = Path(values[0]), _skill_name(values[1])
        skills = _load_receipt(path)
        skills.pop(name, None)
        _write_receipt(path, skills)
    else:
        raise ValueError("invalid Hermes skill helper command")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

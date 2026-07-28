"""Frontmatter-typed markdown IO for canonical vault files.

Documents are ``---\n<yaml>\n---\n\n<body>``. ``parse`` raises ``ValueError``
on malformed YAML so reconcile can skip (and the doctor can report) a file a
human broke mid-edit, instead of silently adopting it as new content.
Writes are atomic (tmp + ``os.replace``) so Obsidian never reads a torn file.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml


def serialize(frontmatter: dict, body: str) -> str:
    fm = {k: v for k, v in frontmatter.items() if v is not None}
    body = body.strip("\n")
    if not fm:
        return body + "\n"
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{dumped}---\n\n{body}\n"


def parse(text: str) -> tuple[dict, str]:
    """Split a document into (frontmatter, body). No frontmatter → ({}, text)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    try:
        fm = yaml.safe_load(text[4 : end + 1])
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed frontmatter: {exc}") from exc
    if not isinstance(fm, dict):
        return {}, text
    body = text[end + 4 :].lstrip("\n")
    if body and not body.endswith("\n"):
        body += "\n"
    return fm, body


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str) -> str:
    """Write via tmp + rename; return the content hash of what was written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return content_hash(text)


def title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:120]
    return fallback[:120]

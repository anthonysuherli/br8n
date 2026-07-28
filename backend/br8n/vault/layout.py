"""Resolve the vault root and canonical file locations.

The vault is one global tree of markdown files:

    snapshots/<project>/<branch>/   category == "snapshot"
    notes/<project>/<branch>/       category == "note"
    journal/<year>/                 category == "journal"
    findings/<project>/<branch>/    every other category (explore output)
    views/                          regenerated projections — never reconciled

Root resolution mirrors ``journal_dir``: ``BR8N_VAULT_PATH`` wins; else the
vault sits beside the SQLite db (parent of ``BR8N_DB_PATH``); else ``~/.br8n``.
Tests that point ``BR8N_DB_PATH`` at a tmp dir therefore get a tmp vault free.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

CANONICAL_DIRS: tuple[str, ...] = ("snapshots", "notes", "journal", "findings")
VIEWS_DIRNAME = "views"

_CATEGORY_DIRS = {"snapshot": "snapshots", "note": "notes", "journal": "journal"}
_CATEGORY_TYPES = {"snapshot": "snapshot", "note": "note", "journal": "journal"}


def vault_root() -> Path:
    env = os.environ.get("BR8N_VAULT_PATH")
    if env:
        root = Path(env)
    else:
        db = os.environ.get("BR8N_DB_PATH")
        base = Path(db).resolve().parent if db else Path.home() / ".br8n"
        root = base / "vault"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_segment(segment: str) -> str:
    """Filesystem-safe single path segment (branch names contain '/').

    Dots are allowed (for extensions/versioned names) but a segment that is
    ENTIRELY dots — "." or ".." or "..." — collapses to a dot-path traversal
    once written under the vault root, so it is rejected and replaced.
    """
    s = re.sub(r"[^A-Za-z0-9._-]+", "__", segment.strip()) or "default"
    if s.strip(".") == "":
        return "default"
    return s


def slug(text: str, cap: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:cap] or "untitled"


def category_dir(category: str) -> str:
    return _CATEGORY_DIRS.get(category or "", "findings")


def file_type(category: str) -> str:
    """The frontmatter ``type`` for a finding category."""
    return _CATEGORY_TYPES.get(category or "", "finding")


def file_path(
    category: str, project: str, kb: str, created_at: str, title: str, finding_id: str
) -> Path:
    """Deterministic canonical path; the id suffix rules out slug collisions."""
    name = (
        f"{created_at[:10]}-{created_at[11:16].replace(':', '')}"
        f"-{slug(title)}-{finding_id[:8]}.md"
    )
    d = category_dir(category)
    if d == "journal":
        return vault_root() / d / created_at[:4] / name
    return vault_root() / d / safe_segment(project) / safe_segment(kb) / name

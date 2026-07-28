"""Persist a journal entry as a `journal` Finding, plus its canonical markdown.

A journal entry is the cross-project, write-anytime counterpart to a session
note. It (1) stores under the reserved JOURNAL_SCOPE KB and (2) never
schedules a synopsis rebuild — the journal is not tied to any repo+branch. On
the local tier, the canonical markdown is the vault file `VaultStore.insert_findings`
already wrote (under `journal/<year>/`) — no second write. On the cloud tier,
this dual-writes: the Finding plus a markdown file in the GLOBAL
~/.br8n/journal/ dir.

    persist_journal(ctx, text, type?, tags?) ──► finding{category:'journal'} + md
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from br8n.agent.state import TenantContext
from br8n.clients.embeddings import embed_batch, embeddings_configured
from br8n.constants import JOURNAL_SCOPE  # noqa: F401 — re-exported for callers
from br8n.store import active_backend, get_store

logger = logging.getLogger(__name__)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "entry"


def journal_dir() -> Path:
    """Global journal markdown dir: ``<br8n home>/journal``, created on demand.

    Colocated with the SQLite db so the journal travels with the store — the
    parent of ``BR8N_DB_PATH`` when set, else ``~/.br8n``."""
    env = os.environ.get("BR8N_DB_PATH")
    root = Path(env).resolve().parent if env else Path.home() / ".br8n"
    d = root / "journal"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _entry_filename(captured_at: str, title: str) -> str:
    """ISO-8601 captured_at + title → ``2026-06-03-1430-slug.md``."""
    return f"{captured_at[:10]}-{captured_at[11:16].replace(':', '')}-{_slug(title)}.md"


def _markdown(title: str, text: str, type_: str, tags: list[str]) -> str:
    """On-disk entry body: an H1 title, an optional meta line, then the text."""
    meta: list[str] = []
    if type_:
        meta.append(f"type: {type_}")
    if tags:
        meta.append(f"tags: {', '.join(tags)}")
    head = f"# {title}\n"
    if meta:
        head += "\n" + " · ".join(meta) + "\n"
    return f"{head}\n{text.strip()}\n"


async def persist_journal(
    ctx: TenantContext,
    *,
    text: str,
    type: str = "",
    tags: list[str] | None = None,
    title: str = "",
    originating_project: str = "",
    session_id: str = "",
    captured_at: str = "",
) -> dict:
    """Embed + insert the entry as a `journal` Finding, then write the markdown.

    Returns ``{"finding_id", "entry_path"}`` — ``entry_path`` is the vault file
    on the local tier, the legacy global journal-dir file on cloud. `type`
    (e.g. insight/reflection/reference/decision) and `tags` are folded into the
    finding's tags for filtering. `originating_project`, when given, is stamped
    into provenance — where you were when you journaled — but storage is always
    the journal scope.
    """
    captured_at = captured_at or datetime.now(timezone.utc).isoformat()
    tags = list(tags or [])
    title = title.strip()
    if not title:
        first = text.strip().splitlines()[0] if text.strip() else ""
        title = first[:80] or "entry"

    all_tags = ["journal", *([type] if type else []), *tags]
    prov: dict = {"source": "br8n-journal", "session": session_id}
    if originating_project:
        prov["project"] = originating_project

    row = {
        "org_id": ctx.org_id,
        "kb_id": ctx.kb_id,
        "title": title[:120],
        "content": text,
        "category": "journal",
        "confidence": 1.0,
        "tags": all_tags,
        "provenance": [prov],
    }
    # See the matching comment in `br8n/livingdocs/notes.py`: an entry must
    # persist unembedded rather than be lost when no embedder is usable —
    # including during a deferred embedding-space switch, where embedding at
    # the new dim raises a dimension mismatch against the old-width table.
    embedding = None
    if embeddings_configured():
        [embedding] = await embed_batch([text])
    else:
        logger.info("embeddings unavailable; storing journal entry without an embedding")
    row["embedding"] = embedding
    store = get_store(ctx.access_token)
    [finding_id] = await store.insert_findings([row])

    if active_backend() == "local":
        # canonical file already written by VaultStore.insert_findings
        entry_path = store.vault_path_for(finding_id) or ""
    else:
        p = journal_dir() / _entry_filename(captured_at, title)
        p.write_text(_markdown(title, text, type, tags))
        entry_path = str(p)

    return {"finding_id": finding_id, "entry_path": entry_path}

"""Persist a session note as a `note` Finding AND a markdown file.

A note is dual-written: it becomes a `category="note"` Finding (embedded,
searchable, feeds resume/activity) and a markdown file under
`.br8n/notes/<kb>/`. This mirrors snapshot persistence in
`br8n/capture/service.py` — same embed→insert→schedule_rebuild shape.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from br8n.agent.state import TenantContext
from br8n.agent.synopsis import schedule_rebuild
from br8n.clients.embeddings import embed_batch
from br8n.livingdocs.paths import DocPaths, ensure_layout
from br8n.store import get_store


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "note"


def _note_filename(captured_at: str, title: str) -> str:
    """ISO-8601 captured_at + title → `2026-06-03-1430-slug.md`."""
    return f"{captured_at[:10]}-{captured_at[11:16].replace(':', '')}-{_slug(title)}.md"


def _markdown(title: str, content: str) -> str:
    """The on-disk note body: ensure a title H1 leads the file, then content."""
    body = content.strip("\n")
    if not body.lstrip().startswith("# "):
        body = f"# {title}\n\n{body}"
    return body + "\n"


async def persist_note(
    ctx: TenantContext,
    *,
    project_path: str,
    kb: str,
    content: str,
    session_id: str,
    title: str,
    captured_at: str = "",
    source: str = "agent",
    next_action: str | None = None,
) -> dict:
    """Embed + insert the note as a `note` Finding, then write the markdown file.

    Returns ``{"finding_id", "note_path"}``. Fires a synopsis rebuild after the
    write (fire-and-forget in the caller) so the resume card stays current.
    """
    captured_at = captured_at or datetime.now(timezone.utc).isoformat()
    row = {
        "org_id": ctx.org_id,
        "kb_id": ctx.kb_id,
        "title": title[:120],
        "content": content,
        "category": "note",
        "confidence": 1.0 if source == "agent" else 0.6,
        "tags": ["note", source],
        "provenance": [
            {
                "source": f"br8n-livingdocs-{source}",
                "session": session_id,
                "path": project_path,
            }
        ],
        "metadata": {"next_action": next_action} if next_action else None,
    }
    [embedding] = await embed_batch([content])
    row["embedding"] = embedding
    [finding_id] = await get_store(ctx.access_token).insert_findings([row])

    paths = DocPaths(project_path=project_path, kb=kb)
    ensure_layout(paths)
    note_path = paths.notes_dir / _note_filename(captured_at, title)
    note_path.write_text(_markdown(title, content))

    schedule_rebuild(ctx)
    return {"finding_id": finding_id, "note_path": str(note_path)}

"""Persist a WorkspaceSnapshot as a Finding in the KB."""

from __future__ import annotations

from br8n.agent.state import TenantContext
from br8n.agent.synopsis import schedule_rebuild
from br8n.capture.adapter import snapshot_to_finding
from br8n.capture.models import WorkspaceSnapshot
from br8n.clients.embeddings import embed_batch
from br8n.store import get_store


def _invalidate_primer_cache(snap: WorkspaceSnapshot) -> None:
    """Best-effort: clear the cached session primer so the next turn rebuilds with this
    snapshot. Keyed by (basename(project_path), branch) to match the hook's derivation.
    A cache error never breaks a capture."""
    try:
        import os

        from br8n import preamble_cache

        project = os.path.basename(snap.project_path.rstrip("/"))
        preamble_cache.invalidate(project, snap.branch or "")
    except Exception:  # noqa: BLE001 — invalidation is best-effort
        pass


async def persist_snapshot(ctx: TenantContext, snap: WorkspaceSnapshot) -> str:
    """Embed + insert snapshot as a Finding. Returns the new finding id.

    Fires a synopsis rebuild after write (fire-and-forget in the caller)
    so the resume card stays current without blocking the capture response.
    """
    payload = snapshot_to_finding(snap)
    [embedding] = await embed_batch([payload["content"]])
    row = {
        "org_id": ctx.org_id,
        "kb_id": ctx.kb_id,
        "title": payload["title"],
        "content": payload["content"],
        "category": payload["category"],
        "confidence": 1.0,
        "tags": payload["tags"],
        "provenance": payload["provenance"],
        "metadata": payload["metadata"],
        "embedding": embedding,
    }
    [finding_id] = await get_store(ctx.access_token).insert_findings([row])
    schedule_rebuild(ctx)
    _invalidate_primer_cache(snap)
    return finding_id

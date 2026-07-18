"""Shared resume core — resolve the tenant, get the store, select the preamble.

    resolve_tenant(create=…) ─► get_store ─► select_preamble ─► ResumeResult

Factored out of the call sites that tap a KB the same way (``interfaces/mcp/server.py::
br8n_resume``, ``api/resume.py::resume``, and the ``hooks/preamble-inject.py``
UserPromptSubmit hook) so the resolve+select trio can't drift. Returns the resolved
``ctx``/``store`` alongside the preamble so callers needing them for follow-on work
(``record_access``, snapshot counts, JSON assembly) don't re-resolve.

May raise — ``resolve_tenant(create=False)`` raises on an unknown project/kb. Callers
that must stay silent (the hook) wrap the call in try/except.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from br8n.agent.preamble import Coverage, Depth, select_preamble

if TYPE_CHECKING:
    from br8n.agent.state import Principal, TenantContext
    from br8n.store import Store


@dataclass
class ResumeResult:
    ctx: "TenantContext"
    store: "Store"
    preamble: str
    coverage: Coverage


async def resume_preamble(
    project: str,
    kb: str,
    query: str | None,
    *,
    depth: Depth = "normal",
    principal: "Principal | None" = None,
    create: bool = False,
) -> ResumeResult:
    """Resolve the KB and return its query-aware preamble + coverage.

    ``create=False`` by default (a read): an unknown project/kb raises rather than
    being created. ``principal`` threads the per-request cloud identity; omit it for
    the local tier / configured-MCP-user path.
    """
    # Lazy imports: keep this module free of import cycles (tenancy imports agent.state;
    # store is heavy). Mirrors the lazy-import idiom in interfaces/mcp/tenancy.py.
    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.store import get_store

    ctx = resolve_tenant(project, kb, create=create, principal=principal)
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    preamble, coverage = await select_preamble(query, store=store, kb_id=ctx.kb_id, depth=depth)
    return ResumeResult(ctx=ctx, store=store, preamble=preamble, coverage=coverage)


# How many recent findings per category to scan for a structured next_action.
# Small: the carrier is almost always the latest snapshot or session note.
_NEXT_ACTION_SCAN_PER_CATEGORY = 10


def _newest_carrier(rows: list[dict]) -> dict | None:
    """First row (newest-first) whose metadata carries a next_action, else None."""
    for r in rows:
        meta = r.get("metadata") or {}
        if meta.get("next_action"):
            return r
    return None


def latest_next_action(store, kb_id: str) -> tuple[str | None, str | None]:
    """(next_action, thread_id) from the newest snapshot/note carrying one.

    Queries the ``snapshot`` and ``note`` categories separately (rather than
    scanning a mixed recent-findings window) so that other features bulk-
    inserting findings of other categories into the same KB can't push the
    carrier out of range. Best-effort: any store error or absent metadata
    degrades to (None, None) — resume surfaces render exactly as they did
    before this field existed."""
    try:
        snapshot_rows = store.list_findings(
            kb_id, category="snapshot", limit=_NEXT_ACTION_SCAN_PER_CATEGORY
        ).get("findings", [])
        note_rows = store.list_findings(
            kb_id, category="note", limit=_NEXT_ACTION_SCAN_PER_CATEGORY
        ).get("findings", [])

        candidates = [
            c for c in (_newest_carrier(snapshot_rows), _newest_carrier(note_rows)) if c is not None
        ]
        if not candidates:
            return None, None

        best = max(candidates, key=lambda r: r.get("created_at") or "")
        meta = best.get("metadata") or {}
        return str(meta["next_action"]), meta.get("thread_id")
    except Exception:  # noqa: BLE001 — resume must never fail on this
        return None, None

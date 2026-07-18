"""Build the broad session primer injected by the UserPromptSubmit hook.

    resume_preamble(depth="deep") + recent snapshots ─► additionalContext payload

The primer is mostly query-independent orientation (synopsis spine + recent capture
snapshots) seeded once by the session's first prompt's deep bands. Returns None when
the KB has nothing to orient with (empty/unknown KB) so the hook stays silent.

May raise (resolve_tenant on an unknown KB, embed/store errors) — the hook wraps the
call and suppresses on any exception.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from br8n.agent.resume import latest_next_action, resume_preamble

_MAX_SNAPSHOTS = 3


async def build_session_primer(project: str, kb: str, query: str | None) -> str | None:
    """Return the additionalContext payload for (project, kb), or None if empty."""
    res = await resume_preamble(project, kb, query, depth="deep")

    listed = res.store.list_findings(res.ctx.kb_id, category="snapshot", limit=_MAX_SNAPSHOTS)
    snaps = listed.get("findings", [])  # list_findings is typed -> dict; let a violation surface

    # render_preamble emits "<synopsis>" only when the synopsis is non-empty and
    # "<finding " only when bands are admitted — so these substring checks reliably
    # detect orientation. No synopsis, no bands, and no snapshots → nothing to inject.
    has_orientation = (
        "<synopsis>" in res.preamble or "<finding " in res.preamble or bool(snaps)
    )
    if not has_orientation:
        return None

    parts = [res.preamble]
    next_action, _tid = latest_next_action(res.store, res.ctx.kb_id)
    if next_action:
        parts.append(f"<next-action>{escape(next_action.strip()[:200])}</next-action>")
    # Truncate the raw title, THEN escape — slicing escaped text could cut an entity.
    snap_lines = [
        f"  <snapshot>{escape(str(f.get('title', '')).strip()[:120])}</snapshot>"
        for f in snaps
        if str(f.get("title", "")).strip()
    ]
    if snap_lines:
        parts.append("<recent-snapshots>\n" + "\n".join(snap_lines) + "\n</recent-snapshots>")
    return "\n".join(parts)

"""GET /v1/resume/{project}/{kb} — tap the KB and return a resume card."""

from __future__ import annotations

import re
from html import escape
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from br8n.agent.resume import latest_next_action, resume_preamble
from br8n.agent.state import Principal
from br8n.api.auth import require_principal
from br8n.knowledge_graph.activity import activity_rollup

router = APIRouter(prefix="/v1", dependencies=[Depends(require_principal)])


class ResumeResponse(BaseModel):
    coverage: str
    preamble: str
    card_html: str
    project: str
    kb: str
    snapshot_count: int
    activity: list[dict] = []


class ResumeSnapshot(BaseModel):
    id: str
    title: str
    captured_at: str


class SynopsisEntry(BaseModel):
    topic: str
    gloss: str


class ResumeCardJSON(BaseModel):
    """Structured resume card for native clients (the iOS app).

    Mirrors the data the HTML card assembles, but as JSON so a SwiftUI view can
    lay it out natively instead of rendering a foreign webview. `hypothesis` and
    `snapshots` come straight from the latest captured snapshots (not from
    query-banded preamble findings), so they're populated even when the caller
    passes no `query`.
    """

    coverage: str
    project: str
    kb: str
    snapshot_count: int
    hypothesis: str | None = None
    next_action: str | None = None
    thread_id: str | None = None
    snapshots: list[ResumeSnapshot] = []
    synopsis: list[SynopsisEntry] = []
    activity: list[dict] = []
    preamble: str


# Most-recent snapshots surfaced in the native card's timeline.
_JSON_SNAPSHOT_LIMIT = 5


@router.get("/resume/{project}/{kb}", response_model=None)
async def resume(
    project: str,
    kb: str,
    query: str | None = Query(default=None, description="Current context hint"),
    format: str = Query(default="html", description="'html' (webview) or 'json' (native)"),
    principal: Principal = Depends(require_principal),
) -> ResumeResponse | ResumeCardJSON:
    """Tap the KB and return the 30-second resume card.

    `format=html` (default) returns the VS Code webview card in `card_html`;
    `format=json` returns the same content as structured fields for native
    clients. `preamble` (raw XML, for Claude Code MCP use) is in both.
    """
    res = await resume_preamble(project, kb, query, principal=principal)
    ctx, store, preamble_xml, coverage = res.ctx, res.store, res.preamble, res.coverage

    # Snapshot count for the card header. list_findings caps at its max limit
    # (100), so this is "snapshots, up to that cap" — fine for a header chip.
    snapshot_count = store.list_findings(
        ctx.kb_id, category="snapshot", limit=100
    )["count"]

    # Cross-repo activity rollup (empty before any capture); newest-first.
    rollup = activity_rollup(access_token=principal.access_token, org_id=principal.org_id)

    if format == "json":
        return _assemble_json(
            store,
            ctx.kb_id,
            coverage=coverage,
            preamble_xml=preamble_xml,
            project=project,
            kb=kb,
            snapshot_count=snapshot_count,
            activity=rollup,
        )

    card_html = _render_card(
        preamble_xml,
        coverage=coverage,
        project=project,
        kb=kb,
        snapshot_count=snapshot_count,
        activity=rollup,
    )
    return ResumeResponse(
        coverage=coverage,
        preamble=preamble_xml,
        card_html=card_html,
        project=project,
        kb=kb,
        snapshot_count=snapshot_count,
        activity=rollup,
    )


# ---------------------------------------------------------------------------
# JSON card assembly (native clients)
# ---------------------------------------------------------------------------

def _assemble_json(
    store,
    kb_id: str,
    *,
    coverage: str,
    preamble_xml: str,
    project: str,
    kb: str,
    snapshot_count: int,
    activity: list[dict],
) -> ResumeCardJSON:
    """Build the native card from the store directly.

    Unlike the HTML renderer (which parses query-banded preamble findings), this
    reads the latest snapshots and the synopsis spine straight from the store, so
    the hypothesis and timeline are present regardless of `query`.
    """
    snaps = store.list_findings(kb_id, category="snapshot", limit=_JSON_SNAPSHOT_LIMIT)[
        "findings"
    ]
    snapshots = [
        ResumeSnapshot(id=f["id"], title=f.get("title") or "", captured_at=f.get("created_at") or "")
        for f in snaps
    ]
    meta0 = (snaps[0].get("metadata") or {}) if snaps else {}
    hypothesis = meta0.get("hypothesis") or (
        _hypothesis_from_title(snaps[0].get("title") or "") if snaps else None
    )
    next_action, thread_id = latest_next_action(store, kb_id)

    syn_row = store.load_synopsis(kb_id) or {}
    synopsis = [
        SynopsisEntry(topic=str(e.get("topic", "")), gloss=str(e.get("gloss", "")))
        for e in (syn_row.get("content") or [])
    ]

    return ResumeCardJSON(
        coverage=coverage,
        project=project,
        kb=kb,
        snapshot_count=snapshot_count,
        hypothesis=hypothesis,
        next_action=next_action,
        thread_id=thread_id,
        snapshots=snapshots,
        synopsis=synopsis,
        activity=activity,
        preamble=preamble_xml,
    )


def _hypothesis_from_title(title: str) -> str | None:
    """A snapshot's title is the hypothesis unless it's a generic auto-label."""
    if title and not title.startswith("Snapshot ") and not title.startswith("Working on "):
        return title
    return None


# ---------------------------------------------------------------------------
# Card HTML renderer
# ---------------------------------------------------------------------------

_STYLE = """
<style>
  :root {
    --accent: #9C2C1F; --ink: #1D1D1F; --muted: #6E6E73; --faint: #8A8A8E;
    --bg: #FAFAF8; --surface: #FFFFFF; --hairline: rgba(0,0,0,0.08);
    --mono: ui-monospace, "SF Mono", Menlo, monospace;
    --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 0; background: var(--bg); color: var(--ink);
    font-family: var(--font); font-size: 13px; line-height: 1.55; }

  .header { padding: 16px 18px 12px; border-bottom: 1px solid var(--hairline); }
  .header-top { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .eyebrow { font-size: 10px; font-weight: 600; letter-spacing: .10em;
    text-transform: uppercase; color: var(--muted); }
  .project-label { font-size: 14px; font-weight: 600; color: var(--ink); flex: 1; }
  .badge { font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 9px;
    letter-spacing: .05em; text-transform: uppercase; }
  .badge-rich   { background: #d1f0da; color: #1a6b34; }
  .badge-sparse { background: #fff3cd; color: #856404; }
  .badge-gap    { background: #fde8e4; color: var(--accent); }
  .meta { font-size: 11px; color: var(--faint); }

  /* Hypothesis — the wedge, shown large */
  .hypothesis { padding: 14px 18px; border-bottom: 1px solid var(--hairline);
    background: var(--surface); }
  .hypothesis-label { font-size: 10px; font-weight: 600; letter-spacing: .10em;
    text-transform: uppercase; color: var(--accent); margin-bottom: 5px; }
  .hypothesis-text { font-size: 15px; font-weight: 500; color: var(--ink);
    line-height: 1.4; }

  /* Synopsis */
  .synopsis { padding: 12px 18px; border-bottom: 1px solid var(--hairline); }
  .section-label { font-size: 10px; font-weight: 600; letter-spacing: .10em;
    text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }
  .topic-entry { margin-bottom: 6px; }
  .topic-name { font-size: 11px; font-weight: 600; color: var(--accent); }
  .topic-gloss { font-size: 12px; color: var(--ink); }

  /* Snapshots */
  .snapshots { padding: 12px 18px; }
  .snapshot { background: var(--surface); border: 1px solid var(--hairline);
    border-radius: 6px; padding: 9px 12px; margin-bottom: 7px; }
  .snap-title { font-size: 12px; font-weight: 600; color: var(--ink); margin-bottom: 3px; }
  .snap-meta { font-size: 11px; color: var(--faint); font-family: var(--mono); }
  .snap-detail { font-size: 11px; color: var(--muted); margin-top: 4px;
    white-space: pre-wrap; font-family: var(--mono); max-height: 80px; overflow: hidden; }

  /* Explore CTA */
  .explore-bar { padding: 10px 18px 14px; }
  .explore-btn { display: inline-block; background: var(--accent); color: #fff;
    font-size: 12px; font-weight: 600; padding: 6px 14px; border-radius: 5px;
    cursor: pointer; border: none; letter-spacing: .02em; }
  .explore-btn:hover { opacity: 0.88; }

  .footer { padding: 8px 18px 14px; font-size: 10px; color: var(--faint);
    letter-spacing: .06em; text-transform: uppercase; }
</style>
"""

_EXPLORE_JS = """
<script>
  const vscode = acquireVsCodeApi();
  document.getElementById('explore-btn').addEventListener('click', () => {
    vscode.postMessage({ command: 'explore' });
  });
</script>
"""


def _render_card(
    preamble_xml: str,
    *,
    coverage: str,
    project: str,
    kb: str,
    snapshot_count: int,
    activity: list[dict] | None = None,
) -> str:
    try:
        root = ET.fromstring(preamble_xml)
    except ET.ParseError:
        root = None

    findings = _parse_findings(root)
    hypothesis = _extract_hypothesis(findings)
    synopsis_entries = _parse_synopsis(root)
    snapshot_findings = [f for f in findings if _is_snapshot(f)]
    last_captured = _extract_timestamp(snapshot_findings[0]["content"] if snapshot_findings else "")

    parts: list[str] = [f"<html><head>{_STYLE}</head><body>"]

    # Header
    badge_cls = f"badge-{coverage}"
    meta_parts: list[str] = []
    if snapshot_count:
        meta_parts.append(f"{snapshot_count} snapshot{'s' if snapshot_count != 1 else ''}")
    if last_captured:
        meta_parts.append(f"last captured {last_captured}")
    meta_str = " · ".join(meta_parts) if meta_parts else ""

    parts.append('<div class="header">')
    parts.append('<div class="header-top">')
    parts.append(f'<span class="project-label">{escape(project)} / {escape(kb)}</span>')
    parts.append(f'<span class="badge {badge_cls}">{coverage}</span>')
    parts.append("</div>")
    if meta_str:
        parts.append(f'<div class="meta">{escape(meta_str)}</div>')
    parts.append("</div>")

    # Hypothesis — the wedge
    if hypothesis:
        parts.append('<div class="hypothesis">')
        parts.append('<div class="hypothesis-label">Hypothesis</div>')
        parts.append(f'<div class="hypothesis-text">{escape(hypothesis)}</div>')
        parts.append("</div>")

    # Synopsis
    if synopsis_entries:
        parts.append('<div class="synopsis">')
        parts.append('<div class="section-label">Context</div>')
        for topic, gloss in synopsis_entries[:4]:
            parts.append('<div class="topic-entry">')
            parts.append(f'<span class="topic-name">{escape(topic)}</span> ')
            parts.append(f'<span class="topic-gloss">{escape(gloss)}</span>')
            parts.append("</div>")
        parts.append("</div>")

    # Recent snapshots (skip the one we already showed as hypothesis)
    shown = snapshot_findings[1:] if hypothesis else snapshot_findings
    if shown:
        parts.append('<div class="snapshots">')
        parts.append('<div class="section-label">Recent snapshots</div>')
        for f in shown[:3]:
            title = f.get("title", "")
            content = f.get("content", "")
            ts = _extract_timestamp(content)
            branch = _extract_field(content, "Branch")
            cursor = _extract_field(content, "Cursor")
            detail_parts: list[str] = []
            if branch:
                detail_parts.append(f"branch: {branch}")
            if cursor:
                detail_parts.append(f"cursor: {cursor}")
            parts.append('<div class="snapshot">')
            parts.append(f'<div class="snap-title">{escape(title)}</div>')
            meta = (ts or "") + ("  " + " · ".join(detail_parts) if detail_parts else "")
            if meta.strip():
                parts.append(f'<div class="snap-meta">{escape(meta.strip())}</div>')
            parts.append("</div>")
        parts.append("</div>")

    # Cross-repo activity rollup — where you've been working, across repos
    if activity:
        parts.append('<div class="snapshots">')
        parts.append('<div class="section-label">Across repos</div>')
        for entry in activity[:5]:
            repo = entry.get("repo") or ""
            branch = entry.get("branch") or ""
            where = f"{repo}/{branch}" if branch else repo
            ts = _fmt_ts(entry.get("captured_at") or "")
            hypo = entry.get("hypothesis") or ""
            parts.append('<div class="snapshot">')
            if where:
                parts.append(f'<div class="snap-title">{escape(where)}</div>')
            meta_bits = " · ".join(b for b in (ts, hypo) if b)
            if meta_bits:
                parts.append(f'<div class="snap-meta">{escape(meta_bits)}</div>')
            parts.append("</div>")
        parts.append("</div>")

    # Explore CTA when coverage is gap
    if coverage == "gap":
        parts.append('<div class="explore-bar">')
        parts.append('<button class="explore-btn" id="explore-btn">Explore external sources →</button>')
        parts.append("</div>")
        parts.append(_EXPLORE_JS)

    parts.append('<div class="footer">br8n · context resume</div>')
    parts.append("</body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Preamble parsing helpers
# ---------------------------------------------------------------------------

def _parse_findings(root: ET.Element | None) -> list[dict]:
    if root is None:
        return []
    findings_el = root.find("findings")
    if findings_el is None:
        return []
    out: list[dict] = []
    for f in findings_el.findall("finding"):
        title_el = f.find("title")
        content_el = f.find("content")
        out.append({
            "category": f.get("category", ""),
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "content": (content_el.text or "").strip() if content_el is not None else "",
        })
    return out


def _parse_synopsis(root: ET.Element | None) -> list[tuple[str, str]]:
    if root is None:
        return []
    synopsis_el = root.find("synopsis")
    if synopsis_el is None:
        return []
    return [
        (e.get("topic", ""), (e.text or "").strip())
        for e in synopsis_el.findall("entry")
    ]


def _is_snapshot(f: dict) -> bool:
    return f.get("category") == "snapshot" or "Hypothesis" in f.get("content", "")


def _extract_hypothesis(findings: list[dict]) -> str | None:
    for f in findings:
        if not _is_snapshot(f):
            continue
        title = f.get("title", "")
        content = f.get("content", "")
        # Hypothesis is the title if it doesn't look like a generic snapshot label
        if title and not title.startswith("Snapshot ") and not title.startswith("Working on "):
            return title
        # Fall back to inline hypothesis line in content
        m = re.search(r"\*\*Hypothesis\*\*:\s*(.+?)$", content, re.MULTILINE)
        if m:
            return m.group(1).strip()
    return None


def _extract_timestamp(content: str) -> str:
    m = re.search(r"Captured (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC", content)
    return m.group(1) if m else ""


def _fmt_ts(iso: str) -> str:
    """ISO-8601 captured_at → 'YYYY-MM-DD HH:MM' (best-effort; empty stays empty)."""
    if not iso:
        return ""
    return iso[:16].replace("T", " ")


def _extract_field(content: str, field: str) -> str:
    m = re.search(rf"\*\*{re.escape(field)}\*\*:\s*`?([^\n`]+)`?", content)
    return m.group(1).strip() if m else ""

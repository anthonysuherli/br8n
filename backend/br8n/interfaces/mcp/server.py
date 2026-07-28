"""br8n MCP server — capture + resume tools for Claude Code.

    python -m br8n.interfaces.mcp.server

Add to .claude/settings.json:

    {
      "mcpServers": {
        "br8n": {
          "command": "python",
          "args": ["-m", "br8n.interfaces.mcp.server"],
          "cwd": "/path/to/br8n/backend"
        }
      }
    }
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from br8n.agent.resume import latest_next_action, resume_preamble
from br8n.capture.models import WorkspaceSnapshot
from br8n.capture.service import persist_snapshot
from br8n.config import get_config, get_settings
from br8n.exploration import run_exploration
from br8n.interfaces.mcp.banner import BR8N_BANNER
from br8n.interfaces.mcp.tenancy import resolve_store, resolve_tenant
from br8n.knowledge_graph.activity import query_activity, schedule_activity_update
from br8n.knowledge_graph.builder import build_graph
from br8n.knowledge_graph.drift import assess_drift
from br8n.knowledge_graph.models import KGSchema
from br8n.knowledge_graph.schema import propose_schema, validate_schema
from br8n.livingdocs.distill import run_distill, schedule_distill
from br8n.livingdocs.timeline import run_timeline, schedule_timeline
from br8n.livingdocs.journal import persist_journal
from br8n.livingdocs.notes import persist_note
from br8n.constants import JOURNAL_SCOPE
from br8n.livingdocs.paths import DocPaths
from br8n.livingdocs.policy import NotePolicy, load_policy, save_policy
from br8n.monitoring.recorder import PREAMBLE_TARGETS
from br8n.store import get_store

mcp = FastMCP("br8n")


@mcp.tool()
async def br8n_capture(
    project: str,
    kb: str,
    trigger: str,
    captured_at: str,
    branch: str | None = None,
    cursor_file: str | None = None,
    cursor_line: int | None = None,
    open_files: list[str] | None = None,
    git_diff_stat: str | None = None,
    terminal_tail: str | None = None,
    hypothesis: str | None = None,
    next_action: str | None = None,
    thread_id: str | None = None,
    project_path: str = "",
) -> dict:
    """Persist a workspace snapshot as a Finding. Creates the project/KB on demand.

    Call this when the developer is interrupted (blur, git checkout, idle).
    `hypothesis` is the one-line intent string — the wedge that makes context
    recovery 3–5× faster. `next_action` is the single ~two-minute step future-you should do first —
    infer it from the diff/conversation when the user doesn't state one.
    Returns the finding id.
    """
    snap = WorkspaceSnapshot(
        project_path=project_path or project,
        trigger=trigger,  # type: ignore[arg-type]
        captured_at=captured_at,
        branch=branch,
        git_diff_stat=git_diff_stat,
        open_files=open_files or [],
        cursor_file=cursor_file,
        cursor_line=cursor_line,
        terminal_tail=terminal_tail,
        hypothesis=hypothesis,
        next_action=next_action,
        thread_id=thread_id,
    )
    ctx = resolve_tenant(project, kb, create=True)
    finding_id = await persist_snapshot(ctx, snap)
    schedule_activity_update(snap, finding_id)  # fire-and-forget; best-effort
    schedule_timeline(ctx, project=project, project_path=project_path, kb=kb)
    return {"finding_id": finding_id, "project": project, "kb": kb}


async def _note_impl(
    project, kb, project_path, content, session_id, title, captured_at="", source="agent",
    next_action=None,
):
    ctx = resolve_tenant(project, kb, create=True)
    res = await persist_note(
        ctx,
        project_path=project_path,
        kb=kb,
        content=content,
        session_id=session_id,
        title=title,
        captured_at=captured_at,
        source=source,
        next_action=next_action,
    )
    schedule_distill(ctx, project_path=project_path, kb=kb)
    schedule_timeline(ctx, project=project, project_path=project_path, kb=kb)
    return {**res, "project": project, "kb": kb}


@mcp.tool()
async def br8n_note(
    project: str,
    kb: str,
    project_path: str,
    content: str,
    session_id: str,
    title: str,
    captured_at: str = "",
    source: str = "agent",
    next_action: str | None = None,
) -> dict:
    """Persist a session note: a `note` Finding (searchable, feeds resume) AND a
    markdown file under .br8n/notes/<kb>/. Then schedules a debounced re-distill of
    the curated doc tree. Called by the Stop hook at session end. `content` should be
    rendered per the KB's note policy (br8n_notes_policy_get).
    next_action: the single ~two-minute step future-you should do first (one line).
    Returns {finding_id, note_path, project, kb}."""
    return await _note_impl(
        project, kb, project_path, content, session_id, title, captured_at, source,
        next_action,
    )


def _policy_get_impl(project, kb, project_path):
    paths = DocPaths(project_path=project_path, kb=kb)
    pol = load_policy(paths)
    return {"policy": pol.model_dump(), "project": project, "kb": kb}


def _policy_set_impl(project, kb, project_path, policy):
    try:
        pol = NotePolicy.model_validate(policy)
    except Exception as exc:  # noqa: BLE001 — return errors, never crash the tool
        return {"ok": False, "errors": [str(exc)], "project": project, "kb": kb}
    save_policy(DocPaths(project_path=project_path, kb=kb), pol)
    return {"ok": True, "policy": pol.model_dump(), "project": project, "kb": kb}


async def _distill_impl(project, kb, project_path, force=False):
    ctx = resolve_tenant(project, kb, create=True)
    if force:
        res = await run_distill(ctx, project_path=project_path, kb=kb)
        return {"distilled": True, "forced": True, **res, "project": project, "kb": kb}
    schedule_distill(ctx, project_path=project_path, kb=kb)
    return {"distilled": False, "forced": False, "scheduled": True, "project": project, "kb": kb}


@mcp.tool()
def br8n_notes_policy_get(project: str, kb: str, project_path: str) -> dict:
    """Read the per-KB note-taking policy (section template + free-text steer) from
    .br8n/notes-policy.json. Returns {policy: {sections, steer}, project, kb}.
    Returns the default policy if none is set yet."""
    return _policy_get_impl(project, kb, project_path)


@mcp.tool()
def br8n_notes_policy_set(project: str, kb: str, project_path: str, policy: dict) -> dict:
    """Persist the per-KB note-taking policy. `policy` = {sections: [{name, enabled}],
    steer: str}. Validates before writing; on a bad shape returns {ok: False, errors}.
    On success returns {ok: True, policy, project, kb}. Used by /br8n:notes."""
    return _policy_set_impl(project, kb, project_path, policy)


@mcp.tool()
async def br8n_distill(project: str, kb: str, project_path: str, force: bool = False) -> dict:
    """(Re)build the curated .br8n/docs/ tree from the KB's session notes. `force=True`
    distills now and returns {distilled, doc_count, folders}; otherwise it just nudges the
    debounced background distiller. Used by /br8n:docs --rebuild."""
    return await _distill_impl(project, kb, project_path, force)


async def _timeline_impl(project, kb, project_path, force=False):
    ctx = resolve_tenant(project, kb, create=True)
    if force:
        res = await run_timeline(ctx, project=project, project_path=project_path, kb=kb)
        return {"forced": True, **res, "project": project, "kb": kb}
    schedule_timeline(ctx, project=project, project_path=project_path, kb=kb)
    return {"forced": False, "scheduled": True, "project": project, "kb": kb}


@mcp.tool()
async def br8n_timeline(project: str, kb: str, project_path: str, force: bool = False) -> dict:
    """(Re)build the append-only activity timeline at .br8n/timeline/ from this
    repo+branch's notes + captures + journal. `force=True` runs a pass now and returns
    {forced, appended, recent_days, week_days, *_path}; otherwise it nudges the debounced
    background rollup. Used by /br8n:timeline --rebuild."""
    return await _timeline_impl(project, kb, project_path, force)


async def _journal_impl(
    text, type="", tags=None, title="", project="", project_path="", session_id=""
):
    ctx = resolve_tenant(JOURNAL_SCOPE, JOURNAL_SCOPE, create=True)
    res = await persist_journal(
        ctx,
        text=text,
        type=type,
        tags=tags or [],
        title=title,
        originating_project=project,
        session_id=session_id,
    )
    return {**res, "scope": "journal"}


@mcp.tool()
async def br8n_journal(
    text: str,
    type: str = "",
    tags: list[str] | None = None,
    title: str = "",
    project: str = "",
    project_path: str = "",
    session_id: str = "",
) -> dict:
    """Write a personal JOURNAL entry — cross-project, searchable any time.

    Unlike br8n_note (a session note bound to the current repo+branch, written
    at session end), the journal is your global notebook: call this WHENEVER
    something is worth keeping — an insight, a decision, a reflection, a pointer.
    `type` is a free label (insight | reflection | reference | decision) and
    `tags` add filterable keywords; both feed search. `title` is optional (the
    first line is used if omitted). `project`/`project_path` are optional context
    (where you were) stamped into provenance — storage is always the journal
    scope. Returns {finding_id, entry_path, scope}."""
    return await _journal_impl(text, type, tags, title, project, project_path, session_id)


async def _journal_search_impl(
    query, scope="both", type=None, limit=10, project="", kb="", project_path=""
):
    from br8n.clients.embeddings import embed_text

    emb = await embed_text(query)
    min_sim = 0.0
    if scope == "journal":
        ctx = resolve_tenant(JOURNAL_SCOPE, JOURNAL_SCOPE, create=True)
        store = get_store(ctx.access_token, org_id=ctx.org_id)
        rows = await store.match_findings(ctx.kb_id, emb, limit, min_sim, categories=["journal"])
    elif scope == "project":
        try:
            ctx = resolve_tenant(project, kb, create=False)
        except RuntimeError as exc:  # unknown project/kb — a search miss, not an error
            if "not found" in str(exc).lower():
                return {"results": [], "scope": scope, "count": 0}
            raise
        store = get_store(ctx.access_token, org_id=ctx.org_id)
        rows = await store.match_findings(ctx.kb_id, emb, limit, min_sim, categories=["note"])
    else:  # "both" — org-wide across the journal + every KB's notes
        store = resolve_store()
        rows = await store.match_findings(None, emb, limit, min_sim, categories=["journal", "note"])

    results: list[dict] = []
    for r in rows:
        tags = r.get("tags") or []
        if type and type not in tags:
            continue
        results.append(
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "snippet": (r.get("content") or "")[:240],
                "score": round(float(r.get("similarity") or 0.0), 4),
                "category": r.get("category"),
                "tags": tags,
            }
        )
    return {"results": results, "scope": scope, "count": len(results)}


@mcp.tool()
async def br8n_journal_search(
    query: str,
    scope: str = "both",
    type: str | None = None,
    limit: int = 10,
    project: str = "",
    kb: str = "",
    project_path: str = "",
) -> dict:
    """Search your journal (and optionally your project notes) by meaning.

    `scope` selects the corpus: ``journal`` (your cross-project entries only),
    ``project`` (this repo+branch's session notes only — pass project/kb), or
    ``both`` (default — journal entries + every project's notes in one ranked
    list). `type` further filters journal results by label (insight/reflection/
    …). Returns {results: [{title, snippet, score, category, tags, id}], scope,
    count}, ranked by similarity."""
    return await _journal_search_impl(query, scope, type, limit, project, kb, project_path)


@mcp.tool()
async def br8n_journal_recent(limit: int = 10) -> dict:
    """List your most recent journal entries, newest first (no query).

    Returns {entries: [{id, title, category, confidence, tags, created_at}],
    count}. Use to skim what you've journaled lately; use br8n_journal_search
    to find by meaning."""
    ctx = resolve_tenant(JOURNAL_SCOPE, JOURNAL_SCOPE, create=True)
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    res = store.list_findings(ctx.kb_id, category="journal", limit=limit)
    return {"entries": res.get("findings", []), "count": res.get("count", 0)}


@mcp.tool()
async def br8n_resume(
    project: str, kb: str, query: str | None = None, depth: str = "normal"
) -> dict:
    """Tap the session KB and return the 30-second resume card.

    Returns `{banner, preamble, coverage, next_action, thread_id, project, kb}`.
    coverage routes behavior: rich → instant recall, gap → offer explore.
    When next_action is set, lead your resume summary with it ("Do this now: …").
    """
    res = await resume_preamble(project, kb, query, depth=depth)
    await res.store.record_access(
        org_id=res.ctx.org_id,
        kb_id=res.ctx.kb_id,
        surface="mcp",
        targets=PREAMBLE_TARGETS,
        query_text=query,
    )
    next_action, thread_id = latest_next_action(res.store, res.ctx.kb_id)
    return {
        "banner": BR8N_BANNER,
        "preamble": res.preamble,
        "coverage": res.coverage,
        "next_action": next_action,
        "thread_id": thread_id,
        "project": project,
        "kb": kb,
    }


@mcp.tool()
async def br8n_projects() -> dict:
    """List every repo+branch you've captured to, most-recent first.

    Powers the `/br8n:pickup` selector: each project carries its branches with
    `last_activity` + `snapshot_count` chips so you can jump back into any repo you've
    been working in — not just the current git checkout. Org-scoped on cloud, the
    single local store on the free tier. Returns
    `{projects: [{project, project_id, kbs: [{kb, kb_id, last_activity, snapshot_count}]}]}`.
    """
    store = resolve_store()
    return {"projects": store.list_projects()}


@mcp.tool()
async def br8n_explore(
    project: str,
    kb: str,
    prompt: str,
    max_findings: int | None = None,
) -> dict:
    """Run the gap-fill explore pipeline (plan→search→crawl→extract→merge).

    Call this when `br8n_resume` returns coverage='gap'. Blocks until the
    pipeline completes (1–3 min). Persists findings to the KB and rebuilds
    the synopsis — the next `br8n_resume` call will be richer.
    Returns finding_count and finding_ids.
    """
    from br8n.agent.synopsis import schedule_rebuild
    from br8n.api.explore import _persist_findings

    ctx = resolve_tenant(project, kb, create=True)
    cfg = get_config().exploration
    max_f = min(max_findings or cfg.default_max_findings, cfg.max_findings)

    import uuid as _uuid
    exploration_id = str(_uuid.uuid4())

    findings = await run_exploration(
        prompt,
        exploration_id=exploration_id,
        project_id=ctx.project_id,
        kb_id=ctx.kb_id,
        cfg=cfg,
    )
    captured = findings[:max_f]
    finding_ids = await _persist_findings(ctx, captured, exploration_id)
    schedule_rebuild(ctx)
    return {
        "finding_count": len(finding_ids),
        "finding_ids": finding_ids,
        "project": project,
        "kb": kb,
    }


@mcp.tool()
async def br8n_kb_exists(project: str, kb: str) -> dict:
    """Cheap first-run guard: does a br8n KB exist for this project/kb?

    Never creates anything. Returns {exists: bool, init_offered: bool, project, kb}.
    ``init_offered`` is True when the KG schema wizard has already been offered
    (migration 0007 stamp) — callers use this to skip the re-offer.
    On genuine backend errors (non-"not found" RuntimeErrors), RAISES so the
    caller fails closed — don't silently return exists=False on a backend outage.
    """
    try:
        ctx = resolve_tenant(project, kb, create=False)
        store = get_store(ctx.access_token, org_id=ctx.org_id)
        init_offered = store.get_init_offered(ctx.kb_id)
        return {
            "exists": True,
            "init_offered": init_offered,
            "project": project,
            "kb": kb,
        }
    except RuntimeError as exc:
        if "not found" in str(exc).lower():
            return {"exists": False, "init_offered": False, "project": project, "kb": kb}
        raise


@mcp.tool()
async def br8n_mark_init_offered(project: str, kb: str) -> dict:
    """Stamp the KB with the time the KG schema wizard was offered.

    Called exactly once after the first-run schema offer is surfaced — prevents
    re-offering on subsequent SessionStart events. Safe to call even if migration
    0007 has not been applied (best-effort, never raises).
    Returns {marked: bool, project, kb}.
    """
    try:
        ctx = resolve_tenant(project, kb, create=False)
        store = get_store(ctx.access_token, org_id=ctx.org_id)
        store.mark_init_offered(ctx.kb_id)
        return {"marked": True, "project": project, "kb": kb}
    except Exception:  # noqa: BLE001 — best-effort stamp; never fail the session
        return {"marked": False, "project": project, "kb": kb}


@mcp.tool()
async def br8n_activity(query: str | None = None, repo: str | None = None) -> dict:
    """Query your cross-repo ACTIVITY graph — what you've been working on.

    The activity graph accumulates automatically from every `br8n_capture`:
    repos, branches, files, work sessions, and the tasks behind them, across all
    your projects. Ask it things like "what was I doing in br8n last" or "what
    touches the store layer".

    `query` seeds a semantic subgraph (omit for the whole graph); `repo` narrows
    to one repository. Returns `{nodes, edges, summary}` — `summary` is a short
    natural-language rollup; `nodes`/`edges` are the graph slice.
    """
    return await query_activity(query, repo=repo)


@mcp.tool()
async def br8n_propose_kg_schema(
    project: str, kb: str, max_findings: int | None = None
) -> dict:
    """STEP 1 of KG-intent co-design. Mine the KB's findings and propose a draft
    target ontology: ``node_types``, ``relation_types``, ``relation_validity``, and
    ``competency_questions``. Persists nothing — review with the user, then approve
    with ``br8n_set_kg_schema``. If the KB has no findings the draft is a
    generic default plus a ``note``."""
    ctx = resolve_tenant(project, kb, create=False)
    cfg = get_config().knowledge_graph
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    n = max_findings or cfg.max_findings
    result = store.list_findings(ctx.kb_id, limit=n)
    findings = [f for f in (result.get("findings") or []) if isinstance(f, dict)]
    stats = store.kg_stats(ctx.kb_id)
    # Build an emergent hint from the current graph's type distribution.
    emergent: dict | None = None
    if stats.get("node_count", 0) or stats.get("edge_count", 0):
        emergent = {
            "node_types": list((stats.get("by_type") or {}).keys()),
            "relations": list((stats.get("by_relation") or {}).keys()),
        }
    draft = await propose_schema(findings, cfg, emergent=emergent)
    out = draft.model_dump()
    if not findings:
        out["note"] = "KB has no findings — explore or ingest first for a grounded proposal."
    return out


@mcp.tool()
async def br8n_set_kg_schema(project: str, kb: str, schema: dict) -> dict:
    """STEP 2 of KG-intent co-design. Validate and persist the user-approved KG
    schema dict (as returned by ``br8n_propose_kg_schema``, edited as the user
    wishes) as a new version. Returns ``{ok: true, schema}`` on success, or
    ``{ok: false, errors}`` if the schema is malformed (nothing is saved).
    The next ``br8n_build_graph(use_schema=True)`` builds against it."""
    ctx = resolve_tenant(project, kb, create=False)
    try:
        parsed = KGSchema.model_validate(schema)
    except Exception as exc:  # noqa: BLE001 — surface validation errors to the caller
        return {"ok": False, "errors": [f"schema does not parse: {exc}"]}
    errors = validate_schema(parsed)
    if errors:
        return {"ok": False, "errors": errors}
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    stored = store.set_kg_intent(ctx.org_id, ctx.kb_id, parsed.model_dump())
    return {"ok": True, "schema": stored}


@mcp.tool()
async def br8n_get_kg_schema(project: str, kb: str) -> dict:
    """Both ontologies for the KB: ``intent`` (the user-approved target schema set
    via ``br8n_set_kg_schema``, or null) and ``emergent`` (the node/relation types
    actually present in the built graph, with totals). Compare to see drift."""
    ctx = resolve_tenant(project, kb, create=False)
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    intent = store.get_kg_intent(ctx.kb_id)
    emergent = store.kg_stats(ctx.kb_id)
    return {"intent": intent, "emergent": emergent}


@mcp.tool()
async def br8n_schema_drift(project: str, kb: str) -> dict:
    """Should br8n offer to (re)design this KB's KG schema right now?

    The trigger behind the self-maintaining loop. Reads the built graph's type
    distribution against the KB's approved intent schema (no extra LLM call) and
    returns a verdict:
      - ``mode``: ``"cold_start"`` (no schema yet, enough collected to propose one),
        ``"drift"`` (a schema is set but residual / off-ontology nodes crossed the
        threshold), ``"ok"`` (the graph fits), or ``"empty"`` (too small to judge).
      - ``should_offer``: whether to surface the offer NOW — debounced so a declined
        offer doesn't re-nag every session.
      - ``offer_line``: the ready-to-show, one-line turn-boundary offer (null unless
        ``should_offer``).
      - ``residual`` / ``ratio`` / ``residual_types``: the off-ontology cluster — the
        seed the ``/br8n:schema`` wizard reshapes around.

    Gated by ``BR8N_SCHEMA_DRIFT`` (default on); returns ``mode="off"`` when
    disabled. Best-effort — never raises; an unbuilt graph reads as ``"empty"``."""
    if os.getenv("BR8N_SCHEMA_DRIFT", "1") == "0":
        return {"mode": "off", "should_offer": False, "offer_line": None, "project": project, "kb": kb}
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception:  # noqa: BLE001 — unknown KB: nothing to judge, stay quiet
        return {"mode": "empty", "should_offer": False, "offer_line": None, "project": project, "kb": kb}
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    cfg = get_config().drift
    verdict = assess_drift(
        store,
        ctx.kb_id,
        cfg,
        init_offered=store.get_init_offered(ctx.kb_id),
        drift_marker=store.get_drift_marker(ctx.kb_id),
    )
    return {**verdict.to_dict(), "project": project, "kb": kb}


@mcp.tool()
async def br8n_mark_drift_offered(project: str, kb: str, residual: int) -> dict:
    """Stamp that a schema-drift offer was surfaced for the KB, at ``residual`` count.

    Call once right after surfacing the drift ``offer_line`` (whether or not the
    user accepts). Debounces re-offers: the next drift offer only re-surfaces once
    residual grows by the configured ``rearm_delta`` beyond this stamp — so a steady
    "no" stays quiet. Best-effort; never raises. Returns
    ``{marked, project, kb, residual}``."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
        store = get_store(ctx.access_token, org_id=ctx.org_id)
        store.set_drift_marker(ctx.kb_id, int(residual))
        return {"marked": True, "project": project, "kb": kb, "residual": int(residual)}
    except Exception:  # noqa: BLE001 — best-effort stamp; never fail the session
        return {"marked": False, "project": project, "kb": kb, "residual": residual}


@mcp.tool()
async def br8n_build_graph(
    project: str,
    kb: str,
    max_findings: int | None = None,
    rebuild: bool = True,
    use_schema: bool = True,
) -> dict:
    """Build/refresh the KB's knowledge graph from its findings. An LLM extracts
    entities + relationships, which are deduped and written to kg_nodes/kg_edges.
    ``rebuild=True`` (default) clears the existing graph first (clean rebuild).
    ``use_schema=True`` steers extraction with the KB's approved intent schema if
    one was set via ``br8n_set_kg_schema``; with none set it builds free-form.
    Returns ``{findings_scanned, nodes_created, edges_created, node_count, edge_count}``."""
    ctx = resolve_tenant(project, kb, create=False)
    return await build_graph(
        ctx,
        max_findings=max_findings,
        rebuild=rebuild,
        use_schema=use_schema,
    )


@mcp.tool()
async def br8n_graph(
    project: str, kb: str, focus: str | None = None, depth: int | None = None
) -> dict:
    """Read the KB's knowledge graph: the full graph (capped) or a depth-bounded
    subgraph around nodes whose label matches ``focus``. Returns nodes/edges +
    counts. Empty until ``br8n_build_graph`` has run."""
    ctx = resolve_tenant(project, kb, create=False)
    cfg = get_config().public_api
    store = get_store(ctx.access_token, org_id=ctx.org_id)

    seed_ids: list[str] | None = None
    if focus:
        # Semantic seed: find nodes whose label is closest to `focus`.
        from br8n.clients.embeddings import embed_text

        try:
            emb = await embed_text(focus)
            matches = await store.match_kg_nodes(
                ctx.kb_id,
                query_embedding=emb,
                match_count=5,
                min_similarity=0.0,
            )
            seed_ids = [m["id"] for m in matches if m.get("id")]
        except Exception:  # noqa: BLE001 — degrade gracefully to whole-graph
            seed_ids = None

    d = min(depth or cfg.graph_default_depth, cfg.graph_max_depth)
    g = store.get_kg_subgraph(
        ctx.kb_id,
        seed_node_ids=seed_ids,
        node_cap=cfg.graph_node_cap,
        edge_cap=cfg.graph_edge_cap,
        depth=d,
    )
    return {**g, "node_count": len(g.get("nodes", [])), "edge_count": len(g.get("edges", []))}


@mcp.tool()
async def br8n_kg_stats(project: str, kb: str) -> dict:
    """KG metrics: node/edge totals plus counts by node type and by relation."""
    ctx = resolve_tenant(project, kb, create=False)
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    return store.kg_stats(ctx.kb_id)


_VALID_EMBED_PROVIDERS = ("auto", "remote", "local", "none")


def _pending_counts() -> tuple[int, int]:
    """(findings, nodes) awaiting embedding. Local tier only; 0/0 elsewhere."""
    from br8n.store import active_backend, get_store

    if active_backend() != "local":
        return 0, 0
    try:
        conn = get_store()._conn
        f = conn.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE needs_embed = 1;"
        ).fetchone()["n"]
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM kg_nodes WHERE needs_embed = 1;"
        ).fetchone()["n"]
        return int(f), int(n)
    except Exception:  # reporting must not raise
        return 0, 0


def _pending_switch() -> dict | None:
    """A deferred auto-detected switch (Change B), or None. Local tier only —
    mirrors the ``_pending_counts`` guard; cloud never runs this sync."""
    from br8n.store import active_backend, get_store

    if active_backend() != "local":
        return None
    try:
        return get_store().pending_embedding_switch()
    except Exception:  # reporting must not raise
        return None


def _embeddings_get_impl() -> dict:
    from br8n.clients import embed_local
    from br8n.clients.embeddings import active_embedder

    ident = active_embedder()
    pending_findings, pending_nodes = _pending_counts()
    return {
        "provider": ident.provider,
        "model": ident.model,
        "dim": ident.dim,
        "source": ident.source,
        "extra_installed": embed_local.installed(),
        # "cached" and "resident" collapse to the same observable: the only
        # honest cache check is attempting a cache-only load, which warm_up()
        # does off-thread.
        "model_cached": embed_local.ready(),
        "ready": embed_local.ready(),
        "pending_findings": pending_findings,
        "pending_nodes": pending_nodes,
        # None unless an auto-detected change is deferred (would discard
        # existing vectors) — {"stored": {...}, "detected": {...}} when it
        # is. /br8n:embeddings (br8n_embeddings_set) is what applies it.
        "pending_switch": _pending_switch(),
    }


def _embeddings_set_impl(provider: str) -> dict:
    from br8n.clients import embed_local
    from br8n.clients.embeddings import active_embedder
    from br8n.settings_file import load_settings, save_setting
    from br8n.store import active_backend, get_store

    if provider not in _VALID_EMBED_PROVIDERS:
        return {
            "ok": False,
            "error": f"unknown provider {provider!r}; expected one of "
                     f"{', '.join(_VALID_EMBED_PROVIDERS)}",
            "fix": None,
        }
    if provider == "local":
        if active_backend() != "local":
            return {
                "ok": False,
                "error": "local embeddings are local-tier only — cloud pgvector "
                         "columns are 1536-wide; keep a remote key on cloud",
                "fix": None,
            }
        if not embed_local.installed():
            return {
                "ok": False,
                "error": "the local-embeddings extra is not installed",
                "fix": "pip install 'br8n[local-embeddings]'",
            }

    # Captured BEFORE writing so a failed resync can revert exactly —
    # including the no-setting-yet case, where the correct revert is
    # "absent," not the literal string "auto" (save_setting(key, None) is how
    # this module spells "remove the key").
    previous = load_settings().get("embedding_provider")
    save_setting("embedding_provider", provider)

    if active_backend() == "local":
        # get_store() caches one store per db_path for the process lifetime
        # (the whole point — so a switch survives without an MCP server
        # restart), which means the store never re-runs its construction-time
        # embedding-space sync on its own. Resync the SAME live store in
        # place — popping the cache would abandon an open sqlite connection —
        # so vec_findings/vec_kg_nodes actually resize and existing rows get
        # flagged needs_embed=1 before we report pending counts below.
        store = get_store()
        store.resync_embedding_space()

        # resync_embedding_space (-> _sync_embedding_space) is best-effort by
        # its own contract: it swallows exceptions and returns nothing, so a
        # locked DB or a transient I/O error during the rebuild DDL degrades
        # to a silent no-op from here. Reporting ok:True regardless would
        # leave the setting persisted, the reported identity flipped, and the
        # store still stamped at the OLD width — worse than not switching at
        # all, since the next capture then hits the identical dimension
        # mismatch. Verify the rebuild actually landed before claiming
        # success; "none" has no target width to land on (that provider
        # deliberately keeps whatever space already exists).
        ident = active_embedder()
        space = store.embedding_space()
        landed = ident.provider == "none" or (
            space is not None
            and space["dim"] == ident.dim
            and space["model"] == ident.model
        )
        if not landed:
            # NOT landing is ambiguous by itself: a deliberate defer (Change
            # B's work-at-risk gate kept the existing space on purpose,
            # because applying `ident` would discard real vectors) and a
            # genuine failure (a locked DB / transient I/O error mid-rebuild)
            # both leave the stamp exactly where it was. Ask the store which
            # one happened — pending_embedding_switch() is the same
            # predicate the gate itself used, so this can never disagree
            # with it — rather than guessing from the stamp.
            pending = store.pending_embedding_switch()
            if pending is not None:
                # Deferred, not failed: this IS what the user asked for
                # ("auto" — detect it) applied correctly. The gate just
                # declined to discard existing vectors to do it, and that
                # decision is not this call's to override. The setting
                # stays exactly as requested; nothing is rolled back.
                state = _embeddings_get_impl()
                return {
                    "ok": True,
                    "deferred": True,
                    **state,
                    "queued_rebuild": state["pending_findings"] + state["pending_nodes"],
                }
            save_setting("embedding_provider", previous)  # exact revert, not "auto"
            return {
                "ok": False,
                "error": "the vector index could not be rebuilt; the switch "
                         "was rolled back",
                "fix": "retry the switch, or run `python -m br8n.vault.reindex` "
                       "to rebuild the index from the vault",
            }

    state = _embeddings_get_impl()
    if state["provider"] == "local":
        embed_local.warm_up()
    return {
        "ok": True,
        "deferred": False,
        **state,
        "queued_rebuild": state["pending_findings"] + state["pending_nodes"],
    }


@mcp.tool()
def br8n_embeddings_get() -> dict:
    """Report the active embedding provider: {provider, model, dim, source,
    extra_installed, model_cached, ready, pending_findings, pending_nodes,
    pending_switch}. `source` names what decided it — "settings"
    (~/.br8n/settings.json), "config" (config.yaml / B2_EMBEDDING__PROVIDER)
    or "auto" (detection). `pending_switch` is None unless an auto-detected
    environment change (e.g. a key going missing) would discard existing
    vectors — that case is deferred rather than silently rebuilt, and
    `pending_switch` reports {stored, detected}: run br8n_embeddings_set to
    apply it. Used by /br8n:embeddings."""
    return _embeddings_get_impl()


@mcp.tool()
def br8n_embeddings_set(provider: str) -> dict:
    """Set the embedding provider — "auto" | "remote" | "local" | "none" —
    persisting to ~/.br8n/settings.json so it applies without restarting the
    MCP server. Refuses "local" when the extra is missing (returns the pip
    command in `fix`) or on the cloud tier. Otherwise resyncs the live store
    in place and returns one of three outcomes:

    - Applied {ok: True, deferred: False, ...state, queued_rebuild}: the
      vec tables were resized and existing rows flagged needs_embed=1 when
      the provider actually changed dim; queued_rebuild is how many rows
      were just flagged and will re-embed in the background.
    - Deferred {ok: True, deferred: True, ...state, queued_rebuild}: the
      request (typically "auto" after an environment change, e.g. a key
      going missing) would have discarded existing vectors, so the
      work-at-risk gate left the space exactly as it was instead of
      rebuilding — this is success, not failure, and the setting is NOT
      rolled back; `state["pending_switch"]` names the stored space, the
      detected space, and that calling this tool again is what applies it.
      `queued_rebuild` here is the LIVE pending count (rows already flagged
      `needs_embed=1` from before this call, e.g. earlier keyless captures)
      and can be >0 — nothing was newly queued by this deferred call itself.
    - Failed {ok: False, error, fix}: the resync genuinely failed (e.g. a
      locked DB during the rebuild DDL) — the persisted setting is rolled
      back to its prior value; `fix` names a retry or
      `python -m br8n.vault.reindex`.

    Used by /br8n:embeddings."""
    return _embeddings_set_impl(provider)


def main() -> None:
    get_settings()
    mcp.run()


if __name__ == "__main__":
    main()

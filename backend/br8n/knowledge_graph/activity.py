"""The activity knowledge graph — a per-user, cross-repo graph of work.

    capture ─► schedule_activity_update ─► activity_extract ─► Store.upsert_kg_*
                                          (deterministic + gated LLM)

Every capture appends to one reserved per-org KB (``__activity__``/``default``):
the snapshot's repo, branch, files and the capture itself become stable graph
nodes; the one-line hypothesis becomes a Task. Population is fire-and-forget and
best-effort — it never blocks or fails a capture. Reads (query/rollup/stats)
power the ``br8n_activity`` tool, the cross-repo resume card, and ``/v1/activity``.

Ontology (seeded, fixed — not user-curated):
    nodes      repo, branch, file, session, task
    relations  on_repo, on_branch, in_repo, edited, viewed, pursued
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid

from pydantic import BaseModel, Field

from br8n.capture.models import WorkspaceSnapshot
from br8n.clients.ai_gateway import structured_completion
from br8n.clients.embeddings import embed_batch
from br8n.config import ActivityConfig, get_config
from br8n.knowledge_graph.models import KGEdge, KGExtraction, KGNode
from br8n.store import Store, get_store

logger = logging.getLogger(__name__)


# --- tenancy: the reserved activity KB --------------------------------------

def resolve_activity_target(*, access_token: str | None = None, org_id: str | None = None, create: bool = True) -> tuple[Store, str, str]:
    """Resolve ``(store, org_id, activity_kb_id)`` for the caller's activity graph.

    Goes through the active Store like all tenancy: local returns
    ``org_id="local"``; cloud derives the org from the configured user's login.
    The reserved ``__activity__``/``default`` project+kb is one per org."""
    cfg = get_config().activity
    store = get_store(access_token, org_id=org_id)
    org_id, project_id = store.resolve_project(cfg.project_name, create=create)
    kb_id = store.resolve_kb(org_id, project_id, cfg.kb_name, create=create)
    return store, org_id, kb_id


def _safe_target(*, access_token: str | None = None, org_id: str | None = None, create: bool) -> tuple[Store, str, str] | None:
    """Resolve the activity target, or ``None`` if it doesn't exist yet (read path)."""
    try:
        return resolve_activity_target(access_token=access_token, org_id=org_id, create=create)
    except RuntimeError:
        return None


# --- deterministic structural extraction ------------------------------------

class _Graph:
    """Accumulates nodes/edges for one snapshot, deduping nodes by (type, label)
    and addressing edges by node index (collision-proof across types)."""

    def __init__(self) -> None:
        self.nodes: list[KGNode] = []
        self.edges: list[KGEdge] = []
        self._idx: dict[tuple[str, str], int] = {}

    def node(self, type: str, label: str, props: dict | None = None,
             grounded: list[str] | None = None) -> int:
        key = (type, label)
        if key in self._idx:
            i = self._idx[key]
            existing = self.nodes[i]
            for k, v in (props or {}).items():
                existing.properties.setdefault(k, v)
            for g in grounded or []:
                if g not in existing.grounded_in:
                    existing.grounded_in.append(g)
            return i
        i = len(self.nodes)
        self.nodes.append(KGNode(
            label=label, type=type,
            properties=dict(props or {}), grounded_in=list(grounded or []),
        ))
        self._idx[key] = i
        return i

    def edge(self, source: int, target: int, relation: str,
             grounded: list[str] | None = None) -> None:
        if source == target:
            return
        self.edges.append(KGEdge(
            source=source, target=target, relation=relation,
            grounded_in=list(grounded or []),
        ))


def _repo_label(snap: WorkspaceSnapshot) -> str:
    """Canonical repo label — the workspace folder name (basename of the path)."""
    path = (snap.project_path or "").rstrip("/")
    return os.path.basename(path) or path or "unknown-repo"


def _parse_diff_files(diff_stat: str | None) -> list[str]:
    """Pull file paths from ``git diff --stat`` lines (``path | 3 +++``)."""
    if not diff_stat:
        return []
    out: list[str] = []
    for line in diff_stat.splitlines():
        m = re.match(r"^\s*(\S.*?)\s+\|\s+\d+", line)
        if m:
            out.append(m.group(1).strip())
    return out


def _edited_files(snap: WorkspaceSnapshot) -> list[str]:
    """Files the session actually changed: the cursor file + diff-stat files."""
    out: list[str] = []
    if snap.cursor_file:
        out.append(snap.cursor_file)
    for f in _parse_diff_files(snap.git_diff_stat):
        if f not in out:
            out.append(f)
    return out


async def activity_extract(snap: WorkspaceSnapshot, finding_id: str) -> KGExtraction:
    """Turn one snapshot into nodes + edges (deterministic structure + a Task)."""
    cfg = get_config().activity
    g = _Graph()
    grounded = [finding_id] if finding_id else []
    repo = _repo_label(snap)

    ri = g.node("repo", repo, {"path": snap.project_path}, grounded)
    si = g.node(
        "session",
        finding_id or f"session {snap.captured_at}",
        {
            "captured_at": snap.captured_at,
            "trigger": snap.trigger,
            "repo": repo,
            "branch": snap.branch or "",
            "hypothesis": snap.hypothesis or "",
        },
        grounded,
    )
    g.edge(si, ri, "on_repo", grounded)

    if snap.branch:
        bi = g.node("branch", snap.branch, {"repo": repo}, grounded)
        g.edge(si, bi, "on_branch", grounded)
        g.edge(bi, ri, "in_repo", grounded)

    edited = _edited_files(snap)
    edited_set = set(edited)
    for path in edited[: cfg.max_files_per_session]:
        fi = g.node("file", path, {"repo": repo}, grounded)
        g.edge(si, fi, "edited", grounded)
        g.edge(fi, ri, "in_repo", grounded)
    viewed = [f for f in snap.open_files if f not in edited_set]
    for path in viewed[: cfg.max_files_per_session]:
        fi = g.node("file", path, {"repo": repo}, grounded)
        g.edge(si, fi, "viewed", grounded)
        g.edge(fi, ri, "in_repo", grounded)

    if snap.hypothesis:
        label = await _task_label(snap.hypothesis, cfg)
        task_props: dict = {
            "repo": repo,
            "thread_id": snap.thread_id or uuid.uuid4().hex,
            "thread_state": "open",
        }
        if snap.next_action:
            task_props["next_action"] = snap.next_action
        ti = g.node("task", label, task_props, grounded)
        g.edge(si, ti, "pursued", grounded)
        g.edge(ti, ri, "in_repo", grounded)

    return KGExtraction(nodes=g.nodes, edges=g.edges)


# --- gated LLM task distillation --------------------------------------------

class _TaskLabel(BaseModel):
    task: str = Field(default="", description="Short canonical task label")


_TASK_SYSTEM = (
    "Distil a developer's one-line intent into a short, canonical TASK label.\n"
    "Rules: 3-7 words, imperative voice (e.g. 'port KG builder to br8n'), no "
    "trailing punctuation, no quotes. Capture the goal, drop incidental detail. "
    "Return JSON {\"task\": \"...\"}."
)


async def _task_label(hypothesis: str, cfg: ActivityConfig) -> str:
    """Concise Task label for a hypothesis. Falls back to the raw text when the
    LLM pass is disabled (``BR8N_ACTIVITY_LLM=0``) or fails — the graph still grows."""
    raw = hypothesis.strip()
    if os.getenv("BR8N_ACTIVITY_LLM", "1") == "0":
        return raw[: cfg.max_task_label_chars]
    try:
        result = await structured_completion(
            model=cfg.task_model,
            response_format=_TaskLabel,
            system=_TASK_SYSTEM,
            user=hypothesis,
            temperature=cfg.temperature,
            fallback_model=cfg.task_fallback_model,
            use_json_schema=False,
        )
        label = (result.task or "").strip() or raw
    except Exception as exc:  # noqa: BLE001 — distillation is best-effort
        logger.warning("activity task distillation failed (%s); using raw hypothesis", exc)
        label = raw
    return label[: cfg.max_task_label_chars]


# --- persistence -------------------------------------------------------------

async def _persist(store: Store, org_id: str, kb_id: str, extraction: KGExtraction) -> dict:
    """Embed node labels, upsert nodes, then resolve + upsert edges."""
    if not extraction.nodes:
        return {"nodes": 0, "edges_created": 0, "node_ids": []}

    embeddings: list[list[float]] = []
    try:
        embeddings = await embed_batch([n.label for n in extraction.nodes])
    except Exception as exc:  # noqa: BLE001 — embeddings only power query; degrade, don't fail
        logger.warning("activity node embedding failed (%s); persisting without vectors", exc)

    node_rows = [
        {
            "org_id": org_id,
            "type": n.type,
            "label": n.label,
            "properties": n.properties,
            "grounded_in": n.grounded_in,
            "embedding": embeddings[i] if i < len(embeddings) else None,
        }
        for i, n in enumerate(extraction.nodes)
    ]
    node_ids = await store.upsert_kg_nodes(kb_id, node_rows)

    edge_rows = []
    for e in extraction.edges:
        if e.source >= len(node_ids) or e.target >= len(node_ids):
            continue
        edge_rows.append({
            "org_id": org_id,
            "source_node_id": node_ids[e.source],
            "target_node_id": node_ids[e.target],
            "relation": e.relation,
            "properties": e.properties,
            "grounded_in": e.grounded_in,
        })
    edges_created = await store.upsert_kg_edges(kb_id, edge_rows)
    return {"nodes": len(node_ids), "edges_created": edges_created, "node_ids": node_ids}


async def _sync_task_props(store: Store, kb_id: str, node_id: str, fresh: dict) -> None:
    """Converge a task node's behavioral props after upsert.

    upsert_kg_nodes merges existing-wins, so a pre-existing node keeps its
    original thread_id (first mint wins) but would also keep a stale
    next_action. Read-merge-write: backfill thread_id/thread_state on legacy
    nodes, always refresh next_action from the latest capture."""
    node = store.get_kg_node(kb_id, node_id)
    if not node:
        return
    props = dict(node.get("properties") or {})
    before = dict(props)
    if fresh.get("thread_id"):
        props.setdefault("thread_id", fresh["thread_id"])
    props.setdefault("thread_state", "open")
    if fresh.get("next_action"):
        props["next_action"] = fresh["next_action"]
    if props != before:
        await store.update_kg_node(kb_id, node_id, properties=props)


# --- population (fire-and-forget, best-effort) ------------------------------

_BG_TASKS: set[asyncio.Task] = set()


async def _run_activity_update(snap: WorkspaceSnapshot, finding_id: str, *, access_token: str | None = None, org_id: str | None = None) -> None:
    try:
        store, org_id_r, kb_id = resolve_activity_target(access_token=access_token, org_id=org_id, create=True)
        extraction = await activity_extract(snap, finding_id)
        result = await _persist(store, org_id_r, kb_id, extraction)
        task_idx = next(
            (i for i, n in enumerate(extraction.nodes) if n.type == "task"), None
        )
        if task_idx is not None and task_idx < len(result["node_ids"]):
            await _sync_task_props(
                store, kb_id, result["node_ids"][task_idx],
                extraction.nodes[task_idx].properties,
            )
        logger.info(
            "activity KG: +%s nodes, +%s edges (finding=%s)",
            result["nodes"], result["edges_created"], finding_id,
        )
    except Exception:  # noqa: BLE001 — best-effort: must never break a capture
        logger.exception("activity KG update failed for finding=%s", finding_id)


def schedule_activity_update(snap: WorkspaceSnapshot, finding_id: str, *, access_token: str | None = None, org_id: str | None = None) -> None:
    """Fire-and-forget activity-graph append after a capture. No-op when
    ``BR8N_ACTIVITY_KG=0``. Holds a strong task ref so it isn't GC'd mid-flight
    (mirrors ``synopsis.schedule_rebuild``)."""
    if os.getenv("BR8N_ACTIVITY_KG", "1") == "0":
        return
    task = asyncio.create_task(_run_activity_update(snap, finding_id, access_token=access_token, org_id=org_id))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


# --- read surfaces -----------------------------------------------------------

def _filter_repo(sub: dict, repo: str) -> dict:
    """Keep nodes belonging to `repo` (by label or properties.repo) + edges between them."""
    keep = {
        n["id"]
        for n in sub["nodes"]
        if n.get("label") == repo or (n.get("properties") or {}).get("repo") == repo
    }
    nodes = [n for n in sub["nodes"] if n["id"] in keep]
    edges = [
        e for e in sub["edges"]
        if e["source_node_id"] in keep and e["target_node_id"] in keep
    ]
    return {"nodes": nodes, "edges": edges}


def _summarize(sub: dict, stats: dict) -> str:
    """A tight NL rollup of a subgraph for the agent."""
    repos = sorted({
        n.get("label", "")
        for n in sub["nodes"] if n.get("type") == "repo"
    } - {""})
    tasks = [n.get("label") for n in sub["nodes"] if n.get("type") == "task"][:5]
    files = [n.get("label") for n in sub["nodes"] if n.get("type") == "file"][:5]
    lines = [
        f"Activity graph: {stats['node_count']} nodes, {stats['edge_count']} edges "
        f"({len(sub['nodes'])} in view)."
    ]
    if repos:
        lines.append(f"Repos: {', '.join(repos)}.")
    if tasks:
        lines.append(f"Tasks: {'; '.join(tasks)}.")
    if files:
        lines.append(f"Files: {', '.join(files)}.")
    return "\n".join(lines)


async def query_activity(query: str | None = None, *, repo: str | None = None, access_token: str | None = None, org_id: str | None = None) -> dict:
    """Query the activity graph. With `query`, seed a subgraph semantically; else
    return the (capped) whole graph. Optional `repo` filter. Returns
    ``{nodes, edges, summary}``."""
    store, _org, kb_id = resolve_activity_target(access_token=access_token, org_id=org_id, create=True)
    cfg = get_config().activity
    if query:
        embs = await embed_batch([query])
        seeds = await store.match_kg_nodes(
            kb_id, embs[0], match_count=12, min_similarity=cfg.query_min_similarity
        )
        seed_ids = [s["id"] for s in seeds]
        sub = store.get_kg_subgraph(
            kb_id, seed_node_ids=seed_ids or None,
            node_cap=cfg.subgraph_node_cap, edge_cap=cfg.subgraph_edge_cap,
        )
    else:
        sub = store.get_kg_subgraph(
            kb_id, node_cap=cfg.subgraph_node_cap, edge_cap=cfg.subgraph_edge_cap
        )
    if repo:
        sub = _filter_repo(sub, repo)
    return {"nodes": sub["nodes"], "edges": sub["edges"], "summary": _summarize(sub, store.kg_stats(kb_id))}


def activity_rollup(limit: int | None = None, *, access_token: str | None = None, org_id: str | None = None) -> list[dict]:
    """Recent work sessions across all repos, newest first — the resume-card rollup.
    Each entry: ``{repo, branch, captured_at, hypothesis}``. Empty if no activity yet."""
    t = _safe_target(access_token=access_token, org_id=org_id, create=False)
    if not t:
        return []
    store, _org, kb_id = t
    cfg = get_config().activity
    out: list[dict] = []
    for s in store.list_kg_nodes(kb_id, type="session", limit=limit or cfg.rollup_sessions):
        p = s.get("properties") or {}
        out.append({
            "repo": p.get("repo", ""),
            "branch": p.get("branch", ""),
            "captured_at": p.get("captured_at", ""),
            "hypothesis": p.get("hypothesis", ""),
        })
    return out


def _hotspots(sub: dict, *, top_n: int = 8) -> dict:
    """Most-touched repos and files by edge degree, from a (capped) full subgraph."""
    degree: dict[str, int] = {}
    for e in sub["edges"]:
        for endpoint in (e["source_node_id"], e["target_node_id"]):
            degree[endpoint] = degree.get(endpoint, 0) + 1

    def _top(node_type: str) -> list[dict]:
        ranked = sorted(
            (n for n in sub["nodes"] if n.get("type") == node_type),
            key=lambda n: (degree.get(n["id"], 0), n.get("label", "")),
            reverse=True,
        )
        return [{"label": n["label"], "degree": degree.get(n["id"], 0)} for n in ranked[:top_n]]

    return {"repos": _top("repo"), "files": _top("file"), "tasks": _top("task")}


def activity_stats(*, access_token: str | None = None, org_id: str | None = None) -> dict:
    """Graph totals + hotspots (most-touched repos/files/tasks). Empty if no activity."""
    t = _safe_target(access_token=access_token, org_id=org_id, create=False)
    if not t:
        return {"node_count": 0, "edge_count": 0, "by_type": {}, "by_relation": {},
                "hotspots": {"repos": [], "files": [], "tasks": []}}
    store, _org, kb_id = t
    stats = store.kg_stats(kb_id)
    sub = store.get_kg_subgraph(kb_id, node_cap=2000, edge_cap=5000)
    return {**stats, "hotspots": _hotspots(sub)}

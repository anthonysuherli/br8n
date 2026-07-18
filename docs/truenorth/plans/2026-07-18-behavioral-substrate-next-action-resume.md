# Behavioral Substrate + Next-Action-First Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use truenorth:subagent-driven-development (recommended) or truenorth:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a structured `metadata` field (`hypothesis`, `next_action`, `thread_id`) to captures and notes, mint stable thread ids on activity-KG task nodes, and make every resume surface lead with a concrete next action.

**Architecture:** A nullable `metadata` jsonb/JSON-text column on `findings` carries the structured fields end-to-end (capture → store → resume), replacing the title-sniffing hack as the primary read path (title-sniff stays as fallback). Thread ids are minted inside the existing `BR8N_ACTIVITY_KG` fire-and-forget pass and converge via read-merge-write `update_kg_node`. All new fields are optional everywhere; missing metadata degrades to today's behavior.

**Vision goals served:** End Goal 1 (next-action-first resume); Planned Detours 1–2 (capture/note schema extension; thread model over the activity KG).

**Tech Stack:** Python 3.11+, FastAPI, FastMCP, SQLite+sqlite-vec / Supabase, pytest (async via existing config).

**Spec:** `docs/truenorth/specs/2026-07-18-behavioral-substrate-next-action-resume-design.md`

## Global Constraints

- Repo root: `/Users/anthonysuherli/Projects/br8n`. All backend paths below are relative to `backend/`.
- Test command: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest <path> -v` (if `.venv` is missing, create per `backend/README.md`: `python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]" sqlite-vec`).
- All new fields are **optional** end-to-end (`None` default); never prompt the user for them; a null/missing metadata read must degrade to current behavior.
- Title/content rendering of snapshot findings is **unchanged** (backward compat with old readers/snapshots).
- No new env flags. Thread-id work rides the existing `BR8N_ACTIVITY_KG` gate. Best-effort code follows the existing bare `try/except Exception` + `logger` idiom.
- Thread **state transitions** (park/close), WIP counting, and scoreboard are OUT OF SCOPE — do not add them.
- Full suite must pass at the end: `.venv/bin/python -m pytest tests/ -x -q`.
- Commit after every task with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `metadata` column on findings (SQLite + Supabase stores)

**Files:**
- Modify: `backend/br8n/store/sqlite.py` (DDL ~line 61, `_ADD_COLUMN_MIGRATIONS` ~line 95, `_FINDING_COLS`/`_FINDING_LIST_COLS` lines 47–48, `insert_findings` ~line 231, `get_finding` ~line 273, `list_findings` ~line 293)
- Modify: `backend/br8n/store/supabase.py` (`_FINDING_COLS`/`_FINDING_LIST_COLS` lines 37–38)
- Create: `supabase/migrations/0009_findings_metadata.sql`
- Test: `backend/tests/test_findings_metadata.py`

**Interfaces:**
- Consumes: existing `Store.insert_findings(rows) -> list[str]`, `get_finding(kb_id, id) -> dict`, `list_findings(kb_id, category=None, limit=None) -> {"count", "findings"}`.
- Produces: finding rows accept an optional `metadata: dict | None` key on insert; `get_finding` and `list_findings` rows include `"metadata": dict | None`. Later tasks rely on `row["metadata"]` being a decoded dict (or None) in list results.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_findings_metadata.py`:

```python
"""Findings `metadata` round-trip against the real in-memory SQLiteStore."""

from __future__ import annotations

import pytest

from br8n.store import SQLiteStore


@pytest.fixture
def store() -> SQLiteStore:
    return SQLiteStore(":memory:")


def _row(kb_id: str, title: str, metadata: dict | None) -> dict:
    return {
        "org_id": "ignored",
        "kb_id": kb_id,
        "title": title,
        "content": "body",
        "category": "snapshot",
        "confidence": 1.0,
        "tags": ["snapshot", "manual"],
        "provenance": [],
        "metadata": metadata,
    }


async def test_metadata_round_trips_on_get(store):
    meta = {"hypothesis": "JWT caching stale tokens", "next_action": "rerun test_auth", "thread_id": "abc123"}
    [fid] = await store.insert_findings([_row("kb1", "t1", meta)])
    got = store.get_finding("kb1", fid)
    assert got["metadata"] == meta


async def test_metadata_present_in_list_findings(store):
    meta = {"next_action": "open store/base.py"}
    await store.insert_findings([_row("kb1", "t1", meta)])
    rows = store.list_findings("kb1", category="snapshot")["findings"]
    assert rows[0]["metadata"] == meta


async def test_missing_metadata_reads_as_none(store):
    row = _row("kb1", "t1", None)
    del row["metadata"]  # caller may omit the key entirely
    [fid] = await store.insert_findings([row])
    assert store.get_finding("kb1", fid)["metadata"] is None
    assert store.list_findings("kb1")["findings"][0]["metadata"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/test_findings_metadata.py -v`
Expected: FAIL (`KeyError: 'metadata'` or assertion on missing key — the column doesn't exist yet).

- [ ] **Step 3: Implement in `sqlite.py`**

In `backend/br8n/store/sqlite.py`:

(a) Column tuples (lines 47–48) — add `"metadata"` to both (leave `_FINDING_MATCH_COLS` alone; the match RPC contract is untouched):

```python
_FINDING_COLS = ("id", "title", "content", "category", "confidence", "tags", "provenance", "metadata", "created_at")
_FINDING_LIST_COLS = ("id", "title", "category", "confidence", "tags", "metadata", "created_at")
```

(b) DDL — in `_SCHEMA`, findings table gains the column:

```sql
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, kb_id TEXT NOT NULL,
  title TEXT, content TEXT, category TEXT, confidence REAL,
  tags TEXT, provenance TEXT, metadata TEXT, created_at TEXT NOT NULL);
```

(c) `_ADD_COLUMN_MIGRATIONS` — append:

```python
    # 0009: structured capture fields (hypothesis / next_action / thread_id)
    "ALTER TABLE findings ADD COLUMN metadata TEXT;",
```

(d) `insert_findings` — add the column to the INSERT statement and params:

```python
                INSERT INTO findings
                  (id, org_id, kb_id, title, content, category, confidence, tags, provenance, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
```

with the param (after the `provenance` json.dumps line):

```python
                    json.dumps(row["metadata"]) if row.get("metadata") else None,
```

(e) `get_finding` return dict — after the `"provenance"` line:

```python
            "metadata": _json_load(r["metadata"], None),
```

(f) `list_findings` row dict — after the `"tags"` line:

```python
                "metadata": _json_load(r["metadata"], None),
```

- [ ] **Step 4: Implement in `supabase.py` + migration**

`backend/br8n/store/supabase.py` lines 37–38:

```python
_FINDING_COLS = "id, title, content, category, confidence, tags, provenance, metadata, created_at"
_FINDING_LIST_COLS = "id, title, category, confidence, tags, metadata, created_at"
```

(Supabase `insert_findings` passes rows verbatim — a `metadata` dict lands in jsonb with no code change. Rows built by later tasks omit the key when None.)

Create `supabase/migrations/0009_findings_metadata.sql`:

```sql
-- 0009: structured capture fields (hypothesis / next_action / thread_id)
alter table findings add column if not exists metadata jsonb;
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/test_findings_metadata.py tests/test_kg_store_sqlite.py -v`
Expected: all PASS (new tests green; existing store tests unaffected).

- [ ] **Step 6: Commit**

```bash
cd /Users/anthonysuherli/Projects/br8n
git add backend/br8n/store/sqlite.py backend/br8n/store/supabase.py supabase/migrations/0009_findings_metadata.sql backend/tests/test_findings_metadata.py
git commit -m "feat(store): nullable metadata column on findings

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `next_action`/`thread_id` on WorkspaceSnapshot; adapter + service write metadata

**Files:**
- Modify: `backend/br8n/capture/models.py` (dataclass, ~line 39)
- Modify: `backend/br8n/capture/adapter.py` (`snapshot_to_finding`, ~line 49)
- Modify: `backend/br8n/capture/service.py` (`persist_snapshot` row build, ~line 36)
- Test: `backend/tests/test_capture_metadata.py`

**Interfaces:**
- Consumes: Task 1's `metadata` key on finding rows.
- Produces: `WorkspaceSnapshot` gains `next_action: str | None = None` and `thread_id: str | None = None`. `snapshot_to_finding(snap) -> dict` gains a `"metadata": dict | None` key (`{"hypothesis", "next_action", "thread_id"}`, null-valued keys omitted; `None` when all empty). `persist_snapshot` writes it to the finding row.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_capture_metadata.py`:

```python
"""snapshot_to_finding structured-metadata contract."""

from __future__ import annotations

from br8n.capture.adapter import snapshot_to_finding
from br8n.capture.models import WorkspaceSnapshot


def _snap(**kw) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        project_path="/repo/br8n", trigger="manual", captured_at="2026-07-18T10:00:00Z", **kw
    )


def test_metadata_carries_all_three_fields():
    payload = snapshot_to_finding(
        _snap(hypothesis="fix auth race", next_action="rerun test_auth.py", thread_id="t-1")
    )
    assert payload["metadata"] == {
        "hypothesis": "fix auth race",
        "next_action": "rerun test_auth.py",
        "thread_id": "t-1",
    }


def test_metadata_omits_null_fields():
    payload = snapshot_to_finding(_snap(hypothesis="fix auth race"))
    assert payload["metadata"] == {"hypothesis": "fix auth race"}


def test_metadata_none_when_all_empty():
    payload = snapshot_to_finding(_snap())
    assert payload["metadata"] is None


def test_title_and_content_unchanged_by_new_fields():
    with_na = snapshot_to_finding(_snap(hypothesis="fix auth race", next_action="rerun tests"))
    without = snapshot_to_finding(_snap(hypothesis="fix auth race"))
    assert with_na["title"] == without["title"]
    assert with_na["content"] == without["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/test_capture_metadata.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'next_action'`.

- [ ] **Step 3: Implement**

`backend/br8n/capture/models.py` — append to the dataclass after `hypothesis`:

```python
    # One concrete ~two-minute step future-you should do first. Optional; the
    # capture skill infers it when the user doesn't state one.
    next_action: str | None = None

    # Stable thread identity (minted by the activity KG on first task-node
    # creation; carried on later captures to converge writes on one thread).
    thread_id: str | None = None
```

`backend/br8n/capture/adapter.py` — in `snapshot_to_finding`, replace the final `return` block with:

```python
    title = _derive_title(snap)
    metadata = {
        k: v
        for k, v in {
            "hypothesis": snap.hypothesis,
            "next_action": snap.next_action,
            "thread_id": snap.thread_id,
        }.items()
        if v
    }
    return {
        "title": title[:120],
        "content": "\n".join(lines),
        "category": "snapshot",
        "tags": ["snapshot", snap.trigger],
        "provenance": [{"source": "br8n-vscode", "trigger": snap.trigger, "path": snap.project_path}],
        "metadata": metadata or None,
    }
```

`backend/br8n/capture/service.py` — in `persist_snapshot`, add to the `row` dict after `"provenance"`:

```python
        "metadata": payload["metadata"],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/test_capture_metadata.py tests/test_findings_metadata.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/anthonysuherli/Projects/br8n
git add backend/br8n/capture/models.py backend/br8n/capture/adapter.py backend/br8n/capture/service.py backend/tests/test_capture_metadata.py
git commit -m "feat(capture): next_action + thread_id on snapshots, written as finding metadata

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: capture surfaces — REST `SnapshotRequest` + MCP `br8n_capture`

**Files:**
- Modify: `backend/br8n/api/capture.py` (`SnapshotRequest` ~line 18, `capture` mapping ~line 46)
- Modify: `backend/br8n/interfaces/mcp/server.py` (`br8n_capture` ~line 49)
- Test: `backend/tests/test_capture_surfaces.py`

**Interfaces:**
- Consumes: Task 2's `WorkspaceSnapshot(next_action=..., thread_id=...)`.
- Produces: `SnapshotRequest` and `br8n_capture` accept optional `next_action: str | None = None` and `thread_id: str | None = None` and thread them into the snapshot. No response-shape change.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_capture_surfaces.py`:

```python
"""REST + MCP capture surfaces thread next_action/thread_id into WorkspaceSnapshot."""

from __future__ import annotations

from br8n.api.capture import SnapshotRequest


def test_snapshot_request_accepts_behavioral_fields():
    body = SnapshotRequest(
        project="br8n", kb="main", trigger="manual", captured_at="2026-07-18T10:00:00Z",
        next_action="rerun test_auth.py", thread_id="t-1",
    )
    assert body.next_action == "rerun test_auth.py"
    assert body.thread_id == "t-1"


def test_snapshot_request_fields_default_none():
    body = SnapshotRequest(project="br8n", kb="main", trigger="manual", captured_at="2026-07-18T10:00:00Z")
    assert body.next_action is None
    assert body.thread_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/test_capture_surfaces.py -v`
Expected: FAIL — pydantic rejects the unknown fields / attribute missing.

- [ ] **Step 3: Implement**

`backend/br8n/api/capture.py` — `SnapshotRequest` gains (after `hypothesis`):

```python
    next_action: str | None = None
    thread_id: str | None = None
```

and the `WorkspaceSnapshot(...)` construction in `capture()` gains:

```python
        next_action=body.next_action,
        thread_id=body.thread_id,
```

`backend/br8n/interfaces/mcp/server.py` — `br8n_capture` signature gains (after `hypothesis: str | None = None`):

```python
    next_action: str | None = None,
    thread_id: str | None = None,
```

its `WorkspaceSnapshot(...)` construction gains the same two kwargs as above, and the docstring gains one line:

```
    `next_action` is the single ~two-minute step future-you should do first —
    infer it from the diff/conversation when the user doesn't state one.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/test_capture_surfaces.py tests/test_api_read_surfaces.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/anthonysuherli/Projects/br8n
git add backend/br8n/api/capture.py backend/br8n/interfaces/mcp/server.py backend/tests/test_capture_surfaces.py
git commit -m "feat(capture): expose next_action/thread_id on REST and MCP capture surfaces

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: thread identity on activity-KG task nodes

**Files:**
- Modify: `backend/br8n/knowledge_graph/activity.py` (`activity_extract` ~line 168, `_persist` ~line 216, `_run_activity_update` ~line 261; add `_sync_task_props`; add `import uuid` to the imports block)
- Test: `backend/tests/knowledge_graph/test_thread_identity.py`

**Interfaces:**
- Consumes: Task 2's `snap.next_action`/`snap.thread_id`; existing `store.get_kg_node(kb_id, node_id) -> dict | None` (sync) and `store.update_kg_node(kb_id, node_id, *, properties, ...)` (async, wholesale replace); `store.upsert_kg_nodes` (existing-wins merge).
- Produces: task nodes carry `properties = {"repo", "thread_id", "thread_state": "open", ["next_action"]}`; `_persist` returns `{"nodes", "edges_created", "node_ids"}`; `_sync_task_props(store, kb_id, node_id, fresh: dict) -> None` (read-merge-write; first-minted `thread_id` wins, `next_action` refreshes).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/knowledge_graph/test_thread_identity.py`:

```python
"""Task nodes mint a stable thread identity; repeat captures converge, next_action refreshes."""

from __future__ import annotations

import pytest

from br8n.capture.models import WorkspaceSnapshot
from br8n.knowledge_graph.activity import _persist, _sync_task_props, activity_extract
from br8n.store import SQLiteStore


@pytest.fixture
def store() -> SQLiteStore:
    return SQLiteStore(":memory:")


def _snap(**kw) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        project_path="/repo/br8n", trigger="manual", captured_at="2026-07-18T10:00:00Z",
        hypothesis="fix auth race", **kw,
    )


def _task_node(extraction):
    return next(n for n in extraction.nodes if n.type == "task")


async def test_extract_mints_thread_identity(monkeypatch):
    monkeypatch.setenv("BR8N_ACTIVITY_LLM", "0")
    ex = await activity_extract(_snap(next_action="rerun test_auth.py"), "f1")
    props = _task_node(ex).properties
    assert props["thread_state"] == "open"
    assert len(props["thread_id"]) == 32  # uuid4().hex
    assert props["next_action"] == "rerun test_auth.py"


async def test_extract_honors_supplied_thread_id(monkeypatch):
    monkeypatch.setenv("BR8N_ACTIVITY_LLM", "0")
    ex = await activity_extract(_snap(thread_id="t-known"), "f1")
    assert _task_node(ex).properties["thread_id"] == "t-known"


async def test_repeat_capture_converges_and_refreshes_next_action(store, monkeypatch):
    monkeypatch.setenv("BR8N_ACTIVITY_LLM", "0")

    ex1 = await activity_extract(_snap(next_action="step one"), "f1")
    r1 = await _persist(store, "org", "akb", ex1)
    idx1 = ex1.nodes.index(_task_node(ex1))
    node_id = r1["node_ids"][idx1]
    await _sync_task_props(store, "akb", node_id, _task_node(ex1).properties)
    first_thread = store.get_kg_node("akb", node_id)["properties"]["thread_id"]

    ex2 = await activity_extract(_snap(next_action="step two"), "f2")
    r2 = await _persist(store, "org", "akb", ex2)
    idx2 = ex2.nodes.index(_task_node(ex2))
    assert r2["node_ids"][idx2] == node_id  # same label → same node (dedupe)
    await _sync_task_props(store, "akb", node_id, _task_node(ex2).properties)

    props = store.get_kg_node("akb", node_id)["properties"]
    assert props["thread_id"] == first_thread      # first mint wins
    assert props["next_action"] == "step two"      # refreshed
    assert props["thread_state"] == "open"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/knowledge_graph/test_thread_identity.py -v`
Expected: FAIL — `ImportError: cannot import name '_sync_task_props'` (and no `thread_id` prop).

- [ ] **Step 3: Implement**

In `backend/br8n/knowledge_graph/activity.py`:

(a) Add `import uuid` alongside the existing `import os` / `import re` imports.

(b) `activity_extract` — replace the task-node block (lines 168–172) with:

```python
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
```

(c) `_persist` — return node ids so the caller can address the task node. Change the two returns:

```python
    if not extraction.nodes:
        return {"nodes": 0, "edges_created": 0, "node_ids": []}
```

and the final return:

```python
    return {"nodes": len(node_ids), "edges_created": edges_created, "node_ids": node_ids}
```

(d) New helper after `_persist`:

```python
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
```

(e) `_run_activity_update` — after the `result = await _persist(...)` line, before the `logger.info`:

```python
        task_idx = next(
            (i for i, n in enumerate(extraction.nodes) if n.type == "task"), None
        )
        if task_idx is not None and task_idx < len(result["node_ids"]):
            await _sync_task_props(
                store, kb_id, result["node_ids"][task_idx],
                extraction.nodes[task_idx].properties,
            )
```

(The surrounding `try/except Exception` already makes this best-effort.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/knowledge_graph/test_thread_identity.py tests/test_activity_extract.py tests/test_activity_flow.py -v`
Expected: PASS (existing activity tests may assert task props `== {"repo": repo}` — if one fails on the new keys, update that assertion to check `props["repo"]` membership instead; that is the only sanctioned edit to existing tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/anthonysuherli/Projects/br8n
git add backend/br8n/knowledge_graph/activity.py backend/tests/knowledge_graph/test_thread_identity.py
git commit -m "feat(activity): mint stable thread_id + open state on task nodes, refresh next_action

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: next-action-first resume (helper, JSON card, MCP tool, session primer)

**Files:**
- Modify: `backend/br8n/agent/resume.py` (add `latest_next_action` helper at module end)
- Modify: `backend/br8n/api/resume.py` (`ResumeCardJSON` ~line 41, `_assemble_json` ~line 127)
- Modify: `backend/br8n/interfaces/mcp/server.py` (`br8n_resume` ~line 310)
- Modify: `backend/br8n/agent/session_primer.py` (`build_session_primer`)
- Test: `backend/tests/test_next_action_resume.py`

**Interfaces:**
- Consumes: Task 1's `metadata` in `list_findings` rows.
- Produces: `latest_next_action(store, kb_id) -> tuple[str | None, str | None]` (`(next_action, thread_id)` from the newest `snapshot`/`note` finding carrying one; `(None, None)` on absence or any error). `ResumeCardJSON` and the `br8n_resume` return dict gain `next_action` and `thread_id`. The session primer emits a `<next-action>…</next-action>` element when present. `_assemble_json` prefers `metadata["hypothesis"]` over title-sniff.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_next_action_resume.py`:

```python
"""latest_next_action precedence + resume-card wiring (fake store, no I/O)."""

from __future__ import annotations

from br8n.agent.resume import latest_next_action
from br8n.api.resume import _assemble_json


class _FakeStore:
    def __init__(self, findings):
        self._findings = findings

    def list_findings(self, kb_id, category=None, limit=None):
        rows = [f for f in self._findings if category is None or f["category"] == category]
        return {"count": len(rows), "findings": rows[: limit or 20]}

    def load_synopsis(self, kb_id):
        return None


def _f(category, title, metadata=None, created_at="2026-07-18T10:00:00Z"):
    return {"id": "x", "title": title, "category": category, "confidence": 1.0,
            "tags": [], "metadata": metadata, "created_at": created_at}


def test_latest_next_action_prefers_newest_carrier():
    store = _FakeStore([  # list is newest-first, mirroring the real stores
        _f("snapshot", "no action here", metadata={"hypothesis": "h2"}),
        _f("note", "older note", metadata={"next_action": "rerun tests", "thread_id": "t-9"}),
    ])
    assert latest_next_action(store, "kb1") == ("rerun tests", "t-9")


def test_latest_next_action_none_when_absent():
    store = _FakeStore([_f("snapshot", "t", metadata=None)])
    assert latest_next_action(store, "kb1") == (None, None)


def test_latest_next_action_swallows_store_errors():
    class _Boom:
        def list_findings(self, *a, **k):
            raise RuntimeError("down")

    assert latest_next_action(_Boom(), "kb1") == (None, None)


def test_assemble_json_carries_next_action_and_metadata_hypothesis():
    store = _FakeStore([
        _f("snapshot", "Working on auth.py",  # generic title — sniff would return None
           metadata={"hypothesis": "fix auth race", "next_action": "rerun test_auth.py", "thread_id": "t-1"}),
    ])
    card = _assemble_json(
        store, "kb1", coverage="sparse", preamble_xml="<preamble/>",
        project="br8n", kb="main", snapshot_count=1, activity=[],
    )
    assert card.hypothesis == "fix auth race"
    assert card.next_action == "rerun test_auth.py"
    assert card.thread_id == "t-1"


def test_assemble_json_falls_back_to_title_sniff_for_legacy_rows():
    store = _FakeStore([_f("snapshot", "fix auth race", metadata=None)])
    card = _assemble_json(
        store, "kb1", coverage="sparse", preamble_xml="<preamble/>",
        project="br8n", kb="main", snapshot_count=1, activity=[],
    )
    assert card.hypothesis == "fix auth race"
    assert card.next_action is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/test_next_action_resume.py -v`
Expected: FAIL — `ImportError: cannot import name 'latest_next_action'`.

- [ ] **Step 3: Implement the helper**

Append to `backend/br8n/agent/resume.py`:

```python
# How many recent findings to scan for a structured next_action. Small: the
# carrier is almost always the latest snapshot or session note.
_NEXT_ACTION_SCAN = 10


def latest_next_action(store, kb_id: str) -> tuple[str | None, str | None]:
    """(next_action, thread_id) from the newest snapshot/note carrying one.

    Best-effort: any store error or absent metadata degrades to (None, None) —
    resume surfaces render exactly as they did before this field existed."""
    try:
        rows = store.list_findings(kb_id, limit=_NEXT_ACTION_SCAN).get("findings", [])
    except Exception:  # noqa: BLE001 — resume must never fail on this
        return None, None
    for r in rows:
        if r.get("category") not in ("snapshot", "note"):
            continue
        meta = r.get("metadata") or {}
        if meta.get("next_action"):
            return str(meta["next_action"]), meta.get("thread_id")
    return None, None
```

- [ ] **Step 4: Wire the JSON card**

In `backend/br8n/api/resume.py`:

(a) Import: extend the existing `from br8n.agent.resume import resume_preamble` to:

```python
from br8n.agent.resume import latest_next_action, resume_preamble
```

(b) `ResumeCardJSON` — after `hypothesis: str | None = None`:

```python
    next_action: str | None = None
    thread_id: str | None = None
```

(c) `_assemble_json` — replace the `hypothesis = ...` line with:

```python
    meta0 = (snaps[0].get("metadata") or {}) if snaps else {}
    hypothesis = meta0.get("hypothesis") or (
        _hypothesis_from_title(snaps[0].get("title") or "") if snaps else None
    )
    next_action, thread_id = latest_next_action(store, kb_id)
```

and add to the `ResumeCardJSON(...)` construction:

```python
        next_action=next_action,
        thread_id=thread_id,
```

- [ ] **Step 5: Wire the MCP tool and the primer**

`backend/br8n/interfaces/mcp/server.py` — in `br8n_resume`, before the return add:

```python
    next_action, thread_id = latest_next_action(res.store, res.ctx.kb_id)
```

extend the return dict:

```python
        "next_action": next_action,
        "thread_id": thread_id,
```

and add `latest_next_action` to the existing `from br8n.agent.resume import resume_preamble` import. Update the docstring's return line to `{banner, preamble, coverage, next_action, thread_id, project, kb}` and append: `When next_action is set, lead your resume summary with it ("Do this now: …").`

`backend/br8n/agent/session_primer.py` — import the helper (`from br8n.agent.resume import latest_next_action, resume_preamble`) and, in `build_session_primer`, insert between `parts = [res.preamble]` and the snapshot-lines block:

```python
    next_action, _tid = latest_next_action(res.store, res.ctx.kb_id)
    if next_action:
        parts.append(f"<next-action>{escape(next_action.strip()[:200])}</next-action>")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/test_next_action_resume.py tests/test_api_read_surfaces.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd /Users/anthonysuherli/Projects/br8n
git add backend/br8n/agent/resume.py backend/br8n/api/resume.py backend/br8n/interfaces/mcp/server.py backend/br8n/agent/session_primer.py backend/tests/test_next_action_resume.py
git commit -m "feat(resume): next-action-first resume card, MCP return, and session primer

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: session notes carry `next_action`; Stop-hook directive asks for it

**Files:**
- Modify: `backend/br8n/livingdocs/notes.py` (`persist_note` ~line 38)
- Modify: `backend/br8n/interfaces/mcp/server.py` (`_note_impl` ~line 89, `br8n_note` ~line 108)
- Modify: `hooks/session-note.py` (`build_note_directive` ~line 60)
- Test: `backend/tests/test_note_next_action.py`

**Interfaces:**
- Consumes: Task 1's `metadata` on finding rows; Task 5's `latest_next_action` (notes are already in its scan set — no change needed there).
- Produces: `persist_note(..., next_action: str | None = None)` writes `metadata={"next_action": ...}` on the note Finding; `br8n_note(..., next_action: str | None = None)` passes it through; the Stop-hook directive instructs Claude to supply it.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_note_next_action.py`:

```python
"""persist_note stores next_action as finding metadata (local tier, fake embedder)."""

from __future__ import annotations

import hashlib

import pytest

DIM = 1536


def _fake_vec(text: str) -> list[float]:
    h = hashlib.sha256(text.encode()).digest()
    v = [0.0] * DIM
    for i in range(8):
        v[i] = (h[i] / 255.0) or 0.01
    return v


async def _fake_embed_batch(texts):
    return [_fake_vec(t) for t in texts]


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr("br8n.livingdocs.notes.embed_batch", _fake_embed_batch)
    monkeypatch.setattr("br8n.livingdocs.notes.schedule_rebuild", lambda ctx: None)
    return tmp_path


async def test_note_next_action_round_trips(local_env, tmp_path):
    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.livingdocs.notes import persist_note
    from br8n.store import get_store

    ctx = resolve_tenant("proj", "main", create=True)
    res = await persist_note(
        ctx, project_path=str(tmp_path), kb="main", content="## Decisions\n- x",
        session_id="s1", title="session note", next_action="rerun failing test_auth.py",
    )
    got = get_store(ctx.access_token).get_finding(ctx.kb_id, res["finding_id"])
    assert got["metadata"] == {"next_action": "rerun failing test_auth.py"}


async def test_note_without_next_action_has_no_metadata(local_env, tmp_path):
    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.livingdocs.notes import persist_note
    from br8n.store import get_store

    ctx = resolve_tenant("proj", "main", create=True)
    res = await persist_note(
        ctx, project_path=str(tmp_path), kb="main", content="## Decisions\n- x",
        session_id="s1", title="session note",
    )
    got = get_store(ctx.access_token).get_finding(ctx.kb_id, res["finding_id"])
    assert got["metadata"] is None


def test_directive_mentions_next_action():
    import importlib.util, pathlib

    spec = importlib.util.spec_from_file_location(
        "session_note_hook",
        pathlib.Path(__file__).resolve().parents[2] / "hooks" / "session-note.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = mod.build_note_directive("br8n", "main")
    assert "next_action" in text
```

(If `tests/hooks/test_auto_capture_hook.py` already has a hook-module loader helper, reuse its import idiom instead of the inline `importlib` block — match the existing convention.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/test_note_next_action.py -v`
Expected: FAIL — `TypeError: persist_note() got an unexpected keyword argument 'next_action'`.

- [ ] **Step 3: Implement**

`backend/br8n/livingdocs/notes.py` — `persist_note` signature gains (after `source: str = "agent"`):

```python
    next_action: str | None = None,
```

and the `row` dict gains (after `"provenance"`):

```python
        "metadata": {"next_action": next_action} if next_action else None,
```

`backend/br8n/interfaces/mcp/server.py` — `_note_impl` signature becomes:

```python
async def _note_impl(
    project, kb, project_path, content, session_id, title, captured_at="", source="agent",
    next_action=None,
):
```

passing `next_action=next_action` through to `persist_note`. `br8n_note` gains the parameter (after `source: str = "agent"`):

```python
    next_action: str | None = None,
```

forwards it to `_note_impl`, and its docstring gains: `next_action: the single ~two-minute step future-you should do first (one line).`

`hooks/session-note.py` — in `build_note_directive`, update instruction 5 to:

```python
        "5. Persist it: call mcp__br8n__br8n_note(project, kb, project_path, "
        "content, session_id, title, next_action=<one line: the single ~two-minute "
        "step future-you should do first — concrete and immediately startable, e.g. "
        "'rerun the failing test_auth.py'>) where content is the rendered markdown, "
        "title is a one-line summary, and session_id is this session's id. Omit "
        "next_action only when the session leaves genuinely nothing to pick up.\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/test_note_next_action.py tests/test_livingdocs_notes.py tests/hooks/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/anthonysuherli/Projects/br8n
git add backend/br8n/livingdocs/notes.py backend/br8n/interfaces/mcp/server.py hooks/session-note.py backend/tests/test_note_next_action.py
git commit -m "feat(notes): structured next_action on session notes + Stop-hook directive

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: skills — capture infers next_action; pickup leads with "Do this now"

**Files:**
- Modify: `skills/capture/SKILL.md` (Step 2 and the tool-call arg list)
- Modify: `skills/pickup/SKILL.md` (Step 1 card layout)
- Modify (if present): mirror the same edits in the plugin repo `/Users/anthonysuherli/Repositories/8star/br8n/skills/capture/SKILL.md` and `/Users/anthonysuherli/Repositories/8star/br8n/skills/resume/SKILL.md` where the equivalent sections exist.

**Interfaces:**
- Consumes: Task 3's `br8n_capture(next_action=...)`; Task 5's `br8n_resume` returning `next_action`/`thread_id`.
- Produces: skill behavior only — no code contracts.

- [ ] **Step 1: Edit `skills/capture/SKILL.md`**

In "Step 2 — Write the hypothesis, then capture": after the hypothesis-inference paragraph, add:

```markdown
Also fill **`next_action`** — the single ~two-minute step future-you should do
first (e.g. "rerun the failing test_auth.py", "finish the TODO in adapter.py:49").
If the user stated one, use it verbatim; otherwise **infer it** from the diff and
conversation. Concrete and immediately startable — a command to run or a file:line
to open, never a project-sized goal. Do not ask the user for it.
```

and add `next_action=<the two-minute step>` to the tool-call argument list shown in the skill (the line currently ending `hypothesis=<the one-liner>, project_path=<repo path>`).

- [ ] **Step 2: Edit `skills/pickup/SKILL.md`**

In "Step 1 — Here mode (resume card)", replace the "Lead with the latest `hypothesis`" paragraph with:

```markdown
Lead with the **`next_action`** returned by `br8n_resume` — the card must open
with a single concrete step, not a menu:

> **Do this now:** `<next_action>`
> **You were:** `<latest hypothesis>`

If `next_action` is null (legacy captures), derive a ~two-minute step yourself
from the hypothesis + `git_diff_stat` (e.g. "open `<cursor_file>:<line>` and
re-read the failing branch") and lead with that instead. Never open with a list
of options — one pre-selected action first, supporting context after.
```

- [ ] **Step 3: Mirror into the plugin repo (if sections match)**

Check `/Users/anthonysuherli/Repositories/8star/br8n/skills/capture/SKILL.md` and `.../skills/resume/SKILL.md`. Where the same Step-2 / card-layout sections exist, apply the same two edits verbatim. If the plugin skills have diverged and lack these sections, skip and note it in the commit message.

- [ ] **Step 4: Verify full suite + read-through**

Run: `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/ -x -q`
Expected: all PASS.

Read both edited SKILL.md files end-to-end once: confirm no contradiction remains with the old "offer the obvious next action (resume the hypothesis)" phrasing in pickup's coverage-routing section — if that line survives, reword it to "lead with the next_action (see Step 1)".

- [ ] **Step 5: Commit (both repos)**

```bash
cd /Users/anthonysuherli/Projects/br8n
git add skills/capture/SKILL.md skills/pickup/SKILL.md
git commit -m "feat(skills): capture infers next_action; pickup leads with 'Do this now'

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
cd /Users/anthonysuherli/Repositories/8star/br8n
git add -A skills/ 2>/dev/null && git diff --cached --quiet || git commit -m "feat(skills): mirror next-action-first capture/resume edits from engine

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

(The plugin repo `8star/br8n` was not a git repo at last check — if `git add` fails there, leave the file edits uncommitted and flag it in the final report.)

---

## Acceptance check (after all tasks)

Maps to spec "Acceptance" / vision AC 1:

1. `cd /Users/anthonysuherli/Projects/br8n/backend && .venv/bin/python -m pytest tests/ -q` — full suite green.
2. Manual: in a repo with the br8n plugin active, run `/br8n:capture` (say what you're doing, don't state a next action) → then `/br8n:pickup` → the card opens with **"Do this now: <concrete two-minute step>"** above **"You were: <hypothesis>"**.
3. `br8n_resume` MCP return includes `next_action` and `thread_id` keys (null-safe on a legacy KB).

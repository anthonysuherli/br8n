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

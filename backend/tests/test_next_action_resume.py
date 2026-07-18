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

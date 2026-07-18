"""latest_next_action precedence + resume-card wiring (fake store, no I/O)."""

from __future__ import annotations

from br8n.agent.resume import latest_next_action
from br8n.api.resume import _assemble_json


class _FakeStore:
    def __init__(self, findings):
        self._findings = findings

    def list_findings(self, kb_id, category=None, limit=None):
        rows = [f for f in self._findings if category is None or f["category"] == category]
        rows = sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)
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


def test_latest_next_action_survives_unrelated_category_flood():
    # 20+ newer findings of an unrelated category push the snapshot/note
    # carrier out of any small fixed-size unfiltered scan window.
    flood = [
        _f("insight", f"insight {i}", created_at=f"2026-07-18T{10 + i:02d}:00:00Z")
        for i in range(25)
    ]
    carrier = _f(
        "note", "old note", metadata={"next_action": "rerun tests", "thread_id": "t-1"},
        created_at="2026-07-18T05:00:00Z",
    )
    store = _FakeStore(flood + [carrier])
    assert latest_next_action(store, "kb1") == ("rerun tests", "t-1")


def test_latest_next_action_cross_category_recency_note_wins():
    store = _FakeStore([
        _f("snapshot", "older snapshot",
           metadata={"next_action": "snapshot action", "thread_id": "t-snap"},
           created_at="2026-07-18T09:00:00Z"),
        _f("note", "newer note",
           metadata={"next_action": "note action", "thread_id": "t-note"},
           created_at="2026-07-18T10:00:00Z"),
    ])
    assert latest_next_action(store, "kb1") == ("note action", "t-note")


def test_latest_next_action_cross_category_recency_snapshot_wins():
    store = _FakeStore([
        _f("note", "older note",
           metadata={"next_action": "note action", "thread_id": "t-note"},
           created_at="2026-07-18T09:00:00Z"),
        _f("snapshot", "newer snapshot",
           metadata={"next_action": "snapshot action", "thread_id": "t-snap"},
           created_at="2026-07-18T10:00:00Z"),
    ])
    assert latest_next_action(store, "kb1") == ("snapshot action", "t-snap")


def test_assemble_json_falls_back_to_title_sniff_for_legacy_rows():
    store = _FakeStore([_f("snapshot", "fix auth race", metadata=None)])
    card = _assemble_json(
        store, "kb1", coverage="sparse", preamble_xml="<preamble/>",
        project="br8n", kb="main", snapshot_count=1, activity=[],
    )
    assert card.hypothesis == "fix auth race"
    assert card.next_action is None

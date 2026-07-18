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


def test_whitespace_only_next_action_dropped_from_metadata():
    payload = snapshot_to_finding(_snap(hypothesis="fix auth race", next_action="   "))
    assert payload["metadata"] == {"hypothesis": "fix auth race"}


def test_title_and_content_unchanged_by_new_fields():
    with_na = snapshot_to_finding(_snap(hypothesis="fix auth race", next_action="rerun tests"))
    without = snapshot_to_finding(_snap(hypothesis="fix auth race"))
    assert with_na["title"] == without["title"]
    assert with_na["content"] == without["content"]

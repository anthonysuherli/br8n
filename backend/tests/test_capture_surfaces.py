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

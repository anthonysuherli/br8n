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


# --- regression: the fields must actually reach WorkspaceSnapshot ----------


async def test_rest_capture_threads_next_action_and_thread_id_into_snapshot(monkeypatch):
    """The REST capture() handler must pass next_action/thread_id through to
    WorkspaceSnapshot, not just accept them on SnapshotRequest."""
    import br8n.api.capture as capture_mod
    from br8n.agent.state import Principal

    captured: dict = {}

    async def fake_persist_snapshot(ctx, snap):
        captured["snap"] = snap
        return "finding-1"

    def fake_resolve_tenant(project, kb, *, create=True, principal=None):
        return object()

    def fake_schedule_activity_update(snap, finding_id, *, access_token=None, org_id=None):
        return None

    monkeypatch.setattr(capture_mod, "persist_snapshot", fake_persist_snapshot)
    monkeypatch.setattr(capture_mod, "resolve_tenant", fake_resolve_tenant)
    monkeypatch.setattr(capture_mod, "schedule_activity_update", fake_schedule_activity_update)

    body = capture_mod.SnapshotRequest(
        project="br8n", kb="main", trigger="manual", captured_at="2026-07-18T10:00:00Z",
        next_action="rerun test_auth.py", thread_id="t-1",
    )
    principal = Principal(user_id="local", org_id="local", access_token="")

    res = await capture_mod.capture(body, principal)

    assert res.finding_id == "finding-1"
    snap = captured["snap"]
    assert snap.next_action == "rerun test_auth.py"
    assert snap.thread_id == "t-1"


async def test_mcp_br8n_capture_threads_next_action_and_thread_id_into_snapshot(monkeypatch):
    """The br8n_capture MCP tool must pass next_action/thread_id through to
    WorkspaceSnapshot, not just accept them as tool params."""
    from br8n.interfaces.mcp import server

    captured: dict = {}

    async def fake_persist_snapshot(ctx, snap):
        captured["snap"] = snap
        return "finding-2"

    def fake_resolve_tenant(project, kb, *, create=True):
        return object()

    def fake_schedule_activity_update(snap, finding_id, *, access_token=None, org_id=None):
        return None

    def fake_schedule_timeline(ctx, *, project, project_path, kb):
        return None

    monkeypatch.setattr(server, "persist_snapshot", fake_persist_snapshot)
    monkeypatch.setattr(server, "resolve_tenant", fake_resolve_tenant)
    monkeypatch.setattr(server, "schedule_activity_update", fake_schedule_activity_update)
    monkeypatch.setattr(server, "schedule_timeline", fake_schedule_timeline)

    res = await server.br8n_capture(
        project="br8n", kb="main", trigger="manual", captured_at="2026-07-18T10:00:00Z",
        next_action="rerun test_auth.py", thread_id="t-1",
    )

    assert res["finding_id"] == "finding-2"
    snap = captured["snap"]
    assert snap.next_action == "rerun test_auth.py"
    assert snap.thread_id == "t-1"

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
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests" / "hooks"))
    from test_auto_capture_hook import _load_hook

    hook_path = pathlib.Path(__file__).resolve().parents[2] / "hooks" / "session-note.py"
    mod = _load_hook(hook_path, "session_note_hook")
    text = mod.build_note_directive("br8n", "main")
    assert "next_action" in text

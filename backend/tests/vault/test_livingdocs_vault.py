"""Local-tier notes/journal land in the vault, not the legacy dirs.

Mirrors the harness in test_livingdocs_notes.py / test_journal_tool.py: real
SQLiteStore (via VaultStore) through a tmp BR8N_DB_PATH/BR8N_VAULT_PATH, only
the embedder faked so no OpenAI call is made. `TenantContext` is built via
`resolve_tenant` (its constructor also needs `project_id` + `thread_id`, which
the brief's inline-construction sketch omitted) — the same pattern every
sibling test in this suite already uses.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

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
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))

    import br8n.store as store_pkg

    store_pkg._local_stores.clear()
    yield tmp_path
    store_pkg._local_stores.clear()


@pytest.mark.asyncio
async def test_persist_note_returns_vault_path(env, monkeypatch):
    tmp_path = env
    monkeypatch.setattr("br8n.livingdocs.notes.embed_batch", _fake_embed_batch)
    monkeypatch.setattr("br8n.livingdocs.notes.schedule_rebuild", lambda ctx: None)

    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.livingdocs.notes import persist_note

    ctx = resolve_tenant("br8n", "main", create=True)
    out = await persist_note(
        ctx,
        project_path=str(tmp_path / "repo"),
        kb="main",
        content="session summary",
        session_id="s1",
        title="Session note",
    )

    assert out["finding_id"]
    assert str(tmp_path / "vault" / "notes") in out["note_path"]
    assert not (tmp_path / "repo" / ".br8n" / "notes").exists()
    assert Path(out["note_path"]).exists()


@pytest.mark.asyncio
async def test_persist_journal_returns_vault_path(env, monkeypatch):
    tmp_path = env
    monkeypatch.setattr("br8n.livingdocs.journal.embed_batch", _fake_embed_batch)

    from br8n.constants import JOURNAL_SCOPE
    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.livingdocs.journal import persist_journal

    ctx = resolve_tenant(JOURNAL_SCOPE, JOURNAL_SCOPE, create=True)
    out = await persist_journal(
        ctx, text="learned that X composes cleanly", type="insight", tags=["arch"],
    )

    assert out["finding_id"]
    assert str(tmp_path / "vault" / "journal") in out["entry_path"]
    assert Path(out["entry_path"]).exists()
    # the legacy global journal dir gets no new file
    assert not (tmp_path / "journal").exists()

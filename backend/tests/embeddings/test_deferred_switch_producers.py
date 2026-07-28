"""The deferred-switch window (spec AC2, amended 2026-07-28) must be
inhabitable: while a pending auto-detected embedding-space switch waits for
user confirmation, producers (capture, semantic search) must degrade to
keyless behavior rather than crash on a dimension mismatch between the NEW
identity ``active_embedder()`` reports and the OLD physical width the vec
tables are still at.

Reproduces the real-world sequence: a remote key silently disappears (shell
change, not a ``br8n_embeddings_set`` call), the next store construction
re-detects ``local/384`` auto and defers instead of rebuilding (existing
vectors would be discarded), and the local fastembed model is already
resident (the normal condition — it caches after one download).
"""
from __future__ import annotations

import sys
import types

import pytest

from br8n import settings_file
from br8n.agent.state import TenantContext
from br8n.capture.models import WorkspaceSnapshot
from br8n.capture.service import persist_snapshot
from br8n.clients import embed_local, embeddings
from br8n.store import get_store


class _FakeTextEmbedding:
    """Stands in for fastembed.TextEmbedding — deterministic 384-dim vectors."""

    def __init__(self, **kwargs):
        pass

    def embed(self, texts):
        for i, t in enumerate(texts):
            yield [float(len(t) + i)] * 384


@pytest.fixture
def fake_fastembed(monkeypatch):
    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    embed_local.reset()
    yield
    embed_local.reset()


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr("br8n.capture.service.schedule_rebuild", lambda ctx: None)
    from br8n import store as store_mod
    from br8n.config import get_config, get_settings

    store_mod._local_stores.clear()
    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()
    yield
    store_mod._local_stores.clear()
    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()


def _ctx(kb_id: str = "kb-1") -> TenantContext:
    return TenantContext(
        user_id="local", org_id="local", project_id="p-1",
        kb_id=kb_id, thread_id="t-1", access_token="",
    )


def _snap(hypothesis: str) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        project_path="/code/br8n", trigger="manual",
        captured_at="2026-07-28T10:00:00Z", branch="main", hypothesis=hypothesis,
    )


async def _stamp_pending_switch(monkeypatch, kb_id: str, note: str):
    """Build the deferred window: stamp remote/1536 with a live vector, then
    reconstruct the store with active_embedder() re-detecting local/384 auto
    — _sync_embedding_space defers (existing vectors would be discarded)
    instead of rebuilding. Returns the reconstructed store, already in the
    deferred state (pending_embedding_switch() is non-None)."""
    from br8n import store as store_mod

    old_ident = embeddings.EmbedderIdentity(
        "remote", "text-embedding-3-small", 1536, "auto"
    )
    monkeypatch.setattr(embeddings, "active_embedder", lambda: old_ident)
    store = get_store("")
    await store.insert_findings([{
        "kb_id": kb_id, "title": note, "content": note, "category": "note",
        "confidence": 1.0, "tags": [], "provenance": [], "embedding": [0.1] * 1536,
    }])

    new_ident = embeddings.EmbedderIdentity(
        "local", "BAAI/bge-small-en-v1.5", 384, "auto"
    )
    store_mod._local_stores.clear()  # simulate a fresh process/store construction
    monkeypatch.setattr(embeddings, "active_embedder", lambda: new_ident)
    store = get_store("")
    assert store.pending_embedding_switch() is not None, (
        "test setup did not reach the deferred state"
    )
    return store


# --- 1. the missing regression guard: a live producer during the deferred
# window must degrade, not crash ---------------------------------------------


@pytest.mark.asyncio
async def test_capture_succeeds_during_a_pending_switch(local_env, fake_fastembed, monkeypatch):
    embed_local.load_now()  # the model is resident — the normal condition
    await _stamp_pending_switch(monkeypatch, "kb-1", "old remote note")

    finding_id = await persist_snapshot(_ctx(), _snap("mid-refactor"))

    assert finding_id
    store = get_store("")
    row = store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (finding_id,)
    ).fetchone()
    assert row["needs_embed"] == 1, "deferred-window capture must flag needs_embed, not embed at 384"


@pytest.mark.asyncio
async def test_semantic_search_degrades_during_a_pending_switch(
    local_env, fake_fastembed, monkeypatch
):
    embed_local.load_now()
    store = await _stamp_pending_switch(monkeypatch, "kb-1", "release prep")

    from br8n.agent.preamble import select_preamble

    preamble, coverage = await select_preamble(
        "where was I", store=store, kb_id="kb-1"
    )

    assert "<finding" in preamble, "keyless-style degrade returned an empty card"
    assert "release prep" in preamble
    assert coverage == "sparse"  # recency fallback, not similarity-ranked


# --- 2. confirming the switch drains the deferred rows — the degrade is
# temporary, not a dead end ---------------------------------------------------


@pytest.mark.asyncio
async def test_confirming_the_switch_drains_deferred_rows_and_becomes_searchable(
    local_env, fake_fastembed, monkeypatch
):
    embed_local.load_now()
    await _stamp_pending_switch(monkeypatch, "kb-1", "old remote note")

    finding_id = await persist_snapshot(_ctx(), _snap("mid-refactor"))
    store = get_store("")
    assert store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (finding_id,)
    ).fetchone()["needs_embed"] == 1

    # The user confirms the switch (what br8n_embeddings_set("local") does):
    # the SAME local/384 identity, now reported as an explicit choice.
    confirmed_ident = embeddings.EmbedderIdentity(
        "local", "BAAI/bge-small-en-v1.5", 384, "settings"
    )
    monkeypatch.setattr(embeddings, "active_embedder", lambda: confirmed_ident)
    store.resync_embedding_space()

    assert store.embedding_space() == {
        "provider": "local", "model": "BAAI/bge-small-en-v1.5", "dim": 384,
    }
    assert store.pending_embedding_switch() is None

    from br8n.agent.preamble import select_preamble

    preamble, _coverage = await select_preamble(
        "mid-refactor", store=store, kb_id="kb-1"
    )

    assert "mid-refactor" in preamble, "drained row did not become searchable"
    remaining = store._conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE needs_embed = 1;"
    ).fetchone()["n"]
    assert remaining == 0, "confirming the switch must drain every flagged row"


# --- 3. Fix B: insert_findings must not leak an orphan row on a mid-insert
# failure ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_findings_rolls_back_on_vec_insert_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    from br8n.store.sqlite import SQLiteStore

    store = SQLiteStore(str(tmp_path / "brain.db"))
    assert store._declared_vec_dim() == 1536  # the width the bad row below will violate

    with pytest.raises(Exception):
        await store.insert_findings([{
            "kb_id": "kb-1", "title": "t", "content": "c", "category": "note",
            "confidence": 1.0, "tags": [], "provenance": [],
            "embedding": [0.1] * 10,  # wrong width -> vec_findings insert raises
        }])

    # The next unrelated commit on the shared connection must not resurrect
    # the orphaned findings row from the failed insert above.
    store._conn.execute(
        "INSERT INTO findings (id, org_id, kb_id, title, content, category, "
        "confidence, tags, provenance, metadata, created_at) VALUES "
        "('other', 'local', 'kb-1', 'x', 'y', 'note', 1.0, '[]', '[]', NULL, 'now');"
    )
    store._conn.commit()

    n = store._conn.execute("SELECT COUNT(*) AS n FROM findings;").fetchone()["n"]
    assert n == 1, "the failed insert left an orphan findings row"
    store.close()

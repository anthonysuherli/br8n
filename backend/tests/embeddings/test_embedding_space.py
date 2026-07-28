"""One active embedding space: stamping, first-stamp inference, rebuild on change."""
import pytest

from br8n.clients.embeddings import EmbedderIdentity
from br8n.store.sqlite import SQLiteStore


def _ident(provider="remote", model="text-embedding-3-small", dim=1536):
    return EmbedderIdentity(provider, model, dim, "auto")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    return str(tmp_path / "brain.db")


def _store(db, monkeypatch, ident):
    monkeypatch.setattr(
        "br8n.clients.embeddings.active_embedder", lambda: ident
    )
    return SQLiteStore(db)


@pytest.mark.asyncio
async def test_fresh_db_stamps_active_identity(db, monkeypatch):
    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    assert store.embedding_space() == {
        "provider": "local", "model": "BAAI/bge-small-en-v1.5", "dim": 384
    }
    assert store._declared_vec_dim() == 384
    store.close()


@pytest.mark.asyncio
async def test_same_identity_does_not_rebuild(db, monkeypatch):
    ident = _ident()
    store = _store(db, monkeypatch, ident)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    store.close()

    store = _store(db, monkeypatch, ident)
    flag = store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["needs_embed"]
    assert not flag  # untouched
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_findings;"
    ).fetchone()["n"] == 1
    store.close()


@pytest.mark.asyncio
async def test_dim_change_rebuilds_and_flags_everything(db, monkeypatch):
    store = _store(db, monkeypatch, _ident())
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    await store.upsert_kg_nodes(
        kb_id, [{"type": "Repo", "label": "r", "properties": {}, "grounded_in": [],
                 "embedding": [0.1] * 1536}]
    )
    store.close()

    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    assert store._declared_vec_dim() == 384
    assert store.embedding_space()["dim"] == 384
    assert store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["needs_embed"] == 1
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM kg_nodes WHERE needs_embed = 1;"
    ).fetchone()["n"] == 1
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_findings;"
    ).fetchone()["n"] == 0  # stale vectors dropped, not mixed
    store.close()


@pytest.mark.asyncio
async def test_legacy_db_infers_width_instead_of_trusting_active(db, monkeypatch):
    """A key removed between runs must NOT stamp 384 over 1536-dim vectors."""
    store = _store(db, monkeypatch, _ident())
    store._conn.execute("DELETE FROM embedding_space;")  # simulate pre-feature db
    store._conn.commit()
    store.close()

    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    # inferred 1536, saw a mismatch, rebuilt to 384
    assert store._declared_vec_dim() == 384
    assert store.embedding_space()["dim"] == 384
    store.close()


@pytest.mark.asyncio
async def test_legacy_db_with_matching_dim_is_adopted_without_rebuild(db, monkeypatch):
    ident = _ident()
    store = _store(db, monkeypatch, ident)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    store._conn.execute("DELETE FROM embedding_space;")
    store._conn.commit()
    store.close()

    store = _store(db, monkeypatch, ident)  # same remote identity
    assert store.embedding_space() == {
        "provider": "remote", "model": "text-embedding-3-small", "dim": 1536
    }
    assert store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["needs_embed"] in (None, 0)  # no needless re-embed
    store.close()


@pytest.mark.asyncio
async def test_provider_none_never_rebuilds(db, monkeypatch):
    """Losing the key entirely must not throw away vectors you might restore."""
    store = _store(db, monkeypatch, _ident())
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    store.close()

    store = _store(db, monkeypatch, EmbedderIdentity("none", "", 0, "auto"))
    assert store._declared_vec_dim() == 1536
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_findings;"
    ).fetchone()["n"] == 1
    store.close()


def test_sync_failure_never_blocks_construction(db, monkeypatch):
    monkeypatch.setattr(
        "br8n.clients.embeddings.active_embedder",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    store = SQLiteStore(db)  # must not raise
    assert store._declared_vec_dim() is not None  # vec tables still exist
    store.close()

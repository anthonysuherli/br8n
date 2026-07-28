"""One active embedding space: stamping, first-stamp inference, rebuild on change."""
import logging
import sqlite3

import pytest
import sqlite_vec

from br8n.clients.embeddings import EmbedderIdentity
from br8n.store.sqlite import SQLiteStore


def _raw_connect(db: str) -> sqlite3.Connection:
    """Raw sqlite3 connection with sqlite-vec loaded, for corrupting a DB
    outside SQLiteStore (DROP on a vec0 virtual table needs the module)."""
    conn = sqlite3.connect(db)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


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


def test_config_failure_never_blocks_construction_on_fresh_db(db, monkeypatch):
    """I1: a malformed config.yaml / bad B2_EMBEDDING__DIM override must not
    stop a FRESH store from opening. Before this task ``sqlite.py`` never
    imported ``br8n.config`` at all, so this is a new coupling; the first two
    statements of ``_sync_embedding_space`` (declared/fallback) sat above the
    ``try:``, so ``or get_config()...`` raised straight through construction
    on exactly the fresh-DB / missing-vec_findings path."""
    monkeypatch.setattr(
        "br8n.store.sqlite.get_config",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    store = SQLiteStore(db)  # must not raise
    assert store._declared_vec_dim() is not None  # vec tables still exist and usable
    store.close()


def test_unparseable_vec_table_does_not_stamp_active_identity(db, monkeypatch):
    """M2: when vec_findings exists but its width can't be parsed (a future/
    foreign schema, not our own float[N] spelling), the sync must not guess —
    it must leave the existing (unknown-width) space alone rather than
    stamping the active identity blindly over vectors of unknown width."""
    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    store.close()

    # Corrupt: drop the stamp (simulate `stored is None`) and replace
    # vec_findings with a table that carries no parseable width at all.
    conn = _raw_connect(db)
    conn.execute("DELETE FROM embedding_space;")
    conn.execute("DROP TABLE vec_findings;")
    conn.execute("CREATE TABLE vec_findings (finding_id TEXT, embedding BLOB);")
    conn.commit()
    conn.close()

    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    assert store.embedding_space() is None  # left alone, not blindly stamped
    store.close()


def test_missing_vec_table_prefers_stamped_dim_over_config_default(db, monkeypatch):
    """M3: when vec_findings is absent but an embedding_space stamp survives,
    the safe-width fallback must prefer the STAMP over config.embedding.dim
    (1536 default) — otherwise a matching active identity never triggers the
    mismatch/rebuild branch and every insert raises on width."""
    ident = _ident("local", "BAAI/bge-small-en-v1.5", 384)
    store = _store(db, monkeypatch, ident)
    assert store.embedding_space()["dim"] == 384
    store.close()

    # Simulate a partial migration: vec_findings dropped, stamp survives.
    conn = _raw_connect(db)
    conn.execute("DROP TABLE vec_findings;")
    conn.commit()
    conn.close()

    # Reopen with the SAME identity (matches the stamp) — config.embedding.dim
    # defaults to 1536, which must not win over the still-valid 384 stamp.
    store = _store(db, monkeypatch, ident)
    assert store._declared_vec_dim() == 384
    store.close()


@pytest.mark.asyncio
async def test_auto_rebuild_logs_warning_with_spaces_and_queued_count(db, monkeypatch, caplog):
    """Also-add: a rebuild triggered while the provider was auto-detected
    (not explicitly configured) must log at WARNING, naming both spaces and
    the number of rows queued for re-embed — this is the silent-corpus-drop
    case (shell without AI_GATEWAY_API_KEY flips remote/1536 -> local/384)."""
    store = _store(db, monkeypatch, _ident())  # remote/1536, source="auto"
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    store.close()

    caplog.set_level(logging.WARNING, logger="br8n.store.sqlite")
    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    store.close()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    msg = " ".join(r.getMessage() for r in warnings)
    assert "text-embedding-3-small" in msg  # old space named
    assert "BAAI/bge-small-en-v1.5" in msg  # new space named
    assert "1" in msg  # 1 row queued for re-embed

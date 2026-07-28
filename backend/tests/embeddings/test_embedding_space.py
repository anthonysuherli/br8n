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


def _ident(provider="remote", model="text-embedding-3-small", dim=1536, source="auto"):
    return EmbedderIdentity(provider, model, dim, source)


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
    """Explicit source (settings/config): the work-at-risk gate (Change B)
    only ever defers AUTO-detected changes, so an explicit choice still
    rebuilds immediately and flags everything, exactly as before."""
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

    store = _store(
        db, monkeypatch,
        _ident("local", "BAAI/bge-small-en-v1.5", 384, source="settings"),
    )
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
async def test_auto_change_with_vectors_present_defers_and_warns(db, monkeypatch, caplog):
    """Change B (spec AC2, amended 2026-07-28): an AUTO-detected change that
    would DISCARD existing vectors must be deferred, not silently rebuilt —
    this is exactly the silent-corpus-drop case (shell without
    AI_GATEWAY_API_KEY flips remote/1536 -> local/384). The space is left
    exactly as it is; a WARNING names both spaces so the deferral is
    observable, but nothing is dropped or re-flagged."""
    store = _store(db, monkeypatch, _ident())  # remote/1536, source="auto"
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    store.close()

    caplog.set_level(logging.WARNING, logger="br8n.store.sqlite")
    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    msg = " ".join(r.getMessage() for r in warnings)
    assert "text-embedding-3-small" in msg  # old space named
    assert "BAAI/bge-small-en-v1.5" in msg  # new space named

    # NO rebuild happened: stamp and physical width unchanged, the existing
    # vector is still there, and the row was never flagged needs_embed.
    assert store._declared_vec_dim() == 1536
    assert store.embedding_space() == {
        "provider": "remote", "model": "text-embedding-3-small", "dim": 1536
    }
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_findings;"
    ).fetchone()["n"] == 1
    assert store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["needs_embed"] in (None, 0)
    store.close()


@pytest.mark.asyncio
async def test_explicit_change_with_vectors_present_rebuilds_immediately(db, monkeypatch):
    """Change B's gate only ever blocks AUTO-detected changes. An EXPLICIT
    choice (settings/config) with vectors present still rebuilds right away,
    exactly as before the amendment — this is br8n_embeddings_set's path."""
    store = _store(db, monkeypatch, _ident())  # remote/1536, source="auto"
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    store.close()

    store = _store(
        db, monkeypatch,
        _ident("local", "BAAI/bge-small-en-v1.5", 384, source="settings"),
    )
    assert store._declared_vec_dim() == 384
    assert store.embedding_space()["dim"] == 384
    assert store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["needs_embed"] == 1
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_findings;"
    ).fetchone()["n"] == 0  # stale vectors dropped, not mixed
    store.close()


@pytest.mark.asyncio
async def test_auto_change_with_no_vectors_rebuilds_immediately(db, monkeypatch):
    """AC1 must not regress: a keyless machine can hold many findings with
    real content and ZERO vectors (never embedded). 'Would discard work'
    is judged from the vec tables' actual row count, not findings' row
    count — otherwise a fresh keyless install would wedge waiting for a
    confirmation nobody can give."""
    store = _store(db, monkeypatch, _ident())  # remote/1536, source="auto"
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    for i in range(5):
        await store.insert_findings(
            [{"kb_id": kb_id, "title": f"t{i}", "content": f"c{i}", "category": "note",
              "confidence": 1.0, "tags": [], "provenance": []}]  # no embedding
        )
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_findings;"
    ).fetchone()["n"] == 0
    store.close()

    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    assert store._declared_vec_dim() == 384
    assert store.embedding_space()["dim"] == 384
    store.close()


@pytest.mark.asyncio
async def test_wedge_self_heals_on_next_open(db, monkeypatch):
    """Change A: a stamp-only mismatch check leaves a permanent wedge. If
    ``_rebuild_vec_tables`` commits (tables recreated at the new width) but
    ``_stamp_embedding_space`` then fails — a transient cross-process lock
    is realistic, since /br8n:capture sweeps every live Claude Code session
    against the same DB — the stamp is stuck at the OLD identity while the
    tables are already at the NEW width. If the caller then reverts to the
    old provider, the active identity matches the STALE stamp exactly, so a
    stamp-only comparison would never re-fire. This hand-constructs that
    exact partially-applied state (tables at 384, stamp at 1536, identity
    matching the stamp, vec tables empty) and asserts the next open
    self-heals: physical width and stamp both converge on the active
    identity, and an insert at that width succeeds. The composed gate does
    not block this even though the identity's source is "auto": the vec
    tables were just recreated and hold no rows, so nothing is discarded."""
    ident = _ident()  # remote / text-embedding-3-small / 1536, source="auto"
    store = _store(db, monkeypatch, ident)
    store.close()

    conn = _raw_connect(db)
    conn.executescript(
        "DROP TABLE vec_findings; DROP TABLE vec_kg_nodes;"
        "CREATE VIRTUAL TABLE vec_findings USING vec0(finding_id TEXT, embedding float[384]);"
        "CREATE VIRTUAL TABLE vec_kg_nodes USING vec0(node_id TEXT, embedding float[384]);"
    )
    conn.commit()
    conn.close()

    store = _store(db, monkeypatch, ident)  # identity matches the STALE stamp
    assert store._declared_vec_dim() == 1536
    assert store.embedding_space() == {
        "provider": "remote", "model": "text-embedding-3-small", "dim": 1536
    }

    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    assert fid
    store.close()


@pytest.mark.asyncio
async def test_clean_install_auto_rebuild_no_warning(db, monkeypatch, caplog):
    """I1: Fresh empty DB with auto-resolved local identity must NOT emit a
    WARNING, even though a rebuild happens. The auto-rebuild WARNING exists to
    announce silent corpus drops (remote key lost -> flip to local 384), not to
    cry wolf on clean installs where n_queued = 0."""
    caplog.set_level(logging.DEBUG, logger="br8n.store.sqlite")
    # Open fresh DB: with local/384 auto-resolved identity, the DB initializes
    # at config.embedding.dim (1536), sees 1536 != 384, and rebuilds to 384.
    # On an empty DB, n_queued = 0 — so INFO, not WARNING.
    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))

    # Verify no WARNING was emitted
    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "embedding space" in r.getMessage()
    ]
    assert len(warnings) == 0, (
        f"Expected no WARNING, got: {[r.getMessage() for r in warnings]}"
    )

    # Verify rebuild and stamp still happened (the most important part)
    assert store._declared_vec_dim() == 384
    assert store.embedding_space()["dim"] == 384
    assert store.embedding_space()["model"] == "BAAI/bge-small-en-v1.5"
    store.close()


def test_unparseable_vec_table_logs_warning_before_returning(db, monkeypatch, caplog):
    """M2: When vec_findings exists but its DDL is unparseable (can't extract
    width), log a WARNING explaining the dead end before returning without
    stamping or rebuilding."""
    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    store.close()

    # Corrupt: drop the stamp and replace vec_findings with unparseable table
    conn = _raw_connect(db)
    conn.execute("DELETE FROM embedding_space;")
    conn.execute("DROP TABLE vec_findings;")
    conn.execute("CREATE TABLE vec_findings (finding_id TEXT, embedding BLOB);")
    conn.commit()
    conn.close()

    # Reopen and verify warning is logged
    caplog.set_level(logging.WARNING, logger="br8n.store.sqlite")
    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))

    # Verify WARNING was emitted about unparseable DDL
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    msg = " ".join(r.getMessage() for r in warnings)
    assert "unparseable" in msg.lower() or "cannot determine" in msg.lower(), \
        f"Expected unparseable DDL warning, got: {msg}"

    # Verify the space was NOT stamped (left alone)
    assert store.embedding_space() is None

    # Verify both vec tables still exist (no corruption, just left as-is)
    assert store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_findings';"
    ).fetchone() is not None
    assert store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_kg_nodes';"
    ).fetchone() is not None

    store.close()


# --- pending_embedding_switch (Change B surfacing) --------------------------


def test_pending_switch_none_when_nothing_pending(db, monkeypatch):
    store = _store(db, monkeypatch, _ident())
    assert store.pending_embedding_switch() is None
    store.close()


@pytest.mark.asyncio
async def test_pending_switch_reports_the_deferred_state(db, monkeypatch):
    """Read-only: computed on demand by comparing the stamp/physical space to
    the CURRENTLY active identity, with no new persisted state — matches
    whatever _sync_embedding_space would have deferred."""
    store = _store(db, monkeypatch, _ident())  # remote/1536, source="auto"
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )

    local_ident = _ident("local", "BAAI/bge-small-en-v1.5", 384)  # source="auto"
    monkeypatch.setattr("br8n.clients.embeddings.active_embedder", lambda: local_ident)

    switch = store.pending_embedding_switch()
    assert switch == {
        "stored": {"provider": "remote", "model": "text-embedding-3-small", "dim": 1536},
        "detected": {"provider": "local", "model": "BAAI/bge-small-en-v1.5", "dim": 384},
    }
    # Read-only: nothing was touched by asking.
    assert store.embedding_space()["dim"] == 1536
    assert store._declared_vec_dim() == 1536
    store.close()


@pytest.mark.asyncio
async def test_pending_switch_none_when_source_is_explicit(db, monkeypatch):
    """An explicit mismatch is never "pending" — br8n_embeddings_set applies
    it immediately, so it can't linger for br8n_embeddings_get to report."""
    store = _store(db, monkeypatch, _ident())  # remote/1536, source="auto"
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )

    local_ident = _ident("local", "BAAI/bge-small-en-v1.5", 384, source="settings")
    monkeypatch.setattr("br8n.clients.embeddings.active_embedder", lambda: local_ident)

    assert store.pending_embedding_switch() is None
    store.close()


@pytest.mark.asyncio
async def test_stored_auto_plus_env_flip_defers_instead_of_destroying(db, monkeypatch):
    """Regression for the hole in ``_requested()``: a stored literal "auto"
    (what ``br8n_embeddings_set("auto")`` persists) must be reported as
    source="auto", not "settings" — otherwise every later environment-driven
    provider flip for a user who ever explicitly selected auto mode is
    misclassified as an "explicit" choice, and the work-at-risk gate below
    rebuilds (destroying vectors) instead of deferring. Unlike the other
    gate tests in this file, this one does NOT inject an ``EmbedderIdentity``
    directly — it drives the real ``active_embedder()`` through
    ``settings_file`` + ``_creds_present``/``_local_eligible``, so it
    exercises ``embeddings.py`` and ``sqlite.py`` together end to end,
    exactly the seam where the hole lived."""
    from br8n import settings_file
    from br8n.clients import embeddings

    monkeypatch.setenv("BR8N_DB_PATH", db)
    settings_file.clear_cache()

    settings_file.save_setting("embedding_provider", "auto")

    # Environment #1: a remote key present -> auto-detects to remote/1536.
    monkeypatch.setattr(embeddings, "_creds_present", lambda: True)
    monkeypatch.setattr(embeddings, "_local_eligible", lambda: True)
    store = SQLiteStore(db)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    assert store.embedding_space() == {
        "provider": "remote", "model": "text-embedding-3-small", "dim": 1536
    }
    store.close()

    # Environment #2: the key silently goes missing (shell change, not a
    # br8n_embeddings_set call) -> auto-detection now resolves to
    # local/384. "auto" is still what's persisted in settings throughout.
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    monkeypatch.setattr(embeddings, "_local_eligible", lambda: True)
    store = SQLiteStore(db)

    # Must DEFER, not rebuild: stamp and physical vec width unchanged, the
    # existing vector is still present, and nothing was flagged for
    # re-embed. Before the fix, source="settings" made this look like an
    # explicit choice, so it rebuilt immediately and silently dropped the
    # vector below instead.
    assert store._declared_vec_dim() == 1536
    assert store.embedding_space() == {
        "provider": "remote", "model": "text-embedding-3-small", "dim": 1536
    }
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_findings;"
    ).fetchone()["n"] == 1
    assert store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["needs_embed"] in (None, 0)
    store.close()

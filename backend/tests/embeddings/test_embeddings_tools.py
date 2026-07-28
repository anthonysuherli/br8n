"""br8n_embeddings_get/set: reporting, refusals, live switching."""
import pytest

from br8n import settings_file
from br8n.interfaces.mcp import server


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()
    yield
    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()


def test_get_reports_identity_and_source(monkeypatch):
    monkeypatch.setattr("br8n.clients.embeddings._creds_present", lambda: True)
    out = server._embeddings_get_impl()
    assert out["provider"] == "remote"
    assert out["dim"] == 1536
    assert out["source"] == "auto"
    assert out["pending_findings"] == 0
    assert out["pending_nodes"] == 0
    assert "extra_installed" in out and "ready" in out
    assert out["pending_switch"] is None  # existing keys unchanged, new key added


async def test_get_reports_pending_switch_when_a_change_is_deferred(monkeypatch):
    """Change B surfacing: when an auto-detected environment change would
    discard existing vectors, _sync_embedding_space defers it rather than
    rebuilding — br8n_embeddings_get must still report that a switch is
    available (stored space, detected space) so /br8n:embeddings can apply
    it, without mutating anything itself."""
    monkeypatch.setattr("br8n.clients.embeddings._creds_present", lambda: True)

    from br8n.store import get_store

    store = get_store()
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    assert store.embedding_space()["dim"] == 1536

    # Simulate the key silently going missing (no br8n_embeddings_set call) —
    # this alone must never rebuild anything.
    monkeypatch.setattr("br8n.clients.embeddings._creds_present", lambda: False)
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: True)

    out = server._embeddings_get_impl()
    assert out["provider"] == "local"
    assert out["source"] == "auto"
    assert out["pending_switch"] == {
        "stored": {"provider": "remote", "model": "text-embedding-3-small", "dim": 1536},
        "detected": {"provider": "local", "model": "BAAI/bge-small-en-v1.5", "dim": 384},
    }
    # Reporting must not have touched the live store.
    assert store.embedding_space()["dim"] == 1536


def test_set_rejects_unknown_provider():
    out = server._embeddings_set_impl("gpu")
    assert out["ok"] is False
    assert "auto" in out["error"]
    assert settings_file.load_settings() == {}  # nothing written


def test_set_local_without_extra_refuses_with_the_fix(monkeypatch):
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: False)
    out = server._embeddings_set_impl("local")
    assert out["ok"] is False
    assert "br8n[local-embeddings]" in out["fix"]
    assert settings_file.load_settings() == {}  # refusal writes nothing


def test_set_local_on_cloud_tier_refuses(monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "cloud")
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: True)
    out = server._embeddings_set_impl("local")
    assert out["ok"] is False
    assert "1536" in out["error"]
    assert settings_file.load_settings() == {}


def test_set_writes_and_takes_effect_without_restart(monkeypatch):
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: True)
    monkeypatch.setattr("br8n.clients.embed_local.warm_up", lambda: None)
    out = server._embeddings_set_impl("local")
    assert out["ok"] is True
    assert out["provider"] == "local" and out["source"] == "settings"
    assert settings_file.load_settings()["embedding_provider"] == "local"
    # a fresh resolution (as a later tool call would do) sees it
    assert server._embeddings_get_impl()["provider"] == "local"


def test_set_reports_the_queued_rebuild_size(monkeypatch):
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: True)
    monkeypatch.setattr("br8n.clients.embed_local.warm_up", lambda: None)
    from br8n.store import get_store

    store = get_store()
    store._conn.execute(
        "INSERT INTO findings (id, org_id, kb_id, title, content, category, "
        "confidence, tags, provenance, created_at, needs_embed) VALUES "
        "('f1','local','k','t','c','note',1.0,'[]','[]','2026-07-28T00:00:00Z',1);"
    )
    store._conn.commit()
    out = server._embeddings_set_impl("local")
    assert out["queued_rebuild"] >= 1


def test_set_auto_returns_to_detection(monkeypatch):
    monkeypatch.setattr("br8n.clients.embeddings._creds_present", lambda: True)
    server._embeddings_set_impl("none")
    assert server._embeddings_get_impl()["provider"] == "none"
    out = server._embeddings_set_impl("auto")
    assert out["ok"] is True
    assert server._embeddings_get_impl()["provider"] == "remote"


async def test_set_local_resyncs_the_live_store_and_prevents_dimension_mismatch(
    monkeypatch,
):
    """I1: a live switch must rebuild vec_findings/vec_kg_nodes on the SAME
    cached store object, not just re-resolve the reported identity — the
    identity report (active_embedder()) is independent of the store's actual
    stamped vec-table width. Without a resync, the store stays stamped at the
    old dim and the next insert at the new dim raises a sqlite-vec dimension
    mismatch."""
    monkeypatch.setattr("br8n.clients.embeddings._creds_present", lambda: True)
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: True)
    monkeypatch.setattr("br8n.clients.embed_local.warm_up", lambda: None)

    from br8n.store import get_store

    store = get_store()
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    assert store.embedding_space()["dim"] == 1536

    out = server._embeddings_set_impl("local")

    # The SAME store object (not a freshly constructed one) must reflect the
    # rebuild — get_store() caches one VaultStore per db_path for the process
    # lifetime, so a live switch has to resync in place.
    assert store.embedding_space()["dim"] == 384
    rows = store._conn.execute("SELECT needs_embed FROM findings;").fetchall()
    assert rows and all(r["needs_embed"] == 1 for r in rows)
    assert out["queued_rebuild"] >= 1

    # The crash is gone: a 384-dim insert on the resynced store succeeds.
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t2", "content": "c2", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 384}]
    )


async def test_set_rolls_back_when_the_resync_silently_degrades(monkeypatch):
    """I1 (round 2): _sync_embedding_space is best-effort by contract — it
    swallows exceptions and returns nothing observable. A caller that reports
    ok:True regardless (e.g. after a locked DB / transient I/O error during
    the DDL) leaves the setting persisted, the reported identity flipped, and
    the store silently stuck at the OLD width — worse than not switching at
    all, because the next capture crashes with a dimension mismatch exactly
    as if I1 (round 1) had never been fixed. A failed resync must instead
    report ok:False AND roll the persisted setting back to its previous
    value, leaving the system exactly as usable as before the call."""
    monkeypatch.setattr("br8n.clients.embeddings._creds_present", lambda: True)
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: True)
    monkeypatch.setattr("br8n.clients.embed_local.warm_up", lambda: None)

    from br8n.store import get_store

    store = get_store()
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    assert store.embedding_space() == {
        "provider": "remote", "model": "text-embedding-3-small", "dim": 1536
    }
    pre_settings = settings_file.load_settings()

    # Model a locked DB / transient I/O error during the rebuild DDL: the
    # resync silently no-ops instead of resizing the vec tables.
    monkeypatch.setattr(store, "resync_embedding_space", lambda: None)

    out = server._embeddings_set_impl("local")

    assert out["ok"] is False
    assert settings_file.load_settings() == pre_settings  # rolled back, not "auto"
    # the store is left exactly as usable as before the call, not silently broken
    assert store.embedding_space() == {
        "provider": "remote", "model": "text-embedding-3-small", "dim": 1536
    }
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t2", "content": "c2", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )

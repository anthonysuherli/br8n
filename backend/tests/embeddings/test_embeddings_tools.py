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

"""vault init: pre-vault rows get canonical files exactly once."""
import pytest

from br8n.vault import migrate


@pytest.fixture
def stores(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    return tmp_path


@pytest.mark.asyncio
async def test_export_missing_is_idempotent(stores, tmp_path):
    from br8n.store.sqlite import SQLiteStore
    from br8n.store.vault import VaultStore

    # simulate a pre-vault db: rows inserted through plain SQLiteStore
    legacy = SQLiteStore(str(tmp_path / "brain.db"))
    org_id, pid = legacy.resolve_project("br8n", create=True)
    kb_id = legacy.resolve_kb(org_id, pid, "main", create=True)
    await legacy.insert_findings(
        [{"kb_id": kb_id, "title": "Old snap", "content": "pre-vault", "category": "snapshot",
          "confidence": 1.0, "tags": [], "provenance": [], "embedding": None}]
    )
    legacy.close()

    store = VaultStore(str(tmp_path / "brain.db"))
    # __init__ auto-ran export_missing; a manual second run must find nothing
    assert migrate.export_missing(store) == 0
    listed = store.list_findings(kb_id)
    fid = listed["findings"][0]["id"]
    path = store.vault_path_for(fid)
    assert path and "pre-vault" in open(path, encoding="utf-8").read()
    store.close()

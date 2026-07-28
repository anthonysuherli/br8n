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


@pytest.mark.asyncio
async def test_repeat_export_failures_are_capped(stores, tmp_path, caplog, monkeypatch):
    """Permanently-failing rows must not emit one full warning+stack each on
    every boot: first failure keeps the stack, the rest log at DEBUG behind
    a single summary warning."""
    import logging

    from br8n.store.sqlite import SQLiteStore

    legacy = SQLiteStore(str(tmp_path / "brain.db"))
    org_id, pid = legacy.resolve_project("br8n", create=True)
    kb_id = legacy.resolve_kb(org_id, pid, "main", create=True)
    await legacy.insert_findings(
        [{"kb_id": kb_id, "title": f"Old {i}", "content": "pre-vault",
          "category": "snapshot", "confidence": 1.0, "tags": [],
          "provenance": [], "embedding": None} for i in range(3)]
    )
    legacy.close()

    from br8n.store.vault import VaultStore

    def boom(self, fid):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(VaultStore, "_write_canonical", boom)
    with caplog.at_level(logging.WARNING, logger="br8n.vault.migrate"):
        store = VaultStore(str(tmp_path / "brain.db"))
    store.close()

    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and r.name == "br8n.vault.migrate"]
    assert len(warns) == 2  # first failure with stack + one summary, not 3
    assert any("3 rows failed" in r.getMessage() for r in warns)

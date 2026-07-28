"""Acceptance criterion 4: delete brain.db, reindex restores from files."""
import os

import pytest

from br8n.vault import reindex as rmod


@pytest.mark.asyncio
async def test_reindex_from_vault_only(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    from br8n.store.vault import VaultStore

    store = VaultStore(str(tmp_path / "brain.db"))
    org_id, pid = store.resolve_project("br8n", create=True)
    kb_id = store.resolve_kb(org_id, pid, "main", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "Keep me", "content": "canonical", "category": "note",
          "confidence": 1.0, "tags": ["note"], "provenance": [], "embedding": None}]
    )
    store.close()
    os.unlink(tmp_path / "brain.db")  # the index is disposable

    result = await rmod.reindex(str(tmp_path / "brain.db"))
    assert result["adopted"] == 1

    fresh = VaultStore(str(tmp_path / "brain.db"))
    org_id, pid = fresh.resolve_project("br8n", create=True)
    kb_id = fresh.resolve_kb(org_id, pid, "main", create=True)
    row = fresh.get_finding(kb_id, fid)  # same id — br8n_id survived the rebuild
    assert row["title"] == "Keep me"
    fresh.close()

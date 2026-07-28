"""Reads reconcile: an Obsidian edit is visible on the very next get/list."""
import pytest

from br8n.vault import files as vfiles


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    from br8n.store.vault import VaultStore

    s = VaultStore(str(tmp_path / "brain.db"))
    yield s
    s.close()


def _mk_kb(store):
    org_id, project_id = store.resolve_project("br8n", create=True)
    return store.resolve_kb(org_id, project_id, "main", create=True)


def _edit(path, new_body):
    fm, _ = vfiles.parse(open(path, encoding="utf-8").read())
    open(path, "w", encoding="utf-8").write(vfiles.serialize(fm, new_body))


@pytest.mark.asyncio
async def test_get_finding_sees_fresh_edit_without_debounce_wait(store):
    kb_id = _mk_kb(store)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "N", "content": "old", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [], "embedding": None}]
    )
    _edit(store.vault_path_for(fid), "# N\n\nfresh edit")
    assert "fresh edit" in store.get_finding(kb_id, fid)["content"]


@pytest.mark.asyncio
async def test_list_findings_triggers_reconcile(store, monkeypatch):
    kb_id = _mk_kb(store)
    called = {}
    from br8n.vault import reconcile as rmod

    real = rmod.reconcile
    monkeypatch.setattr(rmod, "reconcile", lambda s, **k: called.setdefault("yes", True) or real(s, **k))
    store.list_findings(kb_id)
    assert called.get("yes")


@pytest.mark.asyncio
async def test_match_findings_re_embeds_stale(store, monkeypatch):
    """A row with needs_embed=1 gets a vec row once embeddings are available."""
    kb_id = _mk_kb(store)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "N", "content": "vault content", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [], "embedding": None}]
    )
    import br8n.store.vault as vault_mod

    async def fake_embed(texts):
        return [[0.1] * 1536 for _ in texts]

    monkeypatch.setattr(vault_mod, "embed_batch", fake_embed)
    monkeypatch.setattr(vault_mod, "embeddings_configured", lambda: True)
    rows = await store.match_findings(kb_id, [0.1] * 1536, 5, 0.0)
    assert any(r["id"] == fid for r in rows)
    flag = store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["needs_embed"]
    assert flag == 0


def test_get_store_returns_vaultstore_locally(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n.store import get_store
    from br8n.store.vault import VaultStore

    assert isinstance(get_store(), VaultStore)

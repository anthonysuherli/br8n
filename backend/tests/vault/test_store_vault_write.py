"""VaultStore write path: canonical file per finding, delete removes the file."""
import pytest

from br8n.vault import files as vfiles
from br8n.vault import layout


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    from br8n.store.vault import VaultStore

    s = VaultStore(str(tmp_path / "brain.db"))
    yield s
    s.close()


def _mk_kb(store, project="br8n", kb="main"):
    org_id, project_id = store.resolve_project(project, create=True)
    return store.resolve_kb(org_id, project_id, kb, create=True)


@pytest.mark.asyncio
async def test_insert_writes_canonical_file(store):
    kb_id = _mk_kb(store)
    [fid] = await store.insert_findings(
        [{
            "kb_id": kb_id,
            "title": "Snap one",
            "content": "# Snap one\n\nworking on vault",
            "category": "snapshot",
            "confidence": 1.0,
            "tags": ["snapshot"],
            "provenance": [{"source": "test"}],
            "metadata": {"next_action": "run tests"},
            "embedding": None,
        }]
    )
    path = store.vault_path_for(fid)
    assert path is not None
    fm, body = vfiles.parse(open(path, encoding="utf-8").read())
    assert fm["br8n_id"] == fid
    assert fm["type"] == "snapshot"
    assert fm["title"] == "Snap one"
    assert fm["project"] == "br8n"
    assert fm["kb"] == "main"
    assert fm["next_action"] == "run tests"
    assert "working on vault" in body
    # index stamps present
    r = store._conn.execute(
        "SELECT content_hash, vault_mtime, vault_size, needs_embed FROM findings WHERE id = ?;",
        (fid,),
    ).fetchone()
    assert r["content_hash"] and r["vault_mtime"] and r["vault_size"]
    assert r["needs_embed"] == 1  # inserted without an embedding


@pytest.mark.asyncio
async def test_journal_goes_to_year_dir(store):
    from br8n.constants import JOURNAL_SCOPE

    org_id, pid = store.resolve_project(JOURNAL_SCOPE, create=True)
    kb_id = store.resolve_kb(org_id, pid, JOURNAL_SCOPE, create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "J", "content": "entry", "category": "journal",
          "confidence": 1.0, "tags": ["journal"], "provenance": [], "embedding": None}]
    )
    assert "/journal/" in store.vault_path_for(fid)
    assert str(layout.vault_root()) in store.vault_path_for(fid)


@pytest.mark.asyncio
async def test_delete_finding_unlinks_file(store):
    import os

    kb_id = _mk_kb(store)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "T", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [], "embedding": None}]
    )
    path = store.vault_path_for(fid)
    assert os.path.exists(path)
    store.delete_finding(kb_id, fid)
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_vault_write_failure_never_breaks_insert(store, monkeypatch):
    """Fail-silent: a broken vault path degrades to index-only insert."""
    from br8n.store import vault as vault_mod

    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(vault_mod.files, "atomic_write", boom)
    kb_id = _mk_kb(store)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "T", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [], "embedding": None}]
    )
    assert store.get_finding(kb_id, fid)["title"] == "T"  # row exists, no crash

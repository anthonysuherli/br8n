"""Reconcile: Obsidian edits stick, new files adopt, deletes delete."""
import pytest

from br8n.vault import files as vfiles
from br8n.vault import layout, reconcile


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


async def _insert(store, kb_id, title="Note", content="# Note\n\nbody", category="note"):
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": title, "content": content, "category": category,
          "confidence": 1.0, "tags": ["note"], "provenance": [], "embedding": None}]
    )
    return fid


@pytest.mark.asyncio
async def test_edit_updates_index(store):
    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    path = store.vault_path_for(fid)
    text = open(path, encoding="utf-8").read()
    fm, _ = vfiles.parse(text)
    fm["title"] = "Edited title"
    open(path, "w", encoding="utf-8").write(vfiles.serialize(fm, "# Edited title\n\nnew body"))
    counters = reconcile.reconcile(store, force=True)
    assert counters["updated"] == 1
    row = store.get_finding(kb_id, fid)
    assert row["title"] == "Edited title"
    assert "new body" in row["content"]


@pytest.mark.asyncio
async def test_new_file_adopted_and_id_written_back(store):
    kb_id = _mk_kb(store)
    d = layout.vault_root() / "notes" / "br8n" / "main"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-07-27-1200-hand-written.md").write_text("# Hand written\n\nfrom obsidian\n")
    counters = reconcile.reconcile(store, force=True)
    assert counters["adopted"] == 1
    listed = store.list_findings(kb_id, category="note")
    assert any(f["title"] == "Hand written" for f in listed["findings"])
    fm, _ = vfiles.parse((d / "2026-07-27-1200-hand-written.md").read_text())
    assert fm["br8n_id"]  # engine wrote the join key back
    assert fm["source"] == "human"


@pytest.mark.asyncio
async def test_deleted_file_removes_row(store):
    import os

    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    os.unlink(store.vault_path_for(fid))
    counters = reconcile.reconcile(store, force=True)
    assert counters["deleted"] == 1
    with pytest.raises(RuntimeError):
        store.get_finding(kb_id, fid)


@pytest.mark.asyncio
async def test_malformed_frontmatter_skipped(store):
    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    path = store.vault_path_for(fid)
    open(path, "w", encoding="utf-8").write("---\ntags: [broken\n---\n\nbody\n")
    counters = reconcile.reconcile(store, force=True)
    assert counters["malformed"] == 1
    assert store.get_finding(kb_id, fid)["title"] == "Note"  # untouched


@pytest.mark.asyncio
async def test_debounce_skips_back_to_back(store):
    _mk_kb(store)
    first = reconcile.reconcile(store, force=True)
    second = reconcile.reconcile(store)  # immediately after → debounced
    assert second["skipped"] is True
    assert first["skipped"] is False


@pytest.mark.asyncio
async def test_views_never_scanned(store):
    _mk_kb(store)
    d = layout.vault_root() / layout.VIEWS_DIRNAME / "synopsis"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.md").write_text("# derived\n")
    counters = reconcile.reconcile(store, force=True)
    assert counters["adopted"] == 0


@pytest.mark.asyncio
async def test_reconcile_never_raises(store, monkeypatch):
    monkeypatch.setattr(layout, "vault_root", lambda: (_ for _ in ()).throw(OSError("boom")))
    counters = reconcile.reconcile(store, force=True)
    assert counters["scanned"] == 0  # degraded, no exception

"""Derived views land under views/ with the do-not-edit banner."""
from pathlib import Path

import pytest

from br8n.vault import layout, views


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    from br8n.store.vault import VaultStore

    s = VaultStore(str(tmp_path / "brain.db"))
    yield s
    s.close()


def _mk_kb(store):
    org_id, pid = store.resolve_project("br8n", create=True)
    return store.resolve_kb(org_id, pid, "main", create=True)


def test_synopsis_view_written_on_upsert(store):
    kb_id = _mk_kb(store)
    store.upsert_synopsis(kb_id, [{"topic": "vault", "summary": "md canonical"}], 1, "test-model")
    p = layout.vault_root() / "views" / "synopsis" / "br8n" / "main.md"
    text = p.read_text()
    assert text.startswith(views.BANNER)
    assert "md canonical" in text


@pytest.mark.asyncio
async def test_activity_view_written_on_kg_upsert(store):
    kb_id = _mk_kb(store)
    await store.upsert_kg_nodes(kb_id, [
        {"type": "Repo", "label": "br8n", "properties": {}, "grounded_in": []},
    ])
    p = layout.vault_root() / "views" / "activity" / "br8n" / "main.md"
    assert "br8n" in p.read_text()


def test_view_paths_do_not_collide_across_pairs(store):
    """project 'a-b'/kb 'c' and project 'a'/kb 'b-c' both flattened to
    'a-b-c.md' before — each pair must get its own file."""
    org, p1 = store.resolve_project("a-b", create=True)
    kb1 = store.resolve_kb(org, p1, "c", create=True)
    org, p2 = store.resolve_project("a", create=True)
    kb2 = store.resolve_kb(org, p2, "b-c", create=True)

    store.upsert_synopsis(kb1, [{"summary": "first"}], 1, "m")
    store.upsert_synopsis(kb2, [{"summary": "second"}], 1, "m")

    d = layout.vault_root() / "views" / "synopsis"
    written = sorted(str(p.relative_to(d)) for p in d.rglob("*.md"))
    assert written == ["a-b/c.md", "a/b-c.md"]
    assert "first" in (d / "a-b" / "c.md").read_text()
    assert "second" in (d / "a" / "b-c.md").read_text()


@pytest.mark.asyncio
async def test_activity_view_wikilink_uses_file_stem(store):
    kb_id = _mk_kb(store)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "Grounding", "content": "body", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [], "embedding": None}]
    )
    await store.upsert_kg_nodes(kb_id, [
        {"type": "File", "label": "x.py", "properties": {}, "grounded_in": [fid]},
    ])
    p = layout.vault_root() / "views" / "activity" / "br8n" / "main.md"
    stem = Path(store.vault_path_for(fid)).stem
    assert f"[[{stem}]]" in p.read_text()


def test_mirror_timeline(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    views.mirror_timeline("br8n", "main", "recent body", "week body")
    d = layout.vault_root() / "views" / "timeline" / "br8n-main"
    assert "recent body" in (d / "recent.md").read_text()
    assert "week body" in (d / "week.md").read_text()


def test_views_never_raise(monkeypatch, store):
    monkeypatch.setattr(layout, "vault_root", lambda: (_ for _ in ()).throw(OSError("x")))
    views.write_synopsis_view(store, "kb", [])          # no exception
    views.mirror_timeline("p", "k", "r", "w")           # no exception

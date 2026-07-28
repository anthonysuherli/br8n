"""After a space change, KG node vectors refill through the same lazy drain."""
import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n.store.vault import VaultStore

    s = VaultStore(str(tmp_path / "brain.db"))
    yield s
    s.close()


def _fake_embeddings(monkeypatch):
    import br8n.store.vault as vault_mod

    async def fake_embed(texts):
        return [[0.2] * 1536 for _ in texts]

    monkeypatch.setattr(vault_mod, "embed_batch", fake_embed)
    monkeypatch.setattr(vault_mod, "embeddings_configured", lambda: True)


@pytest.mark.asyncio
async def test_flagged_nodes_are_re_embedded(store, monkeypatch):
    _fake_embeddings(monkeypatch)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [nid] = await store.upsert_kg_nodes(
        kb_id, [{"type": "Repo", "label": "br8n", "properties": {}, "grounded_in": []}]
    )
    store._conn.execute("UPDATE kg_nodes SET needs_embed = 1;")
    store._conn.execute("DELETE FROM vec_kg_nodes;")
    store._conn.commit()

    drained = await store._re_embed_stale()
    assert drained >= 1
    assert store._conn.execute(
        "SELECT needs_embed FROM kg_nodes WHERE id = ?;", (nid,)
    ).fetchone()["needs_embed"] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_kg_nodes;"
    ).fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_drain_counts_findings_and_nodes_together(store, monkeypatch):
    _fake_embeddings(monkeypatch)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [], "embedding": None}]
    )
    await store.upsert_kg_nodes(
        kb_id, [{"type": "Repo", "label": "br8n", "properties": {}, "grounded_in": []}]
    )
    store._conn.execute("UPDATE kg_nodes SET needs_embed = 1;")
    store._conn.commit()
    assert await store._re_embed_stale() == 2


@pytest.mark.asyncio
async def test_nodes_without_labels_are_skipped(store, monkeypatch):
    _fake_embeddings(monkeypatch)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    store._conn.execute(
        "INSERT INTO kg_nodes (id, org_id, kb_id, type, label, properties, "
        "grounded_in, created_at, needs_embed) VALUES "
        "('n1', 'local', ?, 'Repo', '', '{}', '[]', '2026-07-28T00:00:00Z', 1);",
        (kb_id,),
    )
    store._conn.commit()
    assert await store._re_embed_stale() == 0


@pytest.mark.asyncio
async def test_embed_failure_leaves_nodes_flagged(store, monkeypatch):
    import br8n.store.vault as vault_mod

    async def boom(texts):
        raise RuntimeError("provider down")

    monkeypatch.setattr(vault_mod, "embed_batch", boom)
    monkeypatch.setattr(vault_mod, "embeddings_configured", lambda: True)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.upsert_kg_nodes(
        kb_id, [{"type": "Repo", "label": "br8n", "properties": {}, "grounded_in": []}]
    )
    store._conn.execute("UPDATE kg_nodes SET needs_embed = 1;")
    store._conn.commit()

    assert await store._re_embed_stale() == 0  # degraded, not raised
    assert store._conn.execute(
        "SELECT needs_embed FROM kg_nodes WHERE label = 'br8n';"
    ).fetchone()["needs_embed"] == 1  # retried next pass

"""Local-tier test for persist_journal: a `journal` Finding, with its canonical
markdown written into the vault (`VaultStore.insert_findings`), and NO
synopsis rebuild. Mirrors test_note_tool.py's harness — real SQLiteStore via
tmp BR8N_DB_PATH, only the embedder faked.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

DIM = 1536


def _fake_vec(text: str) -> list[float]:
    h = hashlib.sha256(text.encode()).digest()
    v = [0.0] * DIM
    for i in range(8):
        v[i] = (h[i] / 255.0) or 0.01
    return v


async def _fake_embed_batch(texts):
    return [_fake_vec(t) for t in texts]


async def test_persist_journal_writes_finding_and_global_md(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))

    import br8n.livingdocs.journal as journal_mod
    import br8n.store as store_pkg
    from br8n.constants import JOURNAL_SCOPE
    from br8n.interfaces.mcp.tenancy import resolve_tenant

    store_pkg._local_stores.clear()
    monkeypatch.setattr(journal_mod, "embed_batch", _fake_embed_batch)

    ctx = resolve_tenant(JOURNAL_SCOPE, JOURNAL_SCOPE, create=True)
    res = await journal_mod.persist_journal(
        ctx, text="learned that X composes cleanly", type="insight", tags=["arch"]
    )

    assert res["finding_id"]
    assert res["entry_path"].endswith(".md")
    # local tier: canonical markdown is the vault file (journal/<year>/...),
    # not the legacy GLOBAL journal dir
    assert str(tmp_path / "vault" / "journal") in res["entry_path"]
    assert Path(res["entry_path"]).exists()
    assert not (tmp_path / "journal").exists()

    store = store_pkg.get_store(ctx.access_token)
    got = store.get_finding(ctx.kb_id, res["finding_id"])
    assert got["category"] == "journal"
    assert "journal" in got["tags"] and "insight" in got["tags"] and "arch" in got["tags"]

    store_pkg._local_stores.clear()


async def test_br8n_journal_tool_writes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))

    import br8n.livingdocs.journal as journal_mod
    import br8n.store as store_pkg
    from br8n.interfaces.mcp import server

    store_pkg._local_stores.clear()
    monkeypatch.setattr(journal_mod, "embed_batch", _fake_embed_batch)

    res = await server._journal_impl(
        text="prefer scope filters over separate corpora", type="decision"
    )

    assert res["finding_id"]
    assert res["scope"] == "journal"
    assert res["entry_path"].endswith(".md")

    store_pkg._local_stores.clear()

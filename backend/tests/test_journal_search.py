"""Local-tier test for br8n_journal_search scope filters. Seeds findings
directly (journal entry in the __journal__ KB, a note in a project KB) and
asserts each scope returns the right corpus. embed_text is faked at its source
module so the search path makes no OpenAI call.

``embeddings_configured`` is faked alongside it: the search path gates on it
and falls back to a recency read when it is False, so without the fake these
tests would exercise the fallback rather than the semantic branch they exist
for. The fallback has its own test at the bottom.
"""
from __future__ import annotations

import hashlib

DIM = 1536


def _fake_vec(text: str) -> list[float]:
    h = hashlib.sha256(text.encode()).digest()
    v = [0.0] * DIM
    for i in range(8):
        v[i] = (h[i] / 255.0) or 0.01
    return v


async def _fake_embed_text(text: str) -> list[float]:
    return _fake_vec(text)


def _fake_embedder(monkeypatch) -> None:
    monkeypatch.setattr("br8n.clients.embeddings.embed_text", _fake_embed_text)
    monkeypatch.setattr("br8n.clients.embeddings.embeddings_configured", lambda: True)


async def test_journal_search_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    _fake_embedder(monkeypatch)

    import br8n.store as store_pkg
    from br8n.constants import JOURNAL_SCOPE
    from br8n.interfaces.mcp import server
    from br8n.interfaces.mcp.tenancy import resolve_tenant

    store_pkg._local_stores.clear()
    jctx = resolve_tenant(JOURNAL_SCOPE, JOURNAL_SCOPE, create=True)
    pctx = resolve_tenant("proj", "main", create=True)
    store = store_pkg.get_store()
    await store.insert_findings(
        [
            {"kb_id": jctx.kb_id, "title": "j-entry", "content": "alpha insight",
             "category": "journal", "tags": ["journal", "insight"],
             "embedding": _fake_vec("alpha insight")},
            {"kb_id": pctx.kb_id, "title": "p-note", "content": "beta note",
             "category": "note", "tags": ["note"],
             "embedding": _fake_vec("beta note")},
        ]
    )

    j = await server._journal_search_impl("alpha", scope="journal")
    assert {r["title"] for r in j["results"]} == {"j-entry"}

    p = await server._journal_search_impl("beta", scope="project", project="proj", kb="main")
    assert {r["title"] for r in p["results"]} == {"p-note"}

    b = await server._journal_search_impl("alpha", scope="both")
    assert {r["title"] for r in b["results"]} == {"j-entry", "p-note"}

    # type filter narrows journal results
    none = await server._journal_search_impl("alpha", scope="journal", type="reflection")
    assert none["results"] == []

    store_pkg._local_stores.clear()


async def test_journal_search_project_scope_unknown_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    _fake_embedder(monkeypatch)

    import br8n.store as store_pkg
    from br8n.interfaces.mcp import server

    store_pkg._local_stores.clear()
    res = await server._journal_search_impl("anything", scope="project", project="nope", kb="nope")
    assert res == {"results": [], "scope": "project", "count": 0}
    store_pkg._local_stores.clear()


async def test_journal_search_degrades_to_recency_without_an_embedder(tmp_path, monkeypatch):
    """No embedder (keyless install, or a deferred embedding-space switch):
    the search must return the SAME corpus per scope, just recency-ordered —
    never raise, and never silently narrow which KBs are in view."""
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr("br8n.clients.embeddings.embeddings_configured", lambda: False)

    import br8n.store as store_pkg
    from br8n.constants import JOURNAL_SCOPE
    from br8n.interfaces.mcp import server
    from br8n.interfaces.mcp.tenancy import resolve_tenant

    store_pkg._local_stores.clear()
    jctx = resolve_tenant(JOURNAL_SCOPE, JOURNAL_SCOPE, create=True)
    pctx = resolve_tenant("proj", "main", create=True)
    await store_pkg.get_store().insert_findings(
        [
            {"kb_id": jctx.kb_id, "title": "j-entry", "content": "alpha insight",
             "category": "journal", "tags": ["journal", "insight"], "embedding": None},
            {"kb_id": pctx.kb_id, "title": "p-note", "content": "beta note",
             "category": "note", "tags": ["note"], "embedding": None},
        ]
    )

    j = await server._journal_search_impl("alpha", scope="journal")
    assert {r["title"] for r in j["results"]} == {"j-entry"}
    assert "embeddings unavailable" in j["degraded"]
    assert "score" not in j["results"][0], "recency rows must not claim a relevance score"

    p = await server._journal_search_impl("beta", scope="project", project="proj", kb="main")
    assert {r["title"] for r in p["results"]} == {"p-note"}

    b = await server._journal_search_impl("alpha", scope="both")
    assert {r["title"] for r in b["results"]} == {"j-entry", "p-note"}, (
        "the degrade must not narrow 'both' to the journal"
    )

    none = await server._journal_search_impl("alpha", scope="journal", type="reflection")
    assert none["results"] == []

    store_pkg._local_stores.clear()


async def test_list_projects_hides_journal_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))

    import br8n.store as store_pkg
    from br8n.constants import JOURNAL_SCOPE
    from br8n.interfaces.mcp.tenancy import resolve_tenant

    store_pkg._local_stores.clear()
    resolve_tenant(JOURNAL_SCOPE, JOURNAL_SCOPE, create=True)
    resolve_tenant("realproj", "main", create=True)

    names = {p["project"] for p in store_pkg.get_store().list_projects()}
    assert "realproj" in names
    assert JOURNAL_SCOPE not in names

    store_pkg._local_stores.clear()

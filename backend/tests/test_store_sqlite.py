"""SQLiteStore tests against a real in-memory SQLite + sqlite-vec.

No mocks of the vector search: findings are embedded with small real vectors and
``match_findings`` runs the actual ``vec_distance_cosine`` KNN. Each test gets a
fresh ``:memory:`` store so state never leaks between tests.
"""

from __future__ import annotations

import sqlite3

import pytest

from br8n.store import SQLiteStore, SupabaseStore
from br8n.store.sqlite import _ORG

DIM = 1536


def _vec(seed: list[float]) -> list[float]:
    """Pad a short prefix to a full 1536-d embedding (rest zero)."""
    v = list(seed) + [0.0] * (DIM - len(seed))
    return v[:DIM]


@pytest.fixture
def store() -> SQLiteStore:
    return SQLiteStore(":memory:")


def _finding(kb_id: str, *, title: str, category: str, embedding: list[float], **kw) -> dict:
    return {
        "org_id": "ignored",
        "kb_id": kb_id,
        "title": title,
        "content": kw.get("content", f"body of {title}"),
        "category": category,
        "confidence": kw.get("confidence", 0.9),
        "tags": kw.get("tags", ["t1", "t2"]),
        "provenance": kw.get("provenance", [{"url": "http://x", "accessed_at": "2026-01-01"}]),
        "embedding": embedding,
    }


# --- round-trip -------------------------------------------------------------


async def test_insert_get_roundtrip(store):
    [fid] = await store.insert_findings(
        [_finding("kb1", title="Hello", category="doc", embedding=_vec([1.0]))]
    )
    got = store.get_finding("kb1", fid)
    assert got["title"] == "Hello"
    assert got["content"] == "body of Hello"
    assert got["category"] == "doc"
    assert got["tags"] == ["t1", "t2"]
    assert got["provenance"] == [{"url": "http://x", "accessed_at": "2026-01-01"}]
    assert got["created_at"]


async def test_insert_forces_local_org(store):
    [fid] = await store.insert_findings(
        [_finding("kb1", title="X", category="doc", embedding=_vec([1.0]))]
    )
    row = store._conn.execute("SELECT org_id FROM findings WHERE id = ?;", (fid,)).fetchone()
    assert row["org_id"] == _ORG


async def test_insert_empty_returns_empty(store):
    assert await store.insert_findings([]) == []


def test_get_finding_missing_raises(store):
    with pytest.raises(RuntimeError, match="finding not found"):
        store.get_finding("kb1", "nope")


# --- match_findings (real vector search) ------------------------------------


async def test_match_findings_ranking_and_similarity(store):
    await store.insert_findings(
        [
            _finding("kb1", title="A", category="doc", embedding=_vec([1.0, 0.0, 0.0])),
            _finding("kb1", title="B", category="doc", embedding=_vec([0.0, 1.0, 0.0])),
            _finding("kb1", title="C", category="doc", embedding=_vec([0.0, 0.0, 1.0])),
        ]
    )
    # Query close to A's direction.
    out = await store.match_findings("kb1", _vec([0.9, 0.1, 0.0]), match_count=10, min_similarity=0.0)

    assert out[0]["title"] == "A"
    sims = [r["similarity"] for r in out]
    assert sims == sorted(sims, reverse=True)
    assert all("similarity" in r for r in out)


async def test_match_findings_min_similarity_filters(store):
    await store.insert_findings(
        [
            _finding("kb1", title="A", category="doc", embedding=_vec([1.0, 0.0, 0.0])),
            _finding("kb1", title="B", category="doc", embedding=_vec([0.0, 1.0, 0.0])),
        ]
    )
    # Exactly aligned with A; B is orthogonal (similarity ~0) and must be dropped.
    out = await store.match_findings("kb1", _vec([1.0, 0.0, 0.0]), match_count=10, min_similarity=0.5)
    titles = [r["title"] for r in out]
    assert titles == ["A"]


async def test_match_findings_scoped_to_kb(store):
    await store.insert_findings(
        [_finding("kbX", title="other", category="doc", embedding=_vec([1.0]))]
    )
    out = await store.match_findings("kb1", _vec([1.0]), match_count=10, min_similarity=0.0)
    assert out == []


# --- list_findings ----------------------------------------------------------


async def test_list_findings_count_order_filter_limit(store):
    await store.insert_findings(
        [
            {**_finding("kb1", title="old", category="doc", embedding=_vec([1.0])), "created_at": "2026-01-01T00:00:00+00:00"},
            {**_finding("kb1", title="new", category="note", embedding=_vec([0.0, 1.0])), "created_at": "2026-05-01T00:00:00+00:00"},
            {**_finding("kb1", title="mid", category="doc", embedding=_vec([0.0, 0.0, 1.0])), "created_at": "2026-03-01T00:00:00+00:00"},
        ]
    )
    out = store.list_findings("kb1")
    assert out["count"] == 3
    # Newest-first.
    assert [f["title"] for f in out["findings"]] == ["new", "mid", "old"]
    # List view drops content/provenance.
    assert "content" not in out["findings"][0]
    assert "provenance" not in out["findings"][0]

    docs = store.list_findings("kb1", category="doc")
    assert docs["count"] == 2
    assert {f["title"] for f in docs["findings"]} == {"old", "mid"}

    limited = store.list_findings("kb1", limit=1)
    assert limited["count"] == 1
    assert limited["findings"][0]["title"] == "new"


# --- count_findings ---------------------------------------------------------


async def test_count_findings_exact_and_uncapped(store):
    # Insert more than one and scoped to a single KB; count is exact.
    await store.insert_findings(
        [
            _finding("kb1", title="A", category="doc", embedding=_vec([1.0])),
            _finding("kb1", title="B", category="doc", embedding=_vec([0.0, 1.0])),
            _finding("kb1", title="C", category="doc", embedding=_vec([0.0, 0.0, 1.0])),
        ]
    )
    await store.insert_findings(
        [_finding("kbX", title="other", category="doc", embedding=_vec([1.0]))]
    )
    assert store.count_findings("kb1") == 3
    assert store.count_findings("kbX") == 1
    assert store.count_findings("empty") == 0


# --- delete_finding ---------------------------------------------------------


async def test_delete_removes_from_findings_and_vec(store):
    [fid] = await store.insert_findings(
        [_finding("kb1", title="ToDelete", category="doc", embedding=_vec([1.0]))]
    )
    assert store.delete_finding("kb1", fid) == {"deleted": fid}

    with pytest.raises(RuntimeError, match="finding not found"):
        store.get_finding("kb1", fid)
    # Vector search no longer returns it.
    out = await store.match_findings("kb1", _vec([1.0]), match_count=10, min_similarity=0.0)
    assert out == []
    # vec_findings row is gone too.
    vrow = store._conn.execute(
        "SELECT count(*) AS n FROM vec_findings WHERE finding_id = ?;", (fid,)
    ).fetchone()
    assert vrow["n"] == 0


# --- synopsis ---------------------------------------------------------------


def test_synopsis_upsert_load_roundtrip_and_overwrite(store):
    assert store.load_synopsis("kb1") is None

    store.upsert_synopsis("kb1", [{"topic": "t", "gloss": "g"}], 5, "gpt-4o-mini")
    row = store.load_synopsis("kb1")
    assert row["content"] == [{"topic": "t", "gloss": "g"}]
    assert row["finding_count_at_build"] == 5
    assert row["model"] == "gpt-4o-mini"
    assert row["built_at"]

    # Second upsert overwrites (ON CONFLICT(kb_id)).
    store.upsert_synopsis("kb1", [{"topic": "t2", "gloss": "g2"}], 9, "gpt-4o")
    row2 = store.load_synopsis("kb1")
    assert row2["content"] == [{"topic": "t2", "gloss": "g2"}]
    assert row2["finding_count_at_build"] == 9
    assert row2["model"] == "gpt-4o"
    # Still one row.
    n = store._conn.execute("SELECT count(*) AS n FROM kb_synopsis;").fetchone()["n"]
    assert n == 1


# --- exploration ------------------------------------------------------------


def test_exploration_lifecycle(store):
    eid = store.create_exploration("orgX", "kb1", "find stuff")
    got = store.get_exploration(eid)
    assert got["id"] == eid
    assert got["status"] == "pending"
    assert got["finding_ids"] == []
    assert got["completed_at"] is None
    assert got["error"] is None

    store.update_exploration(eid, status="completed", finding_ids=["f1", "f2"], completed_at="2026-05-29T00:00:00+00:00")
    done = store.get_exploration(eid)
    assert done["status"] == "completed"
    assert done["finding_ids"] == ["f1", "f2"]
    assert done["completed_at"] == "2026-05-29T00:00:00+00:00"

    # org_id is forced local.
    org = store._conn.execute("SELECT org_id FROM explorations WHERE id = ?;", (eid,)).fetchone()["org_id"]
    assert org == _ORG


def test_get_exploration_missing(store):
    assert store.get_exploration("nope") is None


# --- tenancy: find-or-create ------------------------------------------------


def test_resolve_project_three_way(store):
    org_id, pid = store.resolve_project("proj", create=True)
    assert org_id == _ORG
    # Existing -> same id.
    org_id2, pid2 = store.resolve_project("proj", create=False)
    assert (org_id2, pid2) == (_ORG, pid)
    # Absent + no create -> raise.
    with pytest.raises(RuntimeError, match="not found"):
        store.resolve_project("missing", create=False)


def test_resolve_kb_three_way(store):
    _, pid = store.resolve_project("proj", create=True)
    kb_id = store.resolve_kb(_ORG, pid, "mykb", create=True)
    assert kb_id
    # Existing -> same id.
    assert store.resolve_kb(_ORG, pid, "mykb", create=False) == kb_id
    # Absent + no create -> raise.
    with pytest.raises(RuntimeError, match="not found"):
        store.resolve_kb(_ORG, pid, "other", create=False)


# --- monitoring -------------------------------------------------------------


async def test_record_access_noop(store):
    assert await store.record_access(org_id="o", kb_id="k", surface="mcp", targets=[]) is None
    # Even with targets, never raises and returns None.
    assert (
        await store.record_access(
            org_id="o", kb_id="k", surface="mcp", targets=[object()], query_text="q"
        )
        is None
    )


# --- connection lifecycle / pragmas -----------------------------------------


def test_file_backed_db_uses_wal(tmp_path):
    """File-backed DBs run in WAL so concurrent fg/bg writes block-and-retry."""
    s = SQLiteStore(str(tmp_path / "brain.db"))
    try:
        mode = s._conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"
        timeout = s._conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert timeout == 5000
    finally:
        s.close()


def test_close_and_context_manager(tmp_path):
    with SQLiteStore(str(tmp_path / "br8n.db")) as s:
        assert s.list_findings("kb1") == {"count": 0, "findings": []}
    # After close the connection is unusable.
    with pytest.raises(sqlite3.ProgrammingError):
        s._conn.execute("SELECT 1;")


# --- interchangeability with SupabaseStore ----------------------------------


async def test_shape_parity_with_supabase(store):
    """match_findings / get_finding / list_findings expose the keys the engine
    reads off SupabaseStore — so the two backends are drop-in interchangeable."""
    [fid] = await store.insert_findings(
        [_finding("kb1", title="P", category="doc", embedding=_vec([1.0]))]
    )

    # get_finding keys == SupabaseStore's _FINDING_COLS.
    got = store.get_finding("kb1", fid)
    assert set(got) == {
        "id", "title", "content", "category", "confidence", "tags", "provenance", "metadata", "created_at"
    }

    # match_findings keys == match RPC return columns (sans content optionality) + similarity.
    matched = await store.match_findings("kb1", _vec([1.0]), match_count=5, min_similarity=0.0)
    assert set(matched[0]) == {
        "id", "title", "content", "category", "confidence", "tags", "provenance", "similarity"
    }

    # list_findings shape == SupabaseStore: {"count","findings"} with the list cols.
    listed = store.list_findings("kb1")
    assert set(listed) == {"count", "findings"}
    assert set(listed["findings"][0]) == {
        "id", "title", "category", "confidence", "tags", "metadata", "created_at"
    }

    # The protocol surface matches SupabaseStore's.
    for m in (
        "match_findings", "insert_findings", "get_finding", "list_findings",
        "count_findings", "delete_finding", "load_synopsis", "upsert_synopsis", "create_exploration",
        "update_exploration", "get_exploration", "resolve_project", "resolve_kb",
        "record_access",
    ):
        assert hasattr(SQLiteStore, m) and hasattr(SupabaseStore, m)

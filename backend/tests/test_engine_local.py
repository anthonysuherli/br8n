"""End-to-end local-tier engine test.

BR8N_BACKEND=local + a tmp BR8N_DB_PATH wire the real engine functions to a
real SQLiteStore (no storage mocks). Only the embedder is faked — a deterministic
hash-based vector so no OpenAI call is made — and the synopsis LLM is patched so
the rebuild path can be exercised without Anthropic.

Asserts the full local seam:
  * resolve_tenant(local) → org_id == "local", access_token == ""
  * persist_snapshot writes a finding that lands via the store
  * select_preamble(store=...) reflects the inserted snapshot in its coverage band
  * synopsis upsert/load round-trips through the store (+ a faked rebuild)
"""

from __future__ import annotations

import hashlib

import pytest

DIM = 1536


def _fake_vec(text: str) -> list[float]:
    """Deterministic unit-ish vector: hash → a few seeded dims, rest zero."""
    h = hashlib.sha256(text.encode()).digest()
    v = [0.0] * DIM
    for i in range(8):
        v[i] = (h[i] / 255.0) or 0.01
    return v


async def _fake_embed_batch(texts):
    return [_fake_vec(t) for t in texts]


async def _fake_embed_text(text):
    return _fake_vec(text)


@pytest.fixture
def local_engine(monkeypatch, tmp_path):
    """Force the local backend with a fresh DB and a fake embedder everywhere."""
    import br8n.store as store_pkg

    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "engine.db"))
    store_pkg._local_stores.clear()

    # Patch the embedder at every import site (functions bound `embed_*` at import).
    import br8n.agent.preamble as preamble
    import br8n.capture.service as capture_service

    monkeypatch.setattr(capture_service, "embed_batch", _fake_embed_batch)
    monkeypatch.setattr(preamble, "embed_text", _fake_embed_text)
    # Both modules gate on a real credential before using the embedder. Force it
    # on, or these tests silently exercise the keyless path — and pass or fail
    # depending on whether the developer happens to have a key in .env.
    monkeypatch.setattr(capture_service, "embeddings_configured", lambda: True)
    monkeypatch.setattr(preamble, "embeddings_configured", lambda: True)

    yield store_pkg
    store_pkg._local_stores.clear()


def _snapshot(hypothesis: str):
    from br8n.capture.models import WorkspaceSnapshot

    return WorkspaceSnapshot(
        project_path="/tmp/proj",
        trigger="blur",
        captured_at="2026-05-29T12:00:00Z",
        branch="main",
        git_diff_stat="1 file changed",
        open_files=["a.py"],
        cursor_file="a.py",
        cursor_line=10,
        terminal_tail="pytest",
        hypothesis=hypothesis,
    )


def test_resolve_tenant_local(local_engine):
    from br8n.interfaces.mcp.tenancy import resolve_tenant

    ctx = resolve_tenant("proj", "kb", create=True)
    assert ctx.org_id == "local"
    assert ctx.access_token == ""
    assert ctx.user_id == "local"
    assert ctx.kb_id  # a real generated id


async def test_persist_snapshot_lands_in_store(local_engine):
    from br8n.capture.service import persist_snapshot
    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.store import get_store

    ctx = resolve_tenant("proj", "kb", create=True)
    finding_id = await persist_snapshot(ctx, _snapshot("Investigating the cache bug"))

    store = get_store(ctx.access_token)
    got = store.get_finding(ctx.kb_id, finding_id)
    assert got["category"] == "snapshot"
    assert "Investigating the cache bug" in (got["title"] + got["content"])

    listed = store.list_findings(ctx.kb_id, category="snapshot")
    assert listed["count"] == 1


async def test_select_preamble_reflects_snapshot(local_engine):
    from br8n.agent.preamble import select_preamble
    from br8n.capture.service import persist_snapshot
    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.store import get_store

    ctx = resolve_tenant("proj", "kb", create=True)
    snap = _snapshot("Tracking down the flaky timeout in the scheduler")
    await persist_snapshot(ctx, snap)

    store = get_store(ctx.access_token)
    # Query identical to the snapshot content → same fake vector → similarity ~1.0
    # → a band-1 hit. The preamble must mention the snapshot.
    from br8n.capture.adapter import snapshot_to_finding

    query = snapshot_to_finding(snap)["content"]
    preamble, coverage = await select_preamble(query, store=store, kb_id=ctx.kb_id)

    assert "<preamble>" in preamble
    assert "<finding" in preamble  # the snapshot banded into the preamble
    assert coverage in ("rich", "sparse")  # a hit landed (not "gap")

    # No query → no matching, but synopsis (empty) → coverage gap, still valid XML.
    empty_preamble, empty_cov = await select_preamble(None, store=store, kb_id=ctx.kb_id)
    assert empty_cov == "gap"
    assert "<preamble>" in empty_preamble


def test_synopsis_roundtrip_through_store(local_engine):
    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.store import get_store

    ctx = resolve_tenant("proj", "kb", create=True)
    store = get_store(ctx.access_token)

    assert store.load_synopsis(ctx.kb_id) is None
    store.upsert_synopsis(
        ctx.kb_id, [{"topic": "Scheduler", "gloss": "How jobs are queued"}], 3, "fake-model"
    )
    row = store.load_synopsis(ctx.kb_id)
    assert row["content"] == [{"topic": "Scheduler", "gloss": "How jobs are queued"}]
    assert row["finding_count_at_build"] == 3
    assert row["model"] == "fake-model"
    # built_at must be populated so the age-based rebuild trigger can fire locally.
    assert row["built_at"]


async def test_maybe_rebuild_synopsis_local(local_engine, monkeypatch):
    """The fire-and-forget rebuild path, with the LLM build faked (no Anthropic)."""
    import br8n.agent.synopsis as synopsis
    from br8n.capture.service import persist_snapshot
    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.store import get_store

    ctx = resolve_tenant("proj", "kb", create=True)
    # Seed enough snapshots to clear the rebuild_delta threshold.
    for i in range(6):
        await persist_snapshot(ctx, _snapshot(f"hypothesis {i}"))

    async def _fake_build(findings, cfg):
        return [{"topic": "Captured work", "gloss": f"{len(findings)} snapshots"}]

    monkeypatch.setattr(synopsis, "_build", _fake_build)
    await synopsis.maybe_rebuild_synopsis(ctx)

    row = get_store(ctx.access_token).load_synopsis(ctx.kb_id)
    assert row is not None
    assert row["content"][0]["topic"] == "Captured work"
    assert row["finding_count_at_build"] == 6

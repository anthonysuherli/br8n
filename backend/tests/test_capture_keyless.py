"""Capture must work on a bare install with no embedding credential.

The install docs promise that capture and resume need no API key, and that only
semantic search does. That promise is load-bearing for the free tier, so it gets
a test rather than a comment: without a key the snapshot is stored unembedded,
and with one it is embedded as usual.
"""

from __future__ import annotations

import pytest

from br8n.agent.state import TenantContext
from br8n.capture.models import WorkspaceSnapshot
from br8n.capture.service import persist_snapshot
from br8n.store import get_store


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr("br8n.capture.service.schedule_rebuild", lambda ctx: None)
    from br8n import store as store_mod

    store_mod._local_stores.clear()
    yield
    store_mod._local_stores.clear()


def _ctx() -> TenantContext:
    return TenantContext(
        user_id="local",
        org_id="local",
        project_id="p-1",
        kb_id="kb-1",
        thread_id="t-1",
        access_token="",
    )


def _snap() -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        project_path="/code/br8n",
        trigger="manual",
        captured_at="2026-07-26T10:00:00Z",
        branch="main",
        hypothesis="release prep",
    )


async def test_capture_succeeds_without_any_embedding_key(local_env, monkeypatch):
    monkeypatch.setattr("br8n.capture.service.embeddings_configured", lambda: False)

    async def explode(texts):
        raise AssertionError("must not call the embedding API without a credential")

    monkeypatch.setattr("br8n.capture.service.embed_batch", explode)

    finding_id = await persist_snapshot(_ctx(), _snap())

    assert finding_id
    stored = get_store("").get_finding("kb-1", finding_id)
    assert stored["title"]


async def test_resume_card_is_not_empty_without_a_key(local_env, monkeypatch):
    """The keyless install must still answer "where was I".

    Capture stores findings unembedded with no credential, so a similarity
    search has nothing to rank and would return an empty card. select_preamble
    falls back to recency instead, which is what the resume card wants anyway.
    """
    monkeypatch.setattr("br8n.capture.service.embeddings_configured", lambda: False)
    monkeypatch.setattr("br8n.agent.preamble.embeddings_configured", lambda: False)

    async def explode(*a, **kw):
        raise AssertionError("must not embed without a credential")

    monkeypatch.setattr("br8n.capture.service.embed_batch", explode)
    monkeypatch.setattr("br8n.agent.preamble.embed_text", explode)

    ctx = _ctx()
    await persist_snapshot(ctx, _snap())

    from br8n.agent.preamble import select_preamble

    preamble, coverage = await select_preamble(
        "where was I", store=get_store(""), kb_id=ctx.kb_id
    )

    assert "<finding" in preamble, "keyless resume returned an empty card"
    assert "release prep" in preamble  # the captured hypothesis survives
    # Recency-ordered, not relevance-ranked — say so rather than claiming rich.
    assert coverage == "sparse"


async def test_capture_still_embeds_when_a_key_is_present(local_env, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr("br8n.capture.service.embeddings_configured", lambda: True)

    async def fake_embed(texts):
        seen["texts"] = list(texts)
        return [[0.1] * 1536 for _ in texts]

    monkeypatch.setattr("br8n.capture.service.embed_batch", fake_embed)

    finding_id = await persist_snapshot(_ctx(), _snap())

    assert finding_id
    assert len(seen["texts"]) == 1

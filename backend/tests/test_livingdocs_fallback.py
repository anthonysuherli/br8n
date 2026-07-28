"""Tests for the backend fallback note distiller (snapshots -> note).

Only the PURE, deterministic markdown synthesizer is unit-tested here. The async
``distill_fallback_note`` path is exercised by the existing local-store pattern
(see test_livingdocs_notes.py) and stays best-effort by contract.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from br8n.livingdocs.fallback import synth_note_markdown

DIM = 1536


def _fake_vec(text: str) -> list[float]:
    h = hashlib.sha256(text.encode()).digest()
    v = [0.0] * DIM
    for i in range(8):
        v[i] = (h[i] / 255.0) or 0.01
    return v


async def _fake_embed_batch(texts):
    return [_fake_vec(t) for t in texts]


def test_synth_note_markdown_from_snapshots():
    snaps = [
        {"title": "fix auth race", "content": "**Hypothesis**: fix auth race\n**Branch**: `main`"},
        {"title": "Working on auth.py", "content": "**Cursor**: `auth.py:42`"},
    ]
    md = synth_note_markdown(snaps, policy_sections=["Decisions", "Changes"])
    assert md.startswith("#")
    assert "## Changes" in md
    assert "auth" in md.lower()


def test_synth_note_markdown_empty():
    md = synth_note_markdown([], policy_sections=["Decisions"])
    assert md.startswith("#")  # still a valid titled doc


@pytest.mark.asyncio
async def test_distill_fallback_note_persists_from_snapshots(tmp_path, monkeypatch):
    """End-to-end against the real SQLiteStore: seed snapshots, then the fallback
    distiller fetches their content via get_finding and writes a `backend` note."""
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_LIVING_DOCS_LLM", "0")  # keep it deterministic, no LLM

    import br8n.livingdocs.notes as notes_mod
    import br8n.store as store_pkg

    store_pkg._local_stores.clear()
    monkeypatch.setattr(notes_mod, "embed_batch", _fake_embed_batch)
    monkeypatch.setattr(notes_mod, "schedule_rebuild", lambda ctx: None)

    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.livingdocs.fallback import distill_fallback_note
    from br8n.store import get_store

    ctx = resolve_tenant("proj", "main", create=True)
    store = get_store(ctx.access_token, org_id=ctx.org_id)

    # Seed two snapshot findings (content carries Branch/Cursor like the adapter emits).
    await store.insert_findings([
        {
            "org_id": ctx.org_id, "kb_id": ctx.kb_id,
            "title": "fix auth race", "category": "snapshot",
            "content": "**Hypothesis**: fix auth race\n**Branch**: `main`",
            "embedding": _fake_vec("a"), "tags": ["snapshot"], "provenance": [],
        },
        {
            "org_id": ctx.org_id, "kb_id": ctx.kb_id,
            "title": "Working on auth.py", "category": "snapshot",
            "content": "**Cursor**: `auth.py:42`",
            "embedding": _fake_vec("b"), "tags": ["snapshot"], "provenance": [],
        },
    ])

    res = await distill_fallback_note(
        ctx, project_path=str(tmp_path), kb="main", session_id="sess-fallback"
    )

    assert res is not None
    assert res["finding_id"]
    assert res["note_path"].endswith(".md")

    listed = store.list_findings(ctx.kb_id, category="note")
    assert listed["count"] == 1

    # local tier: canonical markdown is the vault file, not .br8n/notes/<kb>/
    note_path = Path(res["note_path"])
    assert str(tmp_path / "vault" / "notes") in str(note_path)
    body = note_path.read_text()
    assert "## Changes" in body
    assert "auth" in body.lower()

    store_pkg._local_stores.clear()


@pytest.mark.asyncio
async def test_distill_fallback_note_no_snapshots_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))

    import br8n.store as store_pkg
    store_pkg._local_stores.clear()

    from br8n.interfaces.mcp.tenancy import resolve_tenant
    from br8n.livingdocs.fallback import distill_fallback_note

    ctx = resolve_tenant("proj", "main", create=True)
    res = await distill_fallback_note(
        ctx, project_path=str(tmp_path), kb="main", session_id="sess-empty"
    )
    assert res is None

    store_pkg._local_stores.clear()

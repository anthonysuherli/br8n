"""Findings `metadata` round-trip against the real in-memory SQLiteStore."""

from __future__ import annotations

import pytest

from br8n.store import SQLiteStore


@pytest.fixture
def store() -> SQLiteStore:
    return SQLiteStore(":memory:")


def _row(kb_id: str, title: str, metadata: dict | None) -> dict:
    return {
        "org_id": "ignored",
        "kb_id": kb_id,
        "title": title,
        "content": "body",
        "category": "snapshot",
        "confidence": 1.0,
        "tags": ["snapshot", "manual"],
        "provenance": [],
        "metadata": metadata,
    }


async def test_metadata_round_trips_on_get(store):
    meta = {"hypothesis": "JWT caching stale tokens", "next_action": "rerun test_auth", "thread_id": "abc123"}
    [fid] = await store.insert_findings([_row("kb1", "t1", meta)])
    got = store.get_finding("kb1", fid)
    assert got["metadata"] == meta


async def test_metadata_present_in_list_findings(store):
    meta = {"next_action": "open store/base.py"}
    await store.insert_findings([_row("kb1", "t1", meta)])
    rows = store.list_findings("kb1", category="snapshot")["findings"]
    assert rows[0]["metadata"] == meta


async def test_missing_metadata_reads_as_none(store):
    row = _row("kb1", "t1", None)
    del row["metadata"]  # caller may omit the key entirely
    [fid] = await store.insert_findings([row])
    assert store.get_finding("kb1", fid)["metadata"] is None
    assert store.list_findings("kb1")["findings"][0]["metadata"] is None

"""AC6: keyless reindex must restore vectors, not just text.

On a keyless machine with the local extra installed, deleting brain.db and
running ``python -m br8n.vault.reindex`` must restore the vector space too —
that is the End Goal 6 promise the feature exists to keep. A fresh CLI
process never has the local model resident yet (embeddings_configured() is
False on its first call by design; warm-up is fire-and-forget on a daemon
thread), so reindex() must block on loading the model itself before draining
stale embeddings.
"""
import sys
import types

import pytest

from br8n import settings_file
from br8n.clients import embed_local, embeddings
from br8n.vault import reindex as rmod


class _FakeTextEmbedding:
    """Stands in for fastembed.TextEmbedding (mirrors test_embed_local.py)."""

    def __init__(self, **kwargs):
        pass

    def embed(self, texts):
        for i, t in enumerate(texts):
            yield [float(len(t) + i)] * 384


@pytest.fixture
def fake_fastembed(monkeypatch):
    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    embed_local.reset()
    yield
    embed_local.reset()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_BACKEND", "local")
    from br8n.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()
    yield
    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()


@pytest.mark.asyncio
async def test_reindex_restores_vectors_for_local_provider(
    fake_fastembed, monkeypatch, tmp_path
):
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    settings_file.save_setting("embedding_provider", "local")

    from br8n.store.vault import VaultStore

    store = VaultStore(str(tmp_path / "brain.db"))
    org_id, pid = store.resolve_project("br8n", create=True)
    kb_id = store.resolve_kb(org_id, pid, "main", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "Keep me", "content": "canonical", "category": "note",
          "confidence": 1.0, "tags": ["note"], "provenance": [], "embedding": None}]
    )
    store.close()
    (tmp_path / "brain.db").unlink()  # the index is disposable

    # Simulate a genuinely fresh CLI process: no resident model, no warm-up
    # thread already in flight from the insert above.
    embed_local.reset()

    result = await rmod.reindex(str(tmp_path / "brain.db"))
    assert result["re_embedded"] >= 1

    fresh = VaultStore(str(tmp_path / "brain.db"))
    row = fresh._conn.execute(
        "SELECT embedding FROM vec_findings WHERE finding_id = ?;", (fid,)
    ).fetchone()
    assert row is not None, "reindex restored text but not the vector"
    fresh.close()

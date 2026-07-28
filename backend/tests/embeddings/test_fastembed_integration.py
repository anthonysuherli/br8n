"""Real fastembed — skipped unless the optional extra is installed."""
import pytest

fastembed = pytest.importorskip("fastembed", reason="needs br8n[local-embeddings]")


@pytest.mark.asyncio
async def test_real_model_produces_384_dim_vectors(monkeypatch):
    from br8n.clients import embed_local

    embed_local.reset()
    assert embed_local.installed() is True
    assert embed_local.load_now() is True
    out = await embed_local.embed(["a resume card for the br8n project"])
    assert len(out) == 1 and len(out[0]) == 384
    assert all(isinstance(x, float) for x in out[0][:8])
    embed_local.reset()


@pytest.mark.asyncio
async def test_similar_text_ranks_above_unrelated():
    """Sanity that the vectors carry meaning, not just shape."""
    from br8n.clients import embed_local

    embed_local.reset()
    embed_local.load_now()
    q, near, far = await embed_local.embed(
        ["how do I resume my work", "pick up where I left off", "banana bread recipe"]
    )

    def cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb)

    assert cos(q, near) > cos(q, far)
    embed_local.reset()

"""Local provider: readiness gating, warm-up, threading, façade routing."""
import sys
import types

import pytest

from br8n import settings_file
from br8n.clients import embed_local, embeddings


class _FakeTextEmbedding:
    """Stands in for fastembed.TextEmbedding — records kwargs, yields vectors."""

    last_kwargs: dict = {}
    fail_local_only = False

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        if kwargs.get("local_files_only") and type(self).fail_local_only:
            raise RuntimeError("model not cached")

    def embed(self, texts):
        for i, t in enumerate(texts):
            yield [float(len(t) + i)] * 384


@pytest.fixture
def fake_fastembed(monkeypatch):
    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    _FakeTextEmbedding.fail_local_only = False
    embed_local.reset()
    yield _FakeTextEmbedding
    embed_local.reset()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
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


def test_installed_reflects_import_availability(fake_fastembed):
    assert embed_local.installed() is True


def test_load_now_makes_it_ready(fake_fastembed):
    assert embed_local.ready() is False
    assert embed_local.load_now() is True
    assert embed_local.ready() is True


def test_load_passes_model_and_thread_pinning(fake_fastembed):
    embed_local.load_now()
    kwargs = fake_fastembed.last_kwargs
    assert kwargs["model_name"] == "BAAI/bge-small-en-v1.5"
    assert kwargs["threads"] == 1


def test_uncached_model_falls_back_to_downloading_load(fake_fastembed):
    """local_files_only avoids a known firewall hang; a miss then downloads."""
    fake_fastembed.fail_local_only = True
    assert embed_local.load_now() is True
    assert fake_fastembed.last_kwargs.get("local_files_only") is not True


@pytest.mark.asyncio
async def test_embed_returns_correct_width(fake_fastembed):
    embed_local.load_now()
    out = await embed_local.embed(["ab", "cde"])
    assert [len(v) for v in out] == [384, 384]
    assert out[0][0] == 2.0 and out[1][0] == 4.0


def test_warm_up_is_idempotent_and_non_blocking(fake_fastembed):
    embed_local.warm_up()
    embed_local.warm_up()
    embed_local.wait_for_warm_up(timeout=5)
    assert embed_local.ready() is True


def test_missing_extra_reports_uninstalled(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    embed_local.reset()
    monkeypatch.setattr(
        "importlib.util.find_spec", lambda name: None if name == "fastembed" else True
    )
    assert embed_local.installed() is False


def test_configured_is_false_until_model_is_resident(fake_fastembed, monkeypatch):
    """Zero-friction capture: never block a write on a load or a download."""
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    settings_file.save_setting("embedding_provider", "local")
    assert embeddings.active_embedder().provider == "local"
    assert embeddings.embeddings_configured() is False  # schedules warm-up
    embed_local.wait_for_warm_up(timeout=5)
    assert embeddings.embeddings_configured() is True


@pytest.mark.asyncio
async def test_facade_routes_batch_to_local(fake_fastembed, monkeypatch):
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    settings_file.save_setting("embedding_provider", "local")
    embed_local.load_now()
    out = await embeddings.embed_batch(["hello"])
    assert len(out) == 1 and len(out[0]) == 384


@pytest.mark.asyncio
async def test_facade_raises_for_none_provider(monkeypatch):
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    monkeypatch.setattr(embeddings, "_local_eligible", lambda: False)
    with pytest.raises(RuntimeError):
        await embeddings.embed_batch(["hello"])

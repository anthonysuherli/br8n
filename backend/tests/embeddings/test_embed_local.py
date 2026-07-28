"""Local provider: readiness gating, warm-up, threading, façade routing."""
import asyncio
import sys
import time
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


class _WrongWidthTextEmbedding:
    """Returns vectors of the wrong width — for the width guard."""

    def __init__(self, **kwargs):
        pass

    def embed(self, texts):
        for _ in texts:
            yield [0.0] * 10


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


def test_installed_false_when_fastembed_not_importable(monkeypatch):
    """The real-world branch: fastembed was never imported at all, and
    find_spec (genuine discovery, not a sys.modules hit) says it's absent."""
    monkeypatch.delitem(sys.modules, "fastembed", raising=False)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    embed_local.reset()
    assert embed_local.installed() is False


@pytest.mark.asyncio
async def test_cold_embed_batch_keeps_the_loop_responsive(fake_fastembed, monkeypatch):
    """C1: a cold local provider must not freeze the event loop while it loads."""
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    settings_file.save_setting("embedding_provider", "local")

    def slow_init(self, **kwargs):
        time.sleep(0.5)
        type(self).last_kwargs = kwargs

    monkeypatch.setattr(fake_fastembed, "__init__", slow_init)

    # Warm two one-time costs that are unrelated to what this test is about
    # (the event-loop cost of a *cold local-model load*), so they don't get
    # misattributed to it: the loop's default thread-pool executor (OS thread
    # creation can be tens to hundreds of ms on a loaded sandbox) and
    # active_embedder()'s first call (imports br8n.store).
    await asyncio.to_thread(lambda: None)
    embeddings.active_embedder()

    gaps: list[float] = []
    stop = False

    async def ticker():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(0)  # let the ticker actually start before the cold load begins
    out = await embeddings.embed_batch(["hello"])
    stop = True
    await ticker_task

    assert len(out) == 1 and len(out[0]) == 384
    assert gaps, "ticker never got a chance to run"
    assert max(gaps) < 0.15, f"event loop stalled: max tick gap {max(gaps):.3f}s"


def test_configured_does_not_block_on_an_in_flight_load(fake_fastembed, monkeypatch):
    """C2: embeddings_configured() must return quickly on every call, even
    while a warm-up load is in flight — only the first call may schedule it."""
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    settings_file.save_setting("embedding_provider", "local")

    def slow_init(self, **kwargs):
        time.sleep(0.3)
        type(self).last_kwargs = kwargs

    monkeypatch.setattr(fake_fastembed, "__init__", slow_init)

    # Warm active_embedder()'s first-call cost (imports br8n.store) — it's
    # unrelated to what this test checks and must not be attributed to it.
    embeddings.active_embedder()

    t0 = time.monotonic()
    first = embeddings.embeddings_configured()
    d0 = time.monotonic() - t0

    t1 = time.monotonic()
    second = embeddings.embeddings_configured()
    d1 = time.monotonic() - t1

    assert first is False
    assert second is False
    assert d0 < 0.1, f"first call took {d0:.3f}s"
    assert d1 < 0.1, f"second call took {d1:.3f}s"

    embed_local.wait_for_warm_up(timeout=5)
    assert embed_local.ready() is True


def test_warm_up_stops_after_max_consecutive_failures(monkeypatch):
    """I3: don't respawn a doomed load forever — give up after 3 in a row."""
    embed_local.reset()
    monkeypatch.setattr(embed_local, "installed", lambda: True)
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    settings_file.save_setting("embedding_provider", "local")

    calls = {"n": 0}

    def failing_load():
        calls["n"] += 1
        return False

    monkeypatch.setattr(embed_local, "load_now", failing_load)

    for _ in range(5):
        assert embeddings.embeddings_configured() is False
        embed_local.wait_for_warm_up(timeout=5)

    assert 1 <= calls["n"] <= 3, f"expected at most 3 load attempts, got {calls['n']}"
    embed_local.reset()


@pytest.mark.asyncio
async def test_embed_raises_on_width_mismatch(monkeypatch):
    """A model returning the wrong width must never reach the fixed-width
    vector table — fail loudly instead."""
    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = _WrongWidthTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    embed_local.reset()
    embed_local.load_now()
    with pytest.raises(RuntimeError):
        await embed_local.embed(["x"])
    embed_local.reset()

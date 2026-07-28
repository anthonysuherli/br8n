"""Provider precedence: settings file > config/env > auto-detect, plus the tier guard."""
import pytest

from br8n import settings_file
from br8n.clients import embeddings


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_BACKEND", "local")
    settings_file.clear_cache()
    # get_settings()/get_config() are lru_cached — clear so env edits take effect
    from br8n.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    yield
    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()


def _no_keys(monkeypatch):
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)


def _keys(monkeypatch):
    monkeypatch.setattr(embeddings, "_creds_present", lambda: True)


def _local_ok(monkeypatch, ok=True):
    monkeypatch.setattr(embeddings, "_local_eligible", lambda: ok)


def test_auto_prefers_remote_when_key_present(monkeypatch):
    _keys(monkeypatch)
    _local_ok(monkeypatch)
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.dim, ident.source) == ("remote", 1536, "auto")


def test_auto_falls_back_to_local_when_keyless(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch)
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.model, ident.dim) == (
        "local", "BAAI/bge-small-en-v1.5", 384
    )
    assert ident.source == "auto"


def test_auto_yields_none_when_keyless_and_no_extra(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch, False)
    assert embeddings.active_embedder().provider == "none"


def test_settings_file_beats_auto_detect(monkeypatch):
    _keys(monkeypatch)  # a key IS present; the user still asked for local
    _local_ok(monkeypatch)
    settings_file.save_setting("embedding_provider", "local")
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.source) == ("local", "settings")


def test_settings_file_beats_config(monkeypatch):
    _keys(monkeypatch)
    _local_ok(monkeypatch)
    monkeypatch.setenv("B2_EMBEDDING__PROVIDER", "remote")
    from br8n.config import get_config

    get_config.cache_clear()
    settings_file.save_setting("embedding_provider", "none")
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.source) == ("none", "settings")


def test_config_beats_auto_detect(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch)
    monkeypatch.setenv("B2_EMBEDDING__PROVIDER", "none")
    from br8n.config import get_config

    get_config.cache_clear()
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.source) == ("none", "config")


def test_stored_auto_still_auto_detects(monkeypatch):
    """A stored literal "auto" is not an explicit choice — it means "detect
    it" — so the provider it resolves to IS decided by auto-detection and
    must report source="auto", not "settings". This is what the work-at-risk
    gate in br8n.store.sqlite keys off of to decide defer-vs-rebuild."""
    _no_keys(monkeypatch)
    _local_ok(monkeypatch)
    settings_file.save_setting("embedding_provider", "auto")
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.source) == ("local", "auto")


def test_explicit_remote_without_a_key_degrades_to_none(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch)
    settings_file.save_setting("embedding_provider", "remote")
    assert embeddings.active_embedder().provider == "none"


def test_explicit_local_without_eligibility_degrades_to_none(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch, False)
    settings_file.save_setting("embedding_provider", "local")
    assert embeddings.active_embedder().provider == "none"


def test_local_ineligible_on_cloud_tier(monkeypatch):
    """The real guard: cloud pgvector columns are 1536-wide."""
    monkeypatch.setenv("BR8N_BACKEND", "cloud")
    monkeypatch.setattr(
        "br8n.clients.embed_local.installed", lambda: True, raising=False
    )
    assert embeddings._local_eligible() is False


def test_none_identity_has_zero_dim(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch, False)
    ident = embeddings.active_embedder()
    assert ident.dim == 0 and ident.model == ""

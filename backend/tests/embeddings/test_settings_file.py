"""Machine-level settings.json: path resolution, round-trip, cache invalidation."""
import json

import pytest

from br8n import settings_file


@pytest.fixture(autouse=True)
def _tmp_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    settings_file.clear_cache()
    yield
    settings_file.clear_cache()


def test_path_sits_beside_the_db(tmp_path):
    assert settings_file.settings_path() == tmp_path / "settings.json"


def test_missing_file_is_empty_dict():
    assert settings_file.load_settings() == {}


def test_save_then_load_round_trip():
    settings_file.save_setting("embedding_provider", "local")
    assert settings_file.load_settings()["embedding_provider"] == "local"
    on_disk = json.loads(settings_file.settings_path().read_text())
    assert on_disk == {"embedding_provider": "local"}


def test_save_merges_and_none_removes():
    settings_file.save_setting("embedding_provider", "local")
    settings_file.save_setting("other", 1)
    assert settings_file.load_settings() == {"embedding_provider": "local", "other": 1}
    settings_file.save_setting("embedding_provider", None)
    assert settings_file.load_settings() == {"other": 1}


def test_corrupt_file_degrades_to_empty():
    settings_file.settings_path().write_text("{not json")
    assert settings_file.load_settings() == {}


def test_external_write_is_picked_up_without_restart():
    """The MCP server is long-lived: a write by another process must be seen."""
    assert settings_file.load_settings() == {}
    settings_file.settings_path().write_text('{"embedding_provider": "remote"}')
    assert settings_file.load_settings()["embedding_provider"] == "remote"


def test_atomic_write_leaves_no_tmp(tmp_path):
    settings_file.save_setting("embedding_provider", "none")
    assert not (tmp_path / "settings.json.tmp").exists()


def test_embedding_config_defaults():
    from br8n.config import EmbeddingConfig

    cfg = EmbeddingConfig()
    assert cfg.provider == "auto"
    assert cfg.local_model == "BAAI/bge-small-en-v1.5"
    assert cfg.local_dim == 384
    assert cfg.local_threads == 1

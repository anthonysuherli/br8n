"""--check reports the active embedder; vault/embedding problems stay warnings."""


def test_check_prints_embeddings_line(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n.api.main import check

    rc = check()
    out = capsys.readouterr().out
    assert rc == 0
    assert "embeddings" in out


def test_check_names_provider_and_source(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n import settings_file
    from br8n.config import get_config, get_settings

    settings_file.clear_cache()
    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.save_setting("embedding_provider", "none")
    from br8n.api.main import check

    check()
    out = capsys.readouterr().out
    assert "none" in out and "settings" in out
    settings_file.save_setting("embedding_provider", None)

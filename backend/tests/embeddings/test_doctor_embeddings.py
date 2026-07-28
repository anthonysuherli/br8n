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


def test_check_reports_deferred_switch_at_warn_without_flipping_exit_code(
    monkeypatch, tmp_path, capsys
):
    """Change B surfacing: a deferred auto-detected switch (existing vectors,
    environment changed) must be visible in --check at 'warn', naming both
    spaces and pointing at /br8n:embeddings — and must never flip the exit
    code (0), since capture/resume keep working regardless."""
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n import settings_file
    from br8n.config import get_config, get_settings

    settings_file.clear_cache()
    get_settings.cache_clear()
    get_config.cache_clear()

    monkeypatch.setattr("br8n.clients.embeddings._creds_present", lambda: True)
    from br8n.store import get_store

    store = get_store()
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    import asyncio

    asyncio.run(store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    ))
    assert store.embedding_space()["dim"] == 1536

    # Environment drift: key silently gone, local now auto-eligible.
    monkeypatch.setattr("br8n.clients.embeddings._creds_present", lambda: False)
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: True)

    from br8n.api.main import check

    rc = check()
    out = capsys.readouterr().out
    assert rc == 0  # never flips the exit code
    assert "warn" in out
    assert "/br8n:embeddings" in out
    assert "1536" in out and "384" in out  # both spaces named
    # Reporting must not have touched the live store.
    assert store.embedding_space()["dim"] == 1536

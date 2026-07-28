"""--check reports vault health on the local tier."""
def test_check_prints_vault_block(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n.api.main import check

    rc = check()
    out = capsys.readouterr().out
    assert rc == 0
    assert "vault" in out
    assert str(tmp_path / "vault") in out


def test_check_reports_warn_on_vault_drift(monkeypatch, tmp_path, capsys):
    """A finding whose vault file was unlinked out from under it (drift) must
    surface as a warn-status vault line pointing at the reindex fix, without
    failing the overall doctor exit code (fail-silent tier)."""
    import asyncio
    from pathlib import Path

    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n.store.vault import VaultStore

    s = VaultStore(str(tmp_path / "brain.db"))
    org_id, project_id = s.resolve_project("br8n", create=True)
    kb_id = s.resolve_kb(org_id, project_id, "main", create=True)
    [fid] = asyncio.run(
        s.insert_findings(
            [{"kb_id": kb_id, "title": "N", "content": "body", "category": "note",
              "confidence": 1.0, "tags": [], "provenance": [], "embedding": None}]
        )
    )
    Path(s.vault_path_for(fid)).unlink()  # drift: indexed but file missing
    s.close()

    from br8n.api.main import check

    rc = check()
    out = capsys.readouterr().out
    assert rc == 0
    assert "warn" in out
    assert "vault" in out
    assert "br8n.vault.reindex" in out

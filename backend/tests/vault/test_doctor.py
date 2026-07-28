"""--check reports vault health on the local tier."""
import asyncio

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    from br8n.store.vault import VaultStore

    s = VaultStore(str(tmp_path / "brain.db"))
    yield s
    s.close()


def _insert(store, title="Note"):
    org_id, project_id = store.resolve_project("br8n", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)
    [fid] = asyncio.run(
        store.insert_findings(
            [{"kb_id": kb_id, "title": title, "content": "body", "category": "note",
              "confidence": 1.0, "tags": [], "provenance": [], "embedding": None}]
        )
    )
    return fid


def test_vault_health_counts_rows_not_distinct_paths(store):
    """Two rows sharing one vault_path (index corruption) must both be
    visible in `indexed` — a distinct-path count would mask the duplicate."""
    from br8n.vault import reconcile

    fid = _insert(store)
    other = _insert(store, title="Other")
    path = store.vault_path_for(fid)
    store._conn.execute(
        "UPDATE findings SET vault_path = ? WHERE id = ?;", (path, other)
    )
    store._conn.commit()

    h = reconcile.vault_health(store)
    assert h["indexed"] == 2


def test_vault_health_walk_is_capped(store):
    """A zero budget must stop the walk (capped=True) and skip the
    missing_files computation — a partial walk must not misreport drift."""
    from pathlib import Path

    from br8n.vault import reconcile

    fid = _insert(store)
    _insert(store, title="Other")
    Path(store.vault_path_for(fid)).unlink()  # genuine drift on disk

    h = reconcile.vault_health(store, budget_ms=0)
    assert h["capped"] is True
    assert h["missing_files"] == 0  # not computed from a partial walk

    full = reconcile.vault_health(store)
    assert full["capped"] is False
    assert full["missing_files"] == 1

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

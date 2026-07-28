"""Vault layout: root resolution, safe segments, deterministic file paths."""
from br8n.vault import layout


def test_vault_root_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "myvault"))
    root = layout.vault_root()
    assert root == tmp_path / "myvault"
    assert root.is_dir()  # created on demand


def test_vault_root_siblings_db_path(monkeypatch, tmp_path):
    monkeypatch.delenv("BR8N_VAULT_PATH", raising=False)
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    assert layout.vault_root() == tmp_path / "vault"


def test_safe_segment_and_slug():
    assert layout.safe_segment("feat/vault") == "feat__vault"
    assert layout.safe_segment("") == "default"
    assert layout.slug("Fix the Auth Bug!") == "fix-the-auth-bug"
    assert layout.slug("") == "untitled"


def test_safe_segment_rejects_dot_segments():
    assert layout.safe_segment("..") == "default"
    assert layout.safe_segment(".") == "default"
    assert layout.safe_segment("...") == "default"


def test_file_path_rejects_dot_segment_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path))
    p = layout.file_path(
        "note", "..", "..", "2026-07-27T14:30:00+00:00", "Escape", "abcd1234ef"
    )
    assert tmp_path.resolve() in p.resolve().parents


def test_category_mapping():
    assert layout.category_dir("snapshot") == "snapshots"
    assert layout.category_dir("note") == "notes"
    assert layout.category_dir("journal") == "journal"
    assert layout.category_dir("concept") == "findings"
    assert layout.file_type("snapshot") == "snapshot"
    assert layout.file_type("concept") == "finding"


def test_file_path_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path))
    p = layout.file_path(
        "note", "br8n", "feat/vault", "2026-07-27T14:30:00+00:00", "My Note", "abcd1234ef"
    )
    assert p == tmp_path / "notes" / "br8n" / "feat__vault" / "2026-07-27-1430-my-note-abcd1234.md"
    j = layout.file_path(
        "journal", "__journal__", "__journal__", "2026-07-27T14:30:00+00:00", "Entry", "abcd1234ef"
    )
    assert j == tmp_path / "journal" / "2026" / "2026-07-27-1430-entry-abcd1234.md"


def test_vault_config_defaults():
    from br8n.config import VaultConfig

    cfg = VaultConfig()
    assert cfg.reconcile_debounce_seconds == 20.0
    assert cfg.reconcile_time_cap_ms == 200
    assert cfg.reconcile_batch_cap == 200
    assert cfg.re_embed_batch == 32

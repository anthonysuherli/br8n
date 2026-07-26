
from br8n.livingdocs.paths import DocPaths, ensure_layout


def test_paths_layout(tmp_path):
    p = DocPaths(project_path=str(tmp_path), kb="main")
    assert p.root == tmp_path / ".br8n"
    assert p.notes_dir == tmp_path / ".br8n" / "notes" / "main"
    assert p.docs_dir == tmp_path / ".br8n" / "docs"
    assert p.policy_path == tmp_path / ".br8n" / "notes-policy.json"
    assert p.state_path == tmp_path / ".br8n" / "docs-state.json"


def test_ensure_layout_creates_dirs_and_gitignore(tmp_path):
    p = DocPaths(project_path=str(tmp_path), kb="feat/x")
    ensure_layout(p)
    assert p.notes_dir.is_dir()
    assert p.docs_dir.is_dir()
    gitignore = (tmp_path / ".br8n" / ".gitignore")
    assert gitignore.read_text().strip() == "*"


def test_kb_with_slashes_is_filesystem_safe(tmp_path):
    p = DocPaths(project_path=str(tmp_path), kb="feature/auth-fix")
    assert p.notes_dir == tmp_path / ".br8n" / "notes" / "feature__auth-fix"


def test_timeline_paths(tmp_path):
    from br8n.livingdocs.paths import DocPaths

    p = DocPaths(project_path=str(tmp_path), kb="main")
    assert p.timeline_dir == p.root / "timeline"
    assert p.timeline_state_path == p.root / "timeline-state.json"

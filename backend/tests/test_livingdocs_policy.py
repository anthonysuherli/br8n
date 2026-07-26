from br8n.livingdocs.paths import DocPaths
from br8n.livingdocs.policy import load_policy, save_policy, default_policy


def test_default_policy_sections():
    pol = default_policy()
    names = [s.name for s in pol.sections if s.enabled]
    assert names == ["Decisions", "Changes", "Open Questions", "Next Steps"]
    assert pol.steer == ""


def test_load_returns_default_when_absent(tmp_path):
    p = DocPaths(project_path=str(tmp_path), kb="main")
    assert load_policy(p) == default_policy()


def test_save_then_load_roundtrip(tmp_path):
    p = DocPaths(project_path=str(tmp_path), kb="main")
    pol = default_policy()
    pol.steer = "focus on architecture; skip dep bumps"
    save_policy(p, pol)
    assert load_policy(p).steer == "focus on architecture; skip dep bumps"


def test_corrupt_policy_file_falls_back(tmp_path):
    from br8n.livingdocs.paths import ensure_layout
    p = DocPaths(project_path=str(tmp_path), kb="main")
    ensure_layout(p)
    p.policy_path.write_text("not json")
    assert load_policy(p) == default_policy()  # must not raise

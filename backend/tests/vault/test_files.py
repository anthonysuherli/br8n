"""Frontmatter round-trip, atomic writes, hashing."""
import os

import pytest

from br8n.vault import files


def test_atomic_write_tmp_names_are_unique(tmp_path, monkeypatch):
    """Concurrent writers to the same path must never share a tmp file —
    a fixed '<name>.tmp' let one writer replace with another's bytes."""
    target = tmp_path / "same.md"
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(files.os, "replace", spy)
    files.atomic_write(target, "first")
    files.atomic_write(target, "second")

    assert len(seen) == 2
    assert len(set(seen)) == 2  # distinct tmp names per call
    assert target.read_text() == "second"
    assert list(tmp_path.glob("*.tmp")) == []  # nothing left behind


def test_atomic_write_cleans_tmp_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "same.md"

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(files.os, "replace", boom)
    with pytest.raises(OSError):
        files.atomic_write(target, "text")
    assert list(tmp_path.glob("*.tmp")) == []  # unique tmp junk not orphaned


def test_serialize_parse_round_trip():
    fm = {
        "br8n_id": "abc123",
        "type": "note",
        "title": "Fix auth",
        "project": "br8n",
        "kb": "main",
        "created": "2026-07-27T14:30:00+00:00",
        "tags": ["note", "agent"],
        "confidence": 1.0,
        "source": "agent",
        "relates_to": "[[2026-07-20-snap]]",
    }
    text = files.serialize(fm, "# Fix auth\n\nBody line.")
    assert text.startswith("---\n")
    parsed_fm, body = files.parse(text)
    assert parsed_fm == fm
    assert body == "# Fix auth\n\nBody line.\n"


def test_serialize_drops_none_keys():
    text = files.serialize({"br8n_id": "x", "next_action": None}, "b")
    fm, _ = files.parse(text)
    assert "next_action" not in fm


def test_parse_no_frontmatter():
    fm, body = files.parse("just text\n")
    assert fm == {}
    assert body == "just text\n"


def test_parse_preserves_human_keys():
    text = files.serialize({"br8n_id": "x", "type": "note", "mood": "happy"}, "b")
    fm, _ = files.parse(text)
    assert fm["mood"] == "happy"


def test_parse_malformed_yaml_raises():
    bad = "---\ntags: [unclosed\n---\n\nbody\n"
    with pytest.raises(ValueError):
        files.parse(bad)


def test_atomic_write_and_hash(tmp_path):
    p = tmp_path / "a" / "b.md"
    h = files.atomic_write(p, "hello\n")
    assert p.read_text() == "hello\n"
    assert h == files.content_hash("hello\n")
    assert not p.with_suffix(".md.tmp").exists()


def test_title_from_body():
    assert files.title_from_body("# My Title\n\nx", "fb") == "My Title"
    assert files.title_from_body("no heading", "fb") == "fb"


def test_parse_empty_frontmatter_block_raises():
    with pytest.raises(ValueError):
        files.parse("---\n\n---\n\nbody\n")


def test_parse_non_dict_frontmatter_raises():
    with pytest.raises(ValueError):
        files.parse("---\n- a\n- b\n---\n\nbody\n")


def test_round_trip_unicode():
    fm = {"br8n_id": "x", "title": "日本語 — émoji 🚀"}
    parsed_fm, body = files.parse(files.serialize(fm, "内容 café"))
    assert parsed_fm == fm
    assert body == "内容 café\n"


def test_atomic_write_overwrites_existing(tmp_path):
    p = tmp_path / "f.md"
    files.atomic_write(p, "old\n")
    h = files.atomic_write(p, "new\n")
    assert p.read_text() == "new\n"
    assert h == files.content_hash("new\n")

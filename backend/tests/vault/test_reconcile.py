"""Reconcile: Obsidian edits stick, new files adopt, deletes delete."""
from pathlib import Path

import pytest

from br8n.vault import files as vfiles
from br8n.vault import layout, reconcile


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    from br8n.store.vault import VaultStore

    s = VaultStore(str(tmp_path / "brain.db"))
    yield s
    s.close()


def _mk_kb(store, project="br8n", kb="main"):
    org_id, project_id = store.resolve_project(project, create=True)
    return store.resolve_kb(org_id, project_id, kb, create=True)


async def _insert(store, kb_id, title="Note", content="# Note\n\nbody", category="note"):
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": title, "content": content, "category": category,
          "confidence": 1.0, "tags": ["note"], "provenance": [], "embedding": None}]
    )
    return fid


@pytest.mark.asyncio
async def test_edit_updates_index(store):
    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    path = store.vault_path_for(fid)
    text = open(path, encoding="utf-8").read()
    fm, _ = vfiles.parse(text)
    fm["title"] = "Edited title"
    open(path, "w", encoding="utf-8").write(vfiles.serialize(fm, "# Edited title\n\nnew body"))
    counters = reconcile.reconcile(store, force=True)
    assert counters["updated"] == 1
    row = store.get_finding(kb_id, fid)
    assert row["title"] == "Edited title"
    assert "new body" in row["content"]


@pytest.mark.asyncio
async def test_new_file_adopted_and_id_written_back(store):
    kb_id = _mk_kb(store)
    d = layout.vault_root() / "notes" / "br8n" / "main"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-07-27-1200-hand-written.md").write_text("# Hand written\n\nfrom obsidian\n")
    counters = reconcile.reconcile(store, force=True)
    assert counters["adopted"] == 1
    listed = store.list_findings(kb_id, category="note")
    assert any(f["title"] == "Hand written" for f in listed["findings"])
    fm, _ = vfiles.parse((d / "2026-07-27-1200-hand-written.md").read_text())
    assert fm["br8n_id"]  # engine wrote the join key back
    assert fm["source"] == "human"


@pytest.mark.asyncio
async def test_deleted_file_removes_row(store):
    import os

    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    os.unlink(store.vault_path_for(fid))
    counters = reconcile.reconcile(store, force=True)
    assert counters["deleted"] == 1
    with pytest.raises(RuntimeError):
        store.get_finding(kb_id, fid)


@pytest.mark.asyncio
async def test_malformed_frontmatter_skipped(store):
    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    path = store.vault_path_for(fid)
    open(path, "w", encoding="utf-8").write("---\ntags: [broken\n---\n\nbody\n")
    counters = reconcile.reconcile(store, force=True)
    assert counters["malformed"] == 1
    assert store.get_finding(kb_id, fid)["title"] == "Note"  # untouched


@pytest.mark.asyncio
async def test_debounce_skips_back_to_back(store):
    _mk_kb(store)
    first = reconcile.reconcile(store, force=True)
    second = reconcile.reconcile(store)  # immediately after → debounced
    assert second["skipped"] is True
    assert first["skipped"] is False


@pytest.mark.asyncio
async def test_views_never_scanned(store):
    _mk_kb(store)
    d = layout.vault_root() / layout.VIEWS_DIRNAME / "synopsis"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.md").write_text("# derived\n")
    counters = reconcile.reconcile(store, force=True)
    assert counters["adopted"] == 0


@pytest.mark.asyncio
async def test_reconcile_never_raises(store, monkeypatch):
    monkeypatch.setattr(layout, "vault_root", lambda: (_ for _ in ()).throw(OSError("boom")))
    counters = reconcile.reconcile(store, force=True)
    assert counters["scanned"] == 0  # degraded, no exception


# --- C1: missing/re-pointed vault root must never wipe the index ------------


@pytest.mark.asyncio
async def test_repointed_root_does_not_wipe_index(store, monkeypatch, tmp_path):
    import shutil

    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)

    # simulate the mount going away: the indexed file is genuinely gone AND
    # the root now resolves elsewhere (empty) — the delete sweep must not
    # mistake "scan saw nothing" for "everything was deleted"
    shutil.rmtree(layout.vault_root())
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "fresh-empty-vault"))

    counters = reconcile.reconcile(store, force=True, ignore_caps=True)

    assert counters["deleted"] == 0
    assert store.get_finding(kb_id, fid)["title"] == "Note"  # row untouched


# --- C2: an Obsidian rename must not delete the just-re-adopted row --------


@pytest.mark.asyncio
async def test_rename_survives_reconcile(store):
    import os

    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    old_path = Path(store.vault_path_for(fid))
    new_path = old_path.with_name("renamed-" + old_path.name)
    os.rename(old_path, new_path)

    counters = reconcile.reconcile(store, force=True, ignore_caps=True)

    assert counters["adopted"] == 1
    assert counters["deleted"] == 0
    row = store.get_finding(kb_id, fid)
    assert row["title"] == "Note"
    n = store._conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["n"]
    assert n == 1


# --- I1: the carry-over cursor must reach past the time cap ----------------


@pytest.mark.asyncio
async def test_cursor_scan_advances_past_time_cap(store, monkeypatch):
    from br8n.config import get_config

    kb_id = _mk_kb(store)
    fids = [await _insert(store, kb_id, title=f"Note {i}") for i in range(5)]
    paths = sorted(store.vault_path_for(f) for f in fids)
    far_path = paths[-1]
    far_fid = next(f for f in fids if store.vault_path_for(f) == far_path)

    # edit the lexicographically-last file — the scan must eventually reach it
    text = open(far_path, encoding="utf-8").read()
    fm, _ = vfiles.parse(text)
    fm["title"] = "Far edited"
    open(far_path, "w", encoding="utf-8").write(
        vfiles.serialize(fm, "# Far edited\n\nnew body")
    )

    cfg = get_config().vault
    monkeypatch.setattr(cfg, "reconcile_time_cap_ms", 0)  # ~1 stat per pass

    applied = False
    for _ in range(len(paths) + 2):
        counters = reconcile.reconcile(store, force=True)
        if counters["updated"] == 1:
            applied = True
            break

    assert applied, "far file was never reached by the cursor-driven scan"
    assert store.get_finding(kb_id, far_fid)["title"] == "Far edited"


# --- I3: a duplicated file (same br8n_id in two files) must not thrash -----


@pytest.mark.asyncio
async def test_duplicate_file_mints_fresh_id(store):
    import shutil

    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    orig_path = Path(store.vault_path_for(fid))
    dup_path = orig_path.with_name("dup-" + orig_path.name)
    shutil.copy(orig_path, dup_path)

    counters = reconcile.reconcile(store, force=True, ignore_caps=True)
    assert counters["adopted"] == 1

    listed = store.list_findings(kb_id, category="note")
    assert listed["count"] == 2
    ids = {f["id"] for f in listed["findings"]}
    assert len(ids) == 2

    fm1, _ = vfiles.parse(orig_path.read_text(encoding="utf-8"))
    fm2, _ = vfiles.parse(dup_path.read_text(encoding="utf-8"))
    assert fm1["br8n_id"] != fm2["br8n_id"]

    counters2 = reconcile.reconcile(store, force=True, ignore_caps=True)
    assert counters2["adopted"] == 0
    assert counters2["updated"] == 0
    assert counters2["deleted"] == 0
    assert counters2["malformed"] == 0


# --- I4: an abort mid-pass must roll back the shared connection ------------


@pytest.mark.asyncio
async def test_pass_abort_rolls_back_uncommitted_writes(store, monkeypatch):
    import os

    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    deleted_path = store.vault_path_for(fid)
    os.unlink(deleted_path)

    d = layout.vault_root() / "notes" / "br8n" / "main"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-07-27-1300-other.md").write_text("# Other\n\nbody\n")

    real_exists = Path.exists

    def flaky_exists(self):
        if str(self) == deleted_path:
            raise OSError("disk gone")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", flaky_exists)

    counters = reconcile.reconcile(store, force=True, ignore_caps=True)  # must not raise

    assert store._conn.in_transaction is False
    assert counters["adopted"] == 0  # Minor: rollback zeroes mutation counters
    assert counters["updated"] == 0
    assert counters["deleted"] == 0

    monkeypatch.undo()  # restore Path.exists before the follow-up read
    listed = store.list_findings(kb_id, category="note")
    assert not any(f["title"] == "Other" for f in listed["findings"])


# --- M2: malformed files must stop re-suspecting every pass -----------------


@pytest.mark.asyncio
async def test_malformed_file_stops_resuspecting(store):
    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    path = store.vault_path_for(fid)
    open(path, "w", encoding="utf-8").write("---\ntags: [broken\n---\n\nbody\n")

    first = reconcile.reconcile(store, force=True)
    assert first["malformed"] == 1

    second = reconcile.reconcile(store, force=True)
    assert second["malformed"] == 0
    assert second["scanned"] >= 1


# --- M4: tags must be clearable from Obsidian -------------------------------


@pytest.mark.asyncio
async def test_tags_cleared_from_obsidian(store):
    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    path = store.vault_path_for(fid)
    text = open(path, encoding="utf-8").read()
    fm, _ = vfiles.parse(text)
    fm.pop("tags", None)
    open(path, "w", encoding="utf-8").write(vfiles.serialize(fm, "# Note\n\nedited body"))

    counters = reconcile.reconcile(store, force=True)
    assert counters["updated"] == 1
    row = store.get_finding(kb_id, fid)
    assert row["tags"] == []


# --- M1: _target_for must not leak ValueError into the malformed counter ---


def test_target_for_outside_root_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    outside = tmp_path / "elsewhere" / "file.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("# x\n")

    assert reconcile._target_for(outside, {}) == ("unknown", "default", "finding")
    assert reconcile._target_for(outside, {"project": "p", "kb": "k"}) == ("p", "k", "finding")


# --- F1: deletion detection must not depend on a complete stat scan --------


@pytest.mark.asyncio
async def test_deletion_detected_despite_partial_scan(store, monkeypatch):
    import os

    from br8n.config import get_config

    kb_id = _mk_kb(store)
    fids = [await _insert(store, kb_id, title=f"Note {i}") for i in range(5)]
    victim = fids[0]
    victim_path = store.vault_path_for(victim)
    os.unlink(victim_path)

    cfg = get_config().vault
    monkeypatch.setattr(cfg, "reconcile_time_cap_ms", 0)  # forces a partial stat scan

    counters = reconcile.reconcile(store, force=True)

    assert counters["deleted"] == 1
    with pytest.raises(RuntimeError):
        store.get_finding(kb_id, victim)


# --- F2: a magnitude guard must block a mass-delete misread -----------------


@pytest.mark.asyncio
async def test_mass_delete_guard_blocks_wholesale_wipe(store):
    import os

    kb_id = _mk_kb(store)
    fids = [await _insert(store, kb_id, title=f"Note {i}") for i in range(12)]
    for fid in fids:
        os.unlink(store.vault_path_for(fid))  # dirs remain, files gone

    counters = reconcile.reconcile(store, force=True)

    assert counters["deleted"] == 0
    for fid in fids:
        assert store.get_finding(kb_id, fid)  # every row survives


@pytest.mark.asyncio
async def test_mass_delete_guard_bypassed_with_ignore_caps(store):
    import os

    kb_id = _mk_kb(store)
    fids = [await _insert(store, kb_id, title=f"Note {i}") for i in range(12)]
    for fid in fids:
        os.unlink(store.vault_path_for(fid))

    counters = reconcile.reconcile(store, force=True, ignore_caps=True)

    assert counters["deleted"] == 12
    for fid in fids:
        with pytest.raises(RuntimeError):
            store.get_finding(kb_id, fid)


# --- F3: re-adoption must not strip row metadata/provenance ----------------


@pytest.mark.asyncio
async def test_rename_preserves_row_metadata(store):
    import os

    kb_id = _mk_kb(store)
    meta = {"hypothesis": "H", "next_action": "A", "thread_id": "T"}
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "Snap", "content": "# Snap\n\nbody",
          "category": "snapshot", "confidence": 1.0, "tags": ["snap"],
          "provenance": [{"source": "capture", "session": "abc"}],
          "metadata": meta, "embedding": None}]
    )
    old_path = Path(store.vault_path_for(fid))
    new_path = old_path.with_name("renamed-" + old_path.name)
    os.rename(old_path, new_path)

    counters = reconcile.reconcile(store, force=True, ignore_caps=True)

    assert counters["adopted"] == 1
    row = store.get_finding(kb_id, fid)
    assert row["metadata"]["hypothesis"] == "H"
    assert row["metadata"]["thread_id"] == "T"
    assert row["metadata"]["next_action"] == "A"
    assert any(p.get("source") == "capture" for p in row["provenance"])


# --- Minor: scalar/comma-string tags must coerce to a list -----------------


@pytest.mark.asyncio
async def test_scalar_tag_coerced_to_list_on_edit(store):
    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    path = store.vault_path_for(fid)
    text = open(path, encoding="utf-8").read()
    fm, _ = vfiles.parse(text)
    fm["tags"] = "bug"  # YAML scalar, not a list
    open(path, "w", encoding="utf-8").write(vfiles.serialize(fm, "# Note\n\nedited body"))

    counters = reconcile.reconcile(store, force=True)
    assert counters["updated"] == 1
    assert store.get_finding(kb_id, fid)["tags"] == ["bug"]


@pytest.mark.asyncio
async def test_comma_string_tags_coerced_to_list_on_adopt(store):
    kb_id = _mk_kb(store)
    d = layout.vault_root() / "notes" / "br8n" / "main"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-07-27-1400-scalar-tags.md").write_text(
        "---\ntags: bug, feature\n---\n\n# Scalar tags\n\nbody\n"
    )

    counters = reconcile.reconcile(store, force=True)
    assert counters["adopted"] == 1
    listed = store.list_findings(kb_id, category="note")
    row = next(f for f in listed["findings"] if f["title"] == "Scalar tags")
    assert row["tags"] == ["bug", "feature"]


# --- br8n_id self-identification hardening ---------------------------------


@pytest.mark.asyncio
async def test_edit_stripping_br8n_id_heals_frontmatter(store):
    """An edit that strips br8n_id from frontmatter must not orphan the file:
    reconcile writes the row's id back so a future rename can still match it."""
    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    path = store.vault_path_for(fid)
    text = open(path, encoding="utf-8").read()
    fm, _ = vfiles.parse(text)
    fm.pop("br8n_id", None)
    fm["title"] = "Edited title"
    open(path, "w", encoding="utf-8").write(vfiles.serialize(fm, "# Edited title\n\nnew body"))

    counters = reconcile.reconcile(store, force=True)
    assert counters["updated"] == 1

    row = store.get_finding(kb_id, fid)
    assert row["title"] == "Edited title"
    assert "new body" in row["content"]

    healed_fm, _ = vfiles.parse(Path(path).read_text(encoding="utf-8"))
    assert healed_fm["br8n_id"] == fid

    # the row's stamps must match what was actually written (healed text),
    # not the pre-heal edit, else the next pass immediately re-suspects it.
    st = Path(path).stat()
    stamped = store._conn.execute(
        "SELECT content_hash, vault_mtime, vault_size FROM findings WHERE id = ?;", (fid,)
    ).fetchone()
    assert stamped["content_hash"] == vfiles.content_hash(Path(path).read_text(encoding="utf-8"))
    assert stamped["vault_mtime"] == st.st_mtime
    assert stamped["vault_size"] == st.st_size

    counters2 = reconcile.reconcile(store, force=True)
    assert counters2["updated"] == 0  # healed — no longer a suspect


@pytest.mark.asyncio
async def test_adopt_writes_id_back_for_agent_sourced_file(store):
    """A hand-created file declaring source: agent (not 'human') must still
    get its br8n_id written back on adoption — id write-back is unconditional."""
    kb_id = _mk_kb(store)
    d = layout.vault_root() / "notes" / "br8n" / "main"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "2026-07-27-1500-agent-sourced.md"
    path.write_text(
        "---\nsource: agent\n---\n\n# Agent sourced\n\nbody\n", encoding="utf-8"
    )

    counters = reconcile.reconcile(store, force=True)
    assert counters["adopted"] == 1

    fm, _ = vfiles.parse(path.read_text(encoding="utf-8"))
    assert fm.get("br8n_id")
    assert fm["source"] == "agent"

    listed = store.list_findings(kb_id, category="note")
    assert any(f["id"] == fm["br8n_id"] for f in listed["findings"])


# --- Minor: an mtime-only touch restamps without a spurious update ---------


@pytest.mark.asyncio
async def test_mtime_only_touch_restamps_without_update(store):
    import os

    kb_id = _mk_kb(store)
    fid = await _insert(store, kb_id)
    path = store.vault_path_for(fid)
    row_before = store.get_finding(kb_id, fid)

    st = os.stat(path)
    os.utime(path, (st.st_atime + 5, st.st_mtime + 5))  # mtime lies, content unchanged

    counters = reconcile.reconcile(store, force=True)
    assert counters["updated"] == 0
    row_after = store.get_finding(kb_id, fid)
    assert row_after["content"] == row_before["content"]
    assert row_after["title"] == row_before["title"]

    # restamped — no longer a suspect on the following pass
    counters2 = reconcile.reconcile(store, force=True)
    assert counters2["updated"] == 0
    assert counters2["scanned"] >= 1

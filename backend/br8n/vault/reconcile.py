"""On-access reconciliation: the vault is canonical, the index catches up.

Called at Store read boundaries (Task 5). Debounced, time-capped and
batch-bounded per the spec, with a carry-over cursor so huge vaults make
round-robin progress. Contract: NEVER raises — any failure degrades to
"serve the index as-is".

Detection: a file is a *suspect* when its (mtime, size) disagree with the
stamps on its row (or it has no row). Suspects are confirmed by content hash
(sync clients rewrite mtimes without changing bytes). Human edits update the
row text and mark ``needs_embed`` (the vec row is dropped); re-embedding
happens on the next async read (Task 5).

A pass has four phases:
  A. walk    — collect every canonical .md path (no stat). Time-budgeted
               (``reconcile_walk_cap_ms``) so a huge vault can't stall every
               non-debounced read; when the budget trips, the pass covers a
               prefix of the tree and Phase D is skipped entirely (deletions
               defer to a completed walk or an explicit reindex; the doctor
               keeps reporting the drift via ``vault_health``).
  B. scan    — stat each path starting after the carry-over cursor, wrapping
               at most once around the full list; time-capped. Sets
               ``scan_complete`` iff the whole list was covered this call
               (used only to decide whether to reset the cursor).
  C. apply   — adopt/edit/malformed suspects, batch-capped. Only a
               frontmatter parse failure counts as malformed; rowless
               malformed files are memoized by (mtime, size) so the same
               broken bytes count once, not every pass.
  D. delete  — driven by Phase A's walk WHEN IT COMPLETED, not by whether
               Phase B's stat scan finished — a partial stat scan must never
               suppress a genuine deletion. Rows re-adopted this pass (a
               rename) are not candidates. Guarded by: at least one
               canonical dir exists under the current root (a missing/
               re-pointed root must never look like "everything was
               deleted") and only rows whose vault_path is under the CURRENT
               root are candidates at all; and a magnitude guard that skips
               the whole sweep when candidates are large relative to what's
               indexed (an empty-but-present tree — a sync client
               materializing dirs before contents, or evicted/online-only
               files — must not read as a mass delete). The magnitude guard
               is bypassed by ``ignore_caps=True`` (e.g. an explicit
               reindex); until then the doctor (``vault_health``) keeps
               reporting the missing files via ``missing_files``.
"""
from __future__ import annotations

import bisect
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from br8n.config import get_config
from br8n.constants import JOURNAL_SCOPE
from br8n.vault import files, layout

logger = logging.getLogger(__name__)

_DIR_CATEGORY = {"snapshots": "snapshot", "notes": "note", "journal": "journal"}


class _MalformedFile(ValueError):
    """A canonical file whose frontmatter failed to parse (and only that)."""


def _parse_file(text: str) -> tuple[dict, str]:
    """files.parse with the failure narrowed to _MalformedFile, so Phase C
    can't mistake an unrelated ValueError from deeper code for a broken file."""
    try:
        return files.parse(text)
    except ValueError as exc:
        raise _MalformedFile(str(exc)) from exc


def _coerce_tags(value) -> list:
    """Normalize a frontmatter ``tags`` value into a list.

    A YAML scalar (``tags: bug``) or a comma-separated string
    (``tags: bug, feature``) coerces to a list; anything missing or
    otherwise invalid (not a list or a string) clears to [].
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return []


def reconcile(store, *, force: bool = False, ignore_caps: bool = False) -> dict:
    counters = {"scanned": 0, "updated": 0, "adopted": 0, "deleted": 0,
                "malformed": 0, "skipped": False}
    try:
        cfg = get_config().vault
        now = time.monotonic()
        if not force and (now - store._last_reconcile) < cfg.reconcile_debounce_seconds:
            counters["skipped"] = True
            return counters
        store._last_reconcile = now
        _pass(store, cfg, counters, ignore_caps)
    except Exception:  # noqa: BLE001 — reconcile must never raise
        logger.warning("vault reconcile degraded", exc_info=True)
        try:
            store._conn.rollback()  # never leave a write transaction open
        except Exception:  # noqa: BLE001
            pass
        # the rollback discarded any mutations this pass made — don't
        # over-report writes that never landed
        counters["adopted"] = 0
        counters["updated"] = 0
        counters["deleted"] = 0
    return counters


def _pass(store, cfg, counters: dict, ignore_caps: bool) -> None:
    root = layout.vault_root()

    # Phase A: walk — pure directory listing, no stat. Time-budgeted so a
    # huge vault can't stall every non-debounced read; a tripped budget
    # yields a prefix of the tree and skips Phase D (see module docstring).
    walk_deadline = time.monotonic() + cfg.reconcile_walk_cap_ms / 1000.0
    walk_complete = True
    any_dir_exists = False
    all_paths: list[str] = []
    for d in layout.CANONICAL_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        any_dir_exists = True
        for path in base.rglob("*.md"):
            if not ignore_caps and time.monotonic() > walk_deadline:
                walk_complete = False
                break
            all_paths.append(str(path))
        if not walk_complete:
            break
    all_paths.sort()
    n = len(all_paths)

    index: dict[str, dict] = {}
    for r in store._conn.execute(
        "SELECT id, kb_id, vault_path, content_hash, vault_mtime, vault_size "
        "FROM findings WHERE vault_path IS NOT NULL;"
    ).fetchall():
        index[r["vault_path"]] = dict(r)

    # Phase B: stat scan, starting after the carry-over cursor, wrapping at
    # most once around the whole list, time-capped.
    deadline = time.monotonic() + cfg.reconcile_time_cap_ms / 1000.0
    start = 0
    if store._reconcile_cursor and n:
        start = bisect.bisect_right(all_paths, store._reconcile_cursor) % n

    suspects: list[Path] = []
    scan_complete = True
    last_scanned = ""
    for i in range(n):
        if not ignore_caps and i > 0 and time.monotonic() > deadline:
            scan_complete = False
            break
        sp = all_paths[(start + i) % n]
        last_scanned = sp
        path = Path(sp)
        counters["scanned"] += 1
        row = index.get(sp)
        if row is None:
            memo = store._malformed_seen.get(sp)
            if memo is not None:
                try:
                    st = path.stat()
                except OSError:
                    store._malformed_seen.pop(sp, None)
                    continue
                if (st.st_mtime, st.st_size) == memo:
                    continue  # same broken bytes — counted on a prior pass
                store._malformed_seen.pop(sp, None)
            suspects.append(path)
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime != row["vault_mtime"] or st.st_size != row["vault_size"]:
            suspects.append(path)

    store._reconcile_cursor = "" if scan_complete else last_scanned

    # Phase C: apply suspects, batch-capped.
    if not ignore_caps:
        suspects = suspects[: cfg.reconcile_batch_cap]

    adopted_ids: set[str] = set()
    for path in suspects:
        sp = str(path)
        row = index.get(sp)
        try:
            if row is None:
                fid = _adopt(store, path)
                if fid:
                    counters["adopted"] += 1
                    adopted_ids.add(fid)
            elif _apply_edit(store, path, row):
                counters["updated"] += 1
        except _MalformedFile:
            counters["malformed"] += 1
            if row is not None:
                # M2: restamp so a still-broken file stops burning scan
                # budget every pass (content/hash untouched).
                try:
                    st = path.stat()
                    store._conn.execute(
                        "UPDATE findings SET vault_mtime = ?, vault_size = ? WHERE id = ?;",
                        (st.st_mtime, st.st_size, row["id"]),
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("malformed restamp failed for %s", path, exc_info=True)
            else:
                # rowless: nothing to restamp — memoize (mtime, size) so the
                # same broken bytes don't re-count every pass
                try:
                    st = path.stat()
                    store._malformed_seen[sp] = (st.st_mtime, st.st_size)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 — one bad file never stops the pass
            logger.warning("reconcile failed for %s", path, exc_info=True)

    # Phase D: deletions — driven by Phase A's walk when it COMPLETED (F1: a
    # partial stat scan must never suppress a genuine deletion; a partial
    # WALK must never manufacture one, so a tripped walk budget defers the
    # whole sweep). Guarded by (C1) at least one canonical dir existing under
    # the current root, and rows under the CURRENT root only being candidates
    # at all. Rows re-adopted this pass (an Obsidian rename) are excluded
    # up front so a bulk rename can't spuriously trip the magnitude guard.
    if any_dir_exists and walk_complete:
        all_paths_set = set(all_paths)
        # prune malformed memos for files that no longer exist on disk
        store._malformed_seen = {
            k: v for k, v in store._malformed_seen.items() if k in all_paths_set
        }
        root_prefix = str(root) + os.sep
        under_root = [(sp, row) for sp, row in index.items() if sp.startswith(root_prefix)]
        candidates = [
            (sp, row) for sp, row in under_root
            if sp not in all_paths_set and row["id"] not in adopted_ids
        ]

        # F2: a magnitude guard — an empty-but-present tree (a sync client
        # materializing dirs before contents, evicted/online-only files)
        # must not read as "delete everything indexed". Skip the WHOLE sweep
        # when candidates are large relative to what's indexed under this
        # root; ignore_caps=True (an explicit reindex) bypasses the guard.
        threshold = max(10, math.ceil(0.10 * len(under_root)))
        if not ignore_caps and len(candidates) > threshold:
            logger.warning(
                "vault reconcile: mass-delete guard tripped (%d candidates > "
                "threshold %d of %d indexed rows under root) — skipping all "
                "deletions this pass; run an explicit reindex "
                "(ignore_caps=True) once you've confirmed this is real",
                len(candidates), threshold, len(under_root),
            )
        else:
            for sp, row in candidates:
                if Path(sp).exists():
                    continue
                # C2: delete by (id, vault_path) against the stale pre-scan
                # snapshot's path, so a row that was re-adopted under a new
                # path (an Obsidian rename) survives — its current
                # vault_path no longer matches this stale one, so the
                # DELETE affects 0 rows.
                cur = store._conn.execute(
                    "DELETE FROM findings WHERE id = ? AND vault_path = ?;", (row["id"], sp)
                )
                if cur.rowcount == 1:
                    store._conn.execute(
                        "DELETE FROM vec_findings WHERE finding_id = ?;", (row["id"],)
                    )
                    counters["deleted"] += 1
    elif any_dir_exists:
        logger.debug("vault reconcile: walk budget exceeded — deletions deferred")

    store._conn.commit()


def _apply_edit(store, path: Path, row: dict) -> bool:
    """Re-read an engine-known file; update the row iff the content hash moved."""
    text = path.read_text(encoding="utf-8")
    h = files.content_hash(text)
    st = path.stat()
    if h == row["content_hash"]:  # mtime lied (sync client) — restamp only
        store._conn.execute(
            "UPDATE findings SET vault_mtime = ?, vault_size = ? WHERE id = ?;",
            (st.st_mtime, st.st_size, row["id"]),
        )
        return False
    fm, body = _parse_file(text)  # _MalformedFile → counted by caller

    # br8n_id self-heal: an edit that stripped (or mismatched) the join key
    # must not silently orphan the file for future renames — write the row's
    # id back and restamp hash/mtime/size from what actually landed on disk,
    # so this healed text isn't immediately re-suspected next pass.
    if fm.get("br8n_id") != row["id"]:
        fm["br8n_id"] = row["id"]
        text = files.serialize(fm, body)
        files.atomic_write(path, text)
        h = files.content_hash(text)
        st = path.stat()

    title = str(fm.get("title") or files.title_from_body(body, path.stem))
    # M4: tags must be clearable from Obsidian — always write the parsed
    # result, never keep stale tags. Scalar/comma-string tags coerce to a
    # list; only a missing/invalid key clears to [].
    tags = _coerce_tags(fm.get("tags"))
    meta_row = store._conn.execute(
        "SELECT metadata FROM findings WHERE id = ?;", (row["id"],)
    ).fetchone()
    import json

    from br8n.store.sqlite import _json_load

    meta = _json_load(meta_row["metadata"], None) or {}
    if fm.get("next_action"):
        meta["next_action"] = str(fm["next_action"])
    else:
        meta.pop("next_action", None)
    store._conn.execute(
        "UPDATE findings SET title = ?, content = ?, tags = ?, "
        "metadata = ?, content_hash = ?, vault_mtime = ?, vault_size = ?, needs_embed = 1 "
        "WHERE id = ?;",
        (title, body, json.dumps(tags),
         json.dumps(meta) if meta else None, h, st.st_mtime, st.st_size, row["id"]),
    )
    store._conn.execute("DELETE FROM vec_findings WHERE finding_id = ?;", (row["id"],))
    return True


def _adopt(store, path: Path) -> str | None:
    """Index a file the engine has no row for (hand-written, or a fresh db).

    Returns the finding id the file landed under (Phase D excludes it from
    the deletion candidates so a rename never counts against the guard)."""
    import json

    from br8n.store.sqlite import _json_load

    text = path.read_text(encoding="utf-8")
    fm, body = _parse_file(text)  # _MalformedFile propagates → counted
    fid = str(fm.get("br8n_id") or uuid.uuid4().hex)

    existing = store._conn.execute(
        "SELECT vault_path, provenance, metadata FROM findings WHERE id = ? LIMIT 1;",
        (fid,),
    ).fetchone()

    # I3: a duplicated file (same br8n_id copied to a sibling file) must not
    # steal the original's row — that thrashes one row between two paths
    # forever. Mint a fresh id for THIS file when the id's existing row
    # points at a different path that still exists on disk. When the other
    # path is gone, this is a genuine rename, not a duplicate — let it
    # through so _adopt/INSERT OR REPLACE re-points the row (C2), and the
    # metadata/provenance merge below (F3) applies.
    if (
        existing is not None
        and existing["vault_path"]
        and existing["vault_path"] != str(path)
        and Path(existing["vault_path"]).exists()
    ):
        fid = uuid.uuid4().hex
        fm["br8n_id"] = fid
        text = files.serialize(fm, body)
        files.atomic_write(path, text)
        existing = None  # fresh id — nothing of this row's to merge from

    project, kb, category = _target_for(path, fm)
    org_id, project_id = store.resolve_project(project, create=True)
    kb_id = store.resolve_kb(org_id, project_id, kb, create=True)
    title = str(fm.get("title") or files.title_from_body(body, path.stem))
    created = str(fm.get("created") or datetime.now(timezone.utc).isoformat())
    # Scalar/comma-string tags coerce to a list; only a missing/invalid key
    # clears to [].
    tags = _coerce_tags(fm.get("tags"))
    source = str(fm.get("source") or "human")
    confidence = fm.get("confidence") if isinstance(fm.get("confidence"), (int, float)) else 0.6

    # F3: a rename/re-adopt of a KNOWN id must not clobber row metadata the
    # file doesn't (fully) carry — e.g. a snapshot's hypothesis/thread_id
    # only ever lived in the metadata column, never in frontmatter, so
    # rebuilding purely from the file would silently drop them. Merge:
    # start from the existing row's provenance/metadata, append a
    # vault-adopted marker recording where the row was re-joined from, then
    # apply the file's next_action. A brand-new id has nothing to merge.
    marker = {"source": "vault-adopted", "path": str(path)}
    if existing is not None:
        provenance = _json_load(existing["provenance"], None) or []
        if marker not in provenance:
            provenance.append(marker)
        meta = _json_load(existing["metadata"], None) or {}
    else:
        provenance = [marker]
        meta = {}
    if fm.get("next_action"):
        meta["next_action"] = str(fm["next_action"])
    else:
        meta.pop("next_action", None)

    if not fm.get("br8n_id"):
        # write the join key (and source) back so the file self-identifies —
        # unconditional: ANY adopted file lacking br8n_id gets it written
        # back, regardless of declared source (a hand file declaring
        # `source: agent` must not lose its identity on rename either).
        fm.update({"br8n_id": fid, "source": source})
        text = files.serialize(fm, body)
        files.atomic_write(path, text)

    h = files.content_hash(text)
    st = path.stat()
    # I3: a stale embedding must never survive a REPLACE onto this id.
    store._conn.execute("DELETE FROM vec_findings WHERE finding_id = ?;", (fid,))
    store._conn.execute(
        "INSERT OR REPLACE INTO findings (id, org_id, kb_id, title, content, category, "
        "confidence, tags, provenance, metadata, created_at, vault_path, content_hash, "
        "vault_mtime, vault_size, needs_embed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1);",
        (fid, org_id, kb_id, title, body, category, confidence, json.dumps(list(tags)),
         json.dumps(provenance),
         json.dumps(meta) if meta else None, created, str(path), h, st.st_mtime, st.st_size),
    )
    return fid


def _target_for(path: Path, fm: dict) -> tuple[str, str, str]:
    """(project, kb, category) for a new file: frontmatter wins, else the path."""
    try:
        rel = path.relative_to(layout.vault_root())
    except ValueError:
        # M1: path isn't under the vault root (shouldn't happen in normal
        # operation) — fall back rather than leak into the malformed counter.
        project = str(fm.get("project") or "unknown")
        kb = str(fm.get("kb") or "default")
        return (project, kb, "finding")
    top = rel.parts[0]
    category = _DIR_CATEGORY.get(top, "finding")
    if top == "journal":
        return (JOURNAL_SCOPE, JOURNAL_SCOPE, "journal")
    project = str(fm.get("project") or (rel.parts[1] if len(rel.parts) > 2 else "unknown"))
    kb = str(fm.get("kb") or (rel.parts[2] if len(rel.parts) > 3 else "default"))
    return (project, kb, category)


def vault_health(store) -> dict:
    """Doctor read: file/index counts + drift. Best-effort, never raises."""
    out = {"files": 0, "indexed": 0, "unindexed_files": 0, "missing_files": 0, "malformed": 0}
    try:
        root = layout.vault_root()
        indexed_paths = {
            r["vault_path"]
            for r in store._conn.execute(
                "SELECT vault_path FROM findings WHERE vault_path IS NOT NULL;"
            ).fetchall()
        }
        out["indexed"] = len(indexed_paths)
        seen = set()
        for d in layout.CANONICAL_DIRS:
            base = root / d
            if not base.is_dir():
                continue
            for p in base.rglob("*.md"):
                out["files"] += 1
                seen.add(str(p))
                try:
                    files.parse(p.read_text(encoding="utf-8"))
                except ValueError:
                    out["malformed"] += 1
                if str(p) not in indexed_paths:
                    out["unindexed_files"] += 1
        out["missing_files"] = len([p for p in indexed_paths if p not in seen])
    except Exception:  # noqa: BLE001
        logger.warning("vault_health degraded", exc_info=True)
    return out

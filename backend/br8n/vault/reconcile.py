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
  A. walk    — collect every canonical .md path (uncapped, no stat).
  B. scan    — stat each path starting after the carry-over cursor, wrapping
               at most once around the full list; time-capped. Sets
               ``scan_complete`` iff the whole list was covered this call.
  C. apply   — adopt/edit/malformed suspects, batch-capped.
  D. delete  — only when this call's scan proved a full pass AND at least
               one canonical dir exists under the current root, and only for
               rows whose vault_path is under the current root. This is what
               keeps a missing/re-pointed root from wiping the index.
"""
from __future__ import annotations

import bisect
import logging
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
    return counters


def _pass(store, cfg, counters: dict, ignore_caps: bool) -> None:
    root = layout.vault_root()

    # Phase A: walk — pure directory listing, uncapped, no stat.
    any_dir_exists = False
    all_paths: list[str] = []
    for d in layout.CANONICAL_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        any_dir_exists = True
        for path in base.rglob("*.md"):
            all_paths.append(str(path))
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

    seen: set[str] = set()
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
        seen.add(sp)
        counters["scanned"] += 1
        row = index.get(sp)
        if row is None:
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

    for path in suspects:
        sp = str(path)
        row = index.get(sp)
        try:
            if row is None:
                if _adopt(store, path):
                    counters["adopted"] += 1
            elif _apply_edit(store, path, row):
                counters["updated"] += 1
        except ValueError:
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
        except Exception:  # noqa: BLE001 — one bad file never stops the pass
            logger.warning("reconcile failed for %s", path, exc_info=True)

    # Phase D: deletions — only when this call proved a full scan pass over
    # every canonical file AND the root still has at least one canonical dir
    # (C1: a missing/re-pointed root must never look like "everything was
    # deleted"). Even then, only rows under the CURRENT root are candidates.
    if scan_complete and any_dir_exists:
        root_prefix = str(root) + os.sep
        for sp, row in index.items():
            if sp in seen or not sp.startswith(root_prefix):
                continue
            if Path(sp).exists():
                continue
            # C2: delete by (id, vault_path) against the stale pre-scan
            # snapshot's path, so a row that was re-adopted under a new path
            # (an Obsidian rename) survives — its current vault_path no
            # longer matches this stale one, so the DELETE affects 0 rows.
            cur = store._conn.execute(
                "DELETE FROM findings WHERE id = ? AND vault_path = ?;", (row["id"], sp)
            )
            if cur.rowcount == 1:
                store._conn.execute(
                    "DELETE FROM vec_findings WHERE finding_id = ?;", (row["id"],)
                )
                counters["deleted"] += 1

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
    fm, body = files.parse(text)  # ValueError → counted as malformed by caller
    title = str(fm.get("title") or files.title_from_body(body, path.stem))
    # M4: tags must be clearable from Obsidian — always write the parsed
    # result (list if present, [] if absent/invalid), never keep stale tags.
    tags = fm["tags"] if isinstance(fm.get("tags"), list) else []
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


def _adopt(store, path: Path) -> bool:
    """Index a file the engine has no row for (hand-written, or a fresh db)."""
    import json

    text = path.read_text(encoding="utf-8")
    fm, body = files.parse(text)  # ValueError propagates → malformed
    fid = str(fm.get("br8n_id") or uuid.uuid4().hex)

    # I3: a duplicated file (same br8n_id copied to a sibling file) must not
    # steal the original's row — that thrashes one row between two paths
    # forever. Mint a fresh id for THIS file when the id's existing row
    # points at a different path that still exists on disk. When the other
    # path is gone, this is a genuine rename, not a duplicate — let it
    # through so _adopt/INSERT OR REPLACE re-points the row (C2).
    existing = store._conn.execute(
        "SELECT vault_path FROM findings WHERE id = ? LIMIT 1;", (fid,)
    ).fetchone()
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

    project, kb, category = _target_for(path, fm)
    org_id, project_id = store.resolve_project(project, create=True)
    kb_id = store.resolve_kb(org_id, project_id, kb, create=True)
    title = str(fm.get("title") or files.title_from_body(body, path.stem))
    created = str(fm.get("created") or datetime.now(timezone.utc).isoformat())
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    source = str(fm.get("source") or "human")
    confidence = fm.get("confidence") if isinstance(fm.get("confidence"), (int, float)) else 0.6
    meta = {"next_action": str(fm["next_action"])} if fm.get("next_action") else None

    if source == "human" and (not fm.get("br8n_id") or fm.get("source") is None):
        # write the join key (and source) back so the file self-identifies
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
         json.dumps([{"source": "vault-adopted", "path": str(path)}]),
         json.dumps(meta) if meta else None, created, str(path), h, st.st_mtime, st.st_size),
    )
    return True


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

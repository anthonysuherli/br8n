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
"""
from __future__ import annotations

import logging
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
    return counters


def _pass(store, cfg, counters: dict, ignore_caps: bool) -> None:
    root = layout.vault_root()
    deadline = time.monotonic() + cfg.reconcile_time_cap_ms / 1000.0
    index: dict[str, dict] = {}
    for r in store._conn.execute(
        "SELECT id, kb_id, vault_path, content_hash, vault_mtime, vault_size "
        "FROM findings WHERE vault_path IS NOT NULL;"
    ).fetchall():
        index[r["vault_path"]] = dict(r)

    seen: set[str] = set()
    suspects: list[Path] = []
    for d in layout.CANONICAL_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            if not ignore_caps and time.monotonic() > deadline:
                break
            sp = str(path)
            seen.add(sp)
            counters["scanned"] += 1
            row = index.get(sp)
            if row is None:
                suspects.append(path)
                continue
            st = path.stat()
            if st.st_mtime != row["vault_mtime"] or st.st_size != row["vault_size"]:
                suspects.append(path)

    # carry-over cursor: resume after the last processed path, wrap around
    suspects.sort()
    if store._reconcile_cursor:
        after = [p for p in suspects if str(p) > store._reconcile_cursor]
        suspects = after + [p for p in suspects if str(p) <= store._reconcile_cursor]
    if not ignore_caps:
        suspects = suspects[: cfg.reconcile_batch_cap]

    for path in suspects:
        store._reconcile_cursor = str(path)
        try:
            row = index.get(str(path))
            if row is None:
                if _adopt(store, path):
                    counters["adopted"] += 1
            elif _apply_edit(store, path, row):
                counters["updated"] += 1
        except ValueError:
            counters["malformed"] += 1
        except Exception:  # noqa: BLE001 — one bad file never stops the pass
            logger.warning("reconcile failed for %s", path, exc_info=True)

    # deletions: indexed paths whose file is gone
    for sp, row in index.items():
        if sp in seen or Path(sp).exists():
            continue
        store._conn.execute("DELETE FROM findings WHERE id = ?;", (row["id"],))
        store._conn.execute("DELETE FROM vec_findings WHERE finding_id = ?;", (row["id"],))
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
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else None
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
        "UPDATE findings SET title = ?, content = ?, tags = COALESCE(?, tags), "
        "metadata = ?, content_hash = ?, vault_mtime = ?, vault_size = ?, needs_embed = 1 "
        "WHERE id = ?;",
        (title, body, json.dumps(tags) if tags is not None else None,
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
    rel = path.relative_to(layout.vault_root())
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

"""VaultStore — the local tier where markdown is canonical and SQLite is index.

    VaultStore(db_path) ──► SQLiteStore (derived index) + ~/.br8n/vault/*.md

Subclasses SQLiteStore: every index behavior (vector search, KG, tenancy,
synopsis, explorations) is inherited unchanged. This class adds the canonical
file lifecycle: file-first-ish writes on insert (row first for id generation,
file immediately after, atomically), file unlink on delete, and the stamps
(`vault_path`, `content_hash`, `vault_mtime`, `vault_size`, `needs_embed`)
reconcile uses to detect human edits. All vault IO is best-effort: an OSError
degrades to index-only and never breaks the caller.
"""
from __future__ import annotations

import logging
from pathlib import Path

from br8n.store.sqlite import SQLiteStore, _json_load
from br8n.vault import files, layout

logger = logging.getLogger(__name__)


class VaultStore(SQLiteStore):
    def __init__(self, db_path: str | None = None) -> None:
        super().__init__(db_path)
        self._last_reconcile = 0.0  # time.monotonic() of the last pass (Task 4)
        self._reconcile_cursor = ""  # carry-over across batch-capped passes

    # --- canonical write path -------------------------------------------------

    async def insert_findings(self, rows: list[dict]) -> list[str]:
        ids = await super().insert_findings(rows)
        for row, fid in zip(rows, ids):
            if row.get("embedding") is None:
                self._conn.execute(
                    "UPDATE findings SET needs_embed = 1 WHERE id = ?;", (fid,)
                )
            try:
                self._write_canonical(fid)
            except Exception:  # noqa: BLE001 — vault IO is best-effort
                logger.warning("vault write failed for finding %s", fid, exc_info=True)
        self._conn.commit()
        return ids

    def delete_finding(self, kb_id: str, finding_id: str) -> dict:
        path = self.vault_path_for(finding_id)
        result = super().delete_finding(kb_id, finding_id)
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001 — vault IO is best-effort
                logger.warning("vault unlink failed for %s", path, exc_info=True)
        return result

    # --- helpers ----------------------------------------------------------------

    def vault_path_for(self, finding_id: str) -> str | None:
        r = self._conn.execute(
            "SELECT vault_path FROM findings WHERE id = ? LIMIT 1;", (finding_id,)
        ).fetchone()
        return r["vault_path"] if r is not None else None

    def _kb_names(self, kb_id: str) -> tuple[str, str]:
        r = self._conn.execute(
            "SELECT p.name AS project, k.name AS kb FROM kbs k "
            "JOIN projects p ON p.id = k.project_id WHERE k.id = ? LIMIT 1;",
            (kb_id,),
        ).fetchone()
        if r is None:
            return ("unknown", "unknown")
        return (r["project"], r["kb"])

    def _write_canonical(self, finding_id: str) -> None:
        """Render the finding row to its canonical file and stamp the index."""
        r = self._conn.execute(
            "SELECT kb_id, title, content, category, confidence, tags, metadata, created_at "
            "FROM findings WHERE id = ? LIMIT 1;",
            (finding_id,),
        ).fetchone()
        if r is None:
            return
        project, kb = self._kb_names(r["kb_id"])
        meta = _json_load(r["metadata"], None) or {}
        fm = {
            "br8n_id": finding_id,
            "type": layout.file_type(r["category"]),
            "title": r["title"] or "untitled",
            "project": project,
            "kb": kb,
            "created": r["created_at"],
            "tags": _json_load(r["tags"], []),
            "confidence": r["confidence"],
            "source": "agent",
            "next_action": meta.get("next_action"),
        }
        path = layout.file_path(
            r["category"], project, kb, r["created_at"], r["title"] or "untitled", finding_id
        )
        text = files.serialize(fm, r["content"] or "")
        h = files.atomic_write(path, text)
        st = path.stat()
        self._conn.execute(
            "UPDATE findings SET vault_path = ?, content_hash = ?, vault_mtime = ?, "
            "vault_size = ? WHERE id = ?;",
            (str(path), h, st.st_mtime, st.st_size, finding_id),
        )
        self._conn.commit()

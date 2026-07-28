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

from sqlite_vec import serialize_float32

from br8n.clients.embeddings import embed_batch, embeddings_configured
from br8n.config import get_config
from br8n.store.sqlite import SQLiteStore, _json_load
from br8n.vault import files, layout, views as _views
from br8n.vault import reconcile as _reconcile

logger = logging.getLogger(__name__)


class VaultStore(SQLiteStore):
    def __init__(self, db_path: str | None = None) -> None:
        super().__init__(db_path)
        self._last_reconcile = 0.0  # time.monotonic() of the last pass (Task 4)
        self._reconcile_cursor = ""  # carry-over across batch-capped passes
        # rowless malformed files memoized as path -> (mtime, size) so the
        # same broken bytes count (and re-parse) once, not every pass
        self._malformed_seen: dict[str, tuple[float, int]] = {}
        # finding ids currently being re-embedded — overlapping read paths
        # (asyncio-concurrent on this shared store) claim before embedding
        # so the same stale row is never paid for twice
        self._re_embed_inflight: set[str] = set()
        try:
            from br8n.vault.migrate import export_missing

            export_missing(self)
        except Exception:  # noqa: BLE001 — init export is best-effort
            logger.warning("vault init export degraded", exc_info=True)

    # --- canonical write path -------------------------------------------------

    async def insert_findings(self, rows: list[dict]) -> list[str]:
        ids = await super().insert_findings(rows)
        for row, fid in zip(rows, ids):
            try:
                if row.get("embedding") is None:
                    self._conn.execute(
                        "UPDATE findings SET needs_embed = 1 WHERE id = ?;", (fid,)
                    )
                self._write_canonical(fid)
            except Exception:  # vault lifecycle is best-effort
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

    def upsert_synopsis(self, kb_id, content, finding_count, model):
        super().upsert_synopsis(kb_id, content, finding_count, model)
        _views.write_synopsis_view(self, kb_id, content)

    async def upsert_kg_nodes(self, kb_id, nodes):
        ids = await super().upsert_kg_nodes(kb_id, nodes)
        _views.write_activity_view(self, kb_id)
        return ids

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

    # --- read paths: the index catches up before it answers -------------------

    async def match_findings(self, kb_id, query_embedding, match_count,
                             min_similarity, categories=None):
        _reconcile.reconcile(self)
        await self._re_embed_stale()
        return await super().match_findings(
            kb_id, query_embedding, match_count, min_similarity, categories
        )

    def list_findings(self, kb_id, category=None, limit=None):
        _reconcile.reconcile(self)
        return super().list_findings(kb_id, category=category, limit=limit)

    def count_findings(self, kb_id):
        _reconcile.reconcile(self)
        return super().count_findings(kb_id)

    def list_projects(self):
        _reconcile.reconcile(self)
        return super().list_projects()

    def get_finding(self, kb_id, finding_id):
        self._verify_one(finding_id)
        return super().get_finding(kb_id, finding_id)

    def _verify_one(self, finding_id: str) -> None:
        """Hash-check this one file so a single read is never staler than disk.

        Deletion is deliberately NOT handled here: a missing file on this one
        row can't distinguish "genuinely deleted" from "root repointed/
        unmounted", and only the full reconcile() pass has the walk + guards
        (C1 root-exists, F2 magnitude) to make that call safely. A missing
        file here just falls through to serving the indexed row as-is; the
        next debounced reconcile() (triggered by list/count/match) or an
        explicit reindex is what actually deletes it.
        """
        try:
            row = self._conn.execute(
                "SELECT id, vault_path, content_hash, vault_mtime, vault_size "
                "FROM findings WHERE id = ? AND vault_path IS NOT NULL LIMIT 1;",
                (finding_id,),
            ).fetchone()
            if row is None:
                return
            path = Path(row["vault_path"])
            if not path.exists():
                return
            _reconcile._apply_edit(self, path, dict(row))
            self._conn.commit()
        except Exception:  # verification is best-effort
            try:
                self._conn.rollback()  # never leave a write transaction open
            except Exception:
                pass
            logger.debug("vault verify skipped for %s", finding_id, exc_info=True)

    async def _re_embed_stale(self) -> int:
        """Embed rows edits marked stale. Self-heals keyless-capture rows too.

        Claim-then-embed: stale ids are claimed in ``_re_embed_inflight``
        before the (awaited) embedding call, so an overlapping read path on
        this store never pays to embed the same rows twice.
        """
        if not embeddings_configured():
            return 0
        claimed: list = []
        try:
            cap = get_config().vault.re_embed_batch
            rows = self._conn.execute(
                "SELECT id, content FROM findings WHERE needs_embed = 1 "
                "AND content IS NOT NULL AND content != '' LIMIT ?;",
                (cap,),
            ).fetchall()
            rows = [r for r in rows if r["id"] not in self._re_embed_inflight]
            if not rows:
                return 0
            self._re_embed_inflight.update(r["id"] for r in rows)
            claimed = rows
            embeddings = await embed_batch([r["content"] for r in rows])
            for r, emb in zip(rows, embeddings):
                self._conn.execute(
                    "DELETE FROM vec_findings WHERE finding_id = ?;", (r["id"],)
                )
                self._conn.execute(
                    "INSERT INTO vec_findings (finding_id, embedding) VALUES (?, ?);",
                    (r["id"], serialize_float32(list(emb))),
                )
                self._conn.execute(
                    "UPDATE findings SET needs_embed = 0 WHERE id = ?;", (r["id"],)
                )
            self._conn.commit()
            return len(rows)
        except Exception:  # embedding failures never break search
            try:
                self._conn.rollback()  # never leave a write transaction open
            except Exception:
                pass
            logger.warning("re-embed pass degraded", exc_info=True)
            return 0
        finally:
            self._re_embed_inflight.difference_update(r["id"] for r in claimed)

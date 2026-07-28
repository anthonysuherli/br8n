"""SQLiteStore — the free/local-tier Store over SQLite + sqlite-vec.

    SQLiteStore(db_path) ──► sqlite3 conn (+ vec0 virtual table) ──► local file

This is the single-user, no-auth counterpart to SupabaseStore. It mirrors the
Postgres tables minus the tenancy machinery: there is one synthetic org
(``org_id = "local"``), find-or-create resolves projects/KBs by name, and vector
search is sqlite-vec's brute-force ``vec_distance_cosine`` over a join (the
reliable path — no ANN index needed at local scale).

**Return-shape parity is load-bearing.** Every method returns the same dict /
list-of-dicts shape SupabaseStore returns (same keys, same JSON-decoded values)
so the engine cannot tell the two backends apart. In particular:
  * findings expose ``id, title, content, category, confidence, tags,
    provenance, created_at`` (list view drops ``content``/``provenance``);
  * ``match_findings`` rows additionally carry ``similarity`` (= 1 - cosine
    distance), filtered by ``min_similarity`` and ordered desc;
  * the synopsis row uses ``finding_count_at_build`` (NOT ``finding_count``) —
    the key ``agent/synopsis.should_rebuild`` and ``load_synopsis`` read.

**Connection / threading.** One long-lived connection opened with
``check_same_thread=False`` and ``row_factory = sqlite3.Row``. FastAPI background
tasks and the async methods may touch the store from different threads; SQLite
serializes writes internally and our ops are short, so a single shared
connection is simplest and correct here. ``tags``/``provenance``/``content``/
``finding_ids`` are stored as JSON text and decoded on read.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec
from sqlite_vec import serialize_float32

from br8n.config import get_config
from br8n.constants import JOURNAL_SCOPE

logger = logging.getLogger(__name__)

# Synthetic single-tenant org for the local tier.
_ORG = "local"

# Column lists kept in lockstep with findings/service.py + the match RPC.
_FINDING_COLS = ("id", "title", "content", "category", "confidence", "tags", "provenance", "metadata", "created_at")
_FINDING_LIST_COLS = ("id", "title", "category", "confidence", "tags", "metadata", "created_at")
# match_findings returns the full finding minus created_at, plus a computed similarity.
_FINDING_MATCH_COLS = ("id", "title", "content", "category", "confidence", "tags", "provenance")

LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kbs (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, project_id TEXT NOT NULL, name TEXT NOT NULL,
  created_at TEXT NOT NULL, init_offered_at TEXT, drift_offered_count INTEGER);
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, kb_id TEXT NOT NULL,
  title TEXT, content TEXT, category TEXT, confidence REAL,
  tags TEXT, provenance TEXT, metadata TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kb_synopsis (
  kb_id TEXT PRIMARY KEY, org_id TEXT, content TEXT,
  finding_count_at_build INTEGER, model TEXT, built_at TEXT);
CREATE TABLE IF NOT EXISTS explorations (
  id TEXT PRIMARY KEY, org_id TEXT, kb_id TEXT NOT NULL, prompt TEXT,
  status TEXT, error TEXT, finding_ids TEXT, started_at TEXT, completed_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kg_nodes (
  id TEXT PRIMARY KEY, org_id TEXT, kb_id TEXT NOT NULL,
  type TEXT, label TEXT, properties TEXT, grounded_in TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kg_edges (
  id TEXT PRIMARY KEY, org_id TEXT, kb_id TEXT NOT NULL,
  source_node_id TEXT, target_node_id TEXT, relation TEXT,
  properties TEXT, grounded_in TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_findings_kb ON findings(kb_id);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_kb ON kg_nodes(kb_id);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_dedupe ON kg_nodes(kb_id, type, label);
CREATE INDEX IF NOT EXISTS idx_kg_edges_kb ON kg_edges(kb_id);
CREATE TABLE IF NOT EXISTS kg_schemas (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, kb_id TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1, schema TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_schemas_kb_version ON kg_schemas(kb_id, version);
"""


# Vector tables are created at the ACTIVE embedding dimension, not a constant:
# a vec0 table's width is fixed at creation, and vectors from different models
# are not comparable, so a provider change recreates them (see
# ``_sync_embedding_space``).
def _vec_schema(dim: int) -> str:
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_findings "
        f"USING vec0(finding_id TEXT, embedding float[{dim}]);"
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_kg_nodes "
        f"USING vec0(node_id TEXT, embedding float[{dim}]);"
    )


_EMBEDDING_SPACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_space (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  provider TEXT NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL,
  updated_at TEXT NOT NULL);
"""

# Post-schema migrations: ADD COLUMN statements for older DBs.
# SQLite does not support ``IF NOT EXISTS`` on ALTER TABLE so we run each in its
# own try/except inside ``_ensure_schema``.  The list grows with each migration.
_ADD_COLUMN_MIGRATIONS: list[str] = [
    # 0007: offer-once stamp (added when kbs table already exists in older DBs)
    "ALTER TABLE kbs ADD COLUMN init_offered_at TEXT;",
    # 0008: schema-drift offer debounce — residual count stamped at last drift offer
    "ALTER TABLE kbs ADD COLUMN drift_offered_count INTEGER;",
    # 0009: structured capture fields (hypothesis / next_action / thread_id)
    "ALTER TABLE findings ADD COLUMN metadata TEXT;",
    # 0010: vault (markdown-canonical local tier) — index stamps per finding
    "ALTER TABLE findings ADD COLUMN content_hash TEXT;",
    "ALTER TABLE findings ADD COLUMN vault_path TEXT;",
    "ALTER TABLE findings ADD COLUMN vault_mtime REAL;",
    "ALTER TABLE findings ADD COLUMN vault_size INTEGER;",
    "ALTER TABLE findings ADD COLUMN needs_embed INTEGER;",
    # 0011: local embeddings — KG nodes join the lazy re-embed drain
    "ALTER TABLE kg_nodes ADD COLUMN needs_embed INTEGER;",
]

# Cap on how many grounding finding ids a long-lived node (a repo touched for
# months) accumulates — keep the most recent, so the column can't grow unbounded.
_MAX_GROUNDED = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_db_path() -> str:
    """``BR8N_DB_PATH`` if set, else ``~/.br8n/brain.db`` (dir created)."""
    env = os.environ.get("BR8N_DB_PATH")
    if env:
        return env
    home = Path.home() / ".br8n"
    home.mkdir(parents=True, exist_ok=True)
    return str(home / "brain.db")


class SQLiteStore:
    """Store backed by SQLite + sqlite-vec. Single synthetic org ``"local"``."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self._conn = self._connect(self.db_path)
        self._ensure_schema()

    @staticmethod
    def _connect(db_path: str) -> sqlite3.Connection:
        """Open a connection with sqlite-vec loaded and Row factory.

        File-backed DBs use WAL + a 5s ``busy_timeout`` so a background
        ``explore`` write and a foreground ``capture``/``insert_findings`` write
        block-and-retry instead of raising ``database is locked``. WAL is skipped
        for ``:memory:`` (where it is unsupported/pointless); ``busy_timeout`` is
        harmless everywhere."""
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA busy_timeout=5000")
        if db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def close(self) -> None:
        """Close the underlying connection. Explicit — no atexit/__del__ magic."""
        self._conn.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # Run post-schema ADD COLUMN migrations idempotently.  SQLite raises
        # ``OperationalError: duplicate column name`` when a column already exists
        # (no ``IF NOT EXISTS`` support for ALTER TABLE).  We swallow those
        # errors so re-opening an existing DB is always safe.
        for stmt in _ADD_COLUMN_MIGRATIONS:
            try:
                self._conn.execute(stmt)
                self._conn.commit()
            except Exception:  # column already present
                pass
        self._sync_embedding_space()

    # --- embedding space -------------------------------------------------------

    def embedding_space(self) -> dict | None:
        """The stamped active space, or None before the first stamp."""
        try:
            r = self._conn.execute(
                "SELECT provider, model, dim FROM embedding_space WHERE id = 1;"
            ).fetchone()
        except Exception:  # table absent on a partial schema
            return None
        if r is None:
            return None
        return {"provider": r["provider"], "model": r["model"], "dim": int(r["dim"])}

    def _vec_findings_sql(self) -> str | None:
        """Raw CREATE-table SQL for vec_findings, or None if the table is absent.

        Guarded like ``embedding_space()`` (unlike the ``_declared_vec_dim``
        this backs, which used to run unguarded): a locked/corrupt DB must
        degrade to "no table", not propagate ``sqlite3.OperationalError``."""
        try:
            r = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_findings';"
            ).fetchone()
        except Exception:
            return None
        return r["sql"] if r is not None and r["sql"] else None

    def _declared_vec_dim(self) -> int | None:
        """Width the existing vec_findings table was created with, if any.

        Returns None both when the table is absent AND when it exists but its
        width can't be parsed — callers that need to tell those two apart
        (see ``_sync_embedding_space``) use ``_vec_findings_sql`` directly."""
        sql = self._vec_findings_sql()
        if sql is None:
            return None
        # IGNORECASE: a foreign/future vec0 schema may spell the type
        # differently (``FLOAT[384]``); a case-sensitive miss must not be
        # treated the same as "no table at all" (see _sync_embedding_space).
        m = re.search(r"float\[(\d+)\]", sql, re.IGNORECASE)
        return int(m.group(1)) if m else None

    def _stamp_embedding_space(self, provider: str, model: str, dim: int) -> None:
        self._conn.execute(
            "INSERT INTO embedding_space (id, provider, model, dim, updated_at) "
            "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "provider=excluded.provider, model=excluded.model, dim=excluded.dim, "
            "updated_at=excluded.updated_at;",
            (provider, model, dim, _now_iso()),
        )
        self._conn.commit()

    def _rebuild_vec_tables(self, dim: int) -> None:
        """Recreate both vec tables at `dim` and queue everything for re-embed.

        Safe because vectors are derived: the text lives in ``findings.content``
        and, on the vault tier, canonically on disk. The refill is the existing
        lazy ``needs_embed`` drain, so this call stays cheap (DDL + 2 UPDATEs).

        M4 — two-transaction window: ``executescript`` implicitly commits, so
        the DROP+CREATE below lands as its own transaction before the two
        UPDATEs even begin. If a UPDATE then fails, the outer handler's
        ``rollback()`` can only undo the flags, not the DDL — leaving empty
        vec tables with nothing flagged for re-embed. This is acceptable: the
        ``embedding_space`` stamp is only written by the caller *after* this
        method returns (see the rebuild-BEFORE-stamp ordering in
        ``_sync_embedding_space``), so the stamp stays stale and the next
        store open re-detects the mismatch and retries the whole rebuild —
        it self-heals rather than needing a mid-rebuild transaction.
        """
        self._conn.executescript(
            "DROP TABLE IF EXISTS vec_findings; DROP TABLE IF EXISTS vec_kg_nodes;"
        )
        self._conn.executescript(_vec_schema(dim))
        self._conn.execute("UPDATE findings SET needs_embed = 1;")
        self._conn.execute("UPDATE kg_nodes SET needs_embed = 1;")
        self._conn.commit()

    def _sync_embedding_space(self) -> None:
        """Ensure the vec tables match the active embedder. Best-effort.

        Never raises: a failure here must not stop a store from opening, so the
        vec tables are created up-front at a safe width and the identity logic
        runs afterwards. Everything that can fail — including resolving
        ``get_config()`` itself — lives inside the try block: this method is
        called from ``__init__`` via ``_ensure_schema``, so anything raised
        here previously raised straight through store construction (I1).

        Safe-width fallback precedence: the declared table width (table
        already exists) → the last stamped dim (a stamp survived a dropped
        table) → ``config.embedding.dim`` → a literal 1536 if config
        resolution itself blows up (malformed config.yaml / bad
        ``B2_EMBEDDING__DIM``) — a store must always open with usable vec
        tables regardless of config state (M3 + I1).
        """
        try:
            declared = self._declared_vec_dim()
            fallback = declared
            if fallback is None:
                stamped = self.embedding_space()
                fallback = stamped["dim"] if stamped is not None else None
            if fallback is None:
                try:
                    fallback = get_config().embedding.dim
                except Exception:
                    fallback = 1536  # literal: config resolution must never block open
            self._conn.executescript(_vec_schema(fallback))
            self._conn.executescript(_EMBEDDING_SPACE_SCHEMA)
            self._conn.commit()
        except Exception:  # a store must still open
            logger.warning("vec/embedding_space schema degraded", exc_info=True)
            return

        try:
            from br8n.clients.embeddings import active_embedder

            ident = active_embedder()
            # No provider: keep whatever space exists — never discard vectors
            # the user may restore by putting a key back.
            if ident.provider == "none" or ident.dim <= 0:
                return

            stored = self.embedding_space()
            if stored is None:
                vec_sql = self._vec_findings_sql()
                declared = self._declared_vec_dim()
                if vec_sql is not None and declared is None:
                    # Table present but its width didn't parse (M2): never
                    # guess — leave the existing (unknown-width) space alone
                    # rather than stamping the active identity blindly over
                    # vectors that might be a different width entirely.
                    logger.warning(
                        "vec_findings table exists but DDL is unparseable; "
                        "skipping embedding-space sync to avoid silent vector drop"
                    )
                    return
                if declared is not None and declared != ident.dim:
                    # Pre-feature DB whose width disagrees with the active
                    # embedder: trust the TABLE, not the provider, then let the
                    # mismatch below rebuild. Stamping the active identity here
                    # would silently label 1536-dim vectors as 384.
                    self._stamp_embedding_space("unknown", "", declared)
                    stored = self.embedding_space()
                else:
                    self._stamp_embedding_space(ident.provider, ident.model, ident.dim)
                    return

            if stored["dim"] != ident.dim or stored["model"] != ident.model:
                # Log intent before the rebuild (M4: diagnose if rebuild fails)
                logger.info(
                    "embedding space change (%s/%sd -> %s/%sd)%s; rebuilding "
                    "index",
                    stored["model"] or stored["provider"], stored["dim"],
                    ident.model, ident.dim,
                    " [provider auto-detected, not explicitly configured]"
                    if ident.source == "auto" else "",
                )
                self._rebuild_vec_tables(ident.dim)
                self._stamp_embedding_space(ident.provider, ident.model, ident.dim)
                n_queued = self._conn.execute(
                    "SELECT (SELECT COUNT(*) FROM findings WHERE needs_embed = 1) + "
                    "(SELECT COUNT(*) FROM kg_nodes WHERE needs_embed = 1) AS n;"
                ).fetchone()["n"]
                # WARNING (not INFO) only when work was actually discarded
                # (I1: gate on n_queued > 0 to avoid false alarms on clean DBs).
                # With the local extra installed, opening the store from a shell
                # that happens not to export AI_GATEWAY_API_KEY silently flips
                # remote/1536 -> local/384 and drops every vector; on a remote
                # provider that's a paid re-embed of the whole corpus.
                log = logger.warning if (ident.source == "auto" and n_queued > 0) else logger.info
                log(
                    "embedding space change (%s/%sd -> %s/%sd)%s; %d row(s) "
                    "queued for re-embed",
                    stored["model"] or stored["provider"], stored["dim"],
                    ident.model, ident.dim,
                    " [provider auto-detected, not explicitly configured]"
                    if ident.source == "auto" else "",
                    n_queued,
                )
        except Exception:  # identity problems never break a store
            logger.warning("embedding-space sync degraded", exc_info=True)
            try:
                self._conn.rollback()
            except Exception:
                pass

    def resync_embedding_space(self) -> None:
        """Re-run the embedding-space sync against the CURRENT active_embedder().

        ``_sync_embedding_space`` normally runs once, at construction (via
        ``_ensure_schema``). ``get_store()`` caches one store per db_path for
        the process lifetime, so a live provider switch (``br8n_embeddings_set``)
        never reconstructs the store and the rebuild never fires on its own —
        the reported identity updates (it reads settings fresh each call) but
        the store's actual ``vec_findings``/``vec_kg_nodes`` width stays stale,
        and the next insert at the new dim raises a sqlite-vec dimension
        mismatch. Call this on the live, cached store right after such a
        switch instead of evicting the cache — popping it would abandon an
        open sqlite connection. Thin wrapper: ``_sync_embedding_space`` is
        already idempotent and best-effort (never raises), so a no-op switch
        (same provider) stays a no-op here too.
        """
        self._sync_embedding_space()

    # --- findings — hot path -------------------------------------------------

    async def match_findings(
        self,
        kb_id: str | None,
        query_embedding: list[float],
        match_count: int,
        min_similarity: float,
        categories: list[str] | None = None,
    ) -> list[dict]:
        """Cosine KNN over vec_findings joined to findings; rows carry `similarity`.

        `kb_id=None` searches every KB in the local org; `categories` filters by
        `category`. Mirrors the Postgres ``match_findings`` RPC return shape:
        ``id, title, content, category, confidence, tags, provenance, similarity``
        ordered by descending similarity (= 1 - cosine distance), dropping rows
        below ``min_similarity``. JSON columns are decoded."""
        q = serialize_float32(query_embedding)
        select_cols = ", ".join(f"f.{c}" for c in _FINDING_MATCH_COLS)
        where: list[str] = []
        params: list[object] = [q]
        if kb_id is not None:
            where.append("f.kb_id = ?")
            params.append(kb_id)
        else:
            where.append("f.org_id = ?")
            params.append(_ORG)
        if categories:
            placeholders = ",".join("?" for _ in categories)
            where.append(f"f.category IN ({placeholders})")
            params.extend(categories)
        params.append(match_count)
        rows = self._conn.execute(
            f"""
            SELECT {select_cols},
                   vec_distance_cosine(v.embedding, ?) AS dist
            FROM vec_findings v JOIN findings f ON f.id = v.finding_id
            WHERE {' AND '.join(where)}
            ORDER BY dist LIMIT ?;
            """,
            params,
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            similarity = 1.0 - float(r["dist"])
            if similarity < min_similarity:
                continue
            out.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "content": r["content"],
                    "category": r["category"],
                    "confidence": r["confidence"],
                    "tags": _json_load(r["tags"], []),
                    "provenance": _json_load(r["provenance"], []),
                    "similarity": similarity,
                }
            )
        return out

    async def insert_findings(self, rows: list[dict]) -> list[str]:
        """Insert pre-embedded finding rows; return new ids in input order.

        Each row carries ``title, content, category, confidence, tags,
        provenance, embedding`` (and an ignored ``org_id``/``kb_id``). ``tags``/
        ``provenance`` are JSON-encoded into ``findings``; ``embedding`` goes into
        ``vec_findings`` via ``serialize_float32``. ``org_id`` is forced to
        ``"local"``; ids are generated when absent."""
        if not rows:
            return []
        ids: list[str] = []
        for row in rows:
            fid = row.get("id") or uuid.uuid4().hex
            ids.append(fid)
            self._conn.execute(
                """
                INSERT INTO findings
                  (id, org_id, kb_id, title, content, category, confidence, tags, provenance, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    fid,
                    _ORG,
                    row.get("kb_id"),
                    row.get("title"),
                    row.get("content"),
                    row.get("category"),
                    row.get("confidence"),
                    json.dumps(list(row.get("tags") or [])),
                    json.dumps(list(row.get("provenance") or [])),
                    json.dumps(row["metadata"]) if row.get("metadata") is not None else None,
                    row.get("created_at") or _now_iso(),
                ),
            )
            embedding = row.get("embedding")
            if embedding is not None:
                self._conn.execute(
                    "INSERT INTO vec_findings (finding_id, embedding) VALUES (?, ?);",
                    (fid, serialize_float32(list(embedding))),
                )
        self._conn.commit()
        return ids

    def get_finding(self, kb_id: str, finding_id: str) -> dict:
        """One finding scoped to `kb_id`. Raises if absent. JSON cols decoded."""
        r = self._conn.execute(
            f"SELECT {', '.join(_FINDING_COLS)} "
            "FROM findings WHERE kb_id = ? AND id = ? LIMIT 1;",
            (kb_id, finding_id),
        ).fetchone()
        if r is None:
            raise RuntimeError("finding not found")
        return {
            "id": r["id"],
            "title": r["title"],
            "content": r["content"],
            "category": r["category"],
            "confidence": r["confidence"],
            "tags": _json_load(r["tags"], []),
            "provenance": _json_load(r["provenance"], []),
            "metadata": _json_load(r["metadata"], None),
            "created_at": r["created_at"],
        }

    def list_findings(
        self, kb_id: str, category: str | None = None, limit: int | None = None
    ) -> dict:
        """Most-recent findings in `kb_id`. Returns {"count", "findings"}.

        List view omits ``content``/``provenance`` (matching SupabaseStore);
        optional category filter; default/max limits mirror findings/service."""
        n = min(limit or LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT)
        sql = (
            f"SELECT {', '.join(_FINDING_LIST_COLS)} "
            "FROM findings WHERE kb_id = ?"
        )
        params: list[object] = [kb_id]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY created_at DESC LIMIT ?;"
        params.append(n)
        rows = self._conn.execute(sql, params).fetchall()
        findings = [
            {
                "id": r["id"],
                "title": r["title"],
                "category": r["category"],
                "confidence": r["confidence"],
                "tags": _json_load(r["tags"], []),
                "metadata": _json_load(r["metadata"], None),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        return {"count": len(findings), "findings": findings}

    def count_findings(self, kb_id: str) -> int:
        """Exact finding count for `kb_id` (uncapped, unlike list_findings)."""
        r = self._conn.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE kb_id = ?;", (kb_id,)
        ).fetchone()
        return int(r["n"])

    def delete_finding(self, kb_id: str, finding_id: str) -> dict:
        """Delete one finding from `kb_id` (and its vec row). Returns {"deleted"}."""
        self._conn.execute(
            "DELETE FROM findings WHERE kb_id = ? AND id = ?;", (kb_id, finding_id)
        )
        self._conn.execute(
            "DELETE FROM vec_findings WHERE finding_id = ?;", (finding_id,)
        )
        self._conn.commit()
        return {"deleted": finding_id}

    # --- synopsis spine ------------------------------------------------------

    def load_synopsis(self, kb_id: str) -> dict | None:
        """Current synopsis row for `kb_id`, or None.

        Keys match ``agent/synopsis.load_synopsis``: ``content`` (JSON-decoded),
        ``finding_count_at_build``, ``built_at``, ``model`` — the keys
        ``should_rebuild`` and the preamble read."""
        r = self._conn.execute(
            "SELECT content, finding_count_at_build, built_at, model "
            "FROM kb_synopsis WHERE kb_id = ? LIMIT 1;",
            (kb_id,),
        ).fetchone()
        if r is None:
            return None
        return {
            "content": _json_load(r["content"], []),
            "finding_count_at_build": r["finding_count_at_build"],
            "built_at": r["built_at"],
            "model": r["model"],
        }

    def upsert_synopsis(
        self, kb_id: str, content: list[dict], finding_count: int, model: str
    ) -> None:
        """Write the KB's synopsis spine (one current row per KB, conflict on kb_id)."""
        self._conn.execute(
            """
            INSERT INTO kb_synopsis (kb_id, org_id, content, finding_count_at_build, model, built_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(kb_id) DO UPDATE SET
              org_id = excluded.org_id,
              content = excluded.content,
              finding_count_at_build = excluded.finding_count_at_build,
              model = excluded.model,
              built_at = excluded.built_at;
            """,
            (kb_id, _ORG, json.dumps(content), finding_count, model, _now_iso()),
        )
        self._conn.commit()

    # --- exploration row lifecycle -------------------------------------------

    def create_exploration(self, org_id: str, kb_id: str, prompt: str) -> str:
        """Insert a pending exploration row; return its id. org_id forced local."""
        eid = uuid.uuid4().hex
        now = _now_iso()
        self._conn.execute(
            """
            INSERT INTO explorations (id, org_id, kb_id, prompt, status, started_at, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?);
            """,
            (eid, _ORG, kb_id, prompt, now, now),
        )
        self._conn.commit()
        return eid

    def update_exploration(self, exploration_id: str, **patch) -> None:
        """Patch exploration columns (status / completed_at / finding_ids / error).

        ``finding_ids`` is JSON-encoded; unknown keys are ignored to stay aligned
        with the explorations schema."""
        if not patch:
            return
        allowed = {"status", "error", "finding_ids", "started_at", "completed_at", "prompt"}
        cols: list[str] = []
        vals: list[object] = []
        for k, v in patch.items():
            if k not in allowed:
                continue
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v) if k == "finding_ids" else v)
        if not cols:
            return
        vals.append(exploration_id)
        self._conn.execute(
            f"UPDATE explorations SET {', '.join(cols)} WHERE id = ?;", vals
        )
        self._conn.commit()

    def get_exploration(self, exploration_id: str) -> dict | None:
        """Read an exploration row, or None. `finding_ids` decoded from JSON.

        Returns the same keys SupabaseStore selects: ``id, status, finding_ids,
        completed_at, error``."""
        r = self._conn.execute(
            "SELECT id, status, finding_ids, completed_at, error "
            "FROM explorations WHERE id = ? LIMIT 1;",
            (exploration_id,),
        ).fetchone()
        if r is None:
            return None
        return {
            "id": r["id"],
            "status": r["status"],
            "finding_ids": _json_load(r["finding_ids"], []),
            "completed_at": r["completed_at"],
            "error": r["error"],
        }

    # --- tenancy — find-or-create by name ------------------------------------

    def resolve_project(self, name: str, *, create: bool) -> tuple[str, str]:
        """Resolve the named project → ("local", project_id). Find-or-create."""
        pid = self._find_or_create(
            "projects",
            {"org_id": _ORG, "name": name},
            {"id": uuid.uuid4().hex, "org_id": _ORG, "name": name, "created_at": _now_iso()},
            create,
        )
        return _ORG, pid

    def resolve_kb(self, org_id: str, project_id: str, name: str, *, create: bool) -> str:
        """Resolve the named KB within (org_id, project_id) → kb_id. Find-or-create."""
        return self._find_or_create(
            "kbs",
            {"org_id": org_id, "project_id": project_id, "name": name},
            {
                "id": uuid.uuid4().hex,
                "org_id": org_id,
                "project_id": project_id,
                "name": name,
                "created_at": _now_iso(),
            },
            create,
        )

    def list_projects(self) -> list[dict]:
        """All local projects + KBs with snapshot last-activity/count (newest KB rows last)."""
        projects: list[dict] = []
        prows = self._conn.execute(
            "SELECT id, name FROM projects WHERE org_id = ? AND name != ? ORDER BY created_at;",
            (_ORG, JOURNAL_SCOPE),
        ).fetchall()
        for p in prows:
            kbs: list[dict] = []
            krows = self._conn.execute(
                "SELECT id, name FROM kbs WHERE org_id = ? AND project_id = ? ORDER BY created_at;",
                (_ORG, p["id"]),
            ).fetchall()
            for k in krows:
                agg = self._conn.execute(
                    "SELECT COUNT(*) AS n, MAX(created_at) AS last FROM findings "
                    "WHERE kb_id = ? AND category = 'snapshot';",
                    (k["id"],),
                ).fetchone()
                kbs.append({
                    "kb": k["name"],
                    "kb_id": k["id"],
                    "snapshot_count": int(agg["n"]),
                    "last_activity": agg["last"],
                })
            projects.append({"project": p["name"], "project_id": p["id"], "kbs": kbs})
        return projects

    def _find_or_create(
        self, table: str, match: dict[str, str], insert: dict[str, object], create: bool
    ) -> str:
        """Find row by `match`; insert `insert` if absent and `create`, else raise."""
        where = " AND ".join(f"{k} = ?" for k in match)
        existing = self._conn.execute(
            f"SELECT id FROM {table} WHERE {where} LIMIT 1;", list(match.values())
        ).fetchone()
        if existing is not None:
            return existing["id"]
        if not create:
            raise RuntimeError(f"{table} {match!r} not found")
        cols = ", ".join(insert)
        placeholders = ", ".join("?" for _ in insert)
        self._conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders});", list(insert.values())
        )
        self._conn.commit()
        return str(insert["id"])

    # --- activity knowledge graph --------------------------------------------

    async def upsert_kg_nodes(self, kb_id: str, nodes: list[dict]) -> list[str]:
        """Insert-or-merge nodes by exact ``(kb_id, type, label)``; ids in order.

        A repeated ``(type, label)`` — within the batch or already in the KB —
        reuses the existing node, merging ``properties`` (existing wins, so a
        node's identity is stable) and unioning ``grounded_in`` (capped to the
        most recent ``_MAX_GROUNDED``). Embeddings, when present, are written to
        ``vec_kg_nodes`` for semantic seeding."""
        if not nodes:
            return []
        ids: list[str] = []
        batch: dict[tuple[str, str], str] = {}
        for nd in nodes:
            typ = nd.get("type") or ""
            label = nd.get("label") or ""
            props = dict(nd.get("properties") or {})
            grounded = list(nd.get("grounded_in") or [])
            key = (typ, label)
            if key in batch:
                nid = batch[key]
                self._merge_kg_node(nid, props, grounded)
                ids.append(nid)
                continue
            existing = self._conn.execute(
                "SELECT id FROM kg_nodes WHERE kb_id = ? AND type = ? AND label = ? LIMIT 1;",
                (kb_id, typ, label),
            ).fetchone()
            if existing is not None:
                nid = existing["id"]
                self._merge_kg_node(nid, props, grounded)
            else:
                nid = uuid.uuid4().hex
                self._conn.execute(
                    """
                    INSERT INTO kg_nodes
                      (id, org_id, kb_id, type, label, properties, grounded_in, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (nid, _ORG, kb_id, typ, label, json.dumps(props),
                     json.dumps(grounded[-_MAX_GROUNDED:]), _now_iso()),
                )
                embedding = nd.get("embedding")
                if embedding is not None:
                    self._conn.execute(
                        "INSERT INTO vec_kg_nodes (node_id, embedding) VALUES (?, ?);",
                        (nid, serialize_float32(list(embedding))),
                    )
            batch[key] = nid
            ids.append(nid)
        self._conn.commit()
        return ids

    def _merge_kg_node(self, node_id: str, props: dict, grounded: list[str]) -> None:
        """Merge into an existing node: existing properties win; grounding unions."""
        row = self._conn.execute(
            "SELECT properties, grounded_in FROM kg_nodes WHERE id = ?;", (node_id,)
        ).fetchone()
        if row is None:
            return
        existing_props = _json_load(row["properties"], {})
        if not isinstance(existing_props, dict):
            existing_props = {}
        existing_grounded = _json_load(row["grounded_in"], [])
        if not isinstance(existing_grounded, list):
            existing_grounded = []
        merged_props = {**props, **existing_props}
        merged_grounded = list(dict.fromkeys([*existing_grounded, *grounded]))[-_MAX_GROUNDED:]
        self._conn.execute(
            "UPDATE kg_nodes SET properties = ?, grounded_in = ? WHERE id = ?;",
            (json.dumps(merged_props), json.dumps(merged_grounded), node_id),
        )

    async def update_kg_node(
        self,
        kb_id: str,
        node_id: str,
        *,
        properties: dict,
        grounded_in: list[str] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        """Overwrite payload in place (no merge). Re-indexes the vector if given."""
        if grounded_in is not None:
            self._conn.execute(
                "UPDATE kg_nodes SET properties = ?, grounded_in = ? WHERE id = ? AND kb_id = ?;",
                (json.dumps(properties), json.dumps(list(grounded_in)[-_MAX_GROUNDED:]),
                 node_id, kb_id),
            )
        else:
            self._conn.execute(
                "UPDATE kg_nodes SET properties = ? WHERE id = ? AND kb_id = ?;",
                (json.dumps(properties), node_id, kb_id),
            )
        if embedding is not None:
            self._conn.execute("DELETE FROM vec_kg_nodes WHERE node_id = ?;", (node_id,))
            self._conn.execute(
                "INSERT INTO vec_kg_nodes (node_id, embedding) VALUES (?, ?);",
                (node_id, serialize_float32(list(embedding))),
            )
        self._conn.commit()

    async def upsert_kg_edges(self, kb_id: str, edges: list[dict]) -> int:
        """Insert edges, skipping self-loops, dangling ids, and existing
        ``(source, target, relation)`` triples. Returns the count inserted."""
        if not edges:
            return 0
        inserted = 0
        for e in edges:
            sid = e.get("source_node_id")
            tid = e.get("target_node_id")
            rel = e.get("relation") or ""
            if not sid or not tid or sid == tid:
                continue
            dupe = self._conn.execute(
                "SELECT 1 FROM kg_edges WHERE kb_id = ? AND source_node_id = ? "
                "AND target_node_id = ? AND relation = ? LIMIT 1;",
                (kb_id, sid, tid, rel),
            ).fetchone()
            if dupe is not None:
                continue
            self._conn.execute(
                """
                INSERT INTO kg_edges
                  (id, org_id, kb_id, source_node_id, target_node_id, relation,
                   properties, grounded_in, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (uuid.uuid4().hex, _ORG, kb_id, sid, tid, rel,
                 json.dumps(dict(e.get("properties") or {})),
                 json.dumps(list(e.get("grounded_in") or [])), _now_iso()),
            )
            inserted += 1
        self._conn.commit()
        return inserted

    async def match_kg_nodes(
        self,
        kb_id: str,
        query_embedding: list[float],
        match_count: int,
        min_similarity: float,
    ) -> list[dict]:
        """Cosine KNN over vec_kg_nodes joined to kg_nodes; rows carry `similarity`."""
        q = serialize_float32(query_embedding)
        rows = self._conn.execute(
            """
            SELECT n.id, n.type, n.label, n.properties,
                   vec_distance_cosine(v.embedding, ?) AS dist
            FROM vec_kg_nodes v JOIN kg_nodes n ON n.id = v.node_id
            WHERE n.kb_id = ?
            ORDER BY dist LIMIT ?;
            """,
            (q, kb_id, match_count),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            similarity = 1.0 - float(r["dist"])
            if similarity < min_similarity:
                continue
            out.append({
                "id": r["id"],
                "type": r["type"],
                "label": r["label"],
                "properties": _json_load(r["properties"], {}),
                "similarity": similarity,
            })
        return out

    def get_kg_subgraph(
        self,
        kb_id: str,
        *,
        seed_node_ids: list[str] | None = None,
        node_cap: int = 200,
        edge_cap: int = 600,
        depth: int = 1,
    ) -> dict:
        """BFS from ``seed_node_ids`` up to ``depth`` hops; else whole graph, capped."""
        if seed_node_ids:
            frontier = list(dict.fromkeys(seed_node_ids))
            all_node_ids: set[str] = set(frontier)
            all_edge_rows: list = []
            seen_edge_ids: set[str] = set()
            visited_frontiers: set[str] = set()
            for _ in range(max(depth, 1)):
                to_expand = [n for n in frontier if n not in visited_frontiers]
                if not to_expand:
                    break
                visited_frontiers.update(to_expand)
                ph = ",".join("?" for _ in to_expand)
                hop_rows = self._conn.execute(
                    f"SELECT id, source_node_id, target_node_id, relation, properties, grounded_in "
                    f"FROM kg_edges WHERE kb_id = ? "
                    f"AND (source_node_id IN ({ph}) OR target_node_id IN ({ph})) LIMIT ?;",
                    (kb_id, *to_expand, *to_expand, edge_cap),
                ).fetchall()
                new_nodes: set[str] = set()
                for er in hop_rows:
                    eid = er["id"]
                    if eid not in seen_edge_ids:
                        seen_edge_ids.add(eid)
                        all_edge_rows.append(er)
                    new_nodes.add(er["source_node_id"])
                    new_nodes.add(er["target_node_id"])
                all_node_ids.update(new_nodes)
                if len(all_node_ids) >= node_cap:
                    break
                frontier = [n for n in new_nodes if n not in visited_frontiers]
                if not frontier:
                    break
            wanted = list(all_node_ids)[:node_cap]
            nph = ",".join("?" for _ in wanted)
            node_rows = (
                self._conn.execute(
                    f"SELECT id, type, label, properties FROM kg_nodes "
                    f"WHERE kb_id = ? AND id IN ({nph});",
                    (kb_id, *wanted),
                ).fetchall()
                if wanted
                else []
            )
            edge_rows = all_edge_rows[:edge_cap]
        else:
            node_rows = self._conn.execute(
                "SELECT id, type, label, properties FROM kg_nodes WHERE kb_id = ? LIMIT ?;",
                (kb_id, node_cap),
            ).fetchall()
            edge_rows = self._conn.execute(
                "SELECT id, source_node_id, target_node_id, relation, properties, grounded_in "
                "FROM kg_edges WHERE kb_id = ? LIMIT ?;",
                (kb_id, edge_cap),
            ).fetchall()
        nodes = [
            {"id": r["id"], "type": r["type"], "label": r["label"],
             "properties": _json_load(r["properties"], {})}
            for r in node_rows
        ]
        edges = [
            {"id": r["id"], "source_node_id": r["source_node_id"],
             "target_node_id": r["target_node_id"], "relation": r["relation"],
             "properties": _json_load(r["properties"], {})}
            for r in edge_rows
        ]
        return {"nodes": nodes, "edges": edges}

    def list_kg_nodes(
        self, kb_id: str, *, type: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """Most-recent nodes in `kb_id` (optionally one type), newest first."""
        n = min(limit or 50, 500)
        sql = "SELECT id, type, label, properties, grounded_in, created_at FROM kg_nodes WHERE kb_id = ?"
        params: list[object] = [kb_id]
        if type:
            sql += " AND type = ?"
            params.append(type)
        sql += " ORDER BY created_at DESC LIMIT ?;"
        params.append(n)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {"id": r["id"], "type": r["type"], "label": r["label"],
             "properties": _json_load(r["properties"], {}),
             "grounded_in": _json_load(r["grounded_in"], []),
             "created_at": r["created_at"]}
            for r in rows
        ]

    def get_kg_node(self, kb_id: str, node_id: str) -> dict | None:
        """One node by id within `kb_id`, or None. Full decoded row — the
        authoritative read for re-distilling a concept (unlike list_kg_nodes,
        which is capped + recency-windowed and can miss an older target)."""
        r = self._conn.execute(
            "SELECT id, type, label, properties, grounded_in "
            "FROM kg_nodes WHERE id = ? AND kb_id = ? LIMIT 1;",
            (node_id, kb_id),
        ).fetchone()
        if r is None:
            return None
        return {
            "id": r["id"], "type": r["type"], "label": r["label"],
            "properties": _json_load(r["properties"], {}),
            "grounded_in": _json_load(r["grounded_in"], []),
        }

    def clear_kg(self, kb_id: str) -> None:
        """Delete all nodes and edges for `kb_id` (edges first — FK / vec_kg_nodes)."""
        self._conn.execute("DELETE FROM kg_edges WHERE kb_id = ?;", (kb_id,))
        # Delete vec_kg_nodes for all nodes in this KB before deleting the nodes.
        for row in self._conn.execute(
            "SELECT id FROM kg_nodes WHERE kb_id = ?;", (kb_id,)
        ).fetchall():
            self._conn.execute("DELETE FROM vec_kg_nodes WHERE node_id = ?;", (row["id"],))
        self._conn.execute("DELETE FROM kg_nodes WHERE kb_id = ?;", (kb_id,))
        self._conn.commit()

    def kg_stats(self, kb_id: str) -> dict:
        """Node/edge totals + counts by node type and by relation."""
        node_count = self._conn.execute(
            "SELECT COUNT(*) AS n FROM kg_nodes WHERE kb_id = ?;", (kb_id,)
        ).fetchone()["n"]
        edge_count = self._conn.execute(
            "SELECT COUNT(*) AS n FROM kg_edges WHERE kb_id = ?;", (kb_id,)
        ).fetchone()["n"]
        by_type: dict[str, int] = {}
        for r in self._conn.execute(
            "SELECT type, COUNT(*) AS n FROM kg_nodes WHERE kb_id = ? GROUP BY type;", (kb_id,)
        ).fetchall():
            by_type[r["type"] or "unknown"] = r["n"]
        by_relation: dict[str, int] = {}
        for r in self._conn.execute(
            "SELECT relation, COUNT(*) AS n FROM kg_edges WHERE kb_id = ? GROUP BY relation;",
            (kb_id,),
        ).fetchall():
            by_relation[r["relation"] or "unknown"] = r["n"]
        return {
            "node_count": int(node_count),
            "edge_count": int(edge_count),
            "by_type": by_type,
            "by_relation": by_relation,
        }

    # --- KG intent schema (versioned) ----------------------------------------

    def get_kg_intent(self, kb_id: str) -> dict | None:
        """The KB's highest-version approved KG intent schema, or None if never set."""
        r = self._conn.execute(
            "SELECT version, schema FROM kg_schemas WHERE kb_id = ? ORDER BY version DESC LIMIT 1;",
            (kb_id,),
        ).fetchone()
        if r is None:
            return None
        return {"version": r["version"], "schema": _json_load(r["schema"], {})}

    def set_kg_intent(self, org_id: str, kb_id: str, schema: dict) -> dict:
        """Persist an approved schema as the next version (never overwrites history).

        Atomically reads the current max version for `kb_id` and inserts the next.
        Returns ``{"version": <new>, "schema": <schema>}``."""
        # Single shared connection serializes the read-then-write; unique index is the backstop.
        cur = self._conn.execute(
            "SELECT version FROM kg_schemas WHERE kb_id = ? ORDER BY version DESC LIMIT 1;",
            (kb_id,),
        ).fetchone()
        next_version = (cur["version"] if cur is not None else 0) + 1
        self._conn.execute(
            "INSERT INTO kg_schemas (id, org_id, kb_id, version, schema, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (uuid.uuid4().hex, org_id, kb_id, next_version, json.dumps(schema), _now_iso()),
        )
        self._conn.commit()
        return {"version": next_version, "schema": schema}

    # --- first-run offer-once stamp ------------------------------------------

    def get_init_offered(self, kb_id: str) -> bool:
        """Return True iff the wizard has already been offered for `kb_id`."""
        try:
            r = self._conn.execute(
                "SELECT init_offered_at FROM kbs WHERE id = ? LIMIT 1;", (kb_id,)
            ).fetchone()
            return bool(r and r["init_offered_at"])
        except Exception:  # column absent on old DB
            return False

    def mark_init_offered(self, kb_id: str) -> None:
        """Stamp `kb_id` with the init-offered timestamp.

        The column ``init_offered_at`` is added by migration 0007; on an
        older DB that hasn't been migrated the UPDATE silently no-ops (the
        column is absent and SQLite raises ``OperationalError`` — swallowed
        here so the local tier is always safe)."""
        try:
            self._conn.execute(
                "UPDATE kbs SET init_offered_at = ? WHERE id = ?;",
                (_now_iso(), kb_id),
            )
            self._conn.commit()
        except Exception:  # column may not exist yet
            pass

    # --- schema-drift offer debounce -----------------------------------------

    def get_drift_marker(self, kb_id: str) -> int:
        """Residual node count stamped at the last drift offer for `kb_id` (0 if never).

        Powers the re-arm gate in ``drift.assess_drift`` — a declined drift offer
        only re-surfaces once residual grows past this baseline. Best-effort: 0 when
        the column is absent (pre-migration 0008)."""
        try:
            r = self._conn.execute(
                "SELECT drift_offered_count FROM kbs WHERE id = ? LIMIT 1;", (kb_id,)
            ).fetchone()
            return int(r["drift_offered_count"]) if r and r["drift_offered_count"] is not None else 0
        except Exception:  # column absent on old DB
            return 0

    def set_drift_marker(self, kb_id: str, count: int) -> None:
        """Stamp the residual count at which a drift offer was surfaced for `kb_id`.

        Called once per offer (by ``br8n_mark_drift_offered``) so the next session
        doesn't re-nag until drift intensifies. No-op if the column is absent."""
        try:
            self._conn.execute(
                "UPDATE kbs SET drift_offered_count = ? WHERE id = ?;",
                (int(count), kb_id),
            )
            self._conn.commit()
        except Exception:  # column may not exist yet
            pass

    # --- monitoring — best-effort --------------------------------------------

    async def record_access(
        self,
        *,
        org_id: str,
        kb_id: str,
        surface: str,
        targets,
        query_text: str | None = None,
    ) -> None:
        """No-op locally — access monitoring is a cloud-tier (billing) concern."""
        return None


def _json_load(value, default):
    """Decode a JSON text column; tolerate NULL / already-decoded values."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default

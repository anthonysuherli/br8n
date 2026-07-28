"""SupabaseStore — the cloud-tier Store, a faithful wrapper over today's code.

    SupabaseStore(access_token?) ──► {service_client, user_client(token)} ──► Postgres

This introduces no new SQL: every query/RPC here is copied verbatim from the
engine's current call sites (preamble, findings/service, capture, explore,
synopsis, tenancy, monitoring) so flipping those sites to the Store later is a
no-op. See each method's docstring for the source it mirrors.

**Client-selection decision.** The existing code splits clients by intent:
user-scoped writes/reads on findings go through `user_client(access_token)` so
RLS is authoritative, while reads, tenancy find-or-create, synopsis, exploration
rows and monitoring use the `service_client()` (with explicit org/kb scoping).
To keep this a low-risk wrapper we take `access_token` at construction and pick
the client *per method exactly as the original* did — RLS-scoped finding ops use
`_user` (the token client when a token is present, else the service client as a
single-user fallback), everything else uses `service_client()`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from supabase import Client

from br8n.agent.synopsis import load_synopsis as _load_synopsis
from br8n.clients.supabase import service_client, user_client
from br8n.interfaces.mcp.tenancy import (
    _find_or_create,
    _login,
    _org_for,
)
from br8n.constants import JOURNAL_SCOPE
from br8n.monitoring.recorder import record_access as _record_access

# Column lists copied from findings/service.py — keep in lockstep.
_FINDING_COLS = "id, title, content, category, confidence, tags, provenance, metadata, created_at"
_FINDING_LIST_COLS = "id, title, category, confidence, tags, metadata, created_at"

LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100

# Cap on grounding finding ids per node — keep the most recent (see SQLiteStore).
_MAX_GROUNDED = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseStore:
    """Store backed by Supabase. `access_token` selects RLS-scoped finding ops."""

    def __init__(self, access_token: str | None = None, org_id: str | None = None) -> None:
        self.access_token = access_token
        self.org_id = org_id

    # --- client selection ----------------------------------------------------

    def _user(self) -> Client:
        """RLS-scoped client for finding reads/writes; service client when no token."""
        return user_client(self.access_token) if self.access_token else service_client()

    def _resolve_org(self) -> str:
        """Injected org (per-user request) or the configured login's org (MCP path)."""
        if self.org_id is not None:
            return self.org_id
        user_id, _ = _login()
        return _org_for(user_id)

    # --- findings — hot path -------------------------------------------------

    async def match_findings(
        self,
        kb_id: str | None,
        query_embedding: list[float],
        match_count: int,
        min_similarity: float,
        categories: list[str] | None = None,
    ) -> list[dict]:
        """Mirrors agent/preamble.py select_preamble — the match_findings RPC.

        Runs against the service client (the preamble path already uses a service
        client for this read). `kb_id=None` searches the whole org via the
        explicit ``match_org_id`` filter (the tenancy invariant — never run
        org-wide without it); `categories` narrows by category. Rows carry a
        `similarity` field."""
        args: dict = {
            "query_embedding": query_embedding,
            "match_kb_id": kb_id,
            "match_count": match_count,
            "min_similarity": min_similarity,
        }
        if kb_id is None:
            args["match_org_id"] = self._resolve_org()
        if categories:
            args["match_categories"] = list(categories)
        res = service_client().rpc("match_findings", args).execute()
        return res.data or []

    async def insert_findings(self, rows: list[dict]) -> list[str]:
        """Insert already-embedded finding rows; return ids in order.

        Mirrors capture/service.py + findings/ingest.py: rows are built and
        embedded by the caller and written through the user-scoped client."""
        if not rows:
            return []
        inserted = self._user().table("findings").insert(rows).execute()
        return [r["id"] for r in (inserted.data or [])]

    def get_finding(self, kb_id: str, finding_id: str) -> dict:
        """Mirrors findings/service.py get_finding."""
        row = (
            self._user()
            .table("findings")
            .select(_FINDING_COLS)
            .eq("kb_id", kb_id)
            .eq("id", finding_id)
            .limit(1)
            .execute()
        ).data
        if not row:
            raise RuntimeError("finding not found")
        return row[0]

    def list_findings(
        self,
        kb_id: str | None,
        category: str | list[str] | None = None,
        limit: int | None = None,
    ) -> dict:
        """Mirrors findings/service.py list_findings.

        ``kb_id=None`` lists org-wide and ``category`` accepts a list, matching
        ``match_findings``. Org-wide goes through the explicit ``org_id``
        filter, never RLS alone — the tenancy invariant this module documents.
        """
        n = min(limit or LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT)
        q = self._user().table("findings").select(_FINDING_LIST_COLS)
        if kb_id is not None:
            q = q.eq("kb_id", kb_id)
        else:
            q = q.eq("org_id", self._resolve_org())
        cats = [category] if isinstance(category, str) else list(category or [])
        if len(cats) == 1:
            q = q.eq("category", cats[0])
        elif cats:
            q = q.in_("category", cats)
        rows = q.order("created_at", desc=True).limit(n).execute().data or []
        return {"count": len(rows), "findings": rows}

    def delete_finding(self, kb_id: str, finding_id: str) -> dict:
        """Mirrors findings/service.py delete_finding."""
        self._user().table("findings").delete().eq("kb_id", kb_id).eq(
            "id", finding_id
        ).execute()
        return {"deleted": finding_id}

    def count_findings(self, kb_id: str) -> int:
        """Exact finding count for `kb_id` via a count="exact" head query.

        Mirrors kbs/service.py kb_stats: a `count="exact"` select scoped to
        `kb_id`, limited to one row (the count rides on the result, not the rows)."""
        return (
            self._user()
            .table("findings")
            .select("id", count="exact")
            .eq("kb_id", kb_id)
            .limit(1)
            .execute()
        ).count or 0

    # --- synopsis spine ------------------------------------------------------

    def load_synopsis(self, kb_id: str) -> dict | None:
        """Reuses agent/synopsis.py load_synopsis against the service client."""
        return _load_synopsis(service_client(), kb_id)

    def upsert_synopsis(
        self, kb_id: str, content: list[dict], finding_count: int, model: str
    ) -> None:
        """Mirrors the kb_synopsis upsert in agent/synopsis.py maybe_rebuild_synopsis.

        `org_id` is resolved from the kb row to satisfy the table's not-null
        column; the upsert conflicts on kb_id (one current row per KB). `model`
        records the synopsis-builder model for provenance (a not-null-able
        column the original write set)."""
        sb = service_client()
        kb = sb.table("kbs").select("org_id").eq("id", kb_id).limit(1).execute().data
        if not kb:
            raise RuntimeError(f"kb {kb_id} not found — cannot upsert synopsis")
        org_id = kb[0]["org_id"]
        sb.table("kb_synopsis").upsert(
            {
                "org_id": org_id,
                "kb_id": kb_id,
                "content": content,
                "finding_count_at_build": finding_count,
                "model": model,
                "built_at": _now_iso(),
            },
            on_conflict="kb_id",
        ).execute()

    # --- exploration row lifecycle -------------------------------------------

    def create_exploration(self, org_id: str, kb_id: str, prompt: str) -> str:
        """Mirrors the explorations insert in api/explore.py start_explore."""
        row = (
            service_client()
            .table("explorations")
            .insert(
                {
                    "org_id": org_id,
                    "kb_id": kb_id,
                    "prompt": prompt,
                    "status": "pending",
                    "started_at": _now_iso(),
                }
            )
            .execute()
        )
        return row.data[0]["id"]

    def update_exploration(self, exploration_id: str, **patch) -> None:
        """Mirrors the explorations update calls in api/explore.py."""
        service_client().table("explorations").update(patch).eq(
            "id", exploration_id
        ).execute()

    def get_exploration(self, exploration_id: str) -> dict | None:
        """Mirrors the explorations read in api/explore.py explore_status."""
        rows = (
            service_client()
            .table("explorations")
            .select("id, status, finding_ids, completed_at, error")
            .eq("id", exploration_id)
            .limit(1)
            .execute()
        ).data
        return rows[0] if rows else None

    # --- tenancy — find-or-create by name ------------------------------------

    def resolve_project(self, name: str, *, create: bool) -> tuple[str, str]:
        """Mirrors interfaces/mcp/tenancy.py resolve_project_id → (org_id, project_id)."""
        org_id = self._resolve_org()
        sb = service_client()
        pid = _find_or_create(
            sb,
            "projects",
            {"org_id": org_id, "name": name},
            {"org_id": org_id, "name": name},
            create,
        )
        return org_id, pid

    def resolve_kb(self, org_id: str, project_id: str, name: str, *, create: bool) -> str:
        """Mirrors the kbs find-or-create in interfaces/mcp/tenancy.py resolve_tenant."""
        sb = service_client()
        return _find_or_create(
            sb,
            "kbs",
            {"org_id": org_id, "project_id": project_id, "name": name},
            {"org_id": org_id, "project_id": project_id, "name": name},
            create,
        )

    def list_projects(self) -> list[dict]:
        """All of the authenticated user's projects + KBs, with snapshot rollups.

        Scopes to the user's org via the same GoTrue login + `_org_for` lookup as
        `resolve_project`, then service-client reads with explicit `org_id`
        scoping (the load-bearing tenancy invariant). One count="exact" snapshot
        query per KB yields both `snapshot_count` and `last_activity`."""
        org_id = self._resolve_org()
        sb = service_client()
        prows = (
            sb.table("projects").select("id, name")
            .eq("org_id", org_id).neq("name", JOURNAL_SCOPE)
            .order("created_at").execute()
        ).data or []
        projects: list[dict] = []
        for p in prows:
            krows = (
                sb.table("kbs").select("id, name")
                .eq("org_id", org_id).eq("project_id", p["id"])
                .order("created_at").execute()
            ).data or []
            kbs: list[dict] = []
            for k in krows:
                res = (
                    sb.table("findings").select("created_at", count="exact")
                    .eq("kb_id", k["id"]).eq("category", "snapshot")
                    .order("created_at", desc=True).limit(1).execute()
                )
                kbs.append({
                    "kb": k["name"],
                    "kb_id": k["id"],
                    "snapshot_count": res.count or 0,
                    "last_activity": res.data[0]["created_at"] if res.data else None,
                })
            projects.append({"project": p["name"], "project_id": p["id"], "kbs": kbs})
        return projects

    # --- activity knowledge graph --------------------------------------------
    # Reuses delapan's existing `kg_nodes`/`kg_edges` tables and the
    # `match_kg_nodes` RPC (migration 0003) — same instance, same schema. Writes
    # go through the service client (KG ownership is verified by tenancy first,
    # mirroring delapan's builder). Dedupe is exact `(kb_id, type, label)`.

    async def upsert_kg_nodes(self, kb_id: str, nodes: list[dict]) -> list[str]:
        """Insert-or-merge nodes by exact ``(kb_id, type, label)``; ids in order."""
        if not nodes:
            return []
        sb = service_client()
        ids: list[str] = []
        batch: dict[tuple[str, str], str] = {}
        for nd in nodes:
            typ = nd.get("type") or ""
            label = nd.get("label") or ""
            props = dict(nd.get("properties") or {})
            grounded = list(nd.get("grounded_in") or [])
            key = (typ, label)
            if key in batch:
                self._merge_kg_node(sb, batch[key], props, grounded)
                ids.append(batch[key])
                continue
            existing = (
                sb.table("kg_nodes").select("id")
                .eq("kb_id", kb_id).eq("type", typ).eq("label", label)
                .limit(1).execute()
            ).data
            if existing:
                nid = existing[0]["id"]
                self._merge_kg_node(sb, nid, props, grounded)
            else:
                row = {
                    "org_id": self.org_id,
                    "kb_id": kb_id,
                    "type": typ,
                    "label": label,
                    "properties": props,
                    "grounded_in": grounded[-_MAX_GROUNDED:],
                }
                if nd.get("embedding") is not None:
                    row["embedding"] = list(nd["embedding"])
                nid = sb.table("kg_nodes").insert(row).execute().data[0]["id"]
            batch[key] = nid
            ids.append(nid)
        return ids

    def _merge_kg_node(self, sb, node_id: str, props: dict, grounded: list[str]) -> None:
        """Merge into an existing node: existing properties win; grounding unions."""
        cur = (
            sb.table("kg_nodes").select("properties, grounded_in")
            .eq("id", node_id).limit(1).execute()
        ).data
        if not cur:
            return
        merged_props = {**props, **(cur[0].get("properties") or {})}
        merged_grounded = list(
            dict.fromkeys([*(cur[0].get("grounded_in") or []), *grounded])
        )[-_MAX_GROUNDED:]
        sb.table("kg_nodes").update(
            {"properties": merged_props, "grounded_in": merged_grounded}
        ).eq("id", node_id).execute()

    async def update_kg_node(
        self,
        kb_id: str,
        node_id: str,
        *,
        properties: dict,
        grounded_in: list[str] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        """Overwrite a node's payload in place (wholesale `properties`; replace
        `grounded_in` / re-index `embedding` only when given), matched by
        ``id == node_id AND kb_id``."""
        patch: dict = {"properties": properties}
        if grounded_in is not None:
            patch["grounded_in"] = list(grounded_in)[-_MAX_GROUNDED:]
        if embedding is not None:
            patch["embedding"] = list(embedding)
        q = (
            service_client()
            .table("kg_nodes")
            .update(patch)
            .eq("id", node_id)
            .eq("kb_id", kb_id)
        )
        if self.org_id is not None:
            q = q.eq("org_id", self.org_id)
        q.execute()

    async def upsert_kg_edges(self, kb_id: str, edges: list[dict]) -> int:
        """Insert edges, skipping self-loops, dangling ids, and existing triples."""
        if not edges:
            return 0
        sb = service_client()
        rows: list[dict] = []
        for e in edges:
            sid = e.get("source_node_id")
            tid = e.get("target_node_id")
            rel = e.get("relation") or ""
            if not sid or not tid or sid == tid:
                continue
            dupe = (
                sb.table("kg_edges").select("id")
                .eq("kb_id", kb_id).eq("source_node_id", sid)
                .eq("target_node_id", tid).eq("relation", rel)
                .limit(1).execute()
            ).data
            if dupe:
                continue
            rows.append({
                "org_id": self.org_id,
                "kb_id": kb_id,
                "source_node_id": sid,
                "target_node_id": tid,
                "relation": rel,
                "properties": dict(e.get("properties") or {}),
                "grounded_in": list(e.get("grounded_in") or []),
            })
        if not rows:
            return 0
        ins = sb.table("kg_edges").insert(rows).execute().data
        return len(ins or [])

    async def match_kg_nodes(
        self,
        kb_id: str,
        query_embedding: list[float],
        match_count: int,
        min_similarity: float,
    ) -> list[dict]:
        """Reuses the `match_kg_nodes` pgvector RPC (migration 0003)."""
        # NOTE: kb_id is org-unique and resolved via org-scoped tenancy, so this
        # is org-safe today. Defense-in-depth org_id filtering needs an RPC param
        # (tracked as follow-up A6b) — do not add it here without the migration.
        res = service_client().rpc(
            "match_kg_nodes",
            {
                "query_embedding": query_embedding,
                "match_kb_id": kb_id,
                "match_count": match_count,
                "min_similarity": min_similarity,
            },
        ).execute()
        return res.data or []

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
        sb = service_client()
        edge_cols = "id, source_node_id, target_node_id, relation, properties"
        node_cols = "id, type, label, properties"
        if seed_node_ids:
            frontier = list(dict.fromkeys(seed_node_ids))
            all_node_ids: set[str] = set(frontier)
            all_edges: list[dict] = []
            visited_frontiers: set[str] = set()
            for _ in range(max(depth, 1)):
                # Only expand nodes not already used as a frontier.
                to_expand = [n for n in frontier if n not in visited_frontiers]
                if not to_expand:
                    break
                visited_frontiers.update(to_expand)
                ids_str = ",".join(to_expand)
                eq = (
                    sb.table("kg_edges").select(edge_cols).eq("kb_id", kb_id)
                    .or_(
                        f"source_node_id.in.({ids_str}),"
                        f"target_node_id.in.({ids_str})"
                    )
                )
                if self.org_id is not None:
                    eq = eq.eq("org_id", self.org_id)
                hop_edges = eq.limit(edge_cap).execute().data or []
                all_edges.extend(hop_edges)
                # Collect newly discovered neighbours as next frontier.
                new_nodes: set[str] = set()
                for e in hop_edges:
                    new_nodes.add(e["source_node_id"])
                    new_nodes.add(e["target_node_id"])
                all_node_ids.update(new_nodes)
                if len(all_node_ids) >= node_cap:
                    break
                frontier = [n for n in new_nodes if n not in visited_frontiers]
                if not frontier:
                    break
            # Dedupe edges by id.
            seen_edge_ids: set[str] = set()
            unique_edges: list[dict] = []
            for e in all_edges:
                eid = e.get("id")
                if eid not in seen_edge_ids:
                    seen_edge_ids.add(eid)  # type: ignore[arg-type]
                    unique_edges.append(e)
            edges = unique_edges[:edge_cap]
            wanted = list(all_node_ids)[:node_cap]
            if wanted:
                nq = sb.table("kg_nodes").select(node_cols).in_("id", wanted)
                if self.org_id is not None:
                    nq = nq.eq("org_id", self.org_id)
                nodes = nq.execute().data
            else:
                nodes = []
        else:
            nq = sb.table("kg_nodes").select(node_cols).eq("kb_id", kb_id)
            if self.org_id is not None:
                nq = nq.eq("org_id", self.org_id)
            nodes = nq.limit(node_cap).execute().data or []
            eq = sb.table("kg_edges").select(edge_cols).eq("kb_id", kb_id)
            if self.org_id is not None:
                eq = eq.eq("org_id", self.org_id)
            edges = eq.limit(edge_cap).execute().data or []
        return {"nodes": nodes or [], "edges": edges or []}

    def list_kg_nodes(
        self, kb_id: str, *, type: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """Most-recent nodes in `kb_id` (optionally one type), newest first."""
        n = min(limit or 50, 500)
        q = (
            service_client()
            .table("kg_nodes")
            .select("id, type, label, properties, grounded_in, created_at")
            .eq("kb_id", kb_id)
        )
        if self.org_id is not None:
            q = q.eq("org_id", self.org_id)
        if type:
            q = q.eq("type", type)
        return q.order("created_at", desc=True).limit(n).execute().data or []

    def get_kg_node(self, kb_id: str, node_id: str) -> dict | None:
        """One node by id within `kb_id`, or None. Authoritative full-row read
        (vs. the capped, recency-windowed list_kg_nodes) for re-distill."""
        q = (
            service_client()
            .table("kg_nodes")
            .select("id, type, label, properties, grounded_in")
            .eq("id", node_id)
            .eq("kb_id", kb_id)
        )
        if self.org_id is not None:
            q = q.eq("org_id", self.org_id)
        rows = q.limit(1).execute().data or []
        return rows[0] if rows else None

    def clear_kg(self, kb_id: str) -> None:
        """Delete all nodes and edges for `kb_id` (edges first — FK constraint)."""
        sb = service_client()
        sb.table("kg_edges").delete().eq("kb_id", kb_id).execute()
        sb.table("kg_nodes").delete().eq("kb_id", kb_id).execute()

    def kg_stats(self, kb_id: str) -> dict:
        """Node/edge totals + counts by node type and by relation."""
        sb = service_client()
        nq = sb.table("kg_nodes").select("id", count="exact").eq("kb_id", kb_id)
        if self.org_id is not None:
            nq = nq.eq("org_id", self.org_id)
        node_count = nq.limit(1).execute().count or 0
        eq = sb.table("kg_edges").select("id", count="exact").eq("kb_id", kb_id)
        if self.org_id is not None:
            eq = eq.eq("org_id", self.org_id)
        edge_count = eq.limit(1).execute().count or 0
        by_type: dict[str, int] = {}
        tq = sb.table("kg_nodes").select("type").eq("kb_id", kb_id)
        if self.org_id is not None:
            tq = tq.eq("org_id", self.org_id)
        for r in tq.limit(5000).execute().data or []:
            t = (r.get("type") if isinstance(r, dict) else None) or "unknown"
            by_type[t] = by_type.get(t, 0) + 1
        by_relation: dict[str, int] = {}
        rq = sb.table("kg_edges").select("relation").eq("kb_id", kb_id)
        if self.org_id is not None:
            rq = rq.eq("org_id", self.org_id)
        for r in rq.limit(5000).execute().data or []:
            rel = (r.get("relation") if isinstance(r, dict) else None) or "unknown"
            by_relation[rel] = by_relation.get(rel, 0) + 1
        return {
            "node_count": node_count,
            "edge_count": edge_count,
            "by_type": by_type,
            "by_relation": by_relation,
        }

    # --- KG intent schema (versioned) ----------------------------------------
    # Mirrors delapan/knowledge_graph/service.py get_kg_intent/set_kg_intent.
    # Writes go through the service client (tenancy already verified upstream);
    # the explicit org_id/kb_id scoping is the invariant that keeps it tenant-safe.

    def get_kg_intent(self, kb_id: str) -> dict | None:
        """The KB's highest-version approved KG intent schema, or None if never set."""
        rows = (
            service_client()
            .table("kg_schemas")
            .select("version, schema")
            .eq("kb_id", kb_id)
            .order("version", desc=True)
            .limit(1)
            .execute()
        ).data or []
        if not rows or not isinstance(rows[0], dict):
            return None
        row = rows[0]
        return {"version": row.get("version"), "schema": row.get("schema") or {}}

    def set_kg_intent(self, org_id: str, kb_id: str, schema: dict) -> dict:
        """Persist an approved schema as the next version (never overwrites history).

        Reads the current max version for `kb_id`, inserts version+1.
        Returns ``{"version": <new>, "schema": <schema>}``."""
        sb = service_client()
        cur = (
            sb.table("kg_schemas")
            .select("version")
            .eq("kb_id", kb_id)
            .order("version", desc=True)
            .limit(1)
            .execute()
        ).data or []
        next_version = (cur[0]["version"] if cur and isinstance(cur[0], dict) else 0) + 1
        sb.table("kg_schemas").insert(
            {"org_id": org_id, "kb_id": kb_id, "version": next_version, "schema": schema}
        ).execute()
        return {"version": next_version, "schema": schema}

    # --- first-run offer-once stamp ------------------------------------------

    def get_init_offered(self, kb_id: str) -> bool:
        """Return True iff the wizard has already been offered for `kb_id`."""
        try:
            rows = (
                service_client()
                .table("kbs")
                .select("init_offered_at")
                .eq("id", kb_id)
                .limit(1)
                .execute()
            ).data or []
            if rows and isinstance(rows[0], dict):
                return bool(rows[0].get("init_offered_at"))
        except Exception:  # noqa: BLE001 — best-effort
            pass
        return False

    def mark_init_offered(self, kb_id: str) -> None:
        """Stamp `kb_id` with the time the KG schema wizard was offered.

        Uses the service client (KB ownership already verified by resolve_tenant
        upstream). Silently no-ops on error so a missing migration never breaks
        the session."""
        try:
            service_client().table("kbs").update(
                {"init_offered_at": _now_iso()}
            ).eq("id", kb_id).execute()
        except Exception:  # noqa: BLE001 — best-effort, never raise
            pass

    # --- schema-drift offer debounce -----------------------------------------

    def get_drift_marker(self, kb_id: str) -> int:
        """Residual count stamped at the last drift offer for `kb_id` (0 if never).

        Best-effort: 0 when the `drift_offered_count` column is absent (the shared
        Supabase schema may not carry it yet) so cloud degrades to re-checking,
        never crashing."""
        try:
            rows = (
                service_client()
                .table("kbs")
                .select("drift_offered_count")
                .eq("id", kb_id)
                .limit(1)
                .execute()
            ).data or []
            if rows and isinstance(rows[0], dict) and rows[0].get("drift_offered_count") is not None:
                return int(rows[0]["drift_offered_count"])
        except Exception:  # noqa: BLE001 — best-effort
            pass
        return 0

    def set_drift_marker(self, kb_id: str, count: int) -> None:
        """Stamp the residual count at which a drift offer was surfaced for `kb_id`.

        Service client (ownership verified upstream by resolve_tenant). Silently
        no-ops on error so a missing column never breaks the session."""
        try:
            service_client().table("kbs").update(
                {"drift_offered_count": int(count)}
            ).eq("id", kb_id).execute()
        except Exception:  # noqa: BLE001 — best-effort, never raise
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
        """Delegates to monitoring/recorder.py record_access — never raises.

        `targets` is a sequence of monitoring.recorder.Target. Delegating keeps
        the access_events row shape in lockstep with the canonical writer and
        preserves its best-effort (never-raises) contract."""
        await _record_access(
            org_id=org_id,
            kb_id=kb_id,
            surface=surface,
            targets=targets,
            query_text=query_text,
            sb=service_client(),
        )

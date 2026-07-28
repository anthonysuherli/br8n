"""The Store protocol — the engine's single seam over persistence.

    engine ──► Store ──► {Supabase (cloud), SQLite (local)}

br8n is split into a free/local tier (SQLite + sqlite-vec, single user, no
auth) and a paid/cloud tier (Supabase: Postgres + pgvector + GoTrue + RLS). The
engine talks to this protocol instead of any one backend so the two tiers share
one engine. Return shapes are plain dicts / lists of dicts matching today's
Supabase rows; no ORM, no backend-specific objects cross this boundary.

This file defines the contract ONLY. `supabase.py` is the cloud implementation;
the engine call sites are flipped to `get_store(...)` in a later task.
"""

from __future__ import annotations

from typing import Protocol


class Store(Protocol):
    """Persistence surface the engine depends on. Implementations are tier-specific."""

    # --- findings — hot path -------------------------------------------------

    async def match_findings(
        self,
        kb_id: str | None,
        query_embedding: list[float],
        match_count: int,
        min_similarity: float,
        categories: list[str] | None = None,
    ) -> list[dict]:
        """Vector-search findings; rows carry a `similarity` field.

        `kb_id` scopes to one KB; `kb_id=None` searches every KB in the store's
        org (the load-bearing org filter — SQLite's synthetic ``"local"`` or the
        SupabaseStore's injected ``org_id``). `categories`, when given, restricts
        results to those `category` values.
        """
        ...

    async def insert_findings(self, rows: list[dict]) -> list[str]:
        """Insert already-embedded finding rows; return the new ids in order."""
        ...

    def get_finding(self, kb_id: str, finding_id: str) -> dict:
        """One finding scoped to `kb_id`. Raises if not found."""
        ...

    def list_findings(
        self,
        kb_id: str | None,
        category: str | list[str] | None = None,
        limit: int | None = None,
    ) -> dict:
        """Most-recent findings in `kb_id`. Returns {"count", "findings"}.

        ``kb_id=None`` lists org-wide; ``category`` accepts one or several —
        mirroring ``match_findings``, so a recency read can span the same
        corpus as a semantic one."""
        ...

    def delete_finding(self, kb_id: str, finding_id: str) -> dict:
        """Delete one finding from `kb_id`. Returns {"deleted": finding_id}."""
        ...

    def count_findings(self, kb_id: str) -> int:
        """Exact number of findings in `kb_id` (uncapped, unlike list_findings)."""
        ...

    # --- synopsis spine ------------------------------------------------------

    def load_synopsis(self, kb_id: str) -> dict | None:
        """Current synopsis row for `kb_id`, or None."""
        ...

    def upsert_synopsis(
        self, kb_id: str, content: list[dict], finding_count: int, model: str
    ) -> None:
        """Write the KB's synopsis spine (one current row per KB)."""
        ...

    # --- exploration row lifecycle -------------------------------------------

    def create_exploration(self, org_id: str, kb_id: str, prompt: str) -> str:
        """Insert a pending exploration row; return its id."""
        ...

    def update_exploration(self, exploration_id: str, **patch) -> None:
        """Patch an exploration row (status / completed_at / finding_ids / error)."""
        ...

    def get_exploration(self, exploration_id: str) -> dict | None:
        """Read an exploration row, or None if missing."""
        ...

    # --- tenancy — find-or-create by name ------------------------------------

    def resolve_project(self, name: str, *, create: bool) -> tuple[str, str]:
        """Resolve the named project → (org_id, project_id)."""
        ...

    def resolve_kb(self, org_id: str, project_id: str, name: str, *, create: bool) -> str:
        """Resolve the named KB within (org_id, project_id) → kb_id."""
        ...

    def list_projects(self) -> list[dict]:
        """All of the caller's projects with their KBs, for client discovery.

        Returns ``[{project, project_id, kbs: [{kb, kb_id, last_activity,
        snapshot_count}]}]``. ``last_activity`` is the newest snapshot's timestamp
        (or None), ``snapshot_count`` the exact snapshot count. The native client's
        home screen reads this — unlike the editor, the phone doesn't already know
        its project + branch. Cloud scopes to the authenticated user's org."""
        ...

    # --- activity knowledge graph --------------------------------------------
    # Nodes/edges live in their own per-KB namespace (`kb_id` = the reserved
    # activity KB). Dedupe is by exact ``(type, normalized label)`` so a repo or
    # file resolves to one stable node; a stored label `embedding` is used only
    # for semantic subgraph seeding, never for dedupe.

    async def upsert_kg_nodes(self, kb_id: str, nodes: list[dict]) -> list[str]:
        """Insert-or-merge nodes; return their ids in input order.

        Each row carries ``org_id, type, label, properties, grounded_in,
        embedding``. A row whose ``(type, normalized label)`` already exists in
        ``kb_id`` reuses that node (merging ``properties`` + ``grounded_in``);
        duplicates within the batch resolve to the same id."""
        ...

    async def upsert_kg_edges(self, kb_id: str, edges: list[dict]) -> int:
        """Insert edges, skipping duplicates; return the number newly inserted.

        Each row carries ``org_id, source_node_id, target_node_id, relation,
        properties, grounded_in``. An edge equal to an existing one on
        ``(source, target, relation)`` is skipped (idempotent re-capture)."""
        ...

    async def update_kg_node(
        self,
        kb_id: str,
        node_id: str,
        *,
        properties: dict,
        grounded_in: list[str] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        """Overwrite a node's payload (unlike upsert_kg_nodes, which merges with
        existing-wins). `properties` replaces wholesale; `grounded_in` replaces when
        given; `embedding` re-indexes the vector when given. Used to re-distill a
        concept's body/confidence/version in place."""
        ...

    async def match_kg_nodes(
        self,
        kb_id: str,
        query_embedding: list[float],
        match_count: int,
        min_similarity: float,
    ) -> list[dict]:
        """Semantic node search in `kb_id`; rows carry a `similarity` field."""
        ...

    def get_kg_subgraph(
        self,
        kb_id: str,
        *,
        seed_node_ids: list[str] | None = None,
        node_cap: int = 200,
        edge_cap: int = 600,
        depth: int = 1,
    ) -> dict:
        """Return ``{"nodes", "edges"}`` for `kb_id`.

        With ``seed_node_ids`` → BFS from those nodes up to ``depth`` hops,
        capped by ``node_cap``/``edge_cap``. Without → the whole graph, capped.
        ``depth=1`` is the original one-hop behaviour."""
        ...

    def list_kg_nodes(
        self, kb_id: str, *, type: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """Most-recent nodes in `kb_id` (optionally one type). Rows carry
        ``id, type, label, properties, created_at``."""
        ...

    def get_kg_node(self, kb_id: str, node_id: str) -> dict | None:
        """One node by id within `kb_id`, or None. Row carries the full decoded
        ``id, type, label, properties, grounded_in`` — the authoritative read for
        re-distilling a concept in place (versus a capped, recency-windowed list)."""
        ...

    def kg_stats(self, kb_id: str) -> dict:
        """Graph totals + breakdowns: ``node_count, edge_count, by_type,
        by_relation``."""
        ...

    def clear_kg(self, kb_id: str) -> None:
        """Delete all nodes and edges for `kb_id` (edges first — FK constraint).

        Used by ``build_graph(rebuild=True)`` before a full rebuild. Scoped
        strictly to ``kb_id`` so other KBs in the same org are never touched."""
        ...

    # --- KG intent schema (versioned, approved target ontology) ---------------
    # Stored in `kg_schemas` (one row per version, newest = active). `set`
    # inserts the next version; `get` reads the highest. The KG builder reads
    # `get` to steer extraction; a view pairs INTENT with EMERGENT ontology.

    def get_kg_intent(self, kb_id: str) -> dict | None:
        """The KB's highest-version approved KG intent schema, or None if never set.

        Returns a dict with at least ``{version, schema}``."""
        ...

    def set_kg_intent(self, org_id: str, kb_id: str, schema: dict) -> dict:
        """Persist an approved schema as the next version (never overwrites history).

        Version is ``max(existing version for kb_id) + 1`` (first set = 1).
        Returns a dict with at least ``{version, schema}``."""
        ...

    # --- first-run offer-once stamp ------------------------------------------

    def get_init_offered(self, kb_id: str) -> bool:
        """Return True iff the KG schema wizard has already been offered for `kb_id`.

        Reads ``init_offered_at``; returns False when the column is absent or
        null (pre-migration, or wizard not yet offered)."""
        ...

    def mark_init_offered(self, kb_id: str) -> None:
        """Stamp `kb_id` with the time the KG schema wizard was offered.

        Called once — by ``br8n_mark_init_offered`` — after the first-run
        schema offer is surfaced.  Prevents re-offering on subsequent sessions.
        No-op if the column is absent (local tier before migration 0007)."""
        ...

    # --- schema-drift offer debounce -----------------------------------------
    # The residual node count stamped at the last drift offer. Powers the re-arm
    # gate in `drift.assess_drift`: a declined drift offer only re-surfaces once
    # residual grows past this baseline by `rearm_delta`.

    def get_drift_marker(self, kb_id: str) -> int:
        """Residual count stamped at the last drift offer for `kb_id` (0 if never).

        Best-effort: returns 0 when the backing column is absent."""
        ...

    def set_drift_marker(self, kb_id: str, count: int) -> None:
        """Stamp the residual count at which a drift offer was surfaced for `kb_id`.

        Called once per offer so the next session doesn't re-nag until drift
        intensifies. No-op if the backing column is absent."""
        ...

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
        """Append access events. Best-effort by contract — must never raise."""
        ...

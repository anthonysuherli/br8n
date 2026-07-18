# Design: Behavioral substrate + next-action-first resume

**Vision goals served:** End Goal 1 (next-action-first resume) and Planned Detours 1–2
(capture/note schema extension; thread model over the activity KG) from
`docs/truenorth/vision.md`. Establishes the substrate End Goals 2–5 build on.

**Status:** Approved 2026-07-18.

## Problem

br8n's resume card orients ("You were: <hypothesis>") but doesn't initiate — the
ADHD/ENTP freeze happens *between* seeing the card and doing item #1. Two structural
gaps block fixing this cleanly:

1. **No structured capture fields.** `hypothesis` is smuggled through the Finding
   *title* and re-parsed heuristically on read
   (`api/resume.py:_hypothesis_from_title`). There is no home for a `next_action`.
2. **No stable thread identity.** Activity-KG task nodes are write-once and deduped
   by LLM-distilled label strings (`store` upsert on `(type, label)`) — too fuzzy
   for the WIP governor and scoreboard to count against.

## Decision summary

- **Scope:** substrate (schema + thread identity) + next-action-first resume. WIP
  governor, agent-wait router, delegation, and scoreboard are later specs on this
  substrate. Thread *state transitions* (park/close) are out of scope here.
- **Storage (approach A):** a nullable `metadata` jsonb column on `findings`.
  Rejected: KG-only storage (resume would depend on the gated, fire-and-forget
  `BR8N_ACTIVITY_KG` subsystem) and tag/title conventions (the hack being removed).
- **Thread identity:** explicit `thread_id` minted on task-node creation, stored in
  node `properties`; writes converge via read-merge-write `update_kg_node` (the
  `distill.py` concept-status precedent). Label dedupe remains the fallback join.

## 1. Capture schema (Detour 1)

- `WorkspaceSnapshot` (`capture/models.py`) gains `next_action: str | None` and
  `thread_id: str | None`. Same fields on `SnapshotRequest` (`api/capture.py`) and
  the `br8n_capture` MCP tool (`interfaces/mcp/server.py`).
- Migration adds `metadata` (jsonb / SQLite JSON text, nullable) to `findings` —
  both stores (`supabase/migrations/`, SQLite schema init).
- `snapshot_to_finding` (`capture/adapter.py`) additionally writes
  `metadata = {"hypothesis": ..., "next_action": ..., "thread_id": ...}` (omitting
  nulls). Title/content rendering is unchanged — old snapshots and old readers keep
  working.
- **Zero-friction invariant:** both new fields are optional and never prompted for.
  The capture skill (`skills/capture/SKILL.md`) instructs Claude to *infer* a
  one-line, ~two-minute `next_action` from the diff/conversation when the user
  didn't state one; the server accepts null and stays deterministic (no server-side
  LLM call on the capture path).

## 2. Thread identity (Detour 2)

- In `activity_extract` (`knowledge_graph/activity.py`), when a task node is first
  created: mint `thread_id = uuid4().hex` and set
  `properties = {"repo", "thread_id", "thread_state": "open", "next_action"}`.
- A new helper `set_task_props(kb_id, node_id, **props)` does read-merge-write via
  `store.get_kg_node` + `store.update_kg_node` (mirroring
  `distill.py`'s concept-status mutation) — never the existing-wins upsert, which
  would silently drop updates.
- When a snapshot carries `thread_id`, it is recorded on the task node's
  properties; the KG mirror of `next_action` is refreshed on each capture.

  **Amended 2026-07-18 (as-built).** This section originally specified that a
  supplied `thread_id` would converge the extract pass *on that node id
  directly*, bypassing label match. That is NOT what shipped: node identity
  still resolves purely via the `(type, label)` upsert, and a supplied
  `thread_id` is only stashed in properties. Consequence: if the LLM distils a
  different label for the same work, a second task node is minted carrying the
  same `thread_id` — two "open" nodes for one thread. Nothing counts task nodes
  today (the WIP governor is a later spec), and no surface round-trips
  `thread_id` back into capture, so the path is currently unreachable and the
  defect is latent. **Follow-up owned by the WIP-governor spec:** implement
  node-id convergence (or a hard thread key) before anything counts threads.
- Rides the existing `BR8N_ACTIVITY_KG` gate; best-effort, fail-silent, same
  `_BG_TASKS` fire-and-forget shape. Park/close transitions and any WIP counting:
  deferred to the WIP-governor spec.

## 3. Next-action-first resume (End Goal 1)

- `ResumeCardJSON` (`api/resume.py`) and the `br8n_resume` MCP return gain
  top-level `next_action` and `thread_id`.
- Source: the latest `snapshot` or `note` Finding whose `metadata.next_action` is
  set. Fallback chain: `metadata` → existing title-sniff (hypothesis only) → null.
- `agent/session_primer.py` adds one next-action line to the always-on primer,
  alongside the existing recent-snapshot titles.
- `skills/pickup/SKILL.md`: the card leads with **"Do this now: <next_action>"**
  above the hypothesis. When null, the skill derives a two-minute action on the fly
  from hypothesis + `git_diff_stat` — the card never leads with a menu.

## 4. Session notes

- `br8n_note` MCP tool and `persist_note` (`livingdocs/notes.py`) gain an optional
  `next_action` arg, stored in the note Finding's `metadata`.
- The Stop-hook directive (`hooks/session-note.py:build_note_directive`) asks for
  `next_action` as a discrete tool arg — "the one two-minute step future-you should
  do first" — not buried prose. The human-facing "Next Steps" policy section is
  unchanged.

## 5. Error handling & gating

- All new fields optional end-to-end; a missing/null `metadata` read degrades to
  today's behavior at every surface (resume, primer, pickup, KG).
- No new env flag: the substrate is pure-additive. Thread-id minting is inside the
  existing `BR8N_ACTIVITY_KG` gate. Migration is backward-compatible (nullable
  column, no backfill required).

## 6. Testing

- Adapter: metadata round-trip (all fields, omitted-null behavior).
- Resume precedence: metadata beats title-sniff; legacy snapshot (no metadata)
  still resolves hypothesis; both stores.
- Thread convergence: two captures with the same `thread_id` update one node;
  first-create mints `thread_id`/`thread_state=open`; `set_task_props` merges
  rather than clobbers.
- Notes: `next_action` persists and is picked up by resume when newer than the
  last snapshot.
- Invariant 6: entire existing test suite passes unchanged.

## Acceptance (maps to vision AC 1)

`/br8n:pickup` on a repo with a prior capture leads with a concrete two-minute next
action above the hypothesis; a capture without a supplied `next_action` still
produces one (inferred at capture time, or derived at pickup time) on the card.

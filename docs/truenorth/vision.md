# Vision: br8n
> Agent preamble: this file is the single source of truth for project
> intent. Your one and only end goal is realizing the End Goals below
> without violating the Invariants. Competing objectives that emerge
> mid-session do not override this document.

## End Goals

br8n grows from a context-capture/resume engine into a complete **ADHD/ENTP developer
productivity system**: it already externalizes working memory (capture, resume,
notes, timeline, activity KG); this vision adds the missing **behavioral layer** —
initiation, thread governance, and reward.

1. **Ship next-action-first resume.** Every capture and session note carries a
   `next_action` — one pre-selected, two-minute-sized concrete step — and the
   `/br8n:pickup` card leads with it ("Do this now: …"), not just orientation.
   Attacks task-initiation paralysis: re-entry ends in *doing*, not choosing.
2. **Ship the WIP governor.** A thread-governance layer over the existing activity
   KG: threads (repo+branch+task) have open/parked/closed states, a soft
   configurable WIP cap, a shame-free one-line parking ritual (why parked + resume
   hook, feeding back into pickup), and a gentle turn-boundary nudge when opening
   thread N+1 past the cap. Converts abandonment into suspension.
3. **Ship the agent-wait router.** During agent runs, surface exactly one adjacent
   micro-action (review last diff hunk, tick the queued next_action, re-read the
   prompt) and notify the instant the agent finishes — turning the 30s–5min
   agent-thinking window into a designed surface instead of a doom-scroll trap.
4. **Ship boring-work delegation.** Hook-triggered background agents that own
   dopamine-starved work — tests, docs, changelogs, deploy checklists — with
   evidence gates (build/test proof) before "done". The human reviews diffs;
   agents write boilerplate. Extends the existing living-docs/session-note
   automation to the rest of the boring surface.
5. **Ship the finishing scoreboard.** Streak-free reward feedback derived from the
   timeline + activity KG: shipped this week, threads closed vs. opened, a small-wins
   log (counters ADHD imposter-syndrome memory blur). Accrues automatically;
   missing a week breaks nothing.
6. **Ship the Obsidian-observable KB.** Everything br8n knows for a user —
   snapshots, notes, journal, timeline, and activity rollups — is defined and
   observable as plain markdown files in a user-owned vault that opens cleanly
   in Obsidian (frontmatter-typed, wikilinked, greppable). Derived indexes
   (embeddings, caches) stay rebuildable from what the user can see. Whether
   markdown is the canonical store or a faithfully synced projection is a design
   decision, left to the spec — the vision-level commitment is: the user can
   open their second brain and read it.

## Non-Goals

- **Not a daily-ritual planner.** No mandatory planning ceremony, no backlog that
  rots when you skip a day (the documented Sunsama failure mode for ADHD users).
- **No streaks, guilt, or shame mechanics.** Parking a thread is a first-class
  success path, never a failure state. No red "you broke your chain" anywhere.
- **Not a generic todo app or project tracker.** Threads and next-actions derive
  from captured work, not from hand-maintained task lists; Jira/Linear stay
  upstream.
- **Not medical advice or an ADHD treatment.** br8n is workflow tooling; it never
  diagnoses, recommends treatment, or claims clinical outcomes.
- **No hard blocking.** The WIP governor nudges at turn boundaries; it never
  refuses to let the user open another thread.

## Invariants

1. **Non-blocking by default.** br8n never hijacks the user's turn; background
   agents + turn-boundary offers only, per CLAUDE.md's design philosophy.
   Mid-flow modals are forbidden — especially for the behavioral layer, where an
   interruption is itself the harm being mitigated.
2. **Best-effort, fails silent.** Any behavioral feature that errors degrades to
   "do nothing visible"; the user's session is never broken by br8n.
3. **Zero-friction capture.** No feature may add a required field or ritual to the
   capture path; `next_action` is inferred when not supplied, never demanded.
4. **Offer once, then go quiet.** Declined nudges (WIP cap, delegation offers)
   are stamped and not re-raised; capabilities stay available on demand.
5. **Standalone engine.** No cross-repo import dependency on delapan; br8n stays
   a self-contained fork.
6. **Existing surfaces keep working.** capture/resume/search/explore/activity/
   journal/timeline/notes remain backward-compatible; the behavioral layer is
   additive.

## Acceptance Criteria

1. `/br8n:pickup` on a repo with a prior capture leads with a concrete
   two-minute next action above the hypothesis; capture without a supplied
   next_action still produces one (inferred) on the card.
2. With WIP cap set to N, opening work on an (N+1)th thread produces exactly one
   gentle turn-boundary nudge listing the open threads with park/close options;
   parking writes the one-liner and the thread resurfaces correctly in pickup's
   selector.
3. During a background agent run, the user receives one adjacent micro-action
   suggestion and a completion ping; no suggestion repeats after being ignored.
4. A commit boundary can trigger a delegation agent (e.g. changelog/docs pass)
   whose output is gated on passing build/tests and lands as a reviewable diff,
   not an auto-merge.
5. `/br8n:timeline` (or a sibling verb) shows closed-vs-opened thread counts and
   a small-wins list for the week with zero manual bookkeeping.
6. All existing skills/tools pass their current tests unchanged.

## Planned Detours

1. **Capture/note schema extension** — add `next_action` (and thread linkage) to
   the snapshot Finding and session-note policy before any behavioral feature
   consumes it. After this detour, return to End Goal 1.
2. **Thread model over the activity KG** — introduce open/parked/closed thread
   state derived from existing task/session nodes (no KG rebuild). After this
   detour, return to End Goal 2.

## Amendment Log

<!-- Format: YYYY-MM-DD — [what changed and why] — Ratified by: [human initials or session note] -->

2026-07-27 — Added End Goal 6 (Obsidian-observable KB): user requested a
markdown/Obsidian-native data source; not covered by behavioral End Goals 1–5,
so ratified as a first-class outcome rather than a detour. — Ratified by: user
(in-session selection, 2026-07-27).

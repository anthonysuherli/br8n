# Design: multi-session capture (sweep all live Claude Code sessions)

**Date:** 2026-07-23
**Status:** approved (investigate-then-propose path; leans A / i+fallback / live-only)
**Vision goals served:** End Goal 2 (WIP governor — gives the thread-governance layer a
true cross-session view; this is groundwork). Respects Invariants 1 (non-blocking),
2 (best-effort/fails-silent), 3 (zero-friction), 6 (existing capture unchanged).

## Problem

`/br8n:capture` snapshots only the session it runs in. A developer with N concurrent
Claude Code sessions (7 live on the author's machine right now) has to switch into each
to checkpoint it. Goal: one capture also snapshots every *other* live session's
repo+branch, so re-entry from any of them replays correctly.

## Two facts that shape the design

1. **Capture is assembled client-side; the backend is a pure sink.** The capture skill
   shells out to `git rev-parse/branch/diff` and *Claude infers the load-bearing
   `hypothesis`/`next_action` from the live conversation*. `br8n_capture`
   (`interfaces/mcp/server.py:49`) wraps those args into a `WorkspaceSnapshot` and writes
   one Finding to the one KB named by `project`/`kb`; it never inspects the environment.
2. **Other live sessions are discoverable on disk.** `~/.claude/sessions/<PID>.json` lists
   every running CC process — `{pid, sessionId, cwd, startedAt, version}`, PID-verifiable
   with `os.kill(pid,0)`. The current session identifies itself via
   `$CLAUDE_CODE_SESSION_ID`. Each session's transcript is
   `~/.claude/projects/<enc-cwd>/<sessionId>.jsonl` (locate by globbing the sessionId —
   the dir-name encoding is lossy, never decode it).

Consequence: the sweep needs **no backend changes** — the tool already accepts everything
per-session — and enumeration must be **client-side** (the files live on the user's
machine; the cloud-tier backend can't see `~/.claude`).

## Design

**Layer:** pure skill enhancement + one small client-side helper script. No Python engine
changes.

**Flow of an enhanced `/br8n:capture`:**

1. **Current session — inline, rich (unchanged).** Capture as today: git state +
   conversation-derived `hypothesis`/`next_action`, `trigger="manual"`. Emits
   `captured ✓` immediately. This preserves the highest-quality wedge for the session the
   user is actually in.
2. **Other live sessions — background sweep (new).** Run the helper
   `skills/capture/live_sessions.py`, which prints one TSV row per *distinct
   (repo, branch)* among the other live sessions:
   `sessionId <TAB> cwd <TAB> branch <TAB> transcript_path`. It excludes
   `$CLAUDE_CODE_SESSION_ID`, skips dead/stale registry entries (`os.kill` check), and
   **dedupes by (cwd, branch) keeping the most-recently-started session** (a br8n KB is
   repo+branch, so collapsing avoids near-duplicate snapshots when several sessions share
   a branch). Honors an opt-out env gate `BR8N_CAPTURE_SWEEP=0` (default on) by printing
   nothing.
3. If the helper lists ≥1 row, the skill fires **one background agent**
   (`Agent(run_in_background=true)`) — non-blocking; the current turn returns right away.
   The agent processes each row: gathers `git -C <cwd> diff --stat`, reads the last
   ~40–60 lines of `transcript_path` and **distills a one-line `hypothesis` +
   two-minute `next_action`** for that workstream (option i). On unreadable/empty
   transcript it falls back to a **structural** hypothesis from the diff stat, then to the
   branch name (option ii) — never blocks. It then calls
   `br8n_capture(project=basename(cwd), kb=branch, trigger="idle", captured_at=<now>,
   branch, git_diff_stat, hypothesis, next_action, project_path=cwd)`. `trigger="idle"`
   marks these as passive sweep captures, distinct from the user's `manual` one.
4. Per-session errors are swallowed (best-effort). When the agent finishes it emits one
   terse line: `swept N: repo▶branch, …`.

## Decisions (locked)

- **Trigger shape A:** the sweep is automatic on every `/br8n:capture` (opt out with
  `BR8N_CAPTURE_SWEEP=0`), matching "capture all at once."
- **Hypothesis source i+fallback:** LLM-distill from transcript tail (the background agent
  *is* the LLM — no separate call), degrading to structural, then branch name.
- **Scope live-only:** live sessions from the registry (PID-checked). Recently-ended
  (mtime-based) sessions are out — cleaner "ongoing" semantics.
- **Granularity = repo+branch, deduped:** one sweep snapshot per distinct (repo, branch),
  representative = most-recently-started session. Rationale: matches the KB key.

## Known / accepted behavior

- Two sessions doing different work on the *same* repo+branch collapse to one snapshot
  (the newer one). br8n has no finer KB than repo+branch, so this is the coherent grain.
- Repeated `/br8n:capture` re-sweeps and appends more snapshots; acceptable — capture is
  user-invoked and append-only (snapshots are point-in-time).
- The sweep agent reads other sessions' transcript tails (same user, same machine, same
  br8n store) purely to distill intent; only the one-line hypothesis/next_action is
  persisted, never verbatim conversation.

## Non-goals

- No new backend endpoint or MCP tool; no server-side session enumeration.
- No change to current-session capture quality or its silent-confirm contract.
- Not a scheduler — the sweep runs only when the user invokes capture.

## Verification

- `python3 skills/capture/live_sessions.py` on the author's machine lists the other live
  sessions' distinct repo+branches (not the current session), respects
  `BR8N_CAPTURE_SWEEP=0`, and exits cleanly when the registry is absent.
- A manual `/br8n:capture` writes the current-session snapshot immediately and, in the
  background, one `idle` snapshot per other distinct repo+branch (verify via
  `br8n_projects` / the target KBs).

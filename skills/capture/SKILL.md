---
name: capture
description: Save the current workspace context to the br8n session KB as a snapshot — branch, open/cursor files, git diff stat, and a one-line hypothesis of what you're doing — AND sweep every other live Claude Code session, snapshotting each one's repo+branch in the background. Use when the user is about to switch away, says "save my context", "remember where I am", or wants to checkpoint intent before an interruption.
---

# br8n — Capture

Persist a workspace snapshot so a later `/br8n:pickup` can replay it. The snapshot
becomes a Finding in the repo+branch KB. The load-bearing field is the
**`hypothesis`** — a one-line statement of current intent; it makes recovery 3–5×
faster, so always try to fill it.

Capture is **multi-session**: it snapshots the current session inline (Steps 0–3),
then in the **background** sweeps every *other* live Claude Code session and snapshots
each distinct repo+branch too (Step 4). So one `/br8n:capture` checkpoints all your
concurrent work at once, not just the window you're in.

## Step 0 — Resolve target

`project` = git repo basename, `kb` = git branch
(see [`../_shared/preamble-first.md`](../_shared/preamble-first.md)). No prior tap
needed — capture is a pure write.

## Step 1 — Gather workspace state

Collect from the environment (skip any that fail — all are optional except trigger
and captured_at):

```bash
git rev-parse --show-toplevel        # project_path / repo
git branch --show-current            # branch
git diff --stat                      # git_diff_stat
```

- `captured_at` — current ISO-8601 timestamp.
- `trigger` — `"manual"` for a user-invoked capture (other values: `blur`,
  `checkout`, `idle`, `note`).
- `cursor_file` / `cursor_line` / `open_files` — from the user if they mention what
  they're editing; otherwise omit.
- `terminal_tail` — last relevant command output if the user pastes it.

## Step 2 — Write the hypothesis, then capture

If the user gave intent ("I'm tracking down the auth race"), use it verbatim as
`hypothesis`. If not, **infer one** from the diff stat + recent conversation and
**confirm it in one line** before saving — a wrong hypothesis is worse than none.

Also fill **`next_action`** — the single ~two-minute step future-you should do
first (e.g. "rerun the failing test_auth.py", "finish the TODO in adapter.py:49").
If the user stated one, use it verbatim; otherwise **infer it** from the diff and
conversation. Concrete and immediately startable — a command to run or a file:line
to open, never a project-sized goal. Do not ask the user for it.

Then call:

```
mcp__plugin_br8n_br8n__br8n_capture(
  project, kb, trigger="manual", captured_at=<iso>,
  branch, git_diff_stat, cursor_file?, cursor_line?, open_files?,
  hypothesis=<the one-liner>, next_action=<the two-minute step>, project_path=<repo path>
)
```

## Step 3 — Silent confirm

Capture is **silent by design** — the statusline carries the hint, not the chat. Do
**not** print the `finding_id`, dump the snapshot, or echo the hypothesis back.
Emit a single terse line and nothing more:

```
captured ✓
```

The br8n statusline reflects the save on the next render: line 1 shows
`🧠 {project} ▶ {branch} "{hypothesis}"` and line 2 shows `✓ just captured
"{hypothesis}"` (decaying to `✓ fresh · captured Xm ago` after ~2 min). That cue
is the user-facing confirmation of exactly what a future `/br8n:pickup` will replay.

## Step 4 — Sweep other live sessions (background, non-blocking)

After the current session is captured, snapshot every *other* live Claude Code
session too — so this one command checkpoints all concurrent work. This runs in the
**background** and must never delay the `captured ✓` above.

**4a. Enumerate.** Run the helper that sits beside this skill (use this skill's own
base directory):

```bash
python3 "<this skill's base directory>/live_sessions.py"
```

It prints one TSV row per *distinct (repo, branch)* among the other live sessions —
`sessionId⇥repo_toplevel⇥branch⇥transcript_path` — already excluding the current
session, dead PIDs, and non-git / detached-HEAD cwds (a KB needs repo+branch). It
prints **nothing** when there's nothing to sweep, or when `BR8N_CAPTURE_SWEEP=0`.

- **No rows** → you're done. Capture was a normal single-session write; say nothing more.
- **≥1 row** → fire the sweep agent below and return immediately.

**4b. Fan out (one background agent).** Launch a single background agent, embedding the
helper's rows verbatim in its prompt. Do **not** await it — the turn ends after firing:

```
Agent(
  subagent_type="general-purpose",
  run_in_background=true,
  description="br8n multi-session capture sweep",
  prompt=<the prompt below, with the TSV rows pasted in>,
)
```

Sweep-agent prompt:

> You are br8n's background capture sweep. Snapshot each *other* live Claude Code
> session into its own repo+branch KB. Best-effort: if any one session errors, skip it
> and continue — never fail the batch. Here is the work-list, one tab-separated row per
> session — `sessionId⇥repo_toplevel⇥branch⇥transcript_path`:
>
> ```
> <PASTE THE HELPER'S STDOUT ROWS HERE>
> ```
>
> For each row, with `top`=repo_toplevel and `branch`=branch:
> 1. `project` = basename(top), `kb` = branch, `project_path` = top.
> 2. `git_diff_stat` = output of `git -C "<top>" diff --stat` (may be empty).
> 3. Distill the wedge from that session's own context: read the **last ~60 lines** of
>    `transcript_path` (if present and readable) and, from the most recent user +
>    assistant turns, write a **one-line `hypothesis`** (what that session is currently
>    working on — intent, not a transcript quote) and a concrete **~two-minute
>    `next_action`** (a command to run or a file:line to open). Fallbacks, in order:
>    if the transcript is missing/empty, derive `hypothesis` from `git_diff_stat` (e.g.
>    "editing <top changed files>"); if that's empty too, use "on branch <branch>".
>    Never block on missing data. Persist only the distilled one-liners — never verbatim
>    conversation content.
> 4. Timestamp `captured_at` = current ISO-8601 UTC (e.g. `2026-07-23T18:04:00Z`).
> 5. Call
>    `mcp__plugin_br8n_br8n__br8n_capture(project=<basename(top)>, kb=<branch>,
>    trigger="idle", captured_at=<iso>, branch=<branch>, git_diff_stat=<stat>,
>    hypothesis=<one-liner>, next_action=<step>, project_path=<top>)`. `trigger="idle"`
>    marks these as passive sweep captures, distinct from the user's manual one.
> When done, return exactly one terse line: `swept N: <project>▶<branch>, …` (the
> repo+branches you captured). No preamble, no per-step narration.

The sweep is silent until the agent finishes; its one-line summary surfaces then as a
background completion. If the sweep agent errors wholesale, ignore it — the
current-session capture already succeeded (best-effort, fails silent).

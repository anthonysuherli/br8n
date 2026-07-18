---
name: capture
description: Save the current workspace context to the br8n session KB as a snapshot — branch, open/cursor files, git diff stat, and a one-line hypothesis of what you're doing. Use when the user is about to switch away, says "save my context", "remember where I am", or wants to checkpoint intent before an interruption.
---

# br8n — Capture

Persist a workspace snapshot so a later `/br8n:pickup` can replay it. The snapshot
becomes a Finding in the repo+branch KB. The load-bearing field is the
**`hypothesis`** — a one-line statement of current intent; it makes recovery 3–5×
faster, so always try to fill it.

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

---
name: pickup
description: Pick up where you left off — replay the resume card for the current repo+branch, or open a cross-repo selector over every repo you've captured to and resume any of them. Use when the user returns to work, asks "where was I", "what was I doing", or wants to jump back into a different repo than the one they're in.
---

# br8n — Pickup

Pick up where you left off. Two modes from one verb:

- **here** — replay the 30-second resume card for the current repo+branch (the
  default fast path; context recovery in seconds rather than by hand).
- **selector** — list every repo+branch you've captured to, most-recent first, and
  resume any one of them. The cross-repo way back in.

## Step 0 — Branch on the argument

Resolve `project` = git repo basename, `kb` = git branch (see
[`../_shared/preamble-first.md`](../_shared/preamble-first.md)).

- **Bare `/br8n:pickup`** inside a git repo → **here mode** (Step 1).
- **`/br8n:pickup list`** or **`/br8n:pickup pick`**, or **not in a git repo** →
  **selector mode** (Step 2).
- **`/br8n:pickup <name>`** → call `mcp__plugin_br8n_br8n__br8n_projects()`,
  substring-match `<name>` against project/kb. One match → resume it (Step 1 with that
  target). Multiple → show the filtered selector (Step 2).

## Step 1 — Here mode (resume card)

Call `mcp__plugin_br8n_br8n__br8n_resume(project, kb, query)` with `query` =
whatever the user is reorienting toward (omit for the synopsis-only spine).

Lead with the **`next_action`** returned by `br8n_resume` — the card must open
with a single concrete step, not a menu:

> **Do this now:** `<next_action>`
> **You were:** `<latest hypothesis>`

If `next_action` is null (legacy captures), derive a ~two-minute step yourself
from the hypothesis + `git_diff_stat` (e.g. "open `<cursor_file>:<line>` and
re-read the failing branch") and lead with that instead. Never open with a list
of options — one pre-selected action first, supporting context after.

Then the supporting context, tersely:
- **Branch / files** — `cursor_file:cursor_line`, open files from the snapshot.
- **In flight** — `git_diff_stat`, terminal tail if present.
- **Synopsis** — the `<N>` standing topics, titles only.

**Route on coverage:**
- **rich / sparse** → you have a card; lead with the next_action (see Step 1).
- **gap** → no captured context for *this* branch. Don't dead-end — fall through to
  **selector mode** so the user can pick a repo+branch they *have* captured (or, if the
  selector is also empty, offer `/br8n:capture` to start tracking or
  `/br8n:explore <topic>` to seed from the web).

## Step 2 — Selector mode (cross-repo)

Call `mcp__plugin_br8n_br8n__br8n_projects()`. It returns
`{projects: [{project, kbs: [{kb, last_activity, snapshot_count}]}]}`. Flatten to
repo+branch rows, sort most-recent `last_activity` first, and mark the current
checkout:

```
Where do you want to pick up?
  1. br8n · dev          2h ago    12 snapshots   ← you are here
  2. br8n · main         1d ago     4 snapshots
  3. delapan · main     3d ago     9 snapshots
```

The user picks a row (number or name) → resume it via **Step 1** with that
`project`/`kb`. If the list is empty, there are no captures yet — offer
`/br8n:capture` (start tracking here) or `/br8n:explore <topic>` (seed the KB).

Read-only op — no loop-back needed unless the user states new intent worth saving, in
which case offer `/br8n:capture`.

"""br8n Stop hook — session-end note writer.

    python hooks/session-note.py

Reads the hook input JSON from stdin (provided by the Claude Code harness) at the
END of a conversation, derives the repo's project/kb from cwd, and emits a
``{"decision": "block", "reason": ...}`` directive telling Claude to summarize THIS
session into a note (per the KB's note policy) and persist it via
``mcp__br8n__br8n_note``. The actual summary + tool calls are Claude's work;
this hook only emits the directive text.

Why ``decision: block`` (not ``additionalContext``)
---------------------------------------------------
Stop hooks fire *after* the response is complete, so the harness silently drops
``additionalContext`` for the ``Stop`` event — the only way to feed text back to
the model and earn one more turn is the continuation contract
``{"decision": "block", "reason": <text>}``. The model sees ``reason`` and acts on
it. To avoid an infinite loop, we **guard on ``stop_hook_active``**: the note turn
itself ends and re-fires this hook, but that second fire carries
``stop_hook_active=True`` and we exit silently, letting the session end.

Design goals
------------
* **Best-effort, one extra turn.** The directive tells Claude to skip silently if
  nothing substantive happened, so a trivial session still ends immediately.
* **Loop-safe.** ``stop_hook_active`` is honored so we block at most once.
* **Fail-safe.** Any unexpected error (malformed stdin, not a git repo, gate off)
  causes a silent exit — this hook must never crash Claude Code.
* **Importable for testing.** Logic lives in ``build_note_directive``;
  ``main()`` is thin glue. ``repo_identity``/``derive_project_kb`` are reused from
  the SessionStart hook so cwd→project/kb derivation stays identical.
"""

from __future__ import annotations

import json
import os
import sys

# Reuse the exact cwd→project/kb derivation from the SessionStart hook so both
# hooks resolve the same target for the same repo.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "br8n_first_run_init",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "first-run-init.py"),
)
_fri = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_fri)
repo_identity = _fri.repo_identity
derive_project_kb = _fri.derive_project_kb


# ---------------------------------------------------------------------------
# Public helpers (importable for tests)
# ---------------------------------------------------------------------------


def build_note_directive(project: str, kb: str) -> str:
    """Return the additionalContext directive that drives end-of-session note-writing.

    Best-effort by construction: it tells Claude to skip silently if nothing
    substantive happened, otherwise to summarize THIS conversation per the KB's note
    policy and persist it via ``br8n_note``. See skills/_shared/session-note.md."""
    return (
        f"br8n session-end note for project '{project}' (kb='{kb}'). "
        "The conversation is ending — capture a durable session note (non-blocking, "
        "best-effort; see skills/_shared/session-note.md).\n\n"
        "Instructions:\n"
        "1. If nothing substantive happened this session (no decisions, no code "
        "changes, trivial Q&A), skip silently — do nothing.\n"
        "2. Resolve the target: project = git repo basename, kb = git branch, "
        "project_path = repo root (`git rev-parse --show-toplevel`). "
        f"For this session: project='{project}', kb='{kb}'.\n"
        "3. Fetch the KB's note policy: call "
        "mcp__br8n__br8n_notes_policy_get(project, kb, project_path) for the note "
        "template (sections) and steer. If unavailable, use a sensible default "
        "(Decisions / Changes / Open questions).\n"
        "4. Summarize THIS conversation into those sections, honoring the steer — "
        "concise, durable facts only (decisions made, what changed, open threads), "
        "not a transcript.\n"
        "5. Persist it: call mcp__br8n__br8n_note(project, kb, project_path, "
        "content, session_id, title, next_action=<one line: the single ~two-minute "
        "step future-you should do first — concrete and immediately startable, e.g. "
        "'rerun the failing test_auth.py'>) where content is the rendered markdown, "
        "title is a one-line summary, and session_id is this session's id. Omit "
        "next_action only when the session leaves genuinely nothing to pick up.\n"
        "Keep it quick and best-effort: persist the note (or skip silently), then end "
        "your turn — do not start unrelated work."
    )


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Stop hook entry point.

    Reads the hook context JSON from stdin (provided by the Claude Code harness),
    derives the repo's project/kb from cwd, and prints
    ``{"decision": "block", "reason": "..."}`` so the harness feeds the note
    directive back to the model for one more turn. Exits silently on a disabled
    gate, a re-entrant stop (``stop_hook_active``), a non-git cwd, or any error.
    """
    if os.getenv("BR8N_LIVING_DOCS", "1") == "0":
        return
    try:
        raw = sys.stdin.read()
        ctx = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 — malformed input → silent exit
        return

    # Loop guard: after we block once, Claude writes the note and its turn ends,
    # re-firing this Stop hook with stop_hook_active=True. Blocking again would
    # loop forever — exit silently and let the session end.
    if ctx.get("stop_hook_active"):
        return

    cwd: str = (
        ctx.get("cwd")
        or (ctx.get("session") or {}).get("cwd")
        or os.getcwd()
    )

    # Not a git repo → not a br8n target.
    if repo_identity(cwd) is None:
        return

    try:
        project, kb = derive_project_kb(cwd)
        # Stop hooks drop `additionalContext`; decision=block + reason is the
        # documented way to feed text back to the model and earn one more turn.
        print(json.dumps({"decision": "block", "reason": build_note_directive(project, kb)}))
    except Exception:  # noqa: BLE001 — any failure → stay silent
        return


if __name__ == "__main__":
    main()

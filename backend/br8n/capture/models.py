"""Workspace snapshot — the cognitive state captured at interruption."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Trigger = Literal["blur", "git_checkout", "idle", "manual"]


@dataclass
class WorkspaceSnapshot:
    """State of the developer's workspace at the moment of interruption.

    Fields map 1:1 to what the VS Code extension can collect synchronously
    (blur → capture is sub-second; nothing here blocks the editor).
    """

    # Identity
    project_path: str   # absolute workspace root — becomes the KB project name
    trigger: Trigger    # what caused capture
    captured_at: str    # ISO 8601 UTC, e.g. "2026-05-29T10:00:00Z"

    # Git context
    branch: str | None = None
    git_diff_stat: str | None = None  # `git diff --stat` output

    # Editor state
    open_files: list[str] = field(default_factory=list)  # relative paths, recency-ordered
    cursor_file: str | None = None
    cursor_line: int | None = None

    # Terminal (best-effort — VS Code API doesn't expose output directly)
    terminal_tail: str | None = None

    # The wedge: the developer's one-line current hypothesis.
    # None means the user skipped the prompt (still valuable as a state snapshot).
    hypothesis: str | None = None

    # One concrete ~two-minute step future-you should do first. Optional; the
    # capture skill infers it when the user doesn't state one.
    next_action: str | None = None

    # Stable thread identity (minted by the activity KG on first task-node
    # creation; carried on later captures to converge writes on one thread).
    thread_id: str | None = None

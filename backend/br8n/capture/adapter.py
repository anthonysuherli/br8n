"""Convert a WorkspaceSnapshot into a Finding payload."""

from __future__ import annotations

from br8n.capture.models import WorkspaceSnapshot

_MAX_OPEN_FILES = 10
_MAX_DIFF_CHARS = 2000
_MAX_TERMINAL_CHARS = 1000


def snapshot_to_finding(snap: WorkspaceSnapshot) -> dict:
    """Return a dict ready to insert into the `findings` table.

    The hypothesis (when present) leads the content so it anchors the embedding
    and dominates similarity search — it is the wedge nobody owns.
    """
    lines: list[str] = []

    if snap.hypothesis:
        lines += [f"**Hypothesis**: {snap.hypothesis}", ""]

    if snap.branch:
        lines.append(f"**Branch**: `{snap.branch}`")

    if snap.cursor_file:
        loc = snap.cursor_file
        if snap.cursor_line:
            loc += f":{snap.cursor_line}"
        lines.append(f"**Cursor**: `{loc}`")

    if snap.open_files:
        shown = snap.open_files[:_MAX_OPEN_FILES]
        more = len(snap.open_files) - len(shown)
        suffix = f" (+{more} more)" if more else ""
        lines.append(f"**Open files**: {', '.join(f'`{f}`' for f in shown)}{suffix}")

    if snap.git_diff_stat:
        stat = snap.git_diff_stat.strip()[:_MAX_DIFF_CHARS]
        lines += ["", f"**Git diff stat**:\n```\n{stat}\n```"]

    if snap.terminal_tail:
        tail = snap.terminal_tail.strip()[:_MAX_TERMINAL_CHARS]
        lines += ["", f"**Terminal tail**:\n```\n{tail}\n```"]

    lines += ["", f"*Captured {snap.captured_at[:19].replace('T', ' ')} UTC — trigger: {snap.trigger}*"]

    title = _derive_title(snap)
    metadata = {
        k: v
        for k, v in (
            (k, v.strip() if isinstance(v, str) else v)
            for k, v in {
                "hypothesis": snap.hypothesis,
                "next_action": snap.next_action,
                "thread_id": snap.thread_id,
            }.items()
        )
        if v
    }
    return {
        "title": title[:120],
        "content": "\n".join(lines),
        "category": "snapshot",
        "tags": ["snapshot", snap.trigger],
        "provenance": [{"source": "br8n-vscode", "trigger": snap.trigger, "path": snap.project_path}],
        "metadata": metadata or None,
    }


def _derive_title(snap: WorkspaceSnapshot) -> str:
    if snap.hypothesis:
        return snap.hypothesis
    if snap.cursor_file:
        fname = snap.cursor_file.split("/")[-1]
        return f"Working on {fname}"
    if snap.branch:
        return f"Snapshot on {snap.branch}"
    return f"Snapshot {snap.captured_at[:16].replace('T', ' ')}"

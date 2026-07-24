#!/usr/bin/env python3
"""List OTHER live Claude Code sessions for br8n's multi-session capture sweep.

Prints one TSV row per *distinct (repo, branch)* among the currently-running CC
sessions, excluding the session this runs in:

    sessionId <TAB> repo_toplevel <TAB> branch <TAB> transcript_path

`repo_toplevel` is the git work-tree root (`git rev-parse --show-toplevel`), so the
consumer derives project=basename(repo_toplevel), project_path=repo_toplevel,
kb=branch. Sessions whose cwd is not a git repo on a named branch (non-git, or
detached HEAD) are skipped — a br8n KB is keyed by repo+branch and cannot be formed
without both.

Best-effort by contract: unreadable registry entries, dead PIDs, non-git cwds, and
missing transcripts are skipped silently. Exits 0 with no output when there is
nothing to sweep (no other live sessions, no registry dir, or the sweep is disabled
via BR8N_CAPTURE_SWEEP=0).

Discovery basis (see docs/truenorth/specs/2026-07-23-multi-session-capture-design.md):
- <config>/sessions/<PID>.json  = one file per running CC process
  ({pid, sessionId, cwd, startedAt, ...}); PID-checked for liveness.
- $CLAUDE_CODE_SESSION_ID        = the current session (excluded).
- <config>/projects/<enc>/<sessionId>.jsonl = transcript; located by globbing the
  sessionId (the dir-name encoding is lossy — never decode it).
- <config> = $CLAUDE_CONFIG_DIR or ~/.claude.
"""
import glob
import json
import os
import subprocess
import sys


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _git(cwd: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def main() -> int:
    if os.environ.get("BR8N_CAPTURE_SWEEP", "1") == "0":
        return 0

    self_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    reg_dir = os.path.join(config, "sessions")
    proj_dir = os.path.join(config, "projects")
    if not os.path.isdir(reg_dir):
        return 0

    # Collapse to one representative per (repo_toplevel, branch): the
    # most-recently-started live session. A br8n KB is repo+branch, so several
    # sessions on the same branch (even from different subdirs) share one KB —
    # dedupe avoids near-duplicate snapshots.
    best: dict[tuple[str, str], dict] = {}
    for f in glob.glob(os.path.join(reg_dir, "*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        pid, sid, cwd = d.get("pid"), d.get("sessionId"), d.get("cwd")
        if not (pid and sid and cwd):
            continue
        if sid == self_id or not _alive(pid) or not os.path.isdir(cwd):
            continue
        top = _git(cwd, "rev-parse", "--show-toplevel")
        branch = _git(cwd, "branch", "--show-current")
        if not top or not branch:  # non-git or detached HEAD -> no KB, skip
            continue
        key = (top, branch)
        started = d.get("startedAt") or 0
        if key not in best or started > best[key]["started"]:
            best[key] = {"sid": sid, "top": top, "branch": branch, "started": started}

    for v in best.values():
        hits = glob.glob(os.path.join(proj_dir, "*", f"{v['sid']}.jsonl"))
        transcript = hits[0] if hits else ""
        print("\t".join([v["sid"], v["top"], v["branch"], transcript]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

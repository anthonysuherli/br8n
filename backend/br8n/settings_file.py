"""Machine-level user settings — ``~/.br8n/settings.json``.

Three config layers now exist and they are deliberately distinct:

* ``Settings`` (env/.env) — secrets and infra, set by whoever deploys.
* ``AppConfig`` (config.yaml + ``B2_*``) — tunable knobs, set by whoever ships.
* this file — state the **user** changes at runtime from inside Claude Code.

The MCP server is long-lived and started with a fixed environment, so a user
switch has to land somewhere it can be re-read. Reads are cached by mtime
(nanosecond precision) so a hot path can call ``load_settings()`` freely and
still observe a write made by another process.

Location follows ``journal_dir()``: the parent of ``BR8N_DB_PATH`` when set,
else ``~/.br8n``.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_cache: tuple[int, dict] | None = None


def settings_path() -> Path:
    env = os.environ.get("BR8N_DB_PATH")
    root = Path(env).resolve().parent if env else Path.home() / ".br8n"
    root.mkdir(parents=True, exist_ok=True)
    return root / "settings.json"


def clear_cache() -> None:
    """Drop the mtime cache (tests, and after our own writes)."""
    global _cache
    _cache = None


def load_settings() -> dict:
    """The settings dict, or ``{}`` when absent/unreadable. Never raises."""
    global _cache
    try:
        path = settings_path()
        mtime = path.stat().st_mtime_ns
    except OSError:
        _cache = None
        return {}
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001 — a hand-broken settings file is not fatal
        logger.warning("settings.json unreadable; ignoring it", exc_info=True)
        data = {}
    _cache = (mtime, data)
    return data


def save_setting(key: str, value) -> dict:
    """Merge one key (``None`` removes it) and write atomically. Returns the result."""
    path = settings_path()
    data = dict(load_settings())
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    clear_cache()
    return data

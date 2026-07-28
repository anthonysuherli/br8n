"""Store: the engine's persistence seam + backend selection.

`Store` is the protocol; `SupabaseStore` (cloud) and `VaultStore` (markdown-
canonical vault + SQLite index, free/local) are the two implementations.
`get_store()` is the factory the engine calls.

Backend selection (in order):
  1. ``BR8N_BACKEND`` env var — ``"local"`` or ``"cloud"`` (explicit override);
  2. otherwise ``"cloud"`` iff the Supabase creds are present, else ``"local"``.

Caching: the local SQLite store holds a long-lived connection, so it is cached
as a module-level singleton keyed by db_path (one connection reused across
calls; a different ``BR8N_DB_PATH`` — as tests use — gets its own store). The
cloud store is built fresh every call: it carries a per-request ``access_token``
(RLS scope), so caching it would be wrong across users. This mirrors today's
behaviour where ``user_client(token)`` is created per request.
"""

from __future__ import annotations

import os

from br8n.config import get_settings
from br8n.store.base import Store
from br8n.store.sqlite import SQLiteStore, _default_db_path
from br8n.store.supabase import SupabaseStore
from br8n.store.vault import VaultStore

__all__ = [
    "Store", "SupabaseStore", "SQLiteStore", "VaultStore", "get_store", "active_backend",
]

# Local stores cached by db_path so the SQLite connection is reused.
_local_stores: dict[str, VaultStore] = {}


def _has_cloud_creds() -> bool:
    """True iff the Supabase env vars needed for the cloud tier are present.

    Note: `get_settings()` is `@lru_cache`d, so creds are sniffed once per
    process. Changing Supabase env vars after the first read won't re-select the
    backend until the cache is cleared (tests do this). Fine in production where
    env is stable; set `BR8N_BACKEND` explicitly to avoid sniffing entirely.
    """
    s = get_settings()
    return bool(s.supabase_url and s.supabase_service_role_key)


def active_backend() -> str:
    """The selected backend name — ``"local"`` or ``"cloud"``.

    Single source of truth for selection, shared by `get_store` and the tenancy
    fork (local skips GoTrue login). ``BR8N_BACKEND`` overrides creds-sniffing.
    """
    backend = os.getenv("BR8N_BACKEND")
    if not backend:
        backend = "cloud" if _has_cloud_creds() else "local"
    return backend


def get_store(access_token: str | None = None, *, org_id: str | None = None) -> Store:
    """Return the active Store for this request.

    Local: a cached VaultStore (connection reused). Cloud: a fresh
    SupabaseStore bound to ``access_token`` for RLS-scoped finding ops and an
    optional ``org_id`` (sourced from the verified request JWT) that skips the
    store's internal MCP-path login.
    """
    if active_backend() == "local":
        path = _default_db_path()
        store = _local_stores.get(path)
        if store is None:
            store = VaultStore(path)
            _local_stores[path] = store
        return store
    return SupabaseStore(access_token, org_id=org_id)

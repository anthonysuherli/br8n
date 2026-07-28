"""Rebuild the derived index from the vault: ``python -m br8n.vault.reindex``.

The architectural proof that brain.db is disposable. Restores findings,
projects and KBs (each file's ``br8n_id`` survives). KG, synopsis and
exploration rows are derivatives that repopulate through normal use.
Semantic search needs an embedding key at reindex time; without one, text
indexing still restores and embeddings self-heal on later reads.

CRITICAL: reindex() is user-initiated only; ignore_caps=True bypasses the
mass-delete guard, which is exactly why this command must ONLY ever run when
a user explicitly invokes it. Do NOT wire reindex into any automatic path
(no boot hook, no fallback, no import side effects).
"""
from __future__ import annotations

import asyncio

from br8n.vault import reconcile as _reconcile


async def reindex(db_path: str | None = None) -> dict:
    """Rebuild the derived index from vault files.

    Loops reconcile(force=True, ignore_caps=True) until a pass adopts/updates
    nothing, then loops _re_embed_stale() until 0. Returns totals dict with
    keys: adopted, updated, re_embedded. Closes the store in a finally block.
    """
    from br8n.store.vault import VaultStore

    store = VaultStore(db_path)
    try:
        totals = {"adopted": 0, "updated": 0, "re_embedded": 0}
        while True:
            c = _reconcile.reconcile(store, force=True, ignore_caps=True)
            totals["adopted"] += c["adopted"]
            totals["updated"] += c["updated"]
            if c["adopted"] == 0 and c["updated"] == 0:
                break
        while True:
            n = await store._re_embed_stale()
            totals["re_embedded"] += n
            if n == 0:
                break
        return totals
    finally:
        store.close()


def main() -> None:
    """CLI entry point: run reindex and print counts."""
    totals = asyncio.run(reindex())
    print(
        f"reindex: adopted={totals['adopted']} updated={totals['updated']} "
        f"re_embedded={totals['re_embedded']}"
    )


if __name__ == "__main__":
    main()

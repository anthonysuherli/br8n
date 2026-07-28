"""One-time (idempotent) vault init: export pre-vault findings rows to files.

Runs automatically on VaultStore boot. Legacy .br8n/notes/ and ~/.br8n/journal/
markdown copies are left untouched — their content already lives in findings
rows (notes/journal were always dual-written), so exporting rows covers them.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def export_missing(store) -> int:
    exported = 0
    rows = store._conn.execute(
        "SELECT id FROM findings WHERE vault_path IS NULL;"
    ).fetchall()
    for r in rows:
        try:
            store._write_canonical(r["id"])
            exported += 1
        except Exception:  # noqa: BLE001 — one bad row never stops the export
            logger.warning("vault export failed for %s", r["id"], exc_info=True)
    if exported:
        logger.info("vault init: exported %d pre-vault findings", exported)
    return exported

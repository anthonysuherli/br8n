"""Test-wide defaults.

The suite runs against the local (SQLite) tier unless a test opts into cloud.
Pinning ``BR8N_BACKEND`` here means a fresh clone runs ``pytest`` successfully
without knowing that env var exists, and it stops a developer's real Supabase
credentials in ``.env`` from silently redirecting tests at the cloud tier.
"""

from __future__ import annotations

import os

os.environ.setdefault("BR8N_BACKEND", "local")

# Local Embeddings (keyless free tier) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use truenorth:subagent-driven-development (recommended) or truenorth:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the local tier semantic search with no API key, by adding a fastembed-backed local embedding provider behind the existing embeddings façade, plus a single active embedding space that rebuilds itself when the provider changes.

**Architecture:** `br8n/clients/embeddings.py` stays the only seam every caller touches; behind it, an `EmbedderIdentity` resolves to remote / local / none by precedence (settings file → config/env → auto-detect). A new `embedding_space` row records the active `(provider, model, dim)`; on mismatch the `vec0` tables are recreated at the new width and every row is flagged `needs_embed=1` for the lazy drain that already ships. Two MCP tools plus a skill let the user switch providers from inside Claude Code without restarting the MCP server.

**Vision goals served:** End Goal 6 — derived indexes (explicitly including embeddings) stay rebuildable from what the user can see; today `reindex` on a keyless machine restores text but no vectors.

**Tech Stack:** Python 3.11+, fastembed (optional extra, ONNX runtime, no torch), sqlite-vec, pydantic, pytest + pytest-asyncio, ruff.

**Spec:** `docs/truenorth/specs/2026-07-28-local-embeddings-design.md` — read it first.

## Global Constraints

- **Fail-silent, always.** No vault/embedding machinery may break a capture, a read, or store construction. Every new best-effort path logs and degrades.
- **Non-blocking.** fastembed is synchronous and CPU-bound: never call it on the event loop. Embeds go through `asyncio.to_thread`; the model warm-up runs on a daemon thread (no event loop required); `onnxruntime` intra-op threads are pinned to `local_threads` (default 1).
- **Zero-friction capture.** `embeddings_configured()` returns False for a local provider whose model is not yet resident, so a capture during warm-up stores `needs_embed=1` instead of stalling. It must never block on a load or a download.
- **Local tier only.** The local provider is eligible only when `active_backend() == "local"`. Cloud (Supabase/pgvector, 1536-wide) is untouched by every task in this plan.
- **Façade signatures are frozen.** `embed_text`, `embed_batch`, `embed_with_retry`, `embeddings_configured` keep their current names and signatures — no call-site changes anywhere in `br8n/`.
- **Import direction.** `br8n.store` imports `br8n.clients.embeddings` (via `store/vault.py`), so `embeddings.py` must NOT import `br8n.store` at module scope. Use function-local imports for `active_backend`.
- **Quality claims.** Never state or imply that `bge-small-en-v1.5` is at parity with `text-embedding-3-small` (the published MTEB averages measure different things). A present API key wins; local is the fallback.
- Exact values: model `BAAI/bge-small-en-v1.5`, dim `384`, extra name `local-embeddings`, dependency `fastembed>=0.8`, settings key `embedding_provider`, settings file `~/.br8n/settings.json`.
- Run everything from `backend/`: tests `.venv/bin/pytest`, lint `.venv/bin/ruff check br8n tests`. Baseline suite: **419 passed, 1 skipped, 0 failed**. Any other failure is yours.
- Commit after every task with the message given; do not push.

## File structure

| File | Responsibility |
|---|---|
| `backend/br8n/settings_file.py` (new) | Read/write `~/.br8n/settings.json`; mtime-cached so hot paths can call it freely |
| `backend/br8n/clients/embed_local.py` (new) | fastembed provider: lazy import, readiness, background warm-up, threaded embed |
| `backend/br8n/clients/embeddings.py` (mod) | Façade + `EmbedderIdentity` resolution and routing |
| `backend/br8n/config.py` (mod) | `EmbeddingConfig`: `provider`, `local_model`, `local_dim`, `local_threads` |
| `backend/br8n/store/sqlite.py` (mod) | `embedding_space` table, dim-parameterized vec DDL, mismatch rebuild, `kg_nodes.needs_embed` |
| `backend/br8n/store/vault.py` (mod) | `_re_embed_stale` drains KG nodes as well as findings |
| `backend/br8n/interfaces/mcp/server.py` (mod) | `br8n_embeddings_get` / `br8n_embeddings_set` |
| `skills/embeddings/SKILL.md` (new), `.claude-plugin/plugin.json` (mod) | `/br8n:embeddings` |
| `backend/br8n/api/main.py` (mod) | `--check` embeddings line |
| `backend/pyproject.toml`, `backend/.env.example`, `README.md`, `CLAUDE.md` (mod) | Extra + docs |

---

### Task 1: Settings file, config knobs, packaging extra

**Files:**
- Create: `backend/br8n/settings_file.py`
- Modify: `backend/br8n/config.py` (`EmbeddingConfig`, ~line 136)
- Modify: `backend/pyproject.toml` (add `[project.optional-dependencies]`)
- Test: `backend/tests/embeddings/__init__.py`, `backend/tests/embeddings/test_settings_file.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `settings_path() -> Path`, `load_settings() -> dict`, `save_setting(key: str, value) -> dict`, `clear_cache() -> None`. Config: `get_config().embedding.{provider, local_model, local_dim, local_threads}`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/embeddings/__init__.py` (empty file — matches the `tests/vault/` convention) and:

```python
# backend/tests/embeddings/test_settings_file.py
"""Machine-level settings.json: path resolution, round-trip, cache invalidation."""
import json

import pytest

from br8n import settings_file


@pytest.fixture(autouse=True)
def _tmp_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    settings_file.clear_cache()
    yield
    settings_file.clear_cache()


def test_path_sits_beside_the_db(tmp_path):
    assert settings_file.settings_path() == tmp_path / "settings.json"


def test_missing_file_is_empty_dict():
    assert settings_file.load_settings() == {}


def test_save_then_load_round_trip():
    settings_file.save_setting("embedding_provider", "local")
    assert settings_file.load_settings()["embedding_provider"] == "local"
    on_disk = json.loads(settings_file.settings_path().read_text())
    assert on_disk == {"embedding_provider": "local"}


def test_save_merges_and_none_removes():
    settings_file.save_setting("embedding_provider", "local")
    settings_file.save_setting("other", 1)
    assert settings_file.load_settings() == {"embedding_provider": "local", "other": 1}
    settings_file.save_setting("embedding_provider", None)
    assert settings_file.load_settings() == {"other": 1}


def test_corrupt_file_degrades_to_empty():
    settings_file.settings_path().write_text("{not json")
    assert settings_file.load_settings() == {}


def test_external_write_is_picked_up_without_restart():
    """The MCP server is long-lived: a write by another process must be seen."""
    assert settings_file.load_settings() == {}
    settings_file.settings_path().write_text('{"embedding_provider": "remote"}')
    assert settings_file.load_settings()["embedding_provider"] == "remote"


def test_atomic_write_leaves_no_tmp(tmp_path):
    settings_file.save_setting("embedding_provider", "none")
    assert not (tmp_path / "settings.json.tmp").exists()


def test_embedding_config_defaults():
    from br8n.config import EmbeddingConfig

    cfg = EmbeddingConfig()
    assert cfg.provider == "auto"
    assert cfg.local_model == "BAAI/bge-small-en-v1.5"
    assert cfg.local_dim == 384
    assert cfg.local_threads == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/embeddings/test_settings_file.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'br8n.settings_file'`

- [ ] **Step 3: Implement**

```python
# backend/br8n/settings_file.py
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
```

In `backend/br8n/config.py`, extend `EmbeddingConfig` (currently model/dim/input_char_cap/chunk_max_chars):

```python
class EmbeddingConfig(BaseModel):
    model: str = "text-embedding-3-small"
    dim: int = 1536
    input_char_cap: int = 8192
    chunk_max_chars: int = 1800

    # Provider selection: "auto" resolves remote-key → local(fastembed) → none.
    # Overridable per-machine from Claude Code (settings.json) or per-deploy
    # via B2_EMBEDDING__PROVIDER.
    provider: str = "auto"  # auto | remote | local | none
    local_model: str = "BAAI/bge-small-en-v1.5"
    local_dim: int = 384
    local_threads: int = 1  # onnxruntime intra_op_num_threads
```

In `backend/pyproject.toml`, after the `dependencies = [...]` block and before `[project.scripts]`:

```toml
[project.optional-dependencies]
# Keyless semantic search: ONNX embeddings, no torch. Installed on demand —
# `pip install 'br8n[local-embeddings]'`.
local-embeddings = ["fastembed>=0.8"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/embeddings/test_settings_file.py -v`
Expected: 8 PASS

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check br8n tests
git add br8n/settings_file.py br8n/config.py pyproject.toml tests/embeddings
git commit -m "feat(embeddings): machine-level settings file, provider config knobs, optional extra"
```

---

### Task 2: Identity resolution in the façade

**Files:**
- Modify: `backend/br8n/clients/embeddings.py`
- Test: `backend/tests/embeddings/test_identity.py`

**Interfaces:**
- Consumes: `br8n.settings_file.load_settings`, `get_config().embedding`, `get_settings()`.
- Produces: `EmbedderIdentity` (frozen dataclass: `provider: str`, `model: str`, `dim: int`, `source: str`) and `active_embedder() -> EmbedderIdentity`. `provider` ∈ `{"remote","local","none"}`; `source` ∈ `{"settings","config","auto"}`. Also `_local_eligible() -> bool`, which Task 3 replaces the body of.

This task resolves identity only — routing and the real local provider land in Task 3.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/embeddings/test_identity.py
"""Provider precedence: settings file > config/env > auto-detect, plus the tier guard."""
import pytest

from br8n import settings_file
from br8n.clients import embeddings


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_BACKEND", "local")
    settings_file.clear_cache()
    # get_settings()/get_config() are lru_cached — clear so env edits take effect
    from br8n.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    yield
    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()


def _no_keys(monkeypatch):
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)


def _keys(monkeypatch):
    monkeypatch.setattr(embeddings, "_creds_present", lambda: True)


def _local_ok(monkeypatch, ok=True):
    monkeypatch.setattr(embeddings, "_local_eligible", lambda: ok)


def test_auto_prefers_remote_when_key_present(monkeypatch):
    _keys(monkeypatch)
    _local_ok(monkeypatch)
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.dim, ident.source) == ("remote", 1536, "auto")


def test_auto_falls_back_to_local_when_keyless(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch)
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.model, ident.dim) == (
        "local", "BAAI/bge-small-en-v1.5", 384
    )
    assert ident.source == "auto"


def test_auto_yields_none_when_keyless_and_no_extra(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch, False)
    assert embeddings.active_embedder().provider == "none"


def test_settings_file_beats_auto_detect(monkeypatch):
    _keys(monkeypatch)  # a key IS present; the user still asked for local
    _local_ok(monkeypatch)
    settings_file.save_setting("embedding_provider", "local")
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.source) == ("local", "settings")


def test_settings_file_beats_config(monkeypatch):
    _keys(monkeypatch)
    _local_ok(monkeypatch)
    monkeypatch.setenv("B2_EMBEDDING__PROVIDER", "remote")
    from br8n.config import get_config

    get_config.cache_clear()
    settings_file.save_setting("embedding_provider", "none")
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.source) == ("none", "settings")


def test_config_beats_auto_detect(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch)
    monkeypatch.setenv("B2_EMBEDDING__PROVIDER", "none")
    from br8n.config import get_config

    get_config.cache_clear()
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.source) == ("none", "config")


def test_stored_auto_still_auto_detects(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch)
    settings_file.save_setting("embedding_provider", "auto")
    ident = embeddings.active_embedder()
    assert (ident.provider, ident.source) == ("local", "settings")


def test_explicit_remote_without_a_key_degrades_to_none(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch)
    settings_file.save_setting("embedding_provider", "remote")
    assert embeddings.active_embedder().provider == "none"


def test_explicit_local_without_eligibility_degrades_to_none(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch, False)
    settings_file.save_setting("embedding_provider", "local")
    assert embeddings.active_embedder().provider == "none"


def test_local_ineligible_on_cloud_tier(monkeypatch):
    """The real guard: cloud pgvector columns are 1536-wide."""
    monkeypatch.setenv("BR8N_BACKEND", "cloud")
    monkeypatch.setattr(
        "br8n.clients.embed_local.installed", lambda: True, raising=False
    )
    assert embeddings._local_eligible() is False


def test_none_identity_has_zero_dim(monkeypatch):
    _no_keys(monkeypatch)
    _local_ok(monkeypatch, False)
    ident = embeddings.active_embedder()
    assert ident.dim == 0 and ident.model == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/embeddings/test_identity.py -v`
Expected: FAIL with `AttributeError: module 'br8n.clients.embeddings' has no attribute 'active_embedder'`

- [ ] **Step 3: Implement**

Add to `backend/br8n/clients/embeddings.py`, above the existing `embeddings_configured`:

```python
from dataclasses import dataclass

from br8n.settings_file import load_settings

_VALID_PROVIDERS = ("auto", "remote", "local", "none")


@dataclass(frozen=True)
class EmbedderIdentity:
    """Which embedding space is active, and what decided it.

    ``provider`` is resolved (never ``"auto"``): ``remote`` | ``local`` |
    ``none``. ``source`` names the precedence level that won — ``settings``
    (the user's ~/.br8n/settings.json), ``config`` (config.yaml or
    ``B2_EMBEDDING__PROVIDER``), or ``auto`` (credential/extra detection).
    ``dim`` is 0 for ``none``.
    """

    provider: str
    model: str
    dim: int
    source: str


def _creds_present() -> bool:
    s = get_settings()
    return bool(s.ai_gateway_api_key or s.openai_api_key)


def _local_eligible() -> bool:
    """True when the local provider may be selected at all.

    Two gates: the extra must be installed, and the tier must be local —
    cloud pgvector columns are 1536-wide, so a 384-dim vector there is a
    write error. Imported inside the function: ``br8n.store`` imports this
    module, so a module-scope import would be circular.
    """
    try:
        from br8n.clients import embed_local
        from br8n.store import active_backend

        return active_backend() == "local" and embed_local.installed()
    except Exception:  # noqa: BLE001 — never let detection raise
        return False


def _requested() -> tuple[str, str]:
    """(requested provider, source) before validation. Precedence lives here."""
    stored = load_settings().get("embedding_provider")
    if isinstance(stored, str) and stored in _VALID_PROVIDERS:
        return stored, "settings"
    configured = get_config().embedding.provider
    if configured in _VALID_PROVIDERS and configured != "auto":
        return configured, "config"
    return "auto", "auto"


def active_embedder() -> EmbedderIdentity:
    """Resolve the active embedding identity. Never raises."""
    cfg = get_config().embedding
    requested, source = _requested()

    if requested == "auto":
        if _creds_present():
            resolved = "remote"
        elif _local_eligible():
            resolved = "local"
        else:
            resolved = "none"
    else:
        resolved = requested

    # An explicit choice that cannot be honoured degrades rather than
    # producing vectors in the wrong space.
    if resolved == "remote" and not _creds_present():
        resolved = "none"
    if resolved == "local" and not _local_eligible():
        resolved = "none"

    if resolved == "local":
        return EmbedderIdentity("local", cfg.local_model, cfg.local_dim, source)
    if resolved == "remote":
        return EmbedderIdentity("remote", cfg.model, cfg.dim, source)
    return EmbedderIdentity("none", "", 0, source)
```

Note `test_stored_auto_still_auto_detects` expects `source == "settings"` when the file stores `"auto"`: `_requested()` returns `("auto", "settings")`, and the auto branch then resolves the provider. Keep that ordering.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/embeddings/test_identity.py -v`
Expected: 11 PASS

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check br8n tests
git add br8n/clients/embeddings.py tests/embeddings/test_identity.py
git commit -m "feat(embeddings): EmbedderIdentity resolution with settings/config/auto precedence"
```

---

### Task 3: The fastembed provider and façade routing

**Files:**
- Create: `backend/br8n/clients/embed_local.py`
- Modify: `backend/br8n/clients/embeddings.py` (`embeddings_configured`, `embed_text`, `embed_batch`)
- Test: `backend/tests/embeddings/test_embed_local.py`

**Interfaces:**
- Consumes: `active_embedder()`, `get_config().embedding.{local_model, local_threads}`.
- Produces: `embed_local.installed() -> bool`, `embed_local.ready() -> bool`, `embed_local.warm_up() -> None` (non-blocking, once per process), `embed_local.load_now() -> bool` (blocking; returns True if resident), `async embed_local.embed(texts: Sequence[str]) -> list[list[float]]`, `embed_local.reset() -> None` (tests). After this task `embeddings_configured()` is True for local **only when the model is resident**.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/embeddings/test_embed_local.py
"""Local provider: readiness gating, warm-up, threading, façade routing."""
import sys
import types

import pytest

from br8n import settings_file
from br8n.clients import embed_local, embeddings


class _FakeTextEmbedding:
    """Stands in for fastembed.TextEmbedding — records kwargs, yields vectors."""

    last_kwargs: dict = {}
    fail_local_only = False

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        if kwargs.get("local_files_only") and type(self).fail_local_only:
            raise RuntimeError("model not cached")

    def embed(self, texts):
        for i, t in enumerate(texts):
            yield [float(len(t) + i)] * 384


@pytest.fixture
def fake_fastembed(monkeypatch):
    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    _FakeTextEmbedding.fail_local_only = False
    embed_local.reset()
    yield _FakeTextEmbedding
    embed_local.reset()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_BACKEND", "local")
    from br8n.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()
    yield
    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()


def test_installed_reflects_import_availability(fake_fastembed):
    assert embed_local.installed() is True


def test_load_now_makes_it_ready(fake_fastembed):
    assert embed_local.ready() is False
    assert embed_local.load_now() is True
    assert embed_local.ready() is True


def test_load_passes_model_and_thread_pinning(fake_fastembed):
    embed_local.load_now()
    kwargs = fake_fastembed.last_kwargs
    assert kwargs["model_name"] == "BAAI/bge-small-en-v1.5"
    assert kwargs["threads"] == 1


def test_uncached_model_falls_back_to_downloading_load(fake_fastembed):
    """local_files_only avoids a known firewall hang; a miss then downloads."""
    fake_fastembed.fail_local_only = True
    assert embed_local.load_now() is True
    assert fake_fastembed.last_kwargs.get("local_files_only") is not True


@pytest.mark.asyncio
async def test_embed_returns_correct_width(fake_fastembed):
    embed_local.load_now()
    out = await embed_local.embed(["ab", "cde"])
    assert [len(v) for v in out] == [384, 384]
    assert out[0][0] == 2.0 and out[1][0] == 4.0


def test_warm_up_is_idempotent_and_non_blocking(fake_fastembed):
    embed_local.warm_up()
    embed_local.warm_up()
    embed_local.wait_for_warm_up(timeout=5)
    assert embed_local.ready() is True


def test_missing_extra_reports_uninstalled(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    embed_local.reset()
    monkeypatch.setattr(
        "importlib.util.find_spec", lambda name: None if name == "fastembed" else True
    )
    assert embed_local.installed() is False


def test_configured_is_false_until_model_is_resident(fake_fastembed, monkeypatch):
    """Zero-friction capture: never block a write on a load or a download."""
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    settings_file.save_setting("embedding_provider", "local")
    assert embeddings.active_embedder().provider == "local"
    assert embeddings.embeddings_configured() is False  # schedules warm-up
    embed_local.wait_for_warm_up(timeout=5)
    assert embeddings.embeddings_configured() is True


@pytest.mark.asyncio
async def test_facade_routes_batch_to_local(fake_fastembed, monkeypatch):
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    settings_file.save_setting("embedding_provider", "local")
    embed_local.load_now()
    out = await embeddings.embed_batch(["hello"])
    assert len(out) == 1 and len(out[0]) == 384


@pytest.mark.asyncio
async def test_facade_raises_for_none_provider(monkeypatch):
    monkeypatch.setattr(embeddings, "_creds_present", lambda: False)
    monkeypatch.setattr(embeddings, "_local_eligible", lambda: False)
    with pytest.raises(RuntimeError):
        await embeddings.embed_batch(["hello"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/embeddings/test_embed_local.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'br8n.clients.embed_local'`

- [ ] **Step 3: Implement the provider**

```python
# backend/br8n/clients/embed_local.py
"""Local ONNX embeddings via fastembed — the optional ``br8n[local-embeddings]``.

Why this shape:

* **Lazy import.** fastembed is an extra; importing this module must stay free
  when it is absent, so the import happens inside ``load_now``.
* **Never on the event loop.** fastembed is synchronous and CPU-bound, and
  onnxruntime sizes an intra-op thread pool per session — ``embed`` hops to a
  worker thread and ``threads`` is pinned to ``local_threads`` (default 1) so a
  background embed cannot saturate the machine mid-capture.
* **Readiness, not blocking.** The first use downloads ~130 MB. Callers ask
  ``ready()`` and get False until the model is resident; ``warm_up()`` loads it
  on a daemon thread (no event loop needed). A capture in that window stores
  ``needs_embed=1`` and the existing drain backfills.
* **Offline-safe.** A cached load passes ``local_files_only=True``: fastembed
  is known to hang behind a firewall otherwise. A miss retries with a normal
  (downloading) construction.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Sequence

from br8n.config import get_config

logger = logging.getLogger(__name__)

_model = None
_lock = threading.Lock()
_warm_thread: threading.Thread | None = None


def installed() -> bool:
    """True when the extra is importable. Cheap — no model load."""
    from importlib.util import find_spec

    try:
        return find_spec("fastembed") is not None
    except Exception:  # noqa: BLE001 — a broken import path is "not installed"
        return False


def ready() -> bool:
    return _model is not None


def reset() -> None:
    """Drop the loaded model and warm-up state (tests)."""
    global _model, _warm_thread
    with _lock:
        _model = None
        _warm_thread = None


def load_now() -> bool:
    """Load the model on THIS thread (blocking). True when resident afterwards."""
    global _model
    with _lock:
        if _model is not None:
            return True
        cfg = get_config().embedding
        try:
            from fastembed import TextEmbedding
        except Exception:  # noqa: BLE001 — extra absent or broken
            logger.warning("fastembed unavailable; local embeddings off", exc_info=True)
            return False
        base = {"model_name": cfg.local_model, "threads": cfg.local_threads}
        for kwargs in ({**base, "local_files_only": True}, base):
            try:
                _model = TextEmbedding(**kwargs)
                return True
            except TypeError:
                # Older fastembed without local_files_only — fall through to base.
                continue
            except Exception:  # noqa: BLE001 — cache miss: retry with download
                logger.info("local model not cached; fetching", exc_info=True)
                continue
        logger.warning("local embedding model failed to load")
        return False


def warm_up() -> None:
    """Schedule a background load. Non-blocking, at most one thread per process."""
    global _warm_thread
    with _lock:
        if _model is not None or (_warm_thread is not None and _warm_thread.is_alive()):
            return
        _warm_thread = threading.Thread(
            target=load_now, name="br8n-embed-warmup", daemon=True
        )
        _warm_thread.start()


def wait_for_warm_up(timeout: float = 60.0) -> bool:
    """Block until a scheduled warm-up finishes. For tests and the doctor."""
    t = _warm_thread
    if t is not None:
        t.join(timeout)
    return ready()


async def embed(texts: Sequence[str]) -> list[list[float]]:
    """Embed off the event loop. Raises if the model is not resident."""
    if _model is None and not load_now():
        raise RuntimeError("local embedding model unavailable")
    cap = get_config().embedding.input_char_cap
    trimmed = [t[:cap] for t in texts]

    def _run() -> list[list[float]]:
        return [[float(x) for x in vec] for vec in _model.embed(trimmed)]

    return await asyncio.to_thread(_run)
```

- [ ] **Step 4: Route the façade**

In `backend/br8n/clients/embeddings.py`, replace `embeddings_configured` and make the two embed entry points branch. Keep signatures identical:

```python
def embeddings_configured() -> bool:
    """True when an embedding can be produced *right now*.

    Callers that can degrade (capture stores an unembedded finding) check this
    instead of catching an auth error, so a genuine API failure stays loud.
    For the local provider this is False until the model is resident — the
    first check schedules the warm-up and the existing needs_embed drain
    backfills, so a capture never waits on a ~130 MB download.
    """
    ident = active_embedder()
    if ident.provider == "remote":
        return True
    if ident.provider == "local":
        from br8n.clients import embed_local

        if embed_local.ready():
            return True
        embed_local.warm_up()
        return False
    return False


async def embed_text(text: str) -> list[float]:
    [vec] = await embed_batch([text])
    return vec


async def embed_batch(texts: Sequence[str]) -> list[list[float]]:
    if not texts:
        return []
    ident = active_embedder()
    if ident.provider == "local":
        from br8n.clients import embed_local

        return await embed_local.embed(texts)
    if ident.provider == "none":
        raise RuntimeError(
            "no embedding provider available: set AI_GATEWAY_API_KEY/OPENAI_API_KEY, "
            "or install the local extra (pip install 'br8n[local-embeddings]')"
        )
    emb = get_config().embedding
    client = _get_client()
    resp = await client.embeddings.create(
        model=emb.model,
        input=[t[: emb.input_char_cap] for t in texts],
    )
    return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
```

`embed_text` now delegates to `embed_batch` (it previously duplicated the API call) — one code path for both providers. `embed_with_retry` is unchanged; it calls `embed_text`.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/embeddings -v`
Expected: all PASS (11 new here + Tasks 1–2)

- [ ] **Step 6: Full suite + lint + commit**

Run: `.venv/bin/pytest -q` — expect no new failures vs 419 passed / 1 skipped.

```bash
.venv/bin/ruff check br8n tests
git add br8n/clients tests/embeddings/test_embed_local.py
git commit -m "feat(embeddings): fastembed local provider with readiness gating and threaded embeds"
```

---

### Task 4: Embedding space table, dim-parameterized vec DDL, rebuild on mismatch

**Files:**
- Modify: `backend/br8n/store/sqlite.py` (`_SCHEMA` ~line 55-88, `_ADD_COLUMN_MIGRATIONS` ~line 93, `_ensure_schema` ~line 158)
- Test: `backend/tests/embeddings/test_embedding_space.py`

**Interfaces:**
- Consumes: `active_embedder()`.
- Produces: `SQLiteStore._declared_vec_dim() -> int | None`, `SQLiteStore._sync_embedding_space() -> None`, `SQLiteStore.embedding_space() -> dict | None` (returns `{"provider","model","dim"}`), module function `_vec_schema(dim: int) -> str`. New table `embedding_space`; new column `kg_nodes.needs_embed`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/embeddings/test_embedding_space.py
"""One active embedding space: stamping, first-stamp inference, rebuild on change."""
import pytest

from br8n.clients.embeddings import EmbedderIdentity
from br8n.store.sqlite import SQLiteStore


def _ident(provider="remote", model="text-embedding-3-small", dim=1536):
    return EmbedderIdentity(provider, model, dim, "auto")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    return str(tmp_path / "brain.db")


def _store(db, monkeypatch, ident):
    monkeypatch.setattr(
        "br8n.clients.embeddings.active_embedder", lambda: ident
    )
    return SQLiteStore(db)


@pytest.mark.asyncio
async def test_fresh_db_stamps_active_identity(db, monkeypatch):
    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    assert store.embedding_space() == {
        "provider": "local", "model": "BAAI/bge-small-en-v1.5", "dim": 384
    }
    assert store._declared_vec_dim() == 384
    store.close()


@pytest.mark.asyncio
async def test_same_identity_does_not_rebuild(db, monkeypatch):
    ident = _ident()
    store = _store(db, monkeypatch, ident)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    store.close()

    store = _store(db, monkeypatch, ident)
    flag = store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["needs_embed"]
    assert not flag  # untouched
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_findings;"
    ).fetchone()["n"] == 1
    store.close()


@pytest.mark.asyncio
async def test_dim_change_rebuilds_and_flags_everything(db, monkeypatch):
    store = _store(db, monkeypatch, _ident())
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    await store.upsert_kg_nodes(
        kb_id, [{"type": "Repo", "label": "r", "properties": {}, "grounded_in": [],
                 "embedding": [0.1] * 1536}]
    )
    store.close()

    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    assert store._declared_vec_dim() == 384
    assert store.embedding_space()["dim"] == 384
    assert store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["needs_embed"] == 1
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM kg_nodes WHERE needs_embed = 1;"
    ).fetchone()["n"] == 1
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_findings;"
    ).fetchone()["n"] == 0  # stale vectors dropped, not mixed
    store.close()


@pytest.mark.asyncio
async def test_legacy_db_infers_width_instead_of_trusting_active(db, monkeypatch):
    """A key removed between runs must NOT stamp 384 over 1536-dim vectors."""
    store = _store(db, monkeypatch, _ident())
    store._conn.execute("DELETE FROM embedding_space;")  # simulate pre-feature db
    store._conn.commit()
    store.close()

    store = _store(db, monkeypatch, _ident("local", "BAAI/bge-small-en-v1.5", 384))
    # inferred 1536, saw a mismatch, rebuilt to 384
    assert store._declared_vec_dim() == 384
    assert store.embedding_space()["dim"] == 384
    store.close()


@pytest.mark.asyncio
async def test_legacy_db_with_matching_dim_is_adopted_without_rebuild(db, monkeypatch):
    ident = _ident()
    store = _store(db, monkeypatch, ident)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [fid] = await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    store._conn.execute("DELETE FROM embedding_space;")
    store._conn.commit()
    store.close()

    store = _store(db, monkeypatch, ident)  # same remote identity
    assert store.embedding_space() == {
        "provider": "remote", "model": "text-embedding-3-small", "dim": 1536
    }
    assert store._conn.execute(
        "SELECT needs_embed FROM findings WHERE id = ?;", (fid,)
    ).fetchone()["needs_embed"] in (None, 0)  # no needless re-embed
    store.close()


@pytest.mark.asyncio
async def test_provider_none_never_rebuilds(db, monkeypatch):
    """Losing the key entirely must not throw away vectors you might restore."""
    store = _store(db, monkeypatch, _ident())
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [],
          "embedding": [0.1] * 1536}]
    )
    store.close()

    store = _store(db, monkeypatch, EmbedderIdentity("none", "", 0, "auto"))
    assert store._declared_vec_dim() == 1536
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_findings;"
    ).fetchone()["n"] == 1
    store.close()


def test_sync_failure_never_blocks_construction(db, monkeypatch):
    monkeypatch.setattr(
        "br8n.clients.embeddings.active_embedder",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    store = SQLiteStore(db)  # must not raise
    assert store._declared_vec_dim() is not None  # vec tables still exist
    store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/embeddings/test_embedding_space.py -v`
Expected: FAIL with `AttributeError: 'SQLiteStore' object has no attribute 'embedding_space'`

- [ ] **Step 3: Implement**

In `backend/br8n/store/sqlite.py`: add `import re` to the imports, delete the two `CREATE VIRTUAL TABLE ... vec0(...)` lines from `_SCHEMA`, and add after it:

```python
# Vector tables are created at the ACTIVE embedding dimension, not a constant:
# a vec0 table's width is fixed at creation, and vectors from different models
# are not comparable, so a provider change recreates them (see
# ``_sync_embedding_space``).
def _vec_schema(dim: int) -> str:
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_findings "
        f"USING vec0(finding_id TEXT, embedding float[{dim}]);"
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_kg_nodes "
        f"USING vec0(node_id TEXT, embedding float[{dim}]);"
    )


_EMBEDDING_SPACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_space (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  provider TEXT NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL,
  updated_at TEXT NOT NULL);
"""
```

Append to `_ADD_COLUMN_MIGRATIONS`:

```python
    # 0011: local embeddings — KG nodes join the lazy re-embed drain
    "ALTER TABLE kg_nodes ADD COLUMN needs_embed INTEGER;",
```

Extend `_ensure_schema` (after the existing ADD COLUMN loop):

```python
        self._sync_embedding_space()
```

And add the methods:

```python
    # --- embedding space -----------------------------------------------------

    def embedding_space(self) -> dict | None:
        """The stamped active space, or None before the first stamp."""
        try:
            r = self._conn.execute(
                "SELECT provider, model, dim FROM embedding_space WHERE id = 1;"
            ).fetchone()
        except Exception:  # noqa: BLE001 — table absent on a partial schema
            return None
        if r is None:
            return None
        return {"provider": r["provider"], "model": r["model"], "dim": int(r["dim"])}

    def _declared_vec_dim(self) -> int | None:
        """Width the existing vec_findings table was created with, if any."""
        r = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_findings';"
        ).fetchone()
        if r is None or not r["sql"]:
            return None
        m = re.search(r"float\[(\d+)\]", r["sql"])
        return int(m.group(1)) if m else None

    def _stamp_embedding_space(self, provider: str, model: str, dim: int) -> None:
        self._conn.execute(
            "INSERT INTO embedding_space (id, provider, model, dim, updated_at) "
            "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "provider=excluded.provider, model=excluded.model, dim=excluded.dim, "
            "updated_at=excluded.updated_at;",
            (provider, model, dim, _now_iso()),
        )
        self._conn.commit()

    def _rebuild_vec_tables(self, dim: int) -> None:
        """Recreate both vec tables at `dim` and queue everything for re-embed.

        Safe because vectors are derived: the text lives in ``findings.content``
        and, on the vault tier, canonically on disk. The refill is the existing
        lazy ``needs_embed`` drain, so this call stays cheap (DDL + 2 UPDATEs).
        """
        self._conn.executescript(
            "DROP TABLE IF EXISTS vec_findings; DROP TABLE IF EXISTS vec_kg_nodes;"
        )
        self._conn.executescript(_vec_schema(dim))
        self._conn.execute("UPDATE findings SET needs_embed = 1;")
        self._conn.execute("UPDATE kg_nodes SET needs_embed = 1;")
        self._conn.commit()

    def _sync_embedding_space(self) -> None:
        """Ensure the vec tables match the active embedder. Best-effort.

        Never raises: a failure here must not stop a store from opening, so the
        vec tables are created up-front at a safe width and the identity logic
        runs afterwards.
        """
        declared = self._declared_vec_dim()
        fallback = declared or get_config().embedding.dim
        try:
            self._conn.executescript(_vec_schema(fallback))
            self._conn.executescript(_EMBEDDING_SPACE_SCHEMA)
            self._conn.commit()
        except Exception:  # noqa: BLE001 — a store must still open
            logger.warning("vec/embedding_space schema degraded", exc_info=True)
            return

        try:
            from br8n.clients.embeddings import active_embedder

            ident = active_embedder()
            # No provider: keep whatever space exists — never discard vectors
            # the user may restore by putting a key back.
            if ident.provider == "none" or ident.dim <= 0:
                return

            stored = self.embedding_space()
            if stored is None:
                declared = self._declared_vec_dim()
                if declared is not None and declared != ident.dim:
                    # Pre-feature DB whose width disagrees with the active
                    # embedder: trust the TABLE, not the provider, then let the
                    # mismatch below rebuild. Stamping the active identity here
                    # would silently label 1536-dim vectors as 384.
                    self._stamp_embedding_space("unknown", "", declared)
                    stored = self.embedding_space()
                else:
                    self._stamp_embedding_space(ident.provider, ident.model, ident.dim)
                    return

            if stored["dim"] != ident.dim or stored["model"] != ident.model:
                logger.info(
                    "embedding space change (%s/%sd -> %s/%sd); rebuilding index",
                    stored["model"] or stored["provider"], stored["dim"],
                    ident.model, ident.dim,
                )
                self._rebuild_vec_tables(ident.dim)
                self._stamp_embedding_space(ident.provider, ident.model, ident.dim)
        except Exception:  # noqa: BLE001 — identity problems never break a store
            logger.warning("embedding-space sync degraded", exc_info=True)
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001
                pass
```

`sqlite.py` has no logger today — add `import logging` and `logger = logging.getLogger(__name__)` near the top, and import `get_config` from `br8n.config`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/embeddings/test_embedding_space.py -v`
Expected: 7 PASS

- [ ] **Step 5: Regression gate**

Run: `.venv/bin/pytest tests/test_store_sqlite.py tests/store tests/test_kg_store_sqlite.py tests/vault -q`
Expected: all PASS — the vec tables are still created, just parameterized.

- [ ] **Step 6: Full suite, lint, commit**

Run: `.venv/bin/pytest -q` — no new failures vs 419 passed / 1 skipped.

```bash
.venv/bin/ruff check br8n tests
git add br8n/store/sqlite.py tests/embeddings/test_embedding_space.py
git commit -m "feat(embeddings): single active embedding space with rebuild on provider change"
```

---

### Task 5: Drain KG nodes in the lazy re-embed

**Files:**
- Modify: `backend/br8n/store/vault.py` (`_re_embed_stale`, ~line 194)
- Test: `backend/tests/embeddings/test_kg_reembed.py`

**Interfaces:**
- Consumes: `kg_nodes.needs_embed` (Task 4), `embed_batch`, `_re_embed_inflight`.
- Produces: `_re_embed_stale()` returns findings + nodes drained (int), unchanged signature.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/embeddings/test_kg_reembed.py
"""After a space change, KG node vectors refill through the same lazy drain."""
import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n.store.vault import VaultStore

    s = VaultStore(str(tmp_path / "brain.db"))
    yield s
    s.close()


def _fake_embeddings(monkeypatch):
    import br8n.store.vault as vault_mod

    async def fake_embed(texts):
        return [[0.2] * 1536 for _ in texts]

    monkeypatch.setattr(vault_mod, "embed_batch", fake_embed)
    monkeypatch.setattr(vault_mod, "embeddings_configured", lambda: True)


@pytest.mark.asyncio
async def test_flagged_nodes_are_re_embedded(store, monkeypatch):
    _fake_embeddings(monkeypatch)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    [nid] = await store.upsert_kg_nodes(
        kb_id, [{"type": "Repo", "label": "br8n", "properties": {}, "grounded_in": []}]
    )
    store._conn.execute("UPDATE kg_nodes SET needs_embed = 1;")
    store._conn.execute("DELETE FROM vec_kg_nodes;")
    store._conn.commit()

    drained = await store._re_embed_stale()
    assert drained >= 1
    assert store._conn.execute(
        "SELECT needs_embed FROM kg_nodes WHERE id = ?;", (nid,)
    ).fetchone()["needs_embed"] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) AS n FROM vec_kg_nodes;"
    ).fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_drain_counts_findings_and_nodes_together(store, monkeypatch):
    _fake_embeddings(monkeypatch)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.insert_findings(
        [{"kb_id": kb_id, "title": "t", "content": "c", "category": "note",
          "confidence": 1.0, "tags": [], "provenance": [], "embedding": None}]
    )
    await store.upsert_kg_nodes(
        kb_id, [{"type": "Repo", "label": "br8n", "properties": {}, "grounded_in": []}]
    )
    store._conn.execute("UPDATE kg_nodes SET needs_embed = 1;")
    store._conn.commit()
    assert await store._re_embed_stale() == 2


@pytest.mark.asyncio
async def test_nodes_without_labels_are_skipped(store, monkeypatch):
    _fake_embeddings(monkeypatch)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    store._conn.execute(
        "INSERT INTO kg_nodes (id, org_id, kb_id, type, label, properties, "
        "grounded_in, created_at, needs_embed) VALUES "
        "('n1', 'local', ?, 'Repo', '', '{}', '[]', '2026-07-28T00:00:00Z', 1);",
        (kb_id,),
    )
    store._conn.commit()
    assert await store._re_embed_stale() == 0


@pytest.mark.asyncio
async def test_embed_failure_leaves_nodes_flagged(store, monkeypatch):
    import br8n.store.vault as vault_mod

    async def boom(texts):
        raise RuntimeError("provider down")

    monkeypatch.setattr(vault_mod, "embed_batch", boom)
    monkeypatch.setattr(vault_mod, "embeddings_configured", lambda: True)
    org_id, pid = store.resolve_project("p", create=True)
    kb_id = store.resolve_kb(org_id, pid, "k", create=True)
    await store.upsert_kg_nodes(
        kb_id, [{"type": "Repo", "label": "br8n", "properties": {}, "grounded_in": []}]
    )
    store._conn.execute("UPDATE kg_nodes SET needs_embed = 1;")
    store._conn.commit()

    assert await store._re_embed_stale() == 0  # degraded, not raised
    assert store._conn.execute(
        "SELECT needs_embed FROM kg_nodes WHERE label = 'br8n';"
    ).fetchone()["needs_embed"] == 1  # retried next pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/embeddings/test_kg_reembed.py -v`
Expected: FAIL — `_re_embed_stale` returns 0 for nodes (findings-only today)

- [ ] **Step 3: Implement**

In `backend/br8n/store/vault.py`, the two drains differ only in table, column and
claim namespace — identical control flow otherwise — so replace the existing
`_re_embed_stale` body with one parameterized helper called twice rather than
duplicating the claim/embed/swap block:

```python
    async def _re_embed_stale(self) -> int:
        """Embed rows marked stale — findings, then KG nodes.

        Self-heals keyless-capture rows and refills BOTH vector spaces after an
        embedding-space change (see ``SQLiteStore._sync_embedding_space``), so a
        provider switch converges without user action.
        """
        if not embeddings_configured():
            return 0
        return await self._re_embed_rows(
            table="findings", vec_table="vec_findings",
            vec_id_col="finding_id", text_col="content", claim_prefix="",
        ) + await self._re_embed_rows(
            table="kg_nodes", vec_table="vec_kg_nodes",
            vec_id_col="node_id", text_col="label", claim_prefix="kg:",
        )

    async def _re_embed_rows(
        self, *, table: str, vec_table: str, vec_id_col: str,
        text_col: str, claim_prefix: str,
    ) -> int:
        """Drain one table's ``needs_embed`` rows into its vector table.

        Claim-then-embed: ids are claimed in ``_re_embed_inflight`` before the
        awaited embedding call, so an overlapping read path never pays to embed
        the same rows twice. ``claim_prefix`` namespaces the two tables' ids so
        a finding and a node can't collide on the same uuid.

        The interpolated identifiers are module-internal constants, never user
        input — values still go through placeholders.
        """
        claimed: list = []
        try:
            cap = get_config().vault.re_embed_batch
            rows = self._conn.execute(
                f"SELECT id, {text_col} AS text FROM {table} WHERE needs_embed = 1 "
                f"AND {text_col} IS NOT NULL AND {text_col} != '' LIMIT ?;",
                (cap,),
            ).fetchall()
            rows = [
                r for r in rows
                if f"{claim_prefix}{r['id']}" not in self._re_embed_inflight
            ]
            if not rows:
                return 0
            self._re_embed_inflight.update(f"{claim_prefix}{r['id']}" for r in rows)
            claimed = rows
            embeddings = await embed_batch([r["text"] for r in rows])
            for r, emb in zip(rows, embeddings):
                self._conn.execute(
                    f"DELETE FROM {vec_table} WHERE {vec_id_col} = ?;", (r["id"],)
                )
                self._conn.execute(
                    f"INSERT INTO {vec_table} ({vec_id_col}, embedding) VALUES (?, ?);",
                    (r["id"], serialize_float32(list(emb))),
                )
                self._conn.execute(
                    f"UPDATE {table} SET needs_embed = 0 WHERE id = ?;", (r["id"],)
                )
            self._conn.commit()
            return len(rows)
        except Exception:  # noqa: BLE001 — embedding failures never break search
            try:
                self._conn.rollback()  # never leave a write transaction open
            except Exception:  # noqa: BLE001
                pass
            logger.warning("re-embed pass degraded (%s)", table, exc_info=True)
            return 0
        finally:
            self._re_embed_inflight.difference_update(
                f"{claim_prefix}{r['id']}" for r in claimed
            )
```

This preserves every property the merged findings drain had (batch cap, claim
set, vec row swap, rollback on failure, flag cleared only on success) and adds
the node pass without a second copy of that logic.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/embeddings/test_kg_reembed.py tests/vault -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check br8n tests
git add br8n/store/vault.py tests/embeddings/test_kg_reembed.py
git commit -m "feat(embeddings): lazy drain refills KG node vectors after a space change"
```

---

### Task 6: MCP tools and the /br8n:embeddings skill

**Files:**
- Modify: `backend/br8n/interfaces/mcp/server.py`
- Create: `skills/embeddings/SKILL.md`
- Modify: `.claude-plugin/plugin.json` (skills array)
- Test: `backend/tests/embeddings/test_embeddings_tools.py`

**Interfaces:**
- Consumes: `active_embedder()`, `embed_local.{installed,ready,warm_up}`, `settings_file.save_setting`, `active_backend()`.
- Produces: `_embeddings_get_impl() -> dict` with keys `provider, model, dim, source, extra_installed, model_cached, ready, pending_findings, pending_nodes`; `_embeddings_set_impl(provider: str) -> dict` returning `{"ok": True, **state, "queued_rebuild": int}` or `{"ok": False, "error": str, "fix": str | None}`. MCP tools `br8n_embeddings_get()` and `br8n_embeddings_set(provider)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/embeddings/test_embeddings_tools.py
"""br8n_embeddings_get/set: reporting, refusals, live switching."""
import pytest

from br8n import settings_file
from br8n.interfaces.mcp import server


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()
    yield
    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.clear_cache()


def test_get_reports_identity_and_source(monkeypatch):
    monkeypatch.setattr("br8n.clients.embeddings._creds_present", lambda: True)
    out = server._embeddings_get_impl()
    assert out["provider"] == "remote"
    assert out["dim"] == 1536
    assert out["source"] == "auto"
    assert out["pending_findings"] == 0
    assert out["pending_nodes"] == 0
    assert "extra_installed" in out and "ready" in out


def test_set_rejects_unknown_provider():
    out = server._embeddings_set_impl("gpu")
    assert out["ok"] is False
    assert "auto" in out["error"]
    assert settings_file.load_settings() == {}  # nothing written


def test_set_local_without_extra_refuses_with_the_fix(monkeypatch):
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: False)
    out = server._embeddings_set_impl("local")
    assert out["ok"] is False
    assert "br8n[local-embeddings]" in out["fix"]
    assert settings_file.load_settings() == {}  # refusal writes nothing


def test_set_local_on_cloud_tier_refuses(monkeypatch):
    monkeypatch.setenv("BR8N_BACKEND", "cloud")
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: True)
    out = server._embeddings_set_impl("local")
    assert out["ok"] is False
    assert "1536" in out["error"]
    assert settings_file.load_settings() == {}


def test_set_writes_and_takes_effect_without_restart(monkeypatch):
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: True)
    monkeypatch.setattr("br8n.clients.embed_local.warm_up", lambda: None)
    out = server._embeddings_set_impl("local")
    assert out["ok"] is True
    assert out["provider"] == "local" and out["source"] == "settings"
    assert settings_file.load_settings()["embedding_provider"] == "local"
    # a fresh resolution (as a later tool call would do) sees it
    assert server._embeddings_get_impl()["provider"] == "local"


def test_set_reports_the_queued_rebuild_size(monkeypatch):
    monkeypatch.setattr("br8n.clients.embed_local.installed", lambda: True)
    monkeypatch.setattr("br8n.clients.embed_local.warm_up", lambda: None)
    from br8n.store import get_store

    store = get_store()
    store._conn.execute(
        "INSERT INTO findings (id, org_id, kb_id, title, content, category, "
        "confidence, tags, provenance, created_at, needs_embed) VALUES "
        "('f1','local','k','t','c','note',1.0,'[]','[]','2026-07-28T00:00:00Z',1);"
    )
    store._conn.commit()
    out = server._embeddings_set_impl("local")
    assert out["queued_rebuild"] >= 1


def test_set_auto_returns_to_detection(monkeypatch):
    monkeypatch.setattr("br8n.clients.embeddings._creds_present", lambda: True)
    server._embeddings_set_impl("none")
    assert server._embeddings_get_impl()["provider"] == "none"
    out = server._embeddings_set_impl("auto")
    assert out["ok"] is True
    assert server._embeddings_get_impl()["provider"] == "remote"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/embeddings/test_embeddings_tools.py -v`
Expected: FAIL with `AttributeError: module 'br8n.interfaces.mcp.server' has no attribute '_embeddings_get_impl'`

- [ ] **Step 3: Implement the tools**

In `backend/br8n/interfaces/mcp/server.py`, add near the other `_impl` helpers:

```python
_VALID_EMBED_PROVIDERS = ("auto", "remote", "local", "none")


def _pending_counts() -> tuple[int, int]:
    """(findings, nodes) awaiting embedding. Local tier only; 0/0 elsewhere."""
    from br8n.store import active_backend, get_store

    if active_backend() != "local":
        return 0, 0
    try:
        conn = get_store()._conn
        f = conn.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE needs_embed = 1;"
        ).fetchone()["n"]
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM kg_nodes WHERE needs_embed = 1;"
        ).fetchone()["n"]
        return int(f), int(n)
    except Exception:  # noqa: BLE001 — reporting must not raise
        return 0, 0


def _embeddings_get_impl() -> dict:
    from br8n.clients import embed_local
    from br8n.clients.embeddings import active_embedder

    ident = active_embedder()
    pending_findings, pending_nodes = _pending_counts()
    return {
        "provider": ident.provider,
        "model": ident.model,
        "dim": ident.dim,
        "source": ident.source,
        "extra_installed": embed_local.installed(),
        # "cached" and "resident" collapse to the same observable: the only
        # honest cache check is attempting a cache-only load, which warm_up()
        # does off-thread.
        "model_cached": embed_local.ready(),
        "ready": embed_local.ready(),
        "pending_findings": pending_findings,
        "pending_nodes": pending_nodes,
    }


def _embeddings_set_impl(provider: str) -> dict:
    from br8n.clients import embed_local
    from br8n.settings_file import save_setting
    from br8n.store import active_backend

    if provider not in _VALID_EMBED_PROVIDERS:
        return {
            "ok": False,
            "error": f"unknown provider {provider!r}; expected one of "
                     f"{', '.join(_VALID_EMBED_PROVIDERS)}",
            "fix": None,
        }
    if provider == "local":
        if active_backend() != "local":
            return {
                "ok": False,
                "error": "local embeddings are local-tier only — cloud pgvector "
                         "columns are 1536-wide; keep a remote key on cloud",
                "fix": None,
            }
        if not embed_local.installed():
            return {
                "ok": False,
                "error": "the local-embeddings extra is not installed",
                "fix": "pip install 'br8n[local-embeddings]'",
            }

    save_setting("embedding_provider", provider)
    state = _embeddings_get_impl()
    if state["provider"] == "local":
        embed_local.warm_up()
    return {
        "ok": True,
        **state,
        "queued_rebuild": state["pending_findings"] + state["pending_nodes"],
    }
```

And the tool wrappers, next to the other `@mcp.tool()` definitions:

```python
@mcp.tool()
def br8n_embeddings_get() -> dict:
    """Report the active embedding provider: {provider, model, dim, source,
    extra_installed, model_cached, ready, pending_findings, pending_nodes}.
    `source` names what decided it — "settings" (~/.br8n/settings.json),
    "config" (config.yaml / B2_EMBEDDING__PROVIDER) or "auto" (detection).
    Used by /br8n:embeddings."""
    return _embeddings_get_impl()


@mcp.tool()
def br8n_embeddings_set(provider: str) -> dict:
    """Set the embedding provider — "auto" | "remote" | "local" | "none" —
    persisting to ~/.br8n/settings.json so it applies without restarting the
    MCP server. Refuses "local" when the extra is missing (returns the pip
    command in `fix`) or on the cloud tier. On success returns {ok: True,
    ...state, queued_rebuild} where queued_rebuild is how many rows will
    re-embed in the background. Used by /br8n:embeddings."""
    return _embeddings_set_impl(provider)
```

Note `model_cached` aliases `ready()` (see the comment in the dict above) so the tool contract in the docstring holds without a second, unreliable cache probe.

- [ ] **Step 4: Add the skill**

Create `skills/embeddings/SKILL.md`:

```markdown
---
name: embeddings
description: Inspect or switch how br8n embeds text for semantic search — remote (API key) or local (keyless, on-device ONNX). Use when the user asks why search returns nothing, wants search to work without an API key, wants to stop paying for embeddings, or asks which embedding model br8n is using.
---

# br8n — Embeddings (which provider, and switching it)

Semantic search needs vectors. br8n produces them one of three ways:

| Provider | When it applies | Dim |
|---|---|---|
| `remote` | `AI_GATEWAY_API_KEY` or `OPENAI_API_KEY` is set | 1536 |
| `local` | the `br8n[local-embeddings]` extra is installed and no key is set | 384 |
| `none` | neither — capture and chronological surfaces still work, search is text-only | — |

## Step 1 — Report the current state

Call **`br8n_embeddings_get`**. Lead with the provider, model and *why* it was
chosen (`source`), then flag anything actionable:

- `ready: false` with `provider: local` → the model is still downloading
  (~130 MB, first use only). Search stays text-only for a minute; nothing is lost.
- `pending_findings`/`pending_nodes` above zero → a re-embed is draining in the
  background. It refills on ordinary reads; no action needed.
- `provider: none` → say what would fix it: either set a key, or
  `pip install 'br8n[local-embeddings]'` and switch to local.

## Step 2 — Switch, if asked

Call **`br8n_embeddings_set`** with `auto`, `remote`, `local` or `none`.
`auto` is the default and means "use a key if there is one, else local".

The change lands in `~/.br8n/settings.json` and applies to the next call — **no
MCP server restart**. If the tool returns `ok: false`, relay `error` and, when
present, run nothing yourself: show the user the `fix` command.

## What a switch costs

Vectors from different models are not comparable, so changing provider
rebuilds the index: br8n drops the old vectors and re-embeds in the background
from content it already has. `queued_rebuild` in the response says how many
rows. Search quality dips until the drain finishes, then recovers — nothing is
lost, because the text is canonical (in the DB and, on the vault tier, on disk).

Do not tell the user local embeddings match the remote model's quality. They
are a good keyless fallback; a configured key is still the better path.
```

Register it in `.claude-plugin/plugin.json` by adding `"./skills/embeddings"` to the `skills` array (a skill on disk that is not listed there never loads).

- [ ] **Step 5: Run tests, lint, commit**

Run: `.venv/bin/pytest tests/embeddings -v` then `.venv/bin/pytest -q`
Expected: new tests PASS; suite has no new failures.

```bash
.venv/bin/ruff check br8n tests
git add br8n/interfaces/mcp/server.py tests/embeddings/test_embeddings_tools.py ../skills/embeddings ../.claude-plugin/plugin.json
git commit -m "feat(embeddings): br8n_embeddings_get/set tools and the /br8n:embeddings skill"
```

---

### Task 7: Doctor line, real-fastembed integration test, docs

**Files:**
- Modify: `backend/br8n/api/main.py` (`check()`, after the vault block ~line 143)
- Modify: `backend/.env.example`, `README.md`, `CLAUDE.md`
- Modify: `.github/workflows/*.yml` (install the extra on one matrix leg)
- Test: `backend/tests/embeddings/test_doctor_embeddings.py`, `backend/tests/embeddings/test_fastembed_integration.py`

**Interfaces:**
- Consumes: `_embeddings_get_impl`-equivalent data via `active_embedder()` + `embed_local`.
- Produces: a `line(...)` row labelled `embeddings` in `--check`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/embeddings/test_doctor_embeddings.py
"""--check reports the active embedder; vault/embedding problems stay warnings."""


def test_check_prints_embeddings_line(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n.api.main import check

    rc = check()
    out = capsys.readouterr().out
    assert rc == 0
    assert "embeddings" in out


def test_check_names_provider_and_source(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BR8N_BACKEND", "local")
    monkeypatch.setenv("BR8N_DB_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setenv("BR8N_VAULT_PATH", str(tmp_path / "vault"))
    from br8n import settings_file
    from br8n.config import get_config, get_settings

    settings_file.clear_cache()
    get_settings.cache_clear()
    get_config.cache_clear()
    settings_file.save_setting("embedding_provider", "none")
    from br8n.api.main import check

    check()
    out = capsys.readouterr().out
    assert "none" in out and "settings" in out
    settings_file.save_setting("embedding_provider", None)
```

```python
# backend/tests/embeddings/test_fastembed_integration.py
"""Real fastembed — skipped unless the optional extra is installed."""
import pytest

fastembed = pytest.importorskip("fastembed", reason="needs br8n[local-embeddings]")


@pytest.mark.asyncio
async def test_real_model_produces_384_dim_vectors(monkeypatch):
    from br8n.clients import embed_local

    embed_local.reset()
    assert embed_local.installed() is True
    assert embed_local.load_now() is True
    out = await embed_local.embed(["a resume card for the br8n project"])
    assert len(out) == 1 and len(out[0]) == 384
    assert all(isinstance(x, float) for x in out[0][:8])
    embed_local.reset()


@pytest.mark.asyncio
async def test_similar_text_ranks_above_unrelated():
    """Sanity that the vectors carry meaning, not just shape."""
    from br8n.clients import embed_local

    embed_local.reset()
    embed_local.load_now()
    q, near, far = await embed_local.embed(
        ["how do I resume my work", "pick up where I left off", "banana bread recipe"]
    )

    def cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb)

    assert cos(q, near) > cos(q, far)
    embed_local.reset()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/embeddings/test_doctor_embeddings.py -v`
Expected: FAIL — no "embeddings" in the doctor output. (The integration file skips unless the extra is installed; that is the intended state on a default dev box.)

- [ ] **Step 3: Implement the doctor line**

Read `check()` in `backend/br8n/api/main.py` first — it uses `line(status, label, detail)` with statuses `"ok"`, `"FAIL"`, `"warn"`, `"off"`, and only the `python`/`sqlite-vec`/`db path` failures set `ok = False`. Add after the vault block, still inside the local-tier branch, and never touch `ok`:

```python
        # embeddings: which provider is active and whether a refill is draining.
        try:
            from br8n.clients import embed_local
            from br8n.clients.embeddings import active_embedder
            from br8n.store import get_store

            ident = active_embedder()
            conn = get_store()._conn
            pending = (
                conn.execute(
                    "SELECT COUNT(*) AS n FROM findings WHERE needs_embed = 1;"
                ).fetchone()["n"]
                + conn.execute(
                    "SELECT COUNT(*) AS n FROM kg_nodes WHERE needs_embed = 1;"
                ).fetchone()["n"]
            )
            if ident.provider == "none":
                detail = (
                    "no provider (source: %s) — set AI_GATEWAY_API_KEY/OPENAI_API_KEY, "
                    "or pip install 'br8n[local-embeddings]'" % ident.source
                )
                line("warn", "embeddings", detail)
            else:
                detail = (
                    f"{ident.provider} {ident.model} {ident.dim}d "
                    f"(source: {ident.source})"
                )
                if ident.provider == "local" and not embed_local.ready():
                    detail += " — model loading"
                if pending:
                    detail += f", {pending} pending re-embed"
                line("ok" if not pending else "warn", "embeddings", detail)
        except Exception as exc:  # noqa: BLE001 — reporting never fails the doctor
            line("warn", "embeddings", f"unavailable: {exc}")
```

- [ ] **Step 4: Docs and CI**

`backend/.env.example`, in the FREE/local block:

```bash
# Semantic search needs an embedding provider. Either set a key above, or go
# keyless:  pip install 'br8n[local-embeddings]'   (ONNX, ~130MB model, no torch)
# Force a provider without editing env:  /br8n:embeddings  (writes ~/.br8n/settings.json)
# B2_EMBEDDING__PROVIDER=local
```

`README.md`: in the install/keys section, add that `pip install 'br8n[local-embeddings]'` gives semantic search with no API key on the local tier, and that `/br8n:embeddings` reports or switches the provider.

`CLAUDE.md`: in **Conventions**, amend the embedding-key sentence to say that an embedding key gates semantic search *unless* the `local-embeddings` extra is installed, in which case the local tier embeds on-device (bge-small-en-v1.5, 384-dim); add `/br8n:embeddings` to the skills table and `br8n_embeddings_get`/`br8n_embeddings_set` to the MCP tools table; add a Phase status line: `- [x] Local embeddings — keyless semantic search on the local tier; spec docs/truenorth/specs/2026-07-28-local-embeddings-design.md`.

CI: in the workflow that runs the test matrix, install the extra on the 3.12 leg only, e.g. change that leg's install step to `pip install -e ".[dev,local-embeddings]"`, so the real fastembed path is exercised once without slowing every leg.

- [ ] **Step 5: Verify**

```bash
.venv/bin/pytest tests/embeddings -v
.venv/bin/pytest -q
.venv/bin/python -m br8n.api.main --check
```

Expected: embeddings tests pass; suite has no new failures; the doctor prints an `embeddings` line naming the active provider.

- [ ] **Step 6: Lint and commit**

```bash
.venv/bin/ruff check br8n tests
git add br8n/api/main.py .env.example tests/embeddings ../README.md ../CLAUDE.md ../.github
git commit -m "feat(embeddings): doctor line, real-fastembed integration test, docs"
```

---

## Final verification (spec acceptance criteria)

From `backend/`, after Task 7:

1. **Keyless semantic search** — with the extra installed and no key: capture, wait for `br8n_embeddings_get` to report `ready: true` and zero pending, then search and confirm ranked results (spec AC 1).
2. **Provider switch rebuilds** — on a DB holding 1536-dim vectors, `br8n_embeddings_set("local")`, reopen the store, confirm `_declared_vec_dim() == 384` and that the pending count drains to zero on subsequent reads (AC 2).
3. **Live switch** — `br8n_embeddings_set("local")` then `br8n_embeddings_get` reports `source: "settings"` in the same server process (AC 3).
4. **Refusal** — uninstall the extra (or patch `installed()` False) and confirm `set("local")` returns `ok: false` with the pip command and leaves `settings.json` unwritten (AC 4).
5. `.venv/bin/python -m br8n.api.main --check` names the active embedder and pending count (AC 5).
6. **Keyless reindex restores vectors** — delete `brain.db`, run `python -m br8n.vault.reindex` with the extra and no key, then confirm `vec_findings` is non-empty (AC 6 — the End Goal 6 promise).
7. `.venv/bin/pytest -q` green, and with neither extra nor key the behavior is identical to today (AC 7).

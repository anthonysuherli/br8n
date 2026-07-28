# Local embeddings — a keyless free tier

**Date:** 2026-07-28
**Status:** Approved design, pre-implementation
**Vision goals served:** End Goal 6 (Ship the Obsidian-observable KB) — it promises
that derived indexes, *explicitly including embeddings*, stay "rebuildable from what
the user can see". Today that promise breaks on a keyless machine: `python -m
br8n.vault.reindex` restores text but no vectors, so semantic search cannot be
rebuilt from the vault alone. Local embeddings closes that gap. Upholds Invariants
1–3 and 5–6 (non-blocking, fail-silent, zero-friction capture, standalone engine,
existing surfaces keep working).

## Problem

Semantic search requires an embedding credential. Anthropic publishes no embeddings
API, so a Claude Code subscription cannot supply one — the only options are an
OpenAI-compatible key or a local model. Without a key, capture and chronological
surfaces work but `match_findings` returns nothing, and the vault's rebuildability
promise is only half true.

## Decision summary

| Decision | Choice |
|---|---|
| Library | **fastembed** (Qdrant) — 116 KB wheel, only heavy dep is onnxruntime (~19 MB), no torch |
| Model / dim | **`BAAI/bge-small-en-v1.5`, 384 dims**, MIT-licensed, ~130 MB on first use |
| Packaging | **Optional extra** `br8n[local-embeddings]`, auto-used when installed *and* no API key is set |
| Model switch | **One active embedding space.** Store `(provider, model, dim)`; on mismatch recreate the vec tables at the new width, flag rows `needs_embed=1`, drain lazily |
| Manual override | `~/.br8n/settings.json` written by `br8n_embeddings_set`, effective without restarting the MCP server |
| Scope | **Local tier only.** Cloud (Supabase/pgvector, 1536) is untouched |

Rejected alternatives: **sentence-transformers** (drags torch — 111 MB on macOS arm64,
and default Linux resolution can pull CUDA wheels totalling multiple GB); **model2vec**
(near-zero footprint and >20k sentences/sec, but materially weaker retrieval);
**Ollama** (higher quality and zero install weight, but only usable when it happens to
be running — a future opportunistic path, not a dependable fallback).

**Quality honesty.** BGE's model card reports a retrieval-only MTEB average (51.68 for
bge-small-en-v1.5) while OpenAI and Nomic publish whole-benchmark averages (~62.3).
These are not comparable numbers. Do not claim parity with `text-embedding-3-small`
in docs or copy: a present API key still wins, and local is the fallback.

## Architecture

`br8n/clients/embeddings.py` stays the single seam. Every caller — capture, notes,
journal, findings ingest, preamble, activity, concept distill, the KG builder, the
explore API and the MCP server — keeps calling `embed_text` / `embed_batch` /
`embed_with_retry` / `embeddings_configured` with unchanged signatures; all new logic
hides behind that façade.

```
callers (capture, notes, journal, ingest, preamble, activity, distill, …)
   │  embed_batch() / embeddings_configured()
   ▼
br8n/clients/embeddings.py  ── resolves EmbedderIdentity(provider, model, dim)
   ├─ remote  → AsyncOpenAI (AI Gateway or direct OpenAI)   [today's path, 1536]
   ├─ local   → br8n/clients/embed_local.py (fastembed)     [new, 384, lazy import]
   └─ none    → embeddings_configured() False               [today's degraded path]
```

New modules:

- `br8n/clients/embed_local.py` — the fastembed implementation behind a **lazy
  import**, so the package imports cleanly when the extra is absent.
- `br8n/settings_file.py` — read/write `~/.br8n/settings.json` (machine-level user
  state, resolved like `journal_dir()`: parent of `BR8N_DB_PATH` when set, else
  `~/.br8n`).

### Provider selection

Resolved into an `EmbedderIdentity(provider, model, dim)`, in precedence order:

1. `~/.br8n/settings.json` → `embedding_provider` (the user's explicit, most recent act)
2. `B2_EMBEDDING__PROVIDER` env (operator-level, via the existing override mechanism)
3. auto-detect: remote key present → **remote**; else fastembed importable **and**
   `active_backend() == "local"` → **local**; else **none**

The tier guard in step 3 is load-bearing: cloud pgvector columns are 1536 wide, so a
384-dim vector there is a write error. Cloud continues to require a remote key.

`br8n_embeddings_get` always reports which source decided the active identity, so the
resolution is never mysterious.

### Threading and readiness

fastembed is synchronous and CPU-bound, and onnxruntime sizes an intra-op thread pool
per session. Calls therefore run via `asyncio.to_thread` with `intra_op_num_threads`
pinned to `local_threads` (default 1), never on the event loop.

First use downloads ~130 MB. Rather than stall a capture, **`embeddings_configured()`
returns False for the local provider until the model is resident**. A capture during
warm-up stores its finding with `needs_embed=1` (zero-friction capture preserved); the
existing `_re_embed_stale` drain fills the vectors once the model lands. The warm-up
is triggered lazily and best-effort at the first point of need — the first
`embeddings_configured()` call that resolves to a local provider whose model is not
yet resident schedules a fire-and-forget download (and `br8n_embeddings_set` kicks the
same routine), guarded so only one download runs per process. Cached loads pass `local_files_only=True`, which avoids a known
fastembed hang behind firewalls.

## Embedding identity and auto-rebuild

Vectors from different models are not comparable, and a `vec0` table's width is fixed
at creation, so a provider change requires both a new table and a full re-embed.

New single-row table:

```sql
CREATE TABLE IF NOT EXISTS embedding_space (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  provider TEXT NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL,
  updated_at TEXT NOT NULL);
```

`vec_findings` and `vec_kg_nodes` stop being hardcoded at `float[1536]` — their DDL is
built from the active dim.

The comparison runs inside `SQLiteStore._ensure_schema` (so both `SQLiteStore` and
`VaultStore` inherit it, once per store construction), stored identity against active:

- **No stored row (first boot after upgrade):** infer the stored identity from
  reality, not from the active provider — parse the declared width out of
  `sqlite_master`'s `vec_findings` DDL and stamp that. Assuming the active identity
  would mis-stamp a machine whose key was removed (384 recorded while 1536-dim
  vectors sit in the table). Normal mismatch logic then runs.
- **Match:** nothing.
- **Mismatch:** drop and recreate both vec tables at the new width, set
  `needs_embed = 1` on all findings and all `kg_nodes`, stamp the new identity.

That is DDL plus one UPDATE — fast and non-blocking. The expensive part (re-embedding)
is the existing lazy drain. Dropping vectors is safe because they are derived: the
text lives in `findings.content` and, since the VaultStore merge, canonically on disk
in the vault.

`kg_nodes` currently has no `needs_embed` column (node vectors are written at upsert
time), so this design adds one — additive `ALTER`, the same pattern as the vault
columns — and extends `_re_embed_stale` to drain nodes as well as findings, keeping
both spaces coherent after a switch. Node re-embedding uses the node label, matching
what `upsert_kg_nodes` embeds.

## Switching from inside Claude Code

Environment variables are the wrong ergonomics for a long-running MCP server started
with a fixed environment. Two tools mirror the `br8n_notes_policy_get`/`_set`
precedent:

| Tool | Behavior |
|---|---|
| `br8n_embeddings_get` | Returns `{provider, model, dim, source, extra_installed, model_cached, ready, pending_findings, pending_nodes}` — `source` names which precedence level decided it |
| `br8n_embeddings_set(provider)` | Validates `auto\|remote\|local\|none`, writes `~/.br8n/settings.json`, kicks the background warm-up, returns the new state plus the queued rebuild size |

Refusals are explicit, never silent: requesting `local` without the extra returns
`{"ok": false}` with the `pip install 'br8n[local-embeddings]'` line and does not
write the file; requesting `local` on the cloud tier is refused with the 1536-wide
pgvector reason.

Provider resolution reads `settings.json` with an mtime check on each embed, so a
switch **takes effect without restarting the MCP server**. Setting `local` then simply
lets the mismatch detection above run the rebuild at the next store open, with the
drain visible through `br8n_embeddings_get` and `--check`.

A `/br8n:embeddings` skill wraps both tools and is registered in
`.claude-plugin/plugin.json` — a skill on disk that is not listed there never loads.

## Configuration and packaging

`EmbeddingConfig` (config.yaml / `B2_EMBEDDING__*`) gains:

```python
provider: str = "auto"                        # auto | remote | local | none
local_model: str = "BAAI/bge-small-en-v1.5"
local_dim: int = 384
local_threads: int = 1                        # onnxruntime intra_op_num_threads
```

`pyproject.toml` gains:

```toml
[project.optional-dependencies]
local-embeddings = ["fastembed>=0.8"]
```

Documented in `backend/.env.example`, `README.md` (install line), and `CLAUDE.md`.

## Error handling

Every failure degrades, per the fail-silent invariant:

- Extra not installed → provider resolves to `none`; behavior identical to today.
- Model download fails → provider stays un-ready, retried later with bounded logging
  (no per-boot warning spam); captures continue with `needs_embed=1`.
- An embed call raises → unchanged from today (capture stores unembedded; the drain
  logs and returns 0).
- A rebuild that fails partway leaves `needs_embed=1` rows and a stamped identity;
  the next drain resumes. No user-visible break.

`--check` gains an embeddings line: active provider/model/dim, deciding source,
extra installed, model cached, and the pending `needs_embed` count — which doubles as
the visible signal that a rebuild is draining.

## Testing

- **Unit (stub embedder, deterministic vectors):** provider precedence including the
  settings-file override; the tier guard refusing local on cloud; identity
  match/mismatch; vec-table rebuild at a new width; the first-stamp inference rule
  (existing 1536 table with a local-active provider must rebuild, not mis-stamp);
  drain across findings *and* KG nodes; readiness gating `embeddings_configured()`.
- **Tools:** `br8n_embeddings_get` shape and `source` correctness; `set` refusals
  (extra missing, cloud tier) write nothing; `set` round-trips through the file and
  is picked up without a restart (mtime path).
- **Integration (real fastembed):** marked skip-if-not-installed; installed on one CI
  matrix leg so the real embed path, dimension, and offline cached load are genuinely
  exercised.

## Out of scope

- The opportunistic Ollama path (detect a running daemon, prefer `nomic-embed-text`).
- Local embeddings on the cloud tier.
- Matryoshka dimension truncation / one model serving multiple widths.
- Re-ranking, hybrid BM25+vector search.

## Acceptance criteria

1. On a keyless machine with the extra installed, a capture followed by a search
   returns semantically ranked results (not empty, not text-only).
2. Removing an API key on a machine holding 1536-dim vectors rebuilds to 384 and
   refills in the background; no crash, and search recovers without manual action.
3. `br8n_embeddings_set("local")` takes effect without restarting the MCP server, and
   `br8n_embeddings_get` reports `source: "settings"` with the queued rebuild size.
4. `br8n_embeddings_set("local")` without the extra installed returns `ok: false` with
   the install command and leaves `settings.json` unwritten.
5. `python -m br8n.api.main --check` names the active embedder and pending count.
6. `python -m br8n.vault.reindex` on a keyless machine with the extra restores
   *vectors*, not just text — the End Goal 6 promise this feature exists to keep.
7. All existing tests pass unchanged; with no extra and no key, behavior is identical
   to today.

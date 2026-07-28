# Obsidian-observable, markdown-canonical KB (VaultStore)

**Date:** 2026-07-27
**Status:** Approved design, pre-implementation
**Vision goals served:** End Goal 6 (Ship the Obsidian-observable KB) — the KB
becomes plain markdown in a user-owned vault; derived indexes stay rebuildable
from what the user can see. Upholds Invariants 1–3 and 5–6 (non-blocking,
fail-silent, zero-friction capture, standalone engine, existing surfaces keep
working).

## Decision summary

| Decision | Choice |
|---|---|
| Truth model | **Markdown-canonical.** The vault IS the KB; Obsidian edits stick and the engine re-indexes from files. |
| Vault home | **One global vault**, default `~/.br8n/vault/`, relocatable via `BR8N_VAULT_PATH` (e.g. a folder inside an existing Obsidian vault). |
| Canonical set | **Content = files, views = derived.** Canonical: snapshots, notes, journal entries, explore findings. Derived (regenerated md): synopsis, timeline windows, activity-KG rollups. Embeddings/caches: index DB only. |
| Implementation | **VaultStore** — the local tier's `Store` implementation becomes vault + rebuildable SQLite index. Cloud tier (SupabaseStore) unchanged; vault↔cloud sync stays future work. |

## Architecture

The `Store` protocol (`br8n/store/base.py`) remains the engine's single
persistence seam and is not modified. Only the local implementation changes:

```
engine (capture, resume, search, livingdocs, activity KG)
   │ get_store()
   ▼
Store protocol (unchanged)
   ├─ VaultStore (local tier — new, subsumes SQLiteStore)
   │    ├─ ~/.br8n/vault/     canonical markdown (BR8N_VAULT_PATH overrides)
   │    └─ ~/.br8n/brain.db   derived index: sqlite-vec, KG tables, cursors,
   │                          content hashes — fully rebuildable from the vault
   └─ SupabaseStore (cloud — unchanged)
```

New module `br8n/vault/`:

- `layout.py` — vault root resolution, per-type dirs, filesystem-safe segments
  (reuses the `_safe()` convention from `livingdocs/paths.py`).
- `files.py` — frontmatter serialize/parse, atomic write (`.tmp` + `os.rename`),
  SHA-256 content hashing.
- `reconcile.py` — the on-access reconciliation pass (edited / new / deleted
  file detection and index repair).
- `migrate.py` — the idempotent `vault init` importer (existing DB rows and
  legacy md files → vault).

New store implementation `br8n/store/vault.py` (`VaultStore`) implements the
full `Store` protocol. Index-side operations (vector search, KG tables,
exploration rows, tenancy, stamps) reuse the existing SQLite machinery; the
index schema is a superset of today's (adds `content_hash` and `vault_path`
columns to findings). `BR8N_BACKEND=local` selects VaultStore; the prior
files-nowhere SQLiteStore behavior is retired (no compatibility flag).

A `reindex` command (`python -m br8n.vault.reindex`) rebuilds `brain.db` from
the vault from scratch — the architectural proof that the index is derived.

## Vault layout

```
~/.br8n/vault/
  snapshots/<project>/<branch>/2026-07-27-1430-<slug>.md   canonical
  notes/<project>/<branch>/…                               canonical
  journal/<year>/2026-07-27-<slug>.md                      canonical
  findings/<project>/<branch>/…       (explore results)    canonical
  views/                              regenerated; banner: "derived — edits won't stick"
    synopsis/<project>-<branch>.md
    timeline/<project>-<branch>/{recent,week}.md
    activity/…                        KG rollups, wikilinked (graph view)
```

Per-repo `.br8n/docs` and `.br8n/timeline` keep working unchanged (they are
derived views). Canonical note/journal writes move from `.br8n/notes/` and
`~/.br8n/journal/` into the vault; the MCP tool and skill surfaces are
unchanged.

## File format

Every canonical file carries typed YAML frontmatter, Obsidian
Properties/Bases/Dataview-compatible with zero plugin configuration:

```yaml
---
br8n_id: <uuid>            # immutable DB↔file join key; written by the engine
type: snapshot|note|journal|finding
title: "…"                 # round-trips the row title (body H1 is display-only)
project: br8n
kb: main
created: 2026-07-27T14:30:00Z   # ISO-8601, date-typed in Obsidian
tags: [note, agent]
confidence: 0.9
source: agent|human
next_action: "…"           # snapshots/notes, when present
relates_to: "[[…]]"        # optional quoted wikilinks
---

# Title

Body content.
```

Rules: ISO dates; YAML lists for tags; wikilinks in frontmatter always quoted;
frontmatter keys are a small fixed schema (no ad hoc keys from the engine).
Human-added extra keys are preserved on re-serialization.

## Data flow

**Writes (engine → vault).** Every persist (capture, note, journal, explore
merge) is file-first: serialize → atomic write → index (embed, upsert row by
`br8n_id`, store content hash). If indexing fails, the file still exists; the
index self-heals on the next reconcile. Derived views regenerate after writes
using the existing debounced rebuild pattern (timeline/docs).

**Reads (engine ← vault).** Hot reads (vector search, resume tap) hit the
SQLite index at today's speed. Single-item reads (`get_finding`) verify the
file's content hash first and re-index that one file if it changed.

**Reconciliation (Obsidian edits → index).** No daemon, no watcher. At
store-access boundaries (any reading MCP tool call), a debounced pass runs at
most once per interval (cursor + timestamp stored in the index):

1. Fast scan of canonical dirs comparing mtime+size against the index; only
   suspects are hashed (mtime alone is unreliable under iCloud/sync clients).
2. Edited file → re-parse frontmatter, re-embed, update row.
3. New file (typed folder or `type:` frontmatter) → adopted: `br8n_id`
   assigned and written back into frontmatter, embedded, indexed.
4. Deleted file → index row removed; it stops appearing in resume/search.
   Delete in Obsidian is delete.
5. `views/` is exempt — regenerated, never reconciled.

The pass is debounced (default: at most once per 20 s), time-capped (default:
~200 ms of scanning per pass) and batch-bounded (default: 200 suspect files per
pass) with a carry-over cursor; all three are config knobs. No tool call blocks
noticeably. Without an embedding key, adoption indexes text only
(semantic search degrades exactly as the local tier does today).

## Error handling

- Fail-silent contract throughout: reconcile errors, unreadable files, or an
  unwritable vault path degrade to "serve the index as-is"; a capture or
  session is never broken by vault machinery.
- Malformed frontmatter → file skipped and reported by the doctor.
- `--check` doctor gains vault checks: path writability, unparseable files,
  orphan index rows, vault↔index drift count.
- Atomic writes: a crash mid-persist leaves the old or the new file, never a
  torn one.
- Two-writer races (engine vs. sync client): last-write-wins on content; the
  hash pass converges on whichever landed. No locks (single-user local tier).

## Migration

One-time, idempotent `vault init` (also triggered by first VaultStore boot):

- Existing `brain.db` findings export to canonical files (currently 7
  snapshots — trivial volume).
- Existing `.br8n/notes/**/*.md` and `~/.br8n/journal/*.md` are copied into
  the vault with frontmatter added; originals left untouched.
- Index schema migration adds `content_hash`/`vault_path` columns
  (`CREATE TABLE IF NOT EXISTS` / additive `ALTER`, same pattern as today).

## Testing

- **Unit:** frontmatter round-trip (quoted wikilinks, ISO dates, preserved
  human keys), atomic write, hash change detection (edit/new/delete/rename).
- **Contract:** the existing store test suite runs against VaultStore
  unchanged — protocol parity with SQLiteStore is the acceptance bar.
- **Integration:** capture → file exists with valid frontmatter; edit file →
  resume reflects the edit; delete file → absent from search; `reindex` from a
  vault-only state reproduces an equivalent index.
- **Doctor:** clean-vault assertion on CI fixtures.

## Out of scope

- Cloud tier changes and vault↔Supabase sync (future work, alongside the
  other unbuilt cloud value props).
- Canonical markdown for activity-KG nodes (KG stays index-side, projected as
  wikilinked rollup views).
- Any Obsidian plugin; the vault must be pleasant with a stock Obsidian
  install.

## Acceptance criteria

1. With `BR8N_BACKEND=local`, a capture produces a canonical snapshot file in
   the vault whose frontmatter parses in Obsidian, and resume/search work
   unchanged.
2. Editing a note's body in Obsidian changes what `br8n_resume`/`br8n_search`
   return on the next call; deleting the file removes it from results.
3. A note created by hand in `notes/<project>/<branch>/` is adopted (gains
   `br8n_id`) and becomes searchable.
4. Deleting `brain.db` and running `reindex` restores full search/resume from
   the vault alone (semantic search requires an embedding key at reindex time;
   without one, text indexing still restores).
5. `python -m br8n.api.main --check` reports vault health; all existing store
   tests pass against VaultStore.

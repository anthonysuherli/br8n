---
name: embeddings
description: Inspect or switch how br8n embeds text for semantic search — remote (API key) or local (keyless, on-device ONNX). Use when the user asks why search returns nothing, wants search to work without an API key, wants to stop paying for embeddings, or asks which embedding model br8n is using.
---

# br8n — Embeddings (which provider, and switching it)

Semantic search needs vectors. br8n produces them one of three ways:

| Provider | When it applies | Dim |
|---|---|---|
| `remote` | `AI_GATEWAY_API_KEY` or `OPENAI_API_KEY` is set | 1536 |
| `local` | the `br8n[local-embeddings]` extra is installed, no key is set, and the KB is on the **local tier** — cloud pgvector columns are 1536-wide, so `local` always refuses on cloud | 384 |
| `none` | neither — capture and chronological surfaces still work, search is text-only | — |

## Step 1 — Report the current state

Call `mcp__plugin_br8n_br8n__br8n_embeddings_get()`. Lead with the provider,
model and *why* it was chosen (`source`), then flag anything actionable:

- `ready: false` with `provider: local` → the model is still downloading
  (~130 MB, first use only). Search stays text-only for a minute; nothing is lost.
- `pending_findings`/`pending_nodes` above zero → a re-embed is draining in the
  background. It refills on ordinary reads; no action needed.
- `provider: none` → say what would fix it: either set a key, or
  `pip install 'br8n[local-embeddings]'` and switch to local.
- `pending_switch` not `null` → the environment quietly changed (e.g. a key
  went missing) and would flip the space (`pending_switch.stored` →
  `pending_switch.detected`), but existing vectors are at risk, so br8n left
  them alone instead of rebuilding. Tell the user what changed and offer to
  apply it — that offer is exactly Step 2.

## Step 2 — Switch, if asked

Call `mcp__plugin_br8n_br8n__br8n_embeddings_set(provider)` with `auto`,
`remote`, `local` or `none`. `auto` is the default and means "use a key if
there is one, else local". To actually *apply* a deferred `pending_switch`,
pass the concrete `detected.provider` from Step 1 — that always rebuilds
immediately. Passing `auto` instead re-runs detection but is still subject to
the same work-at-risk gate, so if the vectors it would discard are still
there it defers again (`{ok: true, deferred: true, ...}`) rather than forcing
the rebuild through.

The change lands in `~/.br8n/settings.json` and applies to the next call — **no
MCP server restart**. If the tool returns `ok: false`, relay `error` and, when
present, run nothing yourself: show the user the `fix` command.

## What a switch costs

Vectors from different models are not comparable, so an explicit switch via
this tool rebuilds the index in place: br8n resizes the vector tables, drops
the old vectors, and re-embeds in the background from content it already has.
`queued_rebuild` in the response says how many rows were just flagged. Search
quality dips until the drain finishes, then recovers — nothing is lost,
because the text is canonical (in the DB and, on the vault tier, on disk).

An **auto-detected** environment change (no `br8n_embeddings_set` call — the
key just came or went) behaves differently once real vectors exist: br8n
does **not** rebuild on its own. Discarding a corpus of vectors is not a
decision to make silently mid-flow, so the space is left exactly as it is and
the pending switch surfaces via `pending_switch` (Step 1) / the `--check`
doctor line instead. The only case that still rebuilds with no confirmation
is a fresh install with nothing to lose (an empty vector table), which is
what keeps a brand-new keyless machine zero-interaction.

Calling this tool with `auto` is what applies that offer — but the same
work-at-risk gate still governs it: if `auto` currently resolves to a
different space than what's stored *and* real vectors are present, the tool
**defers instead of forcing the rebuild through**. That response looks like
`{ok: true, deferred: true, ...}`, still carries `pending_switch` (unchanged
from Step 1 — nothing was applied), and the setting is saved as `"auto"`
exactly as asked; nothing is rolled back. This is success, not failure — it
means "auto" was recorded, but the space itself is still waiting on an
explicit `remote`/`local`/`none` (or a repeat `auto` call once the vectors
are no longer at risk, e.g. after `python -m br8n.vault.reindex`). Passing a
**concrete** provider (`remote`/`local`/`none`) always rebuilds immediately
when it changes the space — the gate only ever defers `auto`.

If the rebuild itself genuinely fails partway (a locked DB, a transient I/O
error — distinct from a deliberate defer), the tool does not report success
on a half-done switch: it rolls the setting back to whatever it was before
the call, so the provider and the index stay consistent — never a persisted
setting pointing at an index that was never actually rebuilt. That case
returns `{ok: false, error, fix}`, with `fix` suggesting a retry or
`python -m br8n.vault.reindex`.

Do not tell the user local embeddings match the remote model's quality. They
are a good keyless fallback; a configured key is still the better path.

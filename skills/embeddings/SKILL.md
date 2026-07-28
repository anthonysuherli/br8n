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

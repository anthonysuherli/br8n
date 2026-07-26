<!-- mcp-name: io.github.anthonysuherli/br8n -->

# br8n

Context-capture and resume engine — captures developer intent on interruption and
replays it as a resume card, so picking work back up costs seconds instead of
minutes.

This package is the br8n backend: the FastAPI service, the MCP server that Claude
Code talks to, and the local/cloud storage tiers behind them.

Most people should install br8n as a [Claude Code plugin](https://github.com/anthonysuherli/br8n)
rather than by hand — the plugin bootstraps this package for you:

```
/plugin marketplace add anthonysuherli/br8n
/plugin install br8n@br8n
```

## Direct install

```bash
pip install br8n
```

Two entry points are installed:

- `br8n-mcp` — the MCP server (stdio), for wiring into an MCP client by hand.
- `br8n-server` — the local HTTP API. Binds `127.0.0.1:8002`; on the local tier it
  refuses any non-loopback host, because that tier runs without API auth.

Check the install with:

```bash
python -m br8n.api.main --check
```

## Tiers

br8n ships one engine with two storage backends, selected by `BR8N_BACKEND`:

- **local** (default when no Supabase credentials are present) — SQLite +
  `sqlite-vec` at `~/.br8n/brain.db`, single user, loopback only, no auth.
- **cloud** — Supabase (pgvector + GoTrue), multi-user, per-request JWT tenancy.

Capture and resume work with no API keys at all. Semantic search needs an
embedding key (`AI_GATEWAY_API_KEY` or `OPENAI_API_KEY`); the explore/gap-fill
pipeline additionally needs `TAVILY_API_KEY`. See `.env.example` in the repository
for the full matrix.

Source, issues, and documentation: https://github.com/anthonysuherli/br8n

MIT licensed.

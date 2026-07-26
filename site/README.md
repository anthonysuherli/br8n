# br8n — marketing site

A small static, no-build site: plain HTML pages sharing one stylesheet (`styles.css`)
and one script (`site.js`). No framework, no bundler. Drop the folder on any static
host or open the pages directly.

## Pages

| File | Role |
|---|---|
| `index.html` | **Landing** — hero + live resume card, the tax, the two-step loop (teaser), a compact tiers strip (free-led, cloud "soon"), a slim install CTA. Concise; links out to the deeper pages. |
| `how-it-works.html` | **How it works** — the resume card in context, then the knowledge engine (capture → distill → map → tap) and the live preamble artifact. |
| `access.html` | **Access anywhere** — the surfaces grid: HTTP API / Claude Code / MCP shipping; Teams / Discord "coming soon". |
| `install.html` | **Install & quickstart** — Free/Local and Cloud(soon) tabs; the Claude Code plugin (primary path), the capture→pickup loop, optional keys, `--check`, and the direct API / MCP server entry points. |

Shared chrome (nav, footer) is duplicated across pages by hand — at four pages that's
cheaper than a build step. The nav's active page is marked with `class="active"`.

## Design

Implemented from the Claude Design handoff `br8n — Terminal.html` (Direction A,
"Terminal"). Baked-in defaults:

- **Accent** — Lakers gold `#FDB927`, with purple `#552583` woven in structurally
  (top stripe, hero/page-head glow, washed alternating sections, the featured tier's glow).
- **Headline** — "Nine and a half minutes, or thirty seconds."
- **Purple wash** + **dot grid** — on.
- **Type** — JetBrains Mono throughout (Google Fonts).

## Honesty rule

The cloud tier and its paid value props (sync, cross-repo, teams, managed keys) are
**designed, not built** — the site leads free-first and chips every roadmap surface
"coming soon", never as shipping. Keep it that way when editing.

Same rule for keys: capture and resume genuinely need **no key**, but semantic search
needs an embedding key (`AI_GATEWAY_API_KEY` or `OPENAI_API_KEY`) and explore also needs
`TAVILY_API_KEY`. Never flatten that into a blanket "no key needed".

## Interactive (`site.js`)

- Copy buttons on every install line (clipboard with `execCommand` fallback).
- Coverage chips cycle `rich → sparse → gap` on click.
- Tier tabs on the install page (Free/Local ↔ Cloud).

## Local preview

```bash
python3 -m http.server -d site 8080   # http://127.0.0.1:8080
```

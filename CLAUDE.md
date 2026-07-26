# br8n

Context-capture and resume engine — cuts the context-rebuild tax after an interruption
by capturing developer intent and replaying it as a 30-second resume card.

**On the "9.5 minutes" figure:** it appears in the README, the site hero, and
`skills/pickup/SKILL.md`, but nothing in this repo sources it — `docs/launch/ws6`
concedes it "is an assumption." The sourced numbers are ~23 minutes to refocus after
an interruption (Gloria Mark, UC Irvine) and up to 45 for complex code. `ws1` sets the
rule: cite the 23-minute figure, don't invent ROI claims. Fix or source the 9.5 before
using it in launch copy.

## Design philosophy

**Non-blocking by default.** br8n must never hijack the user's turn to do its own
work. The user's flow is sacred; br8n's work happens *around* it. Every feature that
isn't a direct response to a user command should be built to this principle:

- **Work in the background.** Context collection (seeding, enrichment, distillation,
  graph population) runs as background agents — `Agent(run_in_background)` for
  orchestrated multi-step work, fire-and-forget hooks for cheap structural passes.
  Prefer **agent teams** (parallel subagents, each owning one slice — repo crawl, git
  history, web enrichment, schema drafting) over one long serial pass, so the wall-clock
  cost is the slowest slice, not the sum. Precedents: the activity-KG fire-and-forget
  population on capture (`api/capture.py`), the first-run-init background subagent
  (`docs/plans/2026-06-01-br8n-first-run-init-design.md`).
- **Gate decisions at turn boundaries, never mid-flow.** Anything that genuinely needs
  the user — schema creation / KG ontology co-design, creating a new KB, a destructive
  or expensive choice — waits until the background work is *ready*, then surfaces a
  single one-line offer at the next turn boundary ("Schema draft is ready — set it up?").
  Never a modal mid-edit, never a blocking prompt the user didn't ask for. The user opts
  *in* to the interactive step; until they do, br8n stays quiet and keeps working.
- **Best-effort, fails silent.** Background work never breaks the user's session. A
  failed seed, an unreachable backend, a graph error — all degrade to "do nothing
  visible" and fall through to an on-demand recovery path. Fail closed, stay invisible.
- **Offer once, then go quiet.** A gated offer that's declined or ignored is not
  re-raised every launch; stamp it and move on (the capability stays available on
  demand). Don't nag.

When designing or implementing any context-gathering feature, ask: *can this run in the
background as an agent team, and defer the human decision to a turn-boundary offer?* If
yes, that's the default shape.

## Architecture

br8n is a self-contained fork of the Delapan engine. The core engine files
(config, clients, agent/preamble, agent/synopsis, findings, kbs, projects, monitoring)
are copied from `../delapan/backend/delapan/` with all imports renamed from
`delapan.*` to `br8n.*`. Do NOT add a cross-repo import dependency — keep
br8n fully standalone.

### What's new (br8n-specific)

- `br8n/capture/` — WorkspaceSnapshot model, snapshot→Finding adapter, persist service
- `br8n/knowledge_graph/` — the **activity KG**: per-user, cross-repo work graph
  (`models.py` = node/edge carriers ported from delapan; `activity.py` = seeded
  ontology + deterministic-plus-gated-LLM extraction, fire-and-forget population on
  capture, and the query/rollup/stats read surfaces)
- `br8n/livingdocs/` — the notes → distilled-docs spine: an append-only per-repo+branch
  note stream, a journal, a per-KB capture policy, and a debounced cursor-driven
  distill that rolls notes up into `.br8n/docs/`. Surfaces: `/br8n:notes`,
  `/br8n:journal`, `/br8n:docs`
- `br8n/api/` — FastAPI: `/v1/capture`, `/v1/resume/{project}/{kb}`, `/v1/explore/...`,
  `/v1/activity/{graph,stats}`, `/v1/chat`
- `br8n/interfaces/mcp/server.py` — 23 MCP tools across capture/resume, living docs,
  and the KG schema seam (full table in the MCP tools section below)
- `ios-app/` — native SwiftUI companion (first standalone UI). v1 = read spine:
  Sign in, browse cross-repo activity, read resume cards. Consumes `/v1/projects`
  + `/v1/resume?format=json` + `/v1/activity/stats`. XcodeGen project (`project.yml`,
  no checked-in `.xcodeproj`). Design: `docs/plans/2026-05-30-ios-companion-design.md`
- `skills/` — Claude Code plugin skills (built; see Plugin section below)
- `.claude-plugin/` — plugin manifest + marketplace; root `.mcp.json` wires the br8n
  MCP server through `bin/br8n-mcp`
- `bin/` — `br8n-python` (resolves an interpreter that can run br8n; exits 127 rather
  than bootstrapping, so hooks never block) and `br8n-mcp` (the MCP launcher, the one
  place allowed to bootstrap `~/.br8n/venv` on first run)

### What's reused from the fork

- `config`, `clients/*`, `agent/preamble`, `agent/synopsis`, `agent/state` — preamble + coverage banding
- `findings/*`, `kbs/*`, `projects/*` — KB CRUD + finding persistence
- `exploration/*` — the gap-fill pipeline (plan→search→crawl→extract→merge)
- `monitoring/recorder.py` — access recording (only `recorder.py`, not the full module)
- `interfaces/mcp/tenancy.py` — name→tenant resolution

### What's excluded from the fork (not needed)

- `agent/graph.py` — LangGraph chat agent
- `api/agent.py`, `api/public.py`, `api/internal.py` — chat + deploy surfaces
- `research/` — HTML report generation
- `knowledge_graph/` (generic KG) — the delapan build_graph/propose_schema/extractor
  machinery (user-curated ontology over all findings) stays excluded. br8n's
  `knowledge_graph/` ports only `models.py` and adds its own **activity** graph; the
  generic finding→graph builder is not needed by the capture/resume loop.
- `monitoring/service.py`, `monitoring/report.py` — only `recorder.py` is copied

## Storage tiers

br8n ships in two tiers from one engine. The split is **storage + identity**;
all engine logic (capture, preamble/coverage, synopsis, exploration) is shared and
never touches a storage client directly — it calls `store = get_store()`.

- **`br8n/store/`** — `Store` protocol (`base.py`) with two implementations:
  - `SQLiteStore` (free/local) — SQLite + `sqlite-vec`, single synthetic
    `org_id="local"`, no auth/no Supabase/single device. DB at `~/.br8n/brain.db`
    (override `BR8N_DB_PATH`); `_ensure_schema()` runs `CREATE TABLE IF NOT EXISTS`
    on first open. Cached as a per-`db_path` singleton (one reused connection).
  - `SupabaseStore` (paid/cloud) — wraps today's `service_client()` / `user_client()`
    calls (GoTrue + pgvector + RLS). Built fresh per request (carries the per-request
    `access_token` for RLS scope).
  - The protocol also carries the **activity-graph** surface (`upsert_kg_nodes`,
    `upsert_kg_edges`, `match_kg_nodes`, `get_kg_subgraph`, `list_kg_nodes`,
    `kg_stats`). SQLite adds `kg_nodes`/`vec_kg_nodes`/`kg_edges` tables; Supabase
    reuses delapan's existing `kg_nodes`/`kg_edges` + `match_kg_nodes` RPC. Node
    dedupe is exact `(kb_id, type, label)`; stored label embeddings power only
    semantic subgraph seeding, never dedupe.
- **Selection** — `active_backend()` / `get_store()` read `BR8N_BACKEND`
  (`"local"` | `"cloud"`); if unset, cloud iff Supabase creds present, else local.
  `active_backend()` is importable from `br8n.store`.
- **Tenancy fork** — `resolve_tenant` (mcp/tenancy.py) forks on `active_backend()`:
  local skips GoTrue (`user_id="local"`, `access_token=""`, `org_id="local"`); cloud
  does the GoTrue login for a real JWT. `TenantContext` shape is identical on both.
- **Auth fork** — `api/auth.py::require_api_key` is a no-op on the local tier
  (loopback-only, single user — no `BR8N_API_KEY` needed) and validates the Bearer
  key exactly as before on cloud. Safe only because the local server binds 127.0.0.1.
- **Per-user auth (cloud, multi-tenant)** — `api/auth.py::require_principal` is the
  per-request identity dependency the `/v1` data endpoints now use: local tier returns
  a fixed `Principal("local","local","")`; cloud verifies the request's Supabase GoTrue
  JWT (HS256 vs `SUPABASE_JWT_SECRET`, `aud="authenticated"`), derives the org via
  `_org_for_or_create(sub)`, and threads `(user_id, org_id, access_token)` through
  `resolve_tenant(..., principal=…)` → `get_store(token, org_id=…)` so every read/write
  scopes to the caller's own org (no `_login()` on the request path). The MCP server keeps
  the legacy single-configured-user path (`resolve_tenant(principal=None)` → `_login`).
  `BR8N_API_KEY` is now a service-only key, not a user identity. The phone obtains its
  JWT via `POST /v1/auth/apple` (Supabase `sign_in_with_id_token`) and rotates it via
  `POST /v1/auth/refresh`. Activity-KG reads/writes are org-scoped in `SupabaseStore`
  (closing the prior service-client RLS bypass).
- **Settings** — Supabase/server creds are **optional**, so the local tier boots with
  none. `service_client()` raises clearly only if the cloud path is hit creds-less.

**Cloud value props (FUTURE — not built).** Cloud MVP today = the Supabase backend
behind `SupabaseStore` + the existing GoTrue login. The paid differentiators —
(1) managed keys, (2) cross-machine sync, (3) cross-repo search, (4) team sharing —
are designed (see `docs/plans/2026-05-29-br8n-tiers-design.md`) but not yet
implemented. Do not document them as shipping.

## Setup

This is the **contributor** path (working on br8n itself). To *use* br8n, install the
Claude Code plugin instead — see the Plugin section below; it bootstraps its own
environment.

`uv` is the intended toolchain, but it may not be installed — the venv reality is
plain `python3.11 -m venv`. `sqlite-vec` is a declared dependency, so it installs
with the package (don't pass it by hand).

```bash
cd backend
python3 -m venv .venv             # any python >= 3.11; or: uv venv
.venv/bin/pip install -e ".[dev]"   # or: uv sync
cp .env.example .env              # pick the FREE or PAID block (see file)
```

Verify an install (yours or a user's) with the doctor — it reports python version,
storage tier, `sqlite-vec` load, DB-path writability, and which capabilities the
present credentials unlock:

```bash
python -m br8n.api.main --check
```

br8n is published on PyPI as `br8n`, which installs the same code plus two console
entry points (`br8n-mcp`, `br8n-server`). That is the path for wiring the MCP server
into a non-Claude-Code client; contributors want the editable install above.

**Free / local tier** (SQLite, no Supabase, no server API key, loopback-only).
Capture and resume work with no keys at all; semantic search needs an embedding key
(`AI_GATEWAY_API_KEY` or `OPENAI_API_KEY`), and explore additionally needs
`TAVILY_API_KEY`:

```bash
BR8N_BACKEND=local python -m br8n.api.main   # blessed launcher: enforces loopback (auth is off)
BR8N_BACKEND=local python -m br8n.interfaces.mcp.server
```

**Paid / cloud tier** (today's Supabase backend; needs the cloud .env block):

```bash
uvicorn br8n.api.main:app --reload --port 8002
python -m br8n.interfaces.mcp.server          # add to .claude/settings.json
```

## Conventions

- Imports: always `from br8n.*` — never `from delapan.*`
- Engine updates: when syncing engine improvements from delapan, apply them manually
  (copy file, rename imports). Do NOT re-introduce a cross-repo dep.
- Auth: cloud tier uses a pre-shared API key (`BR8N_API_KEY` in .env, passed as a
  Bearer token by clients); local tier requires no br8n auth key (loopback-only, see
  Storage tiers). Separate from auth: an embedding key (`AI_GATEWAY_API_KEY` /
  `OPENAI_API_KEY`) is what gates semantic search, and `TAVILY_API_KEY` gates explore —
  capture/resume run without either.
- Supabase (cloud tier): same instance and schema as delapan (no migration divergence)
- KB naming: project = workspace folder name, kb = git branch name

## Phase status

- [x] Phase 0 — Engine fork (Supabase, pgvector, Finding, preamble/tap)
- [~] Phase 1–2 — VS Code extension (capture triggers, resume-card webview, explore
  seam) — **removed** (not the right surface for now; the Claude Code plugin + iOS
  app are the active surfaces). The backend HTML resume card (`api/resume.py`) stays.
- [x] Phase 3 — Always-open explore seam (gap-band → explore pipeline → auto-refresh card)
- [x] Storage tiers — `Store` protocol + `SQLiteStore`/`SupabaseStore`, `get_store()`/
  `active_backend()` selection, free-tier local entry points (no-auth loopback API +
  local MCP). Paid value props (sync/cross-repo/managed-keys/teams) remain future.
- [x] Activity KG — per-user, cross-repo work graph that auto-populates on every
  capture (deterministic structural pass + gated LLM task distillation), on both
  tiers via the `Store` graph surface. Surfaces: `br8n_activity` MCP tool,
  cross-repo rollup on the resume card, `/v1/activity/{graph,stats}`. Best-effort:
  a graph failure never breaks a capture. Gates: `BR8N_ACTIVITY_KG` (master,
  default on), `BR8N_ACTIVITY_LLM` (task pass, default on — off = deterministic
  only). Design: `docs/plans/2026-05-30-activity-kg-design.md`.
- [x] Activity timeline — per-repo+branch append-only chronological log of
  notes+captures+journal. `all-time.md` (append-only) + regenerated `recent.md`/
  `week.md` window views at `.br8n/timeline/`. Background, debounced, cursor-driven
  (mirrors the doc-tree distill); surfaces: `br8n_timeline` MCP tool + `/br8n:timeline`
  skill. Gates: `BR8N_TIMELINE` (master), `BR8N_TIMELINE_LLM` (window day-headers).
  Design + plan: `docs/plans/2026-06-07-timeline-{design,plan}.md`.
- [x] Multi-user cloud auth (backend) — per-request Supabase GoTrue JWT drives
  tenancy: `require_principal` verifies the JWT and provisions the org;
  `resolve_tenant(principal=…)` injects the caller's org/token into the `Store`
  (no `_login()` on the request path); activity-KG reads/writes are org-scoped
  (closing the prior service-client RLS bypass). `POST /v1/auth/apple`
  (Supabase `sign_in_with_id_token`) + `POST /v1/auth/refresh` mint/rotate the
  session; `BR8N_API_KEY` is now service-only; the MCP server keeps the legacy
  single-configured-user path. Design + plan:
  `docs/plans/2026-05-30-multiuser-deploy-auth-{design,plan}.md`. Remaining
  (external): Fly.io deploy, Supabase Apple provider config, iOS sign-in wiring.
- [x] Distribution — br8n is installable by someone other than the author.
  `bin/br8n-mcp` bootstraps its own environment so `.mcp.json` carries no machine
  paths; `sqlite-vec` is a declared dep; hooks resolve an interpreter instead of
  calling a bare `python`; `--check` is a real doctor; CI (ruff + doctor + suite on
  3.11/3.12 + a `twine check`ed build) gates every push. Released as **v1.1.1** and
  published to PyPI as `br8n`. Three working install paths: plugin marketplace
  (supported), `pip install br8n`, and `uvx --from br8n br8n-mcp`.
- [ ] Dogfooding — br8n is **not yet capturing its own development**. The local
  store holds 7 snapshots total and `br8n/main` has zero, so `/br8n:pickup` on this
  repo returns an empty card. The capture loop needs to run habitually here before
  the resume card can be demoed or trusted.

## API surface

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check |
| `/v1/auth/apple` | POST | Exchange an Apple `identityToken` for a Supabase session (un-gated; Supabase verifies + provisions the user) |
| `/v1/auth/refresh` | POST | Rotate a GoTrue session via the refresh token (un-gated proxy; keeps the anon key server-side) |
| `/v1/capture` | POST | Save a workspace snapshot as a Finding |
| `/v1/resume/{project}/{kb}` | GET | Tap KB → resume card. `?format=html` (default, webview) or `?format=json` (native, structured: hypothesis/snapshots/synopsis/activity/coverage/preamble) |
| `/v1/projects` | GET | Discovery: caller's repos+branches with last-activity + snapshot-count chips (the iOS home screen; `Store.list_projects`) |
| `/v1/explore/{project}/{kb}` | POST | Start gap-fill pipeline (returns exploration_id) |
| `/v1/explore/{id}/status` | GET | Poll exploration progress + results |
| `/v1/activity/graph` | GET | Query the cross-repo activity graph (semantic `q`, optional `repo`) |
| `/v1/activity/stats` | GET | Activity-graph totals + hotspots (most-touched repos/files/tasks) |
| `/v1/chat` | POST | Grounded funnel chat agent (SSE stream). Taps the maintained agent KB via `select_preamble`, stateless (history rides in the body). Gated by `BR8N_CHAT`, default on |

## MCP tools (for Claude Code)

| Tool | Purpose |
|---|---|
| `br8n_capture` | Persist a workspace snapshot |
| `br8n_resume` | Tap KB → resume card (preamble + coverage) |
| `br8n_projects` | List every repo+branch you've captured to (powers the `/br8n:pickup` selector) |
| `br8n_explore` | Run gap-fill pipeline synchronously (blocks ~1-3 min) |
| `br8n_activity` | Query the cross-repo activity graph (subgraph + NL summary) |
| `br8n_timeline` | (Re)build the append-only activity timeline (`.br8n/timeline/`) from notes+captures+journal |
| `br8n_kb_exists` | Does this repo+branch KB exist yet? (first-run gate) |

Living docs — notes, journal, distillation:

| Tool | Purpose |
|---|---|
| `br8n_note` | Append a note to the repo+branch note stream |
| `br8n_notes_policy_get` / `br8n_notes_policy_set` | Read/write the per-KB note-capture policy |
| `br8n_journal` | Append a journal entry |
| `br8n_journal_recent` / `br8n_journal_search` | Read the journal chronologically / by query |
| `br8n_distill` | Roll notes up into the distilled doc tree (debounced, cursor-driven) |

Knowledge graph — the schema seam:

| Tool | Purpose |
|---|---|
| `br8n_graph` | Query the KB's knowledge graph (subgraph around a seed) |
| `br8n_build_graph` | (Re)build the graph from findings |
| `br8n_kg_stats` | Node/edge totals, counts by type and relation |
| `br8n_get_kg_schema` / `br8n_set_kg_schema` | Read/write the KB's ontology |
| `br8n_propose_kg_schema` | Draft an ontology from what's been collected (the wizard's input) |
| `br8n_schema_drift` | Decide cold-start vs drift — drives the gated `/br8n:schema` offer |
| `br8n_mark_init_offered` / `br8n_mark_drift_offered` | Stamp an offer so it is not re-raised ("offer once, then go quiet") |

## Plugin (Claude Code skills)

br8n ships as a Claude Code plugin (the primary developer surface). Skills live
in `skills/` and follow the Delapan pattern — Markdown files that direct how
Claude Code behaves for each slash command, calling the `br8n_*` MCP tools.

```
skills/
  _shared/preamble-first.md   shared grounding convention (calls br8n_resume)
  pickup/SKILL.md             /br8n:pickup — the "where I was" card + cross-repo selector
  capture/SKILL.md            /br8n:capture — save current context + background-sweep every other live CC session (live_sessions.py)
  search/SKILL.md             /br8n:search <q> — grounded answer from session KB
  explore/SKILL.md            /br8n:explore <p> — force the gap-fill pipeline
  activity/SKILL.md           /br8n:activity <q> — query the cross-repo activity graph
  timeline/SKILL.md           /br8n:timeline — the append-only activity log (recent/week/all-time)
  notes/SKILL.md              /br8n:notes — append to / read the repo+branch note stream
  journal/SKILL.md            /br8n:journal — append to / search the journal
  docs/SKILL.md               /br8n:docs — the distilled doc tree built from notes
  schema/SKILL.md             /br8n:schema — design or reshape the KG ontology (the one human-in-the-loop seam)
```

All ten are registered in `.claude-plugin/plugin.json`; a skill on disk that is
not listed there never loads for an installed user.

**Target resolution** (simpler than Delapan — no active-KB state):
- `project` = current git repo name (`git rev-parse --show-toplevel` basename)
- `kb` = current git branch

Status: **built** — MCP tools, skill Markdown files, plugin manifest
(`.claude-plugin/plugin.json`), local dev marketplace
(`.claude-plugin/marketplace.json`), and the root `.mcp.json` (documented
`{"mcpServers": {...}}` plugin format) all exist. Claude Code currently accepts a
bare `{server: {...}}` mapping too — the official marketplace contains both shapes
— but `mcpServers` is the documented form, so that is what we ship.

**Load it** (marketplace → install → enable; reload the session after). This is the
primary install path — no venv, no path editing: the plugin's MCP server bootstraps
its own environment on first run.

```
/plugin marketplace add anthonysuherli/br8n
/plugin install br8n@br8n
```

The `.mcp.json` launches the MCP server through a bootstrap wrapper, which creates
and populates its own environment on first run — no manual venv or `uv sync` needed
to *use* the plugin. (Contributors working on the engine still want the `backend/`
editable install from the Setup section above.)

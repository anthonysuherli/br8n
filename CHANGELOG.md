# Changelog

All notable changes to br8n are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] — 2026-07-26

The install-it-yourself release. v1.0.0 was public but not actually installable by
anyone else: the MCP server pointed at a path inside the maintainer's home
directory, a hard dependency was missing from the package metadata, and the docs
led with a command that did not exist. This release makes a clean clone work.

### Added

- `bin/br8n-mcp` — portable MCP launcher. Resolves an interpreter from
  `$BR8N_PYTHON`, a checkout's `backend/.venv`, or `~/.br8n/venv`, and bootstraps
  that venv on first run when none exists. No path editing, no manual venv.
- `bin/br8n-python` — the interpreter resolver hooks use. Exits 127 rather than
  bootstrapping, so a session hook can never block on a venv build.
- `python -m br8n.api.main --check` — a doctor that reports python version,
  storage tier, `sqlite-vec` loadability, DB-path writability, and which
  capabilities your credentials unlock. Previously documented but unimplemented.
- Console entry points `br8n-mcp` and `br8n-server`.
- GitHub Actions CI: ruff, the doctor, and the test suite on Python 3.11 and 3.12,
  plus an sdist/wheel build validated with `twine check`.
- `backend/tests/conftest.py` pins `BR8N_BACKEND=local`, so a fresh clone can run
  `pytest` without knowing that variable exists, and a developer's real Supabase
  credentials cannot silently redirect the suite at the cloud tier.
- Regression tests covering capture with and without an embedding credential.

### Changed

- **Capture no longer requires an embedding API key.** With no credential the
  snapshot is stored unembedded — it still appears on the resume card and in
  chronological surfaces, and only semantic search is unavailable until a key is
  set. A configured-but-failing key still raises, so real outages stay loud.
- The distribution is now named `br8n` (was `br8n-backend`), making the long-
  advertised `pip install br8n` correct once published.
- `.mcp.json` uses `${CLAUDE_PLUGIN_ROOT}` instead of an absolute path into one
  machine's home directory.
- Hooks invoke the resolved interpreter instead of a bare `python`, which on a
  stock macOS install was either absent or a system python without br8n — the
  plugin appeared installed and silently did nothing.
- `.claude/settings.json` ships `extraKnownMarketplaces` for collaborators
  instead of a statusline command pointing at a nonexistent path. The statusline
  is still installed at runtime by the SessionStart hook.
- Documentation and site now describe commands that exist: `/br8n:pickup`
  (there is no `/br8n:resume`), port 8002, and the accurate split between what
  works with no key and what needs one.

### Fixed

- `sqlite-vec` is declared as a dependency. It is imported at module scope by the
  default local backend, so `pip install` previously produced an install that
  raised `ImportError` on first use unless the extra was passed by hand.
- The `timeline` skill is registered in `plugin.json`; `/br8n:timeline` was
  documented but never loaded for anyone who installed the plugin.
- `plugin.json` declares a version, and the plugin and marketplace manifests
  agree on it.
- The MCP launcher's interpreter probe imports a module that pulls the real
  dependency tree, and probes from a neutral directory — a bare `import br8n`
  succeeded against a source checkout on `sys.path` that had no dependencies
  installed, selecting a python that could not actually run the server.

## [1.0.0] — 2026-07-18

First public tag. Capture/resume engine, activity knowledge graph, activity
timeline, local (SQLite) and cloud (Supabase) storage tiers, Claude Code plugin
with skills, MCP server, and the iOS companion read spine.

[1.1.0]: https://github.com/anthonysuherli/br8n/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/anthonysuherli/br8n/releases/tag/v1.0.0

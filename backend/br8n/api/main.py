"""br8n FastAPI application entry point.

Free (local) tier — the blessed launcher enforces loopback binding because the
local tier disables API auth (see ``br8n.api.auth``)::

    python -m br8n.api.main

It binds 127.0.0.1:8002 by default; override with ``BR8N_HOST`` / ``BR8N_PORT``.
On the local tier ``run()`` refuses any non-loopback host, so the unauthenticated
API can never be exposed on a public interface.

Cloud (paid) tier — auth is enforced by ``BR8N_API_KEY``, so raw uvicorn is fine::

    uvicorn br8n.api.main:app --reload --port 8002
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from br8n.api import activity, apple_auth, capture, chat, explore, health, projects, resume
from br8n.config import get_settings
from br8n.store import active_backend

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


_warned_local = False


def create_app() -> FastAPI:
    global _warned_local
    settings = get_settings()
    if active_backend() == "local" and not _warned_local:
        _warned_local = True
        logger.warning(
            "local tier: API auth is DISABLED; ensure loopback binding."
        )
    app = FastAPI(
        title="br8n",
        version="0.1.0",
        description="Context-capture and resume engine.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(capture.router)
    app.include_router(resume.router)
    app.include_router(projects.router)
    app.include_router(explore.router)
    app.include_router(activity.router)
    app.include_router(apple_auth.router)
    app.include_router(chat.router)
    return app


app = create_app()


def check() -> int:
    """Doctor: verify an install can actually serve. Returns a process exit code.

    Hard failures (exit 1) are things that stop br8n working at all; everything
    else is reported as a capability that is off, not an error — capture and
    resume run with no credentials.
    """
    import os
    import sqlite3
    import sys
    import tempfile

    ok = True

    def line(status: str, label: str, detail: str = "") -> None:
        print(f"  {status:<5} {label}" + (f" — {detail}" if detail else ""))

    print("br8n check")

    v = sys.version_info
    if v >= (3, 11):
        line("ok", "python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        ok = False
        line("FAIL", "python", f"{v.major}.{v.minor} — br8n needs >= 3.11")

    backend = active_backend()
    line("ok", "backend", f"{backend} tier")

    if backend == "local":
        try:
            import sqlite_vec

            conn = sqlite3.connect(":memory:")
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.close()
            line("ok", "sqlite-vec", "extension loads")
        except Exception as exc:  # noqa: BLE001 — report any failure mode
            ok = False
            line("FAIL", "sqlite-vec", f"{type(exc).__name__}: {exc}")

        db_path = os.getenv("BR8N_DB_PATH") or os.path.expanduser("~/.br8n/brain.db")
        db_dir = os.path.dirname(db_path) or "."
        try:
            os.makedirs(db_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=db_dir):
                pass
            line("ok", "db path", db_path)
        except Exception as exc:  # noqa: BLE001 — report any failure mode
            ok = False
            line("FAIL", "db path", f"{db_path} not writable: {exc}")

    settings = get_settings()
    if settings.ai_gateway_api_key or settings.openai_api_key:
        src = "AI_GATEWAY_API_KEY" if settings.ai_gateway_api_key else "OPENAI_API_KEY"
        line("ok", "embeddings", f"{src} set — semantic search enabled")
    else:
        line("off", "embeddings", "no key — capture/resume work, semantic search does not")

    if getattr(settings, "tavily_api_key", None):
        line("ok", "explore", "TAVILY_API_KEY set")
    else:
        line("off", "explore", "no TAVILY_API_KEY — gap-fill pipeline unavailable")

    print("\n" + ("ready" if ok else "not ready — fix the FAIL lines above"))
    return 0 if ok else 1


def run() -> None:
    """Blessed local-run entrypoint: owns the bind host and refuses to expose the
    auth-less local tier on a non-loopback interface."""
    import os

    import uvicorn

    host = os.getenv("BR8N_HOST", "127.0.0.1")
    port = int(os.getenv("BR8N_PORT", "8002"))
    if active_backend() == "local" and host not in _LOOPBACK_HOSTS:
        raise SystemExit(
            f"Refusing to start: BR8N_BACKEND=local disables API auth, but host={host} "
            f"is not loopback. Bind to 127.0.0.1, or use the cloud backend with BR8N_API_KEY."
        )
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv[1:]:
        raise SystemExit(check())
    run()

"""Embedding client (adapted from Delapan). Routes through the Vercel AI Gateway
(OpenAI-compatible) when ``AI_GATEWAY_API_KEY`` is set, else direct OpenAI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Sequence

from openai import AsyncOpenAI

from br8n.config import get_config, get_settings
from br8n.settings_file import load_settings

_client: AsyncOpenAI | None = None

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
    except Exception:  # never let detection raise
        return False


def _requested() -> tuple[str, str]:
    """(requested provider, source) before validation. Precedence lives here.

    A stored literal ``"auto"`` (what ``br8n_embeddings_set("auto")``
    persists) is not an explicit provider choice — it means "detect it" —
    so the provider it resolves to IS decided by auto-detection and must
    report ``source="auto"``, not ``"settings"``. Only a stored concrete
    provider (``local``/``remote``/``none``) is an explicit choice. This
    distinction is what the work-at-risk gate in
    ``br8n.store.sqlite._sync_embedding_space`` keys off of: an
    auto-detected change that would discard existing vectors defers instead
    of rebuilding, but only if it is correctly reported as ``"auto"`` here.
    """
    stored = load_settings().get("embedding_provider")
    if isinstance(stored, str) and stored in _VALID_PROVIDERS:
        if stored == "auto":
            return "auto", "auto"
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


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        settings = get_settings()
        # Prefer the AI Gateway (one credential for the whole pipeline, no OpenAI
        # account needed); fall back to direct OpenAI when only OPENAI_API_KEY set.
        if settings.ai_gateway_api_key:
            _client = AsyncOpenAI(
                api_key=settings.ai_gateway_api_key,
                base_url=settings.ai_gateway_base_url,
            )
        else:
            _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


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


async def embed_with_retry(text: str, retries: int = 2) -> list[float]:
    for attempt in range(retries + 1):
        try:
            return await embed_text(text)
        except Exception:
            if attempt == retries:
                raise
            await asyncio.sleep(0.4 * (attempt + 1))
    raise RuntimeError("unreachable")

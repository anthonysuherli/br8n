"""Local ONNX embeddings via fastembed — the optional ``br8n[local-embeddings]``.

Why this shape:

* **Lazy import.** fastembed is an extra; importing this module must stay free
  when it is absent, so the import happens inside ``load_now``.
* **Never on the event loop.** fastembed is synchronous and CPU-bound, and
  onnxruntime sizes an intra-op thread pool per session — ``embed`` hops to a
  worker thread and ``threads`` is pinned to ``local_threads`` (default 1) so a
  background embed cannot saturate the machine mid-capture.
* **Readiness, not blocking.** The first use downloads ~130 MB. Callers ask
  ``ready()`` and get False until the model is resident; ``warm_up()`` loads it
  on a daemon thread (no event loop needed). A capture in that window stores
  ``needs_embed=1`` and the existing drain backfills.
* **Offline-safe.** A cached load passes ``local_files_only=True``: fastembed
  is known to hang behind a firewall otherwise. A miss retries with a normal
  (downloading) construction.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Sequence

from br8n.config import get_config

logger = logging.getLogger(__name__)

_model = None
_lock = threading.Lock()
_warm_thread: threading.Thread | None = None


def installed() -> bool:
    """True when the extra is importable. Cheap — no model load."""
    import sys

    # A module already in sys.modules (real import, or a test double) is the
    # fast path — find_spec demands a well-formed __spec__ that a test double
    # won't have. ``None`` is the standard marker for "import previously
    # failed"; fall through to find_spec for the real not-yet-imported case.
    if "fastembed" in sys.modules:
        return sys.modules["fastembed"] is not None

    from importlib.util import find_spec

    try:
        return find_spec("fastembed") is not None
    except Exception:  # a broken import path is "not installed"
        return False


def ready() -> bool:
    return _model is not None


def reset() -> None:
    """Drop the loaded model and warm-up state (tests)."""
    global _model, _warm_thread
    with _lock:
        _model = None
        _warm_thread = None


def load_now() -> bool:
    """Load the model on THIS thread (blocking). True when resident afterwards."""
    global _model
    with _lock:
        if _model is not None:
            return True
        cfg = get_config().embedding
        try:
            from fastembed import TextEmbedding
        except Exception:  # extra absent or broken
            logger.warning("fastembed unavailable; local embeddings off", exc_info=True)
            return False
        base = {"model_name": cfg.local_model, "threads": cfg.local_threads}
        for kwargs in ({**base, "local_files_only": True}, base):
            try:
                _model = TextEmbedding(**kwargs)
                return True
            except TypeError:
                # Older fastembed without local_files_only — fall through to base.
                continue
            except Exception:  # cache miss: retry with download
                logger.info("local model not cached; fetching", exc_info=True)
                continue
        logger.warning("local embedding model failed to load")
        return False


def warm_up() -> None:
    """Schedule a background load. Non-blocking, at most one thread per process."""
    global _warm_thread
    with _lock:
        if _model is not None or (_warm_thread is not None and _warm_thread.is_alive()):
            return
        _warm_thread = threading.Thread(
            target=load_now, name="br8n-embed-warmup", daemon=True
        )
        _warm_thread.start()


def wait_for_warm_up(timeout: float = 60.0) -> bool:
    """Block until a scheduled warm-up finishes. For tests and the doctor."""
    t = _warm_thread
    if t is not None:
        t.join(timeout)
    return ready()


async def embed(texts: Sequence[str]) -> list[list[float]]:
    """Embed off the event loop. Raises if the model is not resident."""
    if _model is None and not load_now():
        raise RuntimeError("local embedding model unavailable")
    cap = get_config().embedding.input_char_cap
    trimmed = [t[:cap] for t in texts]

    def _run() -> list[list[float]]:
        return [[float(x) for x in vec] for vec in _model.embed(trimmed)]

    return await asyncio.to_thread(_run)

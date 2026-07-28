"""Local ONNX embeddings via fastembed — the optional ``br8n[local-embeddings]``.

Why this shape:

* **Lazy import.** fastembed is an extra; importing this module must stay free
  when it is absent, so the import happens inside ``load_now``.
* **Never on the event loop.** fastembed is synchronous and CPU-bound, and
  onnxruntime sizes an intra-op thread pool per session — both the load and the
  embed hop to a worker thread, and ``threads`` is pinned to ``local_threads``
  (default 1) so a background embed cannot saturate the machine mid-capture.
* **Readiness, not blocking.** The first use downloads ~130 MB. Callers ask
  ``ready()`` and get False until the model is resident; ``warm_up()`` loads it
  on a daemon thread (no event loop needed). A capture in that window stores
  ``needs_embed=1`` and the existing drain backfills.
* **Two locks, not one.** ``_load_lock`` serializes the (slow, possibly
  network-bound) model construction. ``_state_lock`` guards the cheap
  ``_warm_thread``/``_fail_count`` bookkeeping and is never held across a
  load — so ``warm_up()``/``embeddings_configured()`` stay fast even while a
  load is in flight; only the load itself is exclusive.
* **Offer once, then go quiet.** A background load that fails
  ``_MAX_CONSECUTIVE_FAILURES`` times in a row stops being rescheduled (and
  logs once) until ``reset()`` clears the count — otherwise every capture
  would silently retry a doomed ~130 MB download forever.
* **Offline-safe.** A cached load passes ``local_files_only=True``: fastembed
  is known to hang behind a firewall otherwise. A miss retries with a normal
  (downloading) construction.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import threading
from typing import Sequence

from br8n.config import get_config

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_FAILURES = 3

_model = None
_load_lock = threading.Lock()
_state_lock = threading.Lock()
_warm_thread: threading.Thread | None = None
_fail_count = 0


def installed() -> bool:
    """True when the extra is importable. Cheap — no model load."""
    # A module already in sys.modules (real import, or a test double) is the
    # fast path — find_spec demands a well-formed __spec__ that a test double
    # won't have. ``None`` is the standard marker for "import previously
    # failed"; fall through to find_spec for the real not-yet-imported case.
    if "fastembed" in sys.modules:
        return sys.modules["fastembed"] is not None

    try:
        return importlib.util.find_spec("fastembed") is not None
    except Exception:  # a broken import path is "not installed"
        return False


def ready() -> bool:
    return _model is not None


def reset() -> None:
    """Drop the loaded model and warm-up state (tests).

    Bookkeeping only — guarded by the short-lived state lock, never the load
    lock, so reset() never blocks behind an in-flight (possibly minutes-long)
    load. A load that finishes after a reset() capturing its own local
    reference (see ``embed``) still can't turn that into an AttributeError.
    """
    global _model, _warm_thread, _fail_count
    with _state_lock:
        _model = None
        _warm_thread = None
        _fail_count = 0


def load_now() -> bool:
    """Load the model on THIS thread (blocking). True when resident afterwards.

    A pure loader: it does not touch the failure counter or scheduling state
    (that's ``warm_up``'s job) so a deliberate direct call — the doctor, a
    forced retry — is never silently rate-limited by background failures.
    """
    global _model
    if _model is not None:
        return True
    with _load_lock:
        if _model is not None:
            return True
        cfg = get_config().embedding
        try:
            from fastembed import TextEmbedding
        except Exception:  # extra absent or broken
            logger.warning("fastembed unavailable; local embeddings off", exc_info=True)
            return False
        base = {"model_name": cfg.local_model, "threads": cfg.local_threads}
        attempts = ({**base, "local_files_only": True}, base)
        for i, kwargs in enumerate(attempts):
            try:
                _model = TextEmbedding(**kwargs)
                return True
            except TypeError:
                # Older fastembed without local_files_only — fall through to base.
                continue
            except Exception:  # cache miss (or genuine failure)
                if i < len(attempts) - 1:
                    logger.info("local model not cached; fetching", exc_info=True)
                continue
        logger.warning("local embedding model failed to load")
        return False


def _warm_load() -> None:
    """Thread body for ``warm_up``: run the blocking load, then update the
    consecutive-failure counter under the state lock — never across the load."""
    global _fail_count
    ok = load_now()
    with _state_lock:
        if ok:
            _fail_count = 0
        else:
            _fail_count += 1
            if _fail_count == _MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    "local embedding model failed to load %d times in a row; "
                    "pausing background warm-up until reset()",
                    _fail_count,
                )


def warm_up() -> None:
    """Schedule a background load. Non-blocking, at most one thread per
    process. Stops scheduling after ``_MAX_CONSECUTIVE_FAILURES`` consecutive
    failures ("offer once, then go quiet") until ``reset()`` clears the count.

    Only ever holds ``_state_lock`` — a load in flight (holding ``_load_lock``
    for possibly minutes) never makes this block, so every caller (notably
    ``embeddings_configured()``) stays fast regardless of load state.
    """
    global _warm_thread, _fail_count
    with _state_lock:
        if _model is not None:
            return
        if _warm_thread is not None and _warm_thread.is_alive():
            return
        if _fail_count >= _MAX_CONSECUTIVE_FAILURES:
            return
        _warm_thread = threading.Thread(
            target=_warm_load, name="br8n-embed-warmup", daemon=True
        )
        try:
            _warm_thread.start()
        except RuntimeError:
            # Thread pool exhausted or other startup failure — do not raise
            # (callers rely on warm_up never raising). Clear the failed thread,
            # increment the failure counter (same bound as load failures),
            # and return normally so the bounded-retry logic applies.
            _warm_thread = None
            _fail_count += 1
            if _fail_count == _MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    "local embedding warm-up thread failed to start %d times in a row; "
                    "pausing background warm-up until reset()",
                    _fail_count,
                )
            return


def wait_for_warm_up(timeout: float = 60.0) -> bool:
    """Block until a scheduled warm-up finishes. For tests and the doctor."""
    t = _warm_thread
    if t is not None:
        t.join(timeout)
    return ready()


async def embed(texts: Sequence[str]) -> list[list[float]]:
    """Embed off the event loop. Raises if the model cannot be made resident,
    or if it returns vectors of the wrong width."""
    if _model is None:
        # The load itself is CPU/network-bound and synchronous — never run it
        # on the loop thread, even for this "make it resident now" fallback.
        loaded = await asyncio.to_thread(load_now)
        if not loaded:
            raise RuntimeError("local embedding model unavailable")

    # Capture into a local so a concurrent reset() between here and the
    # to_thread call below can't turn this into an AttributeError on None.
    model = _model
    if model is None:
        raise RuntimeError("local embedding model unavailable")

    cfg = get_config().embedding
    trimmed = [t[: cfg.input_char_cap] for t in texts]

    def _run() -> list[list[float]]:
        return [[float(x) for x in vec] for vec in model.embed(trimmed)]

    out = await asyncio.to_thread(_run)

    expected = cfg.local_dim
    for vec in out:
        if len(vec) != expected:
            logger.error(
                "local embedding width mismatch: got %d dims, expected %d (model=%s)",
                len(vec),
                expected,
                cfg.local_model,
            )
            raise RuntimeError(
                f"local embedding width mismatch: got {len(vec)}, expected {expected}"
            )
    return out

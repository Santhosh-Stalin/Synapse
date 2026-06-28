"""Shared Groq client and extraction helper for Synapse."""

from __future__ import annotations

import itertools
import json
import queue
import re
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import SynapseConfig

# Extraction uses 70b only — scout's 1,000 RPD free-tier cap is too low for project scans.
# Triage (memory_triage) makes far fewer requests so scout works fine there.
_MODELS = [
    "llama-3.3-70b-versatile",
]
_model_cycle = itertools.cycle(_MODELS)
_model_lock = threading.Lock()
_cerebras_slots = threading.BoundedSemaphore(2)

_MAX_RETRIES = 5
_RETRY_DELAY = 2.0
_CEREBRAS_WALL_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# Token-bucket rate limiter — free tier: 12,000 TPM actual (not 200K, that's paid)
# We target 10,000 TPM to leave headroom for bursts.
# ---------------------------------------------------------------------------
_TPM_LIMIT = 10_000
_tpm_lock = threading.Lock()
_tpm_tokens_used = 0.0
_tpm_window_start = time.monotonic()


def _tpm_acquire(estimated_tokens: int) -> None:
    """Block until sending estimated_tokens won't exceed the TPM budget."""
    global _tpm_tokens_used, _tpm_window_start
    with _tpm_lock:
        now = time.monotonic()
        elapsed = now - _tpm_window_start
        if elapsed >= 60.0:
            # New minute — reset window
            _tpm_tokens_used = 0.0
            _tpm_window_start = now
            elapsed = 0.0

        if _tpm_tokens_used + estimated_tokens > _TPM_LIMIT:
            # Wait out the remainder of this minute
            wait = 60.0 - elapsed + 1.0
            print(f"[Groq] TPM budget ({_TPM_LIMIT}) reached — waiting {wait:.1f}s", flush=True)
        else:
            wait = 0.0

        _tpm_tokens_used += estimated_tokens

    if wait > 0:
        time.sleep(wait)
        with _tpm_lock:
            _tpm_tokens_used = estimated_tokens
            _tpm_window_start = time.monotonic()


def _next_model() -> str:
    with _model_lock:
        return next(_model_cycle)


def get_client(config: "SynapseConfig") -> Any:
    if not config.groq_api_key:
        raise ValueError("groq_api_key required; set GROQ_API_KEY in .env")
    try:
        from groq import Groq

        return Groq(api_key=config.groq_api_key)
    except ImportError:
        raise ImportError("groq not installed (pip install groq)")


def groq_complete(client: Any, system: str, user: str, max_tokens: int = 4096) -> str:
    """Call Groq with retries and TPM rate limiting."""
    # Estimate tokens before sending (1 token ≈ 4 chars)
    estimated_tokens = (len(system) + len(user)) // 4 + max_tokens // 2
    _tpm_acquire(estimated_tokens)

    model = _next_model()
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            msg = str(exc).lower()
            if "rate limit" in msg or "429" in msg:
                model = _next_model()
                wait = _RETRY_DELAY * (2**attempt)
                print(
                    f"[Groq] Rate limited; switching to {model}, retrying in {wait:.1f}s",
                    flush=True,
                )
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Groq: max retries exceeded")


def best_complete(config: "SynapseConfig", system: str, user: str, max_tokens: int = 4096) -> str:
    """Try Cerebras first (fast), fall back to Groq on failure or missing key."""
    cerebras_error: Exception | None = None
    if getattr(config, "cerebras_api_key", ""):
        try:
            from .cerebras_client import get_client as _cb_get, cerebras_complete

            cb = _cb_get(config)
            if getattr(config, "groq_api_key", ""):
                if not _cerebras_slots.acquire(blocking=False):
                    raise RuntimeError("Cerebras concurrency cap reached")
                result_queue: queue.Queue[tuple[bool, str | Exception]] = queue.Queue(maxsize=1)

                def _run_cerebras() -> None:
                    try:
                        result_queue.put((True, cerebras_complete(cb, system, user, max_tokens)))
                    except Exception as exc:
                        result_queue.put((False, exc))
                    finally:
                        _cerebras_slots.release()

                thread = threading.Thread(
                    target=_run_cerebras, daemon=True, name="synapse-cerebras-call"
                )
                thread.start()
                try:
                    ok, result = result_queue.get(timeout=_CEREBRAS_WALL_TIMEOUT)
                except queue.Empty as exc:
                    raise RuntimeError(
                        f"Cerebras exceeded {_CEREBRAS_WALL_TIMEOUT:.0f}s wall timeout"
                    ) from exc
                if ok:
                    return str(result)
                raise result
            return cerebras_complete(cb, system, user, max_tokens)
        except Exception as exc:
            cerebras_error = exc
            if not getattr(config, "groq_api_key", ""):
                raise
            print(
                f"[Cerebras] Failed ({exc.__class__.__name__}: {exc}); falling back to Groq",
                flush=True,
            )

    if not getattr(config, "groq_api_key", "") and cerebras_error:
        raise cerebras_error

    groq = get_client(config)
    return groq_complete(groq, system, user, max_tokens)


def parse_json_patches(raw: str) -> list[dict[str, Any]]:
    """Parse a JSON array of patches from Groq output, stripping markdown fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    return [p for p in parsed if isinstance(p, dict) and p.get("key") and p.get("content")]

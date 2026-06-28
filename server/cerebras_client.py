"""Cerebras inference client for Synapse; primary extractor, Groq is fallback."""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import SynapseConfig

# Best quality model; gpt-oss-120b is the congestion fallback within Cerebras.
_MODEL_PRIMARY = "zai-glm-4.7"
_MODEL_FAST = "gpt-oss-120b"

_MAX_RETRIES = 3
_QUEUE_RETRY_DELAY = 3.0
_REQUEST_TIMEOUT = 12.0


def get_client(config: "SynapseConfig") -> Any:
    key = getattr(config, "cerebras_api_key", "")
    if not key:
        raise ValueError("cerebras_api_key required; set CEREBRAS_API_KEY in .env")
    try:
        from cerebras.cloud.sdk import Cerebras

        return Cerebras(api_key=key)
    except ImportError:
        raise ImportError("cerebras-cloud-sdk not installed (pip install cerebras-cloud-sdk)")


def cerebras_complete(client: Any, system: str, user: str, max_tokens: int = 4096) -> str:
    """Call Cerebras with queue-congestion retries, falling back to fast model."""
    model = _MODEL_PRIMARY
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
                timeout=_REQUEST_TIMEOUT,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            msg = str(exc).lower()
            if "timed out" in msg or "timeout" in msg:
                raise RuntimeError(
                    f"Cerebras request timed out after {_REQUEST_TIMEOUT:.0f}s"
                ) from exc
            if "queue" in msg or "429" in msg or "too_many_requests" in msg:
                if model == _MODEL_PRIMARY:
                    model = _MODEL_FAST
                    print(f"[Cerebras] Queue full; switching to {model}", flush=True)
                else:
                    wait = _QUEUE_RETRY_DELAY * (attempt + 1)
                    print(f"[Cerebras] Queue full; retrying in {wait:.0f}s", flush=True)
                    time.sleep(wait)
            else:
                raise
    raise RuntimeError("Cerebras: max retries exceeded")


def parse_json_patches(raw: str) -> list[dict[str, Any]]:
    """Parse a JSON array of patches, stripping markdown fences."""
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

"""
Raw conversation lookup via synapse_extracted/index/conversations_index.json.

Provides O(1) UUID → markdown path resolution, full-text retrieval,
and query-guided chunk extraction to keep token cost low.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import SynapseConfig

# Separator used between messages in the raw markdown
_MSG_SEP = "-" * 100

_STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "are",
    "was",
    "were",
    "has",
    "have",
    "had",
    "not",
    "but",
    "can",
    "its",
    "user",
    "also",
    "will",
    "been",
    "they",
    "their",
    "more",
    "used",
    "use",
    "using",
    "about",
    "into",
    "when",
    "which",
    "some",
    "than",
    "then",
    "one",
    "all",
    "any",
    "each",
    "both",
    "only",
    "very",
    "just",
    "chatgpt",
    "content",
    "type",
    "text",
    "node",
}

# Lines that are pure export metadata — strip before returning chunks
_NOISE_PATTERNS = re.compile(
    r"^(Content type:.*|Node ID:.*|text\s*$|image_asset_pointer\s*$)",
    re.MULTILINE,
)


def _clean_message(msg: str) -> str:
    """Strip export metadata noise lines from a message block."""
    cleaned = _NOISE_PATTERNS.sub("", msg)
    # Collapse multiple blank lines into one
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------


def _find_md_path(config: SynapseConfig, entry: dict) -> Path | None:
    """Locate the markdown file for an index entry via date-prefix glob (index has no markdown_path field)."""
    created = entry.get("created", "")
    date_prefix = created[:10] if len(created) >= 10 else ""
    month = created[:7] if len(created) >= 7 else ""
    md_root = config.raw_archive_path / "conversations_md"
    if month:
        month_dir = md_root / month
        if date_prefix:
            candidates = list(month_dir.glob(f"{date_prefix}_*.md")) if month_dir.exists() else []
        else:
            candidates = list(month_dir.glob("*.md")) if month_dir.exists() else []
        if candidates:
            return candidates[0]
    if date_prefix:
        for p in md_root.glob(f"**/{date_prefix}_*.md"):
            return p
    return None


def _index_path(config: SynapseConfig) -> Path | None:
    if not config.raw_archive_path:
        return None
    p = config.raw_archive_path / "index" / "conversations_index.json"
    return p if p.exists() else None


@lru_cache(maxsize=1)
def _load_index(index_path: str) -> dict[str, dict]:
    data = json.loads(Path(index_path).read_text(encoding="utf-8", errors="replace"))
    return {entry["conversation_id"]: entry for entry in data}


def _get_index(config: SynapseConfig) -> dict[str, dict] | None:
    p = _index_path(config)
    if not p:
        return None
    return _load_index(str(p))


def _resolve(config: SynapseConfig, chat_id: str) -> tuple[dict | None, str | None]:
    """Return (index_entry, error_string)."""
    cid = chat_id.removeprefix("chats.")
    index = _get_index(config)
    if index is None:
        return (
            None,
            "raw_archive_path not configured or index not found. Set raw_archive_path in config.yaml.",
        )
    entry = index.get(cid)
    if not entry:
        return None, f"Conversation {cid!r} not found in raw archive index."
    return entry, None


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _split_messages(content: str) -> list[str]:
    """Split raw markdown into cleaned individual message blocks."""
    blocks = content.split(_MSG_SEP)
    messages = []
    for block in blocks:
        block = _clean_message(block)
        if not block or block.startswith("---") or len(block) < 20:
            continue
        messages.append(block)
    return messages


def _tokenise(text: str) -> set[str]:
    tokens = re.findall(r"[a-z][a-z0-9]*", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in _STOP_WORDS}


def _score_chunk(chunk_text: str, query_tokens: set[str]) -> float:
    """TF-density score: counts total query term occurrences, not just presence."""
    if not query_tokens:
        return 0.0
    all_tokens = re.findall(r"[a-z][a-z0-9]*", chunk_text.lower())
    if not all_tokens:
        return 0.0
    hit_count = sum(1 for t in all_tokens if t in query_tokens)
    # Density: hits per 100 tokens, boosted by coverage (how many distinct terms matched)
    density = hit_count / len(all_tokens) * 100
    coverage = len(query_tokens & set(all_tokens)) / len(query_tokens)
    return density * coverage


def _extract_chunks(
    messages: list[str],
    query_tokens: set[str],
    window: int = 8,
    stride: int = 4,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """
    Slide a window over messages, score each window against query,
    return top_k non-overlapping windows.
    """
    if not messages:
        return []

    windows: list[tuple[float, int, str]] = []  # (score, start_idx, text)
    for i in range(0, len(messages), stride):
        chunk_msgs = messages[i : i + window]
        chunk_text = f"\n{_MSG_SEP}\n".join(chunk_msgs)
        score = _score_chunk(chunk_text, query_tokens)
        windows.append((score, i, chunk_text))

    # Sort by score descending, then pick non-overlapping top_k
    windows.sort(key=lambda x: -x[0])
    selected: list[dict[str, Any]] = []
    used_ranges: list[tuple[int, int]] = []

    for score, start, text in windows:
        end = start + window
        # Skip if overlaps with already selected
        overlaps = any(not (end <= s or start >= e) for s, e in used_ranges)
        if not overlaps:
            selected.append(
                {
                    "message_range": f"{start + 1}–{min(end, len(messages))}",
                    "score": round(score, 3),
                    "content": text,
                    "estimated_tokens": len(text) // 4,
                }
            )
            used_ranges.append((start, end))
        if len(selected) >= top_k:
            break

    return selected


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_raw_conversation(config: SynapseConfig, chat_id: str) -> dict[str, Any]:
    """Full raw conversation. Expensive — use get_raw_chunks when you have a query."""
    entry, err = _resolve(config, chat_id)
    if err:
        return {"error": err}

    md_path = _find_md_path(config, entry)
    if not md_path or not md_path.exists():
        return {"error": f"Markdown file not found for conversation {chat_id}", "metadata": entry}

    raw_text = md_path.read_text(encoding="utf-8", errors="replace")
    return {
        "conversation_id": chat_id.removeprefix("chats."),
        "title": entry.get("title", ""),
        "created": entry.get("created", ""),
        "updated": entry.get("updated", ""),
        "message_count": entry.get("message_count", 0),
        "estimated_tokens": entry.get("estimated_tokens", 0),
        "content": raw_text,
    }


def get_raw_chunks(
    config: SynapseConfig,
    chat_id: str,
    query: str,
    top_k: int = 3,
    window: int = 8,
) -> dict[str, Any]:
    """
    Return only the most query-relevant message windows from a conversation.
    Typical token cost: ~1-5k instead of ~35k for the full conversation.
    """
    entry, err = _resolve(config, chat_id)
    if err:
        return {"error": err}

    md_path = _find_md_path(config, entry)
    if not md_path or not md_path.exists():
        return {"error": f"Markdown file not found for conversation {chat_id}"}

    raw_text = md_path.read_text(encoding="utf-8", errors="replace")
    messages = _split_messages(raw_text)
    query_tokens = _tokenise(query)
    chunks = _extract_chunks(messages, query_tokens, window=window, top_k=top_k)

    total_tokens = sum(c["estimated_tokens"] for c in chunks)

    return {
        "conversation_id": chat_id.removeprefix("chats."),
        "title": entry["title"],
        "created": entry["created"],
        "message_count": entry["message_count"],
        "total_messages": len(messages),
        "chunks_returned": len(chunks),
        "estimated_tokens": total_tokens,
        "chunks": chunks,
    }


def search_raw_index(config: SynapseConfig, query: str, top_k: int = 10) -> list[dict[str, Any]]:
    """Lightweight title search over the raw archive index."""
    index = _get_index(config)
    if index is None:
        return [{"error": "raw_archive_path not configured or index not found."}]

    terms = query.lower().split()
    scored = []
    for entry in index.values():
        title_lower = entry["title"].lower()
        score = sum(1 for t in terms if t in title_lower)
        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda x: -x[0])
    return [
        {
            "conversation_id": e["conversation_id"],
            "title": e.get("title", ""),
            "created": e.get("created", ""),
            "message_count": e.get("message_count", 0),
            "score": s,
        }
        for s, e in scored[:top_k]
    ]

from __future__ import annotations

import json
import re
from typing import Any

from .config import SynapseConfig
from .encryption import read_text
from .index import MemoryIndex
from .memory_file import key_to_path, parse_memory_text

CLAUDE_ANALYSIS_THRESHOLD = 0.7

# Words that bias embedding toward the person rather than the topic
_STOP_WORDS = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "shall",
    "can",
    "need",
    "dare",
    "ought",
    "i",
    "me",
    "my",
    "we",
    "our",
    "you",
    "your",
    "he",
    "she",
    "it",
    "they",
    "them",
    "their",
    "this",
    "that",
    "these",
    "those",
    "what",
    "which",
    "who",
    "whom",
    "how",
    "why",
    "when",
    "where",
    "and",
    "or",
    "but",
    "if",
    "then",
    "else",
    "so",
    "yet",
    "both",
    "either",
    "neither",
    "not",
    "no",
    "nor",
    "just",
    "very",
    "also",
    "too",
    "only",
    "even",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "up",
    "about",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "out",
    "off",
    "over",
    "under",
    "again",
    "further",
    "then",
    "once",
    "as",
    "than",
    "while",
    "since",
    "because",
    "although",
    "does",
    "make",
    "makes",
    "made",
    "get",
    "gets",
    "got",
    "use",
    "used",
    "using",
    "give",
    "gives",
    "gave",
    "take",
    "takes",
    "took",
    "come",
    "goes",
    "went",
    "think",
    "feel",
    "know",
    "want",
    "like",
    "look",
    "see",
    # add your own name(s) here so searches focus on topic, not person
}


def _clean_query(query: str) -> str:
    """Strip stop words and owner name so embeddings focus on the topic."""
    words = query.lower().split()
    kept = [w for w in words if w.strip(".,?!:;'\"") not in _STOP_WORDS]
    # Fall back to original if nothing survives (all stop words)
    return " ".join(kept) if kept else query


_SYSTEM_PROMPT = """\
You are a semantic memory router for a personal knowledge vault.

Your only job is to rank existing memory keys by relevance to a query.

INPUT you will receive:
- A search query from the user
- A list of memory keys with their content previews

YOUR OUTPUT must be a JSON array, nothing else. No explanation, no markdown, no preamble.
Format exactly:
[
  {"key": "work.stack", "score": 0.92, "reason": "query asks about tech stack"},
  {"key": "projects.loophole", "score": 0.71, "reason": "query mentions Electron"}
]

RULES:
- Only return keys from the list you were given. Never invent keys.
- Only include keys with genuine relevance. If nothing is relevant, return [].
- Score is 0.0 to 1.0. Higher = more relevant.
- Reason is one short phrase explaining why this key matched.
- Return at most 5 results.
- Do not include memory content in your response, only the key and score.
- If the query is ambiguous, prefer broader keys (identity, patterns) over project-specific ones.\
"""

_MODEL_ACK = """\
Understood. Please provide the search query and the list of memory keys with their content previews.\
"""


def memory_search(config: SynapseConfig, query: str) -> list[dict[str, Any]]:
    index = MemoryIndex(config.vault_path, lambda path: read_text(config, path))

    # Semantic is primary when API key available; FTS5 boosts semantic hits or acts as fallback
    if config.gemini_api_key:
        try:
            from .embeddings import semantic_search

            sem_results = semantic_search(
                config.gemini_api_key,
                config.vault_path / "_index.db",
                _clean_query(query),
                limit=4,
            )
            if sem_results:
                from .embeddings import hybrid_merge

                fts_results = index.search(query)
                # Only let FTS5 boost semantic results — never add FTS-only noise keys
                sem_keys = {r["key"] for r in sem_results}
                fts_filtered = [r for r in fts_results if r["key"] in sem_keys]
                results = hybrid_merge(fts_filtered, sem_results)[:4]
            else:
                results = index.search(query)
        except Exception:
            results = index.search(query)
    else:
        results = index.search(query)

    # Gemini LLM fallback only if both FTS5 and semantic return nothing
    if not results and config.cloud_search:
        results = _gemini_fallback(config, query)

    # Attach full content for high-scoring active vault results only
    # Skip chats.* — they are large summaries; use memory_get_raw_chunks for depth
    for r in results:
        if r.get("score", 0.0) >= CLAUDE_ANALYSIS_THRESHOLD and not r["key"].startswith("chats."):
            path = key_to_path(config.vault_path, r["key"])
            if path.exists():
                try:
                    _, full_content = parse_memory_text(read_text(config, path))
                    r["full_content"] = full_content
                    r["requires_analysis"] = True
                except Exception:
                    pass

    return results


def _gemini_fallback(config: SynapseConfig, query: str) -> list[dict[str, Any]]:
    if not config.gemini_api_key:
        return []
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return []

    # Build vault context: key + content preview for every indexed memory
    index = MemoryIndex(config.vault_path, lambda path: read_text(config, path))
    rows = index.search("", limit=100)  # fetch all via broad empty-ish search
    if not rows:
        # fall back to direct index scan
        try:
            with index.connect() as conn:
                rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT key, content_preview FROM memories ORDER BY key"
                    ).fetchall()
                ]
        except Exception:
            rows = []

    if not rows:
        return []

    vault_lines = "\n".join(f"- {r['key']}: {(r.get('content_preview') or '')[:200]}" for r in rows)
    user_input = f"Query: {query}\n\nMemory keys and previews:\n{vault_lines}"

    client = genai.Client(api_key=config.gemini_api_key)
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text=_SYSTEM_PROMPT)]),
        types.Content(role="model", parts=[types.Part.from_text(text=_MODEL_ACK)]),
        types.Content(role="user", parts=[types.Part.from_text(text=user_input)]),
    ]
    cfg = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
        tools=[types.Tool(googleSearch=types.GoogleSearch())],
    )

    try:
        chunks: list[str] = []
        for chunk in client.models.generate_content_stream(
            model="gemma-4-31b-it",
            contents=contents,
            config=cfg,
        ):
            if chunk.text:
                chunks.append(chunk.text)
        raw = "".join(chunks)
    except Exception:
        return []

    return _parse_response(raw, {r["key"] for r in rows})


def _parse_response(raw: str, valid_keys: set[str]) -> list[dict[str, Any]]:
    # Strip markdown fences if present
    text = raw.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip()

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract first [...] block
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return []
        try:
            items = json.loads(match.group())
        except json.JSONDecodeError:
            return []

    if not isinstance(items, list):
        return []

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key or key not in valid_keys:
            continue
        score = float(item.get("score", 0.0))
        results.append(
            {
                "key": key,
                "score": score,
                "reason": str(item.get("reason", "")),
                "source": "cloud",
            }
        )

    results.sort(key=lambda x: -x["score"])
    return results[:5]

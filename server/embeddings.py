from __future__ import annotations

import math
import sqlite3
import struct
from pathlib import Path
from typing import Any


# ── SQLite vector store ───────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS embeddings (
  key        TEXT PRIMARY KEY,
  vector     BLOB NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def upsert_vector(db_path: Path, key: str, vec: list[float]) -> None:
    import datetime
    blob = _vec_to_blob(vec)
    now = datetime.datetime.utcnow().isoformat()
    with sqlite3.connect(db_path) as conn:
        _init_db(conn)
        conn.execute(
            "INSERT INTO embeddings(key, vector, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET vector=excluded.vector, updated_at=excluded.updated_at",
            (key, blob, now),
        )
        conn.commit()


def delete_vector(db_path: Path, key: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM embeddings WHERE key = ?", (key,))
        conn.commit()


def load_all_vectors(db_path: Path) -> dict[str, list[float]]:
    if not db_path.exists():
        return {}
    with sqlite3.connect(db_path) as conn:
        _init_db(conn)
        rows = conn.execute("SELECT key, vector FROM embeddings").fetchall()
    return {key: _blob_to_vec(blob) for key, blob in rows}


# ── Gemini embedding calls ────────────────────────────────────────────────────

_MODEL = "gemini-embedding-001"


def embed_document(api_key: str, key: str, text: str) -> list[float] | None:
    """Embed a vault document for storage. Returns None on failure."""
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.embed_content(
            model=_MODEL,
            contents=text,
            config={"task_type": "RETRIEVAL_DOCUMENT"},
        )
        return resp.embeddings[0].values
    except Exception:
        return None


def embed_query(api_key: str, query: str) -> list[float] | None:
    """Embed a search query. Returns None on failure."""
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.embed_content(
            model=_MODEL,
            contents=query,
            config={"task_type": "RETRIEVAL_QUERY"},
        )
        return resp.embeddings[0].values
    except Exception:
        return None


# ── Cosine similarity search ──────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# Public alias used by merger.py
cosine_similarity = _cosine


def semantic_search(
    api_key: str,
    db_path: Path,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Embed query, compute cosine similarity against all stored vectors.
    Returns list of {key, score} sorted by score descending.
    Returns [] if embedding fails or no vectors stored.
    """
    q_vec = embed_query(api_key, query)
    if q_vec is None:
        return []
    store = load_all_vectors(db_path)
    if not store:
        return []
    scored = [
        {"key": key, "score": _cosine(q_vec, vec)}
        for key, vec in store.items()
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


# ── Hybrid merge (FTS5 + semantic) ───────────────────────────────────────────

def hybrid_merge(
    fts_results: list[dict[str, Any]],
    sem_results: list[dict[str, Any]],
    fts_weight: float = 0.5,
    sem_weight: float = 0.5,
) -> list[dict[str, Any]]:
    """
    Merge FTS5 and semantic results into a single ranked list.
    Keys appearing in both get a weighted combined score.
    Keys only in one source get that source's score * its weight.
    """
    scores: dict[str, float] = {}
    sources: dict[str, str] = {}

    for r in fts_results:
        key = r["key"]
        scores[key] = scores.get(key, 0.0) + r.get("score", 0.0) * fts_weight
        sources[key] = "fts5"

    for r in sem_results:
        key = r["key"]
        scores[key] = scores.get(key, 0.0) + r.get("score", 0.0) * sem_weight
        sources[key] = "hybrid" if key in sources else "semantic"

    merged = [
        {"key": key, "score": score, "source": sources[key]}
        for key, score in scores.items()
    ]
    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged

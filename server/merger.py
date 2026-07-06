"""
merger.py - Semantic duplicate detection and AI-powered content merging.

Unlike dedup.py (Jaccard on word overlap), this module:
  1. Uses embedding cosine similarity to find semantic duplicates.
  2. Calls the configured inference provider to merge two files into one rich combined file.
  3. Proposes the merged result and deletes the redundant file.

Works even when duplicate files have completely different trigger words or keys,
as long as they talk about the same topic semantically.
"""

from __future__ import annotations

import re
import uuid as _uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

_MERGER_SESSION_ID: str = _uuid.uuid4().hex

if TYPE_CHECKING:
    from .config import SynapseConfig

_SIMILARITY_THRESHOLD = 0.93  # cosine similarity above this = semantic duplicate
_SAME_CATEGORY_ONLY = True  # only merge within the same top-level folder (projects/work/etc.)

_MERGE_SYSTEM_PROMPT = """\
You are a personal memory curator. You have two memory files about the same topic that need to be merged into one.

Your job: produce a single, unified memory file that contains ALL unique information from both files.

Rules:
- Keep EVERY specific fact from both files — names, numbers, tool names, techniques, dates, decisions
- Do NOT drop any detail that appears in either file
- Remove pure duplicates (same fact stated twice)
- Go 2 levels deep: Level 1 = topic, Level 2 = specific sub-components with exact values
- Use ## headers for sections, bullet points for facts
- Target 150-500 words in the final output
- Output ONLY the merged content body (no YAML frontmatter, no JSON, just clean Markdown)

BAD: Drop one file's unique details to keep it short.
GOOD: Combine all details, restructure so nothing is lost.

Output raw Markdown only. No explanation, no preamble.\
"""


# ─── Similarity engine ─────────────────────────────────────────────────────────


def _find_duplicate_pairs(
    db_path: Path,
    entries: list[dict[str, Any]],
    threshold: float,
    same_category: bool,
) -> list[tuple[dict, dict, float]]:
    """
    Return (entry_a, entry_b, similarity) pairs above threshold.
    Uses embedding vectors from SQLite; falls back to Jaccard if unavailable.
    """
    from .embeddings import load_all_vectors, cosine_similarity

    vectors = load_all_vectors(db_path)

    pairs: list[tuple[dict, dict, float]] = []
    seen: set[frozenset] = set()

    for i, a in enumerate(entries):
        for b in entries[i + 1 :]:
            pair = frozenset([a["key"], b["key"]])
            if pair in seen:
                continue
            seen.add(pair)

            if same_category:
                cat_a = a["key"].split(".")[0]
                cat_b = b["key"].split(".")[0]
                if cat_a != cat_b:
                    continue

            if a["key"] in vectors and b["key"] in vectors:
                sim = cosine_similarity(vectors[a["key"]], vectors[b["key"]])
            else:
                # Jaccard fallback
                aw = set(re.findall(r"\w+", a["content"].lower()))
                bw = set(re.findall(r"\w+", b["content"].lower()))
                u = aw | bw
                sim = len(aw & bw) / len(u) if u else 0.0

            if sim >= threshold:
                pairs.append((a, b, round(sim, 4)))

    pairs.sort(key=lambda x: -x[2])
    return pairs


# ─── AI merge ──────────────────────────────────────────────────────────────────


def _ai_merge_content(
    config: "SynapseConfig", key_a: str, content_a: str, key_b: str, content_b: str
) -> str | None:
    """Ask the configured inference provider to merge two memory files."""
    from .groq_client import best_complete

    user_msg = (
        f"File 1 (key: {key_a}):\n\n{content_a}\n\n"
        f"---\n\n"
        f"File 2 (key: {key_b}):\n\n{content_b}"
    )
    try:
        result = best_complete(config, _MERGE_SYSTEM_PROMPT, user_msg)
        return result if result else None
    except Exception:
        return None


# ─── Load vault entries ────────────────────────────────────────────────────────


def _load_entries(config: "SynapseConfig") -> list[dict[str, Any]]:
    from .encryption import read_text
    from .memory_file import parse_memory_text, path_to_key

    vault = config.vault_path
    entries = []
    for md in sorted(vault.rglob("*.md")):
        try:
            text = read_text(config, md)
            fm, content = parse_memory_text(text)
        except Exception:
            continue
        key = str(fm.get("key") or path_to_key(vault, md))
        if not content.strip():
            continue
        entries.append(
            {
                "key": key,
                "path": md,
                "frontmatter": fm,
                "content": content,
            }
        )
    return entries


# ─── Git deletion helper ──────────────────────────────────────────────────────


def _git_remove_and_commit(config: "SynapseConfig", path: Path, message: str) -> None:
    """Delete a file and commit its removal via git rm (not git add)."""
    if not config.git_enabled:
        if path.exists():
            path.unlink()
        return
    try:
        from git import InvalidGitRepositoryError, Repo

        repo = Repo(config.root_path, search_parent_directories=True)
        if path.exists():
            # Use git rm to stage + delete in one step
            repo.index.remove([str(path)], working_tree=True)
        else:
            # File already gone — just stage the removal
            try:
                repo.index.remove([str(path)], working_tree=False)
            except Exception:
                pass
        # Clean up empty parent dir
        try:
            path.parent.rmdir()
        except OSError:
            pass
        repo.index.commit(message)
    except Exception:
        # Fallback: plain delete, no git
        if path.exists():
            path.unlink()


# ─── Public entry point ────────────────────────────────────────────────────────


def smart_merge_duplicates(
    config: "SynapseConfig",
    dry_run: bool = True,
    threshold: float = _SIMILARITY_THRESHOLD,
) -> dict[str, Any]:
    """
    Find semantic duplicate pairs using embedding cosine similarity, then
    merge each pair's content with the configured inference provider into a single rich file.

    dry_run=True  → report pairs found, no files changed.
    dry_run=False → merge content, propose updates, delete redundant files.

    Returns {pairs_found, merged, dry_run}.
    """
    db_path = config.vault_path / "_index.db"
    entries = _load_entries(config)
    pairs = _find_duplicate_pairs(db_path, entries, threshold, _SAME_CATEGORY_ONLY)

    report: list[dict[str, Any]] = []
    for a, b, sim in pairs:
        report.append(
            {
                "key_a": a["key"],
                "key_b": b["key"],
                "similarity": sim,
                "lengths": {"a": len(a["content"]), "b": len(b["content"])},
                "keep": a["key"] if len(a["content"]) >= len(b["content"]) else b["key"],
                "remove": b["key"] if len(a["content"]) >= len(b["content"]) else a["key"],
            }
        )

    if dry_run:
        return {
            "dry_run": True,
            "threshold": threshold,
            "pairs_found": len(pairs),
            "pairs": report,
            "next_step": "Call memory_smart_merge(dry_run=False) to execute merges.",
        }

    # ── Execute merges ─────────────────────────────────────────────────────────
    if not config.groq_api_key and not getattr(config, "cerebras_api_key", ""):
        return {"error": "groq_api_key or cerebras_api_key required for merge"}

    try:
        from .groq_client import best_complete as _complete
    except ImportError:
        return {"error": "groq not installed (pip install groq)"}

    from .diff import propose_update, apply_update

    merged: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    # Track keys already consumed in a merge (avoid triple-merging)
    consumed: set[str] = set()

    for a, b, sim in pairs:
        if a["key"] in consumed or b["key"] in consumed:
            continue

        keep_entry = a if len(a["content"]) >= len(b["content"]) else b
        drop_entry = b if keep_entry is a else a

        print(
            f"[Synapse Merge] Merging {a['key']} + {b['key']} "
            f"(sim={sim}) -> keep {keep_entry['key']}",
            flush=True,
        )

        merged_content = _ai_merge_content(
            config,
            a["key"],
            a["content"],
            b["key"],
            b["content"],
        )
        if not merged_content:
            errors.append(
                {"pair": f"{a['key']} + {b['key']}", "error": "Inference provider returned empty"}
            )
            continue

        # Build merged frontmatter: start from keep_entry, merge related lists
        fm_keep = dict(keep_entry["frontmatter"])
        fm_drop = dict(drop_entry["frontmatter"])
        related_keep = set(fm_keep.get("related") or [])
        related_drop = set(fm_drop.get("related") or [])
        triggers_keep = set(fm_keep.get("triggers") or [])
        triggers_drop = set(fm_drop.get("triggers") or [])
        fm_keep["related"] = sorted(
            (related_keep | related_drop) - {keep_entry["key"], drop_entry["key"]}
        )
        fm_keep["triggers"] = sorted(triggers_keep | triggers_drop)[:12]
        fm_keep["version"] = int(fm_keep.get("version", 1)) + 1

        patch = {
            "key": keep_entry["key"],
            "content": merged_content,
            "type": str(fm_keep.get("type", "note")),
            "scope": str(fm_keep.get("scope", "global")),
            "weight": float(fm_keep.get("weight", 0.9)),
            "signal": str(fm_keep.get("signal", "high_signal")),
            "reason": f"Merged duplicate: {drop_entry['key']} (sim={sim})",
            "related": fm_keep["related"],
        }

        try:
            patch.setdefault("session_id", _MERGER_SESSION_ID)
            proposal = propose_update(config, patch)
            result = apply_update(config, proposal["patch_id"])

            # Delete the redundant file and stage via git rm
            drop_path: Path = drop_entry["path"]
            _git_remove_and_commit(
                config,
                drop_path,
                f"merge: remove duplicate {drop_entry['key']} -> {keep_entry['key']}",
            )

            # Remove from embeddings DB
            try:
                from .embeddings import delete_vector

                delete_vector(db_path, drop_entry["key"])
            except Exception:
                pass

            # Remove from FTS index
            try:
                from .index import MemoryIndex
                from .encryption import read_text as _rt

                idx = MemoryIndex(config.vault_path, lambda p: _rt(config, p))
                with idx.connect() as conn:
                    conn.execute("DELETE FROM memories WHERE key = ?", (drop_entry["key"],))
                    conn.commit()
            except Exception:
                pass

            merged.append(
                {
                    "kept": keep_entry["key"],
                    "removed": drop_entry["key"],
                    "similarity": sim,
                    "commit": result.get("git", {}).get("commit", ""),
                }
            )
            consumed.add(a["key"])
            consumed.add(b["key"])

        except Exception as exc:
            errors.append({"pair": f"{a['key']} + {b['key']}", "error": str(exc)})

    return {
        "dry_run": False,
        "threshold": threshold,
        "pairs_found": len(pairs),
        "merged": merged,
        "errors": errors,
        "summary": f"{len(merged)} pairs merged, {len(errors)} errors",
    }

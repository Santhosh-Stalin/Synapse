"""
Build a topic graph over vault/chats/*.md nodes.

Edges are computed from tag/project/keyword overlap — no LLM, no embeddings.
Outputs:
  - vault/metadata/topic_graph.json
  - Updated frontmatter (related field) + ## Related Conversations section in each chat file
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from .config import SynapseConfig

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
    except Exception:
        fm = {}
    return fm, body


def _extract_section(body: str, heading: str) -> list[str]:
    """Return bullet lines under a ## heading, stripped of leading '- '."""
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    lines = body.splitlines()
    result = []
    inside = False
    for line in lines:
        if re.match(pattern, line, re.IGNORECASE):
            inside = True
            continue
        if inside:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped.startswith("- "):
                result.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                result.append(stripped)
    return result


def _tokenise(text: str) -> set[str]:
    """Lower-case word tokens, length ≥ 3, ignoring common stop words."""
    _STOPS = {
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
    }
    tokens = re.findall(r"[a-z][a-z0-9_\-]*", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in _STOPS}


# ---------------------------------------------------------------------------
# Node loading
# ---------------------------------------------------------------------------


def _load_nodes(chats_dir: Path) -> list[dict[str, Any]]:
    nodes = []
    for md in sorted(chats_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        fm, body = _parse_frontmatter(text)

        chat_id = md.stem
        title = fm.get("title", chat_id)
        tags = [t.lower() for t in (fm.get("tags") or [])]
        categories = [c.lower() for c in (fm.get("categories") or [])]

        # Extract projects and keywords from body sections
        projects = [p.lower() for p in _extract_section(body, "Projects")]
        keywords_raw = _extract_section(body, "Keywords")
        keyword_tokens: set[str] = set()
        for kw in keywords_raw:
            keyword_tokens |= _tokenise(kw)

        title_tokens = _tokenise(title)

        nodes.append(
            {
                "chat_id": chat_id,
                "title": title,
                "path": str(md),
                "tags": tags,
                "categories": categories,
                "projects": projects,
                "keyword_tokens": keyword_tokens,
                "title_tokens": title_tokens,
                "raw_text": text,
                "frontmatter": fm,
                "body": body,
            }
        )
    return nodes


# ---------------------------------------------------------------------------
# Edge scoring
# ---------------------------------------------------------------------------


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _edge_score(a: dict, b: dict) -> float:
    score = 0.0

    # Project overlap (highest weight)
    a_proj = set(a["projects"])
    b_proj = set(b["projects"])
    if a_proj and b_proj:
        score += 5.0 * _jaccard(a_proj, b_proj)

    # Tag overlap
    a_tags = set(a["tags"])
    b_tags = set(b["tags"])
    if a_tags and b_tags:
        score += 3.0 * _jaccard(a_tags, b_tags)

    # Keyword token overlap
    a_kw = a["keyword_tokens"]
    b_kw = b["keyword_tokens"]
    if a_kw and b_kw:
        score += 2.0 * _jaccard(a_kw, b_kw)

    # Title token overlap
    a_tt = a["title_tokens"]
    b_tt = b["title_tokens"]
    if a_tt and b_tt:
        score += 1.0 * _jaccard(a_tt, b_tt)

    return score


# ---------------------------------------------------------------------------
# Graph construction via inverted index (avoids O(n²) full scan)
# ---------------------------------------------------------------------------


def _build_edges(nodes: list[dict], top_k: int = 8, min_score: float = 0.15) -> list[dict]:
    # Inverted index: tag/project → list of node indices
    inv: dict[str, list[int]] = defaultdict(list)
    for i, node in enumerate(nodes):
        for t in node["tags"]:
            inv[f"tag:{t}"].append(i)
        for p in node["projects"]:
            if p:
                inv[f"proj:{p}"].append(i)
        for kw in node["keyword_tokens"]:
            inv[f"kw:{kw}"].append(i)

    # For each node, find candidates via inverted index then score
    edges: list[dict] = []
    for i, node in enumerate(nodes):
        candidates: set[int] = set()
        for t in node["tags"]:
            candidates.update(inv[f"tag:{t}"])
        for p in node["projects"]:
            if p:
                candidates.update(inv[f"proj:{p}"])
        for kw in node["keyword_tokens"]:
            candidates.update(inv[f"kw:{kw}"])
        candidates.discard(i)  # no self-edges

        scored: list[tuple[float, int]] = []
        for j in candidates:
            s = _edge_score(node, nodes[j])
            if s >= min_score:
                scored.append((s, j))

        scored.sort(reverse=True)
        for score, j in scored[:top_k]:
            # Determine edge type
            a_proj = set(node["projects"])
            b_proj = set(nodes[j]["projects"])
            if a_proj & b_proj:
                etype = "shared_project"
            elif set(node["tags"]) & set(nodes[j]["tags"]):
                etype = "shared_topic"
            else:
                etype = "keyword_overlap"

            edges.append(
                {
                    "source": node["chat_id"],
                    "target": nodes[j]["chat_id"],
                    "type": etype,
                    "weight": round(score, 4),
                }
            )

    return edges


# ---------------------------------------------------------------------------
# Write outputs
# ---------------------------------------------------------------------------


def _related_for_node(chat_id: str, edges: list[dict], nodes_by_id: dict) -> list[dict]:
    """Return sorted related entries for a given chat_id."""
    related: list[tuple[float, str]] = []
    for e in edges:
        if e["source"] == chat_id:
            related.append((e["weight"], e["target"]))
    related.sort(reverse=True)
    result = []
    for weight, tid in related[:8]:
        node = nodes_by_id.get(tid, {})
        result.append(
            {
                "id": tid,
                "title": node.get("title", tid),
                "weight": weight,
            }
        )
    return result


def _update_chat_file(node: dict, related: list[dict]) -> None:
    """Rewrite the chat .md file with updated frontmatter related field + wikilinks section."""
    fm = dict(node["frontmatter"])
    fm["related"] = [r["id"] for r in related]

    # Serialize frontmatter
    fm_yaml = yaml.dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    fm_yaml = fm_yaml.rstrip("\n")

    body = node["body"]

    # Remove existing ## Related Conversations section if present
    body = re.sub(
        r"\n## Related Conversations\n[\s\S]*?(?=\n## |\Z)",
        "",
        body,
    ).rstrip()

    # Build new wikilinks section
    if related:
        lines = ["\n\n## Related Conversations\n"]
        for r in related:
            lines.append(f"- [[chats/{r['id']}|{r['title']}]]")
        body = body + "\n".join(lines)

    new_text = f"---\n{fm_yaml}\n---\n\n{body}\n"
    Path(node["path"]).write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_topic_graph(config: SynapseConfig, top_k: int = 8) -> dict[str, Any]:
    """
    Build topic_graph.json from vault/chats/*.md and update each chat file
    with related links. Returns a summary dict.
    """
    chats_dir = config.vault_path / "chats"
    if not chats_dir.exists():
        return {"error": "vault/chats/ directory not found"}

    import sys as _sys
    print("Loading chat nodes...", file=_sys.stderr, flush=True)
    nodes = _load_nodes(chats_dir)
    if not nodes:
        return {"error": "No chat files found in vault/chats/"}

    print(f"  {len(nodes)} nodes loaded. Building edges via inverted index...", file=_sys.stderr, flush=True)
    edges = _build_edges(nodes, top_k=top_k)
    print(f"  {len(edges)} edges computed.", file=_sys.stderr, flush=True)

    nodes_by_id = {n["chat_id"]: n for n in nodes}

    # Write topic_graph.json
    metadata_dir = config.vault_path / "metadata"
    metadata_dir.mkdir(exist_ok=True)
    graph_path = metadata_dir / "topic_graph.json"

    graph_json = {
        "nodes": {
            n["chat_id"]: {
                "title": n["title"],
                "path": f"chats/{n['chat_id']}.md",
                "tags": n["tags"],
                "categories": n["categories"],
                "projects": n["projects"],
            }
            for n in nodes
        },
        "edges": edges,
    }
    graph_path.write_text(json.dumps(graph_json, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  topic_graph.json written to {graph_path}", file=_sys.stderr, flush=True)

    # Update each chat .md file
    print("Updating chat files with related links...", file=_sys.stderr, flush=True)
    updated = 0
    for node in nodes:
        related = _related_for_node(node["chat_id"], edges, nodes_by_id)
        try:
            _update_chat_file(node, related)
            updated += 1
        except Exception as exc:
            print(f"  WARNING: could not update {node['chat_id']}: {exc}", file=_sys.stderr, flush=True)

    print(f"  {updated}/{len(nodes)} files updated.", file=_sys.stderr, flush=True)

    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "graph_path": str(graph_path),
        "files_updated": updated,
    }


# ---------------------------------------------------------------------------
# Deep search using graph traversal
# ---------------------------------------------------------------------------


def deep_search(
    config: SynapseConfig, query: str, depth: int = 2, top_k: int = 8
) -> list[dict[str, Any]]:
    """
    FTS5 entry search → graph expansion → ranked results.
    Returns top_k chat summaries most relevant to query.
    """
    from .index import MemoryIndex
    from .encryption import read_text as _read_text

    # Load graph
    graph_path = config.vault_path / "metadata" / "topic_graph.json"
    if not graph_path.exists():
        return [{"error": "topic_graph.json not found. Run memory_build_graph first."}]

    graph = json.loads(graph_path.read_text(encoding="utf-8", errors="replace"))
    edges = graph.get("edges", [])

    # Build adjacency: chat_id → [(weight, neighbor_id)]
    adjacency: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for e in edges:
        adjacency[e["source"]].append((e["weight"], e["target"]))
        adjacency[e["target"]].append((e["weight"], e["source"]))

    # FTS5 search restricted to chats/ folder
    # Use a larger limit so specific terms aren't crowded out by high-frequency generic terms
    index = MemoryIndex(config.vault_path, lambda path: _read_text(config, path))
    fts_hits = index.search(query, limit=50)
    chat_hits = [h for h in fts_hits if h["key"].startswith("chats.")]

    if not chat_hits:
        return []

    # Also run a secondary search on the most distinctive query terms alone
    # (filters out noise from common terms like "python", "rat" dominating)
    query_tokens_list = [t for t in _tokenise(query) if len(t) > 5]
    if query_tokens_list:
        secondary_query = " ".join(query_tokens_list[:4])
        secondary_hits = index.search(secondary_query, limit=20)
        secondary_chat_hits = [h for h in secondary_hits if h["key"].startswith("chats.")]
        # Merge: boost secondary hits that weren't in the primary top results
        primary_keys = {h["key"] for h in chat_hits[:20]}
        for h in secondary_chat_hits:
            if h["key"] not in primary_keys:
                h["score"] = h["score"] * 1.5  # boost distinctive-term-only matches
                chat_hits.append(h)

    # Collect entry node IDs from top FTS hits
    entry_ids: list[str] = []
    fts_score_map: dict[str, float] = {}
    for hit in chat_hits[:20]:
        # key format: chats.<uuid>
        cid = hit["key"][len("chats.") :]
        if cid not in fts_score_map:
            entry_ids.append(cid)
            fts_score_map[cid] = float(hit.get("score", 1.0))

    # Compute max possible edge weight for normalization
    max_edge_weight = max((e["weight"] for e in edges), default=1.0) or 1.0

    # Graph expansion: BFS up to `depth` hops
    # Direct FTS hits: score in [0, 1]. Graph neighbors: fraction of that.
    visited: dict[str, float] = {}  # chat_id → best score seen
    for cid in entry_ids:
        visited[cid] = fts_score_map.get(cid, 1.0)  # direct hit: FTS score [0,1]

    frontier = list(entry_ids)
    for hop in range(depth):
        next_frontier = []
        decay = 0.4 ** (hop + 1)  # hop1=0.4, hop2=0.16 — always below direct hit scores
        for cid in frontier:
            for weight, neighbor in adjacency.get(cid, []):
                norm_weight = weight / max_edge_weight  # normalize to [0,1]
                hop_score = fts_score_map.get(cid, visited.get(cid, 0.0)) * norm_weight * decay
                if neighbor not in visited or visited[neighbor] < hop_score:
                    visited[neighbor] = hop_score
                    next_frontier.append(neighbor)
        frontier = next_frontier

    # Score all candidates
    query_tokens = _tokenise(query)

    def _rank_score(chat_id: str) -> float:
        base = visited.get(chat_id, 0.0)
        node = graph["nodes"].get(chat_id, {})
        # Topic overlap bonus
        node_tokens = _tokenise(" ".join(node.get("tags", []) + node.get("projects", [])))
        overlap = len(query_tokens & node_tokens) if query_tokens else 0
        title_overlap = len(query_tokens & _tokenise(node.get("title", ""))) if query_tokens else 0
        return base + overlap * 0.5 + title_overlap * 1.0

    ranked = sorted(visited.keys(), key=_rank_score, reverse=True)

    # Load summaries for top results
    results = []
    chats_dir = config.vault_path / "chats"
    for cid in ranked[:top_k]:
        md_path = chats_dir / f"{cid}.md"
        if not md_path.exists():
            continue
        text = md_path.read_text(encoding="utf-8", errors="replace")
        fm, body = _parse_frontmatter(text)
        node_meta = graph["nodes"].get(cid, {})

        # Extract short summary (first non-heading paragraph after frontmatter)
        summary_lines = []
        for line in body.splitlines():
            if line.startswith("#") or not line.strip():
                if summary_lines:
                    break
                continue
            summary_lines.append(line.strip())
            if len(summary_lines) >= 3:
                break

        results.append(
            {
                "chat_id": cid,
                "key": f"chats.{cid}",
                "title": fm.get("title", cid),
                "tags": node_meta.get("tags", []),
                "projects": node_meta.get("projects", []),
                "graph_score": round(_rank_score(cid), 4),
                "is_direct_hit": cid in fts_score_map,
                "summary": " ".join(summary_lines)[:400],
                "related": fm.get("related", [])[:4],
            }
        )

    return results

from __future__ import annotations

import difflib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SynapseConfig
from .encryption import read_text, write_text
from .graph import related_file_ids
from .git_manager import commit_paths
from .index import MemoryIndex
from .memory_file import (
    ensure_history_entry,
    key_to_path,
    new_frontmatter,
    parse_memory_text,
    read_memory_file,
    render_memory_file,
)
from .schema import validate_memory

SENSITIVITY_VALUES = {"low", "medium", "high"}

_STOPWORDS = {
    "this",
    "that",
    "with",
    "from",
    "have",
    "will",
    "been",
    "were",
    "they",
    "their",
    "what",
    "when",
    "where",
    "which",
    "there",
    "these",
    "those",
    "also",
    "only",
    "just",
    "like",
    "into",
    "than",
    "then",
    "more",
    "some",
    "each",
    "does",
    "would",
    "could",
    "should",
    "about",
    "after",
    "before",
    "every",
    "never",
    "always",
    "added",
    "used",
    "uses",
    "using",
    "built",
    "runs",
    "call",
    "calls",
    "return",
    "returns",
    "function",
    "import",
    "export",
    "class",
    "const",
    "async",
    "await",
    "history",
    "initial",
    "entry",
    "sample",
    "current",
    "updated",
    "update",
    "imported",
    "approved",
    "memory",
    "file",
    "code",
    "note",
    "step",
    "test",
    "true",
    "false",
    "null",
    "none",
    "type",
    "list",
    "dict",
    "string",
    "value",
    "data",
    "path",
    "name",
    "user",
    "local",
    "global",
    "default",
    "config",
    # noise words that appear frequently but carry no signal
    "across",
    "inferred",
    "server",
    "client",
    "available",
    "project",
    "projects",
    "pattern",
    "patterns",
    "version",
    "same",
    "both",
    "kept",
    "known",
    "means",
    "keeps",
    "runs",
    "sends",
    "makes",
    "lives",
    "stay",
    "install",
    "installed",
    "notes",
    "note",
    "existing",
    "final",
    "state",
    "intended",
    "intended",
    "named",
    "daily",
    "primary",
    "main",
    "between",
}


def _extract_triggers(content: str) -> list[str]:
    # Strip markdown code spans and filenames before extracting
    cleaned = re.sub(r"`[^`]+`", " ", content)  # strip `code spans`
    cleaned = re.sub(r"\S+\.\w{2,4}\b", " ", cleaned)  # strip file.ext tokens
    cleaned = re.sub(r"https?://\S+", " ", cleaned)  # strip URLs
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9+#-]*\b", cleaned)
    freq: dict[str, int] = {}
    for w in words:
        wl = w.lower()
        if len(wl) >= 4 and wl not in _STOPWORDS and not wl.isdigit():
            freq[wl] = freq.get(wl, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])][:8]


def _load_project_graph(vault_path: Path, key: str) -> dict | None:
    parts = key.split(".")
    if len(parts) < 2 or parts[0] != "projects":
        return None
    graph_path = vault_path / "projects" / parts[1] / "_graph.json"
    if not graph_path.exists():
        return None
    try:
        import json as _json

        return _json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _inject_wikilinks(content: str, related_keys: list[str]) -> str:
    lines = [ln for ln in content.split("\n") if not ln.startswith("Related:")]
    body = "\n".join(lines).rstrip()
    if not related_keys:
        return body
    links = " | ".join(f"[[{k.replace('.', '/')}]]" for k in related_keys)
    return f"{body}\n\nRelated: {links}"


def _compute_related(vault_path: Path, current_key: str, triggers: list[str]) -> list[str]:
    graph = _load_project_graph(vault_path, current_key)
    if graph:
        parts = current_key.split(".")
        if len(parts) >= 3 and parts[0] == "projects":
            project_prefix = f"{parts[0]}.{parts[1]}"
            node_id = "-".join(parts[2:])
            structural = related_file_ids(graph, node_id, limit=5)
            if structural:
                return [f"{project_prefix}.{fid}" for fid in structural]

    if not triggers:
        return []
    try:
        index = MemoryIndex(vault_path)
        query = " OR ".join(f'"{t}"' for t in triggers[:6])
        results = index.search(query, limit=10)
        return [r["key"] for r in results if r["key"] != current_key][:5]
    except Exception:
        return []


def _refresh_related(
    config: SynapseConfig, path: Path, index: MemoryIndex, force: bool = False
) -> None:
    """Recompute triggers and related links for a memory file."""
    try:
        frontmatter, content = read_memory_file(path)
        key = str(frontmatter.get("key", ""))
        if not key:
            return

        changed = False
        frontmatter = dict(frontmatter)

        # Normalize existing related: slash-paths -> dot-notation
        raw_related = [r.replace("/", ".") for r in frontmatter.get("related", [])]

        triggers = list(frontmatter.get("triggers", []))
        if not triggers or force:
            new_triggers = _extract_triggers(content)
            if new_triggers and new_triggers != triggers:
                frontmatter["triggers"] = new_triggers
                triggers = new_triggers
                changed = True

        # Code nodes (type: code) have their related set by the scanner from real
        # call-graph edges — don't overwrite with trigger-based vault graph lookup.
        if frontmatter.get("type") != "code":
            computed = _compute_related(config.vault_path, key, triggers)
            if set(computed) != set(raw_related):
                frontmatter["related"] = computed
                changed = True
            elif raw_related != frontmatter.get("related", []):
                frontmatter["related"] = raw_related
                changed = True

        # Always re-inject wikilinks so Obsidian graph shows edges
        effective_related = frontmatter.get("related", [])
        new_content = _inject_wikilinks(content, effective_related)
        if new_content != content:
            changed = True
            content = new_content

        if changed:
            new_text = render_memory_file(frontmatter, content)
            write_text(config, path, new_text)
            index.upsert_file(path)
    except Exception:
        pass


def pending_path(config: SynapseConfig) -> Path:
    return config.vault_path / "_pending.json"


def propose_update(config: SynapseConfig, patch: dict[str, Any]) -> dict[str, Any]:
    patch = _normalize_write_rules(dict(patch))
    key = str(patch.get("key", "")).strip()
    if not key:
        raise ValueError("patch.key is required")
    sensitivity = str(patch.get("sensitivity", "medium"))
    if sensitivity not in SENSITIVITY_VALUES:
        raise ValueError("patch.sensitivity must be low, medium, or high")

    before_text = _current_text(config, key)
    after_text = _build_after_text(config, patch)
    frontmatter, content = _parse_rendered(after_text)
    errors = validate_memory(frontmatter, content)
    if errors:
        raise ValueError("; ".join(errors))

    patch_id = uuid.uuid4().hex[:12]
    item = {
        "patch_id": patch_id,
        "created_at": _now(),
        "key": key,
        "reason": patch.get("reason", ""),
        "sensitivity": sensitivity,
        "urgent": sensitivity == "high" or bool(patch.get("urgent", False)),
        "session_id": patch.get("session_id", ""),
        "trigger_reason": patch.get("trigger_reason", patch.get("reason", "")),
        "before": before_text,
        "after": after_text,
        "diff": _diff(before_text, after_text, key),
        "status": "pending",
    }
    conflicts = detect_conflicts(config, after_patch=item, scope_key=key)
    if conflicts:
        item["urgent"] = True
        item["conflict_warning"] = conflicts

    queue = load_pending(config)
    queue.append(item)
    save_pending(config, queue)
    return {
        "patch_id": patch_id,
        "diff": item["diff"],
        "conflicts": conflicts,
    }


def _compute_freshness(fm: dict[str, Any]) -> float:
    """Freshness in [0, 1]: decays with age, boosted by retrieval, penalised by corrections."""
    import math
    from datetime import date as _date
    try:
        last_used = str(fm.get("last_used") or fm.get("last_updated") or _date.today().isoformat())
        days = (_date.today() - _date.fromisoformat(last_used)).days
    except (ValueError, TypeError):
        days = 0
    base = math.exp(-days / 90)  # half-life ~62 days
    retrieval_boost = min(1.5, 1.0 + 0.05 * int(fm.get("retrieval_count", 0)))
    correction_penalty = max(0.5, 1.0 - 0.1 * int(fm.get("correction_count", 0)))
    return round(min(1.0, base * retrieval_boost * correction_penalty), 4)


def apply_update(config: SynapseConfig, patch_id: str) -> dict[str, Any]:
    queue = load_pending(config)
    item = _find_patch(queue, patch_id)
    if not item:
        raise ValueError(f"Patch not found: {patch_id}")

    path = key_to_path(config.vault_path, item["key"])
    path.parent.mkdir(parents=True, exist_ok=True)

    # Preserve correction_count and retrieval_count from existing file
    after_text = item["after"]
    if path.exists():
        existing_fm, _ = read_memory_file(path)
        after_fm, after_content = parse_memory_text(after_text)
        after_fm = dict(after_fm)
        after_fm["retrieval_count"] = int(existing_fm.get("retrieval_count", 0))
        after_fm["correction_count"] = int(existing_fm.get("correction_count", 0)) + 1
        after_fm["freshness"] = _compute_freshness(after_fm)
        after_text = render_memory_file(after_fm, after_content)

    write_text(config, path, after_text)
    index = MemoryIndex(config.vault_path, lambda candidate: read_text(config, candidate))
    index.upsert_file(path)
    _refresh_related(config, path, index)

    queue = [patch for patch in queue if patch["patch_id"] != patch_id]
    save_pending(config, queue)
    git = commit_paths(
        config,
        [path, pending_path(config), config.vault_path / "_index.db"],
        f"Synapse memory update: {item['key']}",
    )
    return {
        "status": "applied",
        "patch_id": patch_id,
        "key": item["key"],
        "file_path": str(path.relative_to(config.vault_path)).replace("\\", "/"),
        "git": git,
    }


def reject_update(config: SynapseConfig, patch_id: str, reason: str = "") -> dict[str, Any]:
    queue = load_pending(config)
    item = _find_patch(queue, patch_id)
    if not item:
        raise ValueError(f"Patch not found: {patch_id}")
    rejected = _rejection_log_path(config)
    with rejected.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"rejected_at": _now(), "patch_id": patch_id, "key": item["key"], "reason": reason}
            )
            + "\n"
        )
    queue = [patch for patch in queue if patch["patch_id"] != patch_id]
    save_pending(config, queue)
    return {"status": "rejected", "patch_id": patch_id, "key": item["key"]}


def list_pending(config: SynapseConfig) -> list[dict[str, Any]]:
    return [
        {
            "patch_id": item["patch_id"],
            "created_at": item["created_at"],
            "key": item["key"],
            "reason": item["reason"],
            "sensitivity": item["sensitivity"],
            "urgent": item["urgent"],
            "session_id": item.get("session_id", ""),
            "trigger_reason": item.get("trigger_reason", ""),
            "conflict_warning": item.get("conflict_warning", []),
            "diff": item["diff"],
        }
        for item in load_pending(config)
    ]


def detect_conflicts(
    config: SynapseConfig,
    after_patch: dict[str, Any] | None = None,
    scope_key: str = "",
) -> list[dict[str, str]]:
    """
    Scan active vault files for factual contradictions.

    When scope_key is given (e.g. during propose_update), only files that share
    the same top-level folder prefix OR have overlapping triggers are compared —
    keeping O(n) instead of O(n²) and minimising false positives.

    Excludes chats/ and raw/ — narrative text generates too many false positives.
    """
    EXCLUDED_FOLDERS = {"chats", "raw"}
    memories: list[dict[str, Any]] = []

    # Collect all candidate files
    for path in sorted(config.vault_path.rglob("*.md")):
        if path.name.startswith("_"):
            continue
        parts = path.relative_to(config.vault_path).parts
        if any(p in EXCLUDED_FOLDERS for p in parts):
            continue
        fm, content = parse_memory_text(read_text(config, path))
        key = str(fm.get("key", ""))
        if not key:
            continue
        memories.append({
            "key": key,
            "content": content.lower(),
            "prefix": key.split(".")[0] if "." in key else key,
            "triggers": set(fm.get("triggers", [])),
        })

    if after_patch:
        fm, content = _parse_rendered(after_patch["after"])
        patch_key = str(fm.get("key", after_patch["key"]))
        memories.append({
            "key": patch_key,
            "content": content.lower(),
            "prefix": patch_key.split(".")[0] if "." in patch_key else patch_key,
            "triggers": set(fm.get("triggers", [])),
        })

    # When scoped, only compare the incoming patch against same-topic files
    if scope_key and after_patch:
        incoming = memories[-1]
        candidates = [
            m for m in memories[:-1]
            if m["prefix"] == incoming["prefix"] or (m["triggers"] & incoming["triggers"])
        ]
        pairs = [(incoming, c) for c in candidates]
    else:
        # Full O(n²) scan (used by memory_conflicts MCP tool)
        pairs = [(memories[i], memories[j]) for i in range(len(memories)) for j in range(i + 1, len(memories))]

    conflicts: list[dict[str, str]] = []
    for left, right in pairs:
        explanation = _conflict_explanation(left["content"], right["content"])
        if explanation:
            conflicts.append({"left": left["key"], "right": right["key"], "explanation": explanation})
    return conflicts


def cleanup_stale_nodes(
    config: SynapseConfig, project_slug: str, graph: dict[str, Any]
) -> dict[str, Any]:
    """
    Remove vault nodes that no longer exist in the current code graph.
    Only touches files that were actually scanned — leaves unscanned files alone.
    """
    project_dir = config.vault_path / "projects" / project_slug
    if not project_dir.exists():
        return {"removed": [], "count": 0}

    project_key = f"projects.{project_slug}"

    scanned_file_ids: set[str] = set()
    expected_fn_slugs: dict[str, set[str]] = {}

    for node in graph.get("nodes", []):
        ntype = node.get("type")
        node_id = node["id"]
        if ntype == "file":
            scanned_file_ids.add(node_id)
            expected_fn_slugs.setdefault(node_id, set())
        elif ntype in ("function", "class"):
            file_id = node.get("parent", "")
            fn_slug = node_id[len(file_id) + 1 :] if node_id.startswith(file_id + "-") else node_id
            expected_fn_slugs.setdefault(file_id, set()).add(fn_slug)

    removed: list[str] = []

    for file_id in scanned_file_ids:
        fn_dir = project_dir / file_id
        if not fn_dir.is_dir():
            continue
        expected = expected_fn_slugs.get(file_id, set())
        for md in list(fn_dir.glob("*.md")):
            if md.name.startswith("_"):
                continue
            if md.stem not in expected:
                md.unlink()
                removed.append(f"{project_key}.{file_id}.{md.stem}")
        if fn_dir.is_dir() and not any(fn_dir.iterdir()):
            fn_dir.rmdir()

    if removed:
        MemoryIndex(config.vault_path, lambda p: read_text(config, p)).rebuild()

    return {"removed": removed, "count": len(removed)}


def relink_all(config: SynapseConfig) -> dict[str, Any]:
    """Recompute triggers and related links for every memory file in the vault."""
    index = MemoryIndex(config.vault_path, lambda p: read_text(config, p))
    index.rebuild()
    updated = []
    for path in sorted(config.vault_path.rglob("*.md")):
        if path.name.startswith("_"):
            continue
        _refresh_related(config, path, index, force=True)
        updated.append(str(path.relative_to(config.vault_path)).replace("\\", "/"))
    return {"status": "relinked", "files": updated}


def load_pending(config: SynapseConfig) -> list[dict[str, Any]]:
    path = pending_path(config)
    if not path.exists():
        return []
    try:
        queue = json.loads(path.read_text(encoding="utf-8").strip() or "[]")
    except json.JSONDecodeError:
        return []
    expire_days = int(getattr(config, "pending_auto_expire_days", 90) or 90)
    if expire_days > 0:
        cutoff = datetime.now(timezone.utc).timestamp() - expire_days * 86400
        pruned = []
        changed = False
        for item in queue:
            try:
                ts = datetime.fromisoformat(item["created_at"]).timestamp()
            except (KeyError, ValueError):
                ts = cutoff + 1  # keep items with bad timestamps
            if ts >= cutoff:
                pruned.append(item)
            else:
                changed = True
        if changed:
            save_pending(config, pruned)
            return pruned
    return queue


def save_pending(config: SynapseConfig, queue: list[dict[str, Any]]) -> None:
    path = pending_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(queue, indent=2), encoding="utf-8")


def _build_after_text(config: SynapseConfig, patch: dict[str, Any]) -> str:
    key = str(patch["key"])
    path = key_to_path(config.vault_path, key)
    if path.exists():
        frontmatter, existing_content = read_memory_file(path)
        frontmatter = dict(frontmatter)
        frontmatter.update(patch.get("frontmatter", {}))
        if patch.get("related"):
            frontmatter["related"] = list(patch["related"])
        frontmatter["version"] = int(frontmatter.get("version", 0)) + 1
        frontmatter["last_updated"] = datetime.now().date().isoformat()
        new_content = str(patch.get("content", existing_content)).strip()
        merge_mode = str(patch.get("merge", "replace")).lower()
        if merge_mode == "append":
            content = f"{existing_content.strip()}\n\n{new_content}" if existing_content.strip() else new_content
        elif merge_mode == "prepend":
            content = f"{new_content}\n\n{existing_content.strip()}" if existing_content.strip() else new_content
        else:
            content = new_content
    else:
        frontmatter = new_frontmatter(
            key,
            memory_type=str(patch.get("type", "note")),
            scope=str(patch.get("scope", "global")),
            weight=float(patch.get("weight", 0.5)),
            confidence=str(patch.get("confidence", "proposed")),
            triggers=list(patch.get("triggers", [])),
            related=list(patch.get("related", [])),
        )
        frontmatter.update(patch.get("frontmatter", {}))
        content = str(patch.get("content", "")).strip()
    # Inject provenance into frontmatter
    session_id = patch.get("session_id", "")
    if session_id:
        frontmatter["source_session"] = session_id
    _GENERIC_REASONS = {
        "High-signal memory moment.",
        "Approved memory update.",
        "Repeated casual-session pattern.",
        "Manual remember-this request.",
    }
    history = str(patch.get("history") or patch.get("reason") or "")
    if history and history not in _GENERIC_REASONS:
        content = ensure_history_entry(content, history, session_id=session_id)
    if not frontmatter.get("triggers"):
        frontmatter["triggers"] = _extract_triggers(content)
    frontmatter["related"] = [r.replace("/", ".") for r in frontmatter.get("related", [])]
    return render_memory_file(frontmatter, content)


def _normalize_write_rules(patch: dict[str, Any]) -> dict[str, Any]:
    signal = str(patch.get("signal", "high_signal")).lower()
    if signal == "casual":
        occurrences = int(patch.get("session_occurrences", 1))
        if occurrences < 2:
            raise ValueError(
                "Casual chat proposals require the pattern to appear 2+ times in session"
            )
        patch["sensitivity"] = "low"
        patch.setdefault("reason", "Repeated casual-session pattern.")
    elif signal in {"manual", "manual_flag", "remember"}:
        patch["sensitivity"] = "high"
        patch["urgent"] = True
        patch.setdefault("reason", "Manual remember-this request.")
    else:
        patch.setdefault("sensitivity", "medium")
        patch.setdefault("reason", "High-signal memory moment.")
    return patch


def _current_text(config: SynapseConfig, key: str) -> str:
    path = key_to_path(config.vault_path, key)
    if not path.exists():
        return ""
    return read_text(config, path)


def _diff(before: str, after: str, key: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"{key}:before",
            tofile=f"{key}:after",
            lineterm="",
        )
    )


def _find_patch(queue: list[dict[str, Any]], patch_id: str) -> dict[str, Any] | None:
    return next((patch for patch in queue if patch["patch_id"] == patch_id), None)


def _parse_rendered(text: str) -> tuple[dict[str, Any], str]:
    from .memory_file import parse_memory_text

    return parse_memory_text(text)


_CONFLICT_PAIRS: list[tuple[str, str]] = [
    # Database / storage
    ("prefer firebase", "prefer postgresql"),
    ("prefer firebase", "prefer supabase"),
    ("prefer firebase", "prefer sqlite"),
    ("prefer firebase", "prefer mongodb"),
    ("uses firebase", "uses postgresql"),
    ("uses firebase", "uses supabase"),
    ("uses firebase", "uses sqlite"),
    ("prefer postgresql", "prefer mongodb"),
    ("prefer postgresql", "prefer sqlite"),
    ("prefer supabase", "prefer sqlite"),
    # Cloud stance
    ("no cloud", "cloud enabled"),
    ("local-first", "cloud-first"),
    ("self-hosted", "cloud-hosted"),
    ("offline-first", "always-online"),
    # Framework
    ("react native", "flutter"),
    ("uses react", "uses vue"),
    ("uses react", "uses svelte"),
    ("uses react", "uses angular"),
    ("uses vue", "uses svelte"),
    ("uses nextjs", "uses nuxt"),
    ("prefers typescript", "prefers javascript"),
    ("uses tailwind", "uses bootstrap"),
    ("uses tailwind", "uses material ui"),
    # AI / provider
    ("uses openai", "uses anthropic"),
    ("uses gemini", "uses openai"),
    ("uses cerebras", "uses openai"),
    ("prefer openai", "prefer anthropic"),
    ("prefer gemini", "prefer openai"),
    # Write modes
    ("write_mode: auto", "write_mode: manual"),
    ("write_mode: auto", "write_mode: review"),
    ("write_mode: manual", "write_mode: review"),
    # Language
    ("prefers python", "prefers go"),
    ("prefers python", "prefers rust"),
    ("prefers python", "prefers typescript"),
    ("prefers go", "prefers rust"),
    ("prefers go", "prefers typescript"),
    # Lifecycle / status
    ("project: active", "project: abandoned"),
    ("project: active", "project: archived"),
    ("project: abandoned", "project: archived"),
    # Auth
    ("uses jwt", "uses session cookies"),
    ("uses oauth", "uses api keys"),
    # Mobile
    ("ios only", "android only"),
    ("ios only", "cross-platform"),
    ("android only", "cross-platform"),
    # Encryption
    ("encryption: true", "encryption: false"),
    ("encryption enabled", "encryption disabled"),
    # Git
    ("git enabled", "git disabled"),
    ("git_enabled: true", "git_enabled: false"),
    # Extraction provider
    ("extraction_provider: cerebras", "extraction_provider: gemini"),
    ("extraction_provider: cerebras", "extraction_provider: groq"),
    ("extraction_provider: gemini", "extraction_provider: groq"),
]


def _conflict_explanation(left: str, right: str) -> str:
    """
    Check for factual contradictions between two active vault files.
    Both terms must appear to match — no generic negation heuristics.
    """
    def lines_containing(text: str, term: str) -> list[str]:
        return [ln for ln in text.splitlines() if term in ln]

    for a, b in _CONFLICT_PAIRS:
        if lines_containing(left, a) and lines_containing(right, b):
            return f"Contradiction: '{a}' vs '{b}'."
        if lines_containing(left, b) and lines_containing(right, a):
            return f"Contradiction: '{b}' vs '{a}'."
    return ""


def _rejection_log_path(config: SynapseConfig) -> Path:
    return config.vault_path / "_rejections.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

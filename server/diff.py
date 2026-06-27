from __future__ import annotations

import difflib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
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
        "before": before_text,
        "after": after_text,
        "diff": _diff(before_text, after_text, key),
        "status": "pending",
    }
    queue = load_pending(config)
    queue.append(item)
    save_pending(config, queue)
    return {
        "patch_id": patch_id,
        "diff": item["diff"],
        "conflicts": detect_conflicts(config, after_patch=item),
    }


def apply_update(config: SynapseConfig, patch_id: str) -> dict[str, Any]:
    queue = load_pending(config)
    item = _find_patch(queue, patch_id)
    if not item:
        raise ValueError(f"Patch not found: {patch_id}")

    path = key_to_path(config.vault_path, item["key"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(config, path, item["after"])
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
            "diff": item["diff"],
        }
        for item in load_pending(config)
    ]


def detect_conflicts(
    config: SynapseConfig, after_patch: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """
    Scan active vault files for factual contradictions.
    Intentionally excludes chats/ — chat summaries are narrative and will
    always contain words like 'never'/'always' in different contexts, causing
    O(n²) false positives across thousands of files.
    """
    EXCLUDED_FOLDERS = {"chats", "raw"}
    memories: list[dict[str, str]] = []
    for path in sorted(config.vault_path.rglob("*.md")):
        # Skip index/system files
        if path.name.startswith("_"):
            continue
        # Skip chat archive and raw folders
        parts = path.relative_to(config.vault_path).parts
        if any(p in EXCLUDED_FOLDERS for p in parts):
            continue
        fm, content = parse_memory_text(read_text(config, path))
        key = str(fm.get("key", ""))
        if not key:
            continue
        memories.append({"key": key, "content": content.lower()})

    if after_patch:
        fm, content = _parse_rendered(after_patch["after"])
        memories.append({"key": str(fm.get("key", after_patch["key"])), "content": content.lower()})

    conflicts: list[dict[str, str]] = []
    for i, left in enumerate(memories):
        for right in memories[i + 1 :]:
            explanation = _conflict_explanation(left["content"], right["content"])
            if explanation:
                conflicts.append(
                    {"left": left["key"], "right": right["key"], "explanation": explanation}
                )
    return conflicts


def cleanup_stale_nodes(
    config: SynapseConfig, project_slug: str, graph: dict[str, Any]
) -> dict[str, Any]:
    """
    Remove vault nodes that no longer exist in the current code graph.
    Only touches files that were actually scanned — leaves unscanned files alone.
    Three cases handled:
      1. Function node whose parent file was scanned but function no longer exists
      2. Empty function-node directory left after all children removed
      3. File-level node whose source file no longer exists on disk
    """
    project_dir = config.vault_path / "projects" / project_slug
    if not project_dir.exists():
        return {"removed": [], "count": 0}

    project_key = f"projects.{project_slug}"

    # Build expected state from graph
    scanned_file_ids: set[str] = set()
    expected_fn_slugs: dict[str, set[str]] = {}  # file_id -> {fn_slug, ...}

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

    # Case 1 & 2: stale function nodes within scanned file directories
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
        # Remove directory if now empty
        if fn_dir.is_dir() and not any(fn_dir.iterdir()):
            fn_dir.rmdir()

    # Note: file-level node deletion (when a source file is removed from disk) is
    # intentionally NOT done here — the scanner only reads a budget-limited subset
    # of files, so absence from the current graph does not mean the file was deleted.

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
        raw = json.loads(path.read_text(encoding="utf-8").strip() or "[]")
    except json.JSONDecodeError:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.pending_auto_expire_days)).isoformat()
    live = [p for p in raw if p.get("created_at", "9999") >= cutoff]
    if len(live) < len(raw):
        save_pending(config, live)
    return live


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
        # Top-level 'related' in the patch overrides the stored one so that
        # code-graph call edges and scanner-provided links are not lost on updates.
        if patch.get("related"):
            frontmatter["related"] = list(patch["related"])
        frontmatter["version"] = int(frontmatter.get("version", 0)) + 1
        frontmatter["last_updated"] = datetime.now().date().isoformat()
        new_content = str(patch.get("content", "")).strip()
        merge = str(patch.get("merge", "replace")).lower()
        if merge == "append" and new_content:
            content = existing_content.rstrip() + "\n\n" + new_content
        elif merge == "prepend" and new_content:
            content = new_content + "\n\n" + existing_content.lstrip()
        else:
            content = new_content if new_content else existing_content
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
        # merge modes only apply to existing files; for new files always use content as-is
    _GENERIC_REASONS = {
        "High-signal memory moment.",
        "Approved memory update.",
        "Repeated casual-session pattern.",
        "Manual remember-this request.",
    }
    history = str(patch.get("history") or patch.get("reason") or "")
    if history and history not in _GENERIC_REASONS:
        content = ensure_history_entry(content, history)
    # Auto-extract triggers when not supplied
    if not frontmatter.get("triggers"):
        frontmatter["triggers"] = _extract_triggers(content)
    # Normalize any related paths to dot-notation
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


def _conflict_explanation(left: str, right: str) -> str:
    """
    Check for factual contradictions between two active vault files.
    Rules:
    - Only meaningful technology/preference pairs (not generic words like never/always)
    - Both terms must appear on the same line as a keyword anchor so we're comparing
      facts about the same topic, not unrelated sentences
    """
    pairs = [
        ("prefer firebase", "prefer postgresql"),
        ("prefer firebase", "prefer supabase"),
        ("uses firebase", "uses postgresql"),
        ("uses firebase", "uses supabase"),
        ("no cloud", "cloud enabled"),
        ("local-first", "cloud-first"),
        ("react native", "flutter"),
        ("write_mode: auto", "write_mode: review"),
    ]

    def lines_containing(text: str, term: str) -> list[str]:
        return [ln for ln in text.splitlines() if term in ln]

    for a, b in pairs:
        a_lines = lines_containing(left, a)
        b_lines = lines_containing(right, b)
        if a_lines and b_lines:
            return f"Potential contradiction: '{a}' vs '{b}'."
        a_lines = lines_containing(left, b)
        b_lines = lines_containing(right, a)
        if a_lines and b_lines:
            return f"Potential contradiction: '{b}' vs '{a}'."
    return ""


def _rejection_log_path(config: SynapseConfig) -> Path:
    return config.vault_path / "_rejections.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

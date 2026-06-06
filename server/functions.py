from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import SynapseConfig
from .dedup import memory_deduplicate as _dedup
from .diff import (
    apply_update,
    detect_conflicts,
    list_pending,
    propose_update,
    reject_update,
    relink_all,
)
from .organizer import organize_vault
from .watcher import start_watcher, stop_watcher, watcher_status
from .encryption import read_text
from .index import MemoryIndex
from .memory_file import key_to_path, path_to_key, parse_memory_text
from .ai_importer import (
    import_ai_export as _import_ai_export,
    ingest_text as _ingest_text,
    import_filtered_jsonl as _import_filtered_jsonl,
    import_synapse_summaries as _import_synapse_summaries,
    save_chat_memory as _save_chat_memory,
)
from .graph_builder import build_topic_graph as _build_topic_graph, deep_search as _deep_search
from .raw_archive import (
    get_raw_conversation as _get_raw,
    get_raw_chunks as _get_raw_chunks,
    search_raw_index as _search_raw_index,
)
from .merger import smart_merge_duplicates as _smart_merge
from .triage import run_triage as _run_triage
from .scanner import scan_and_extract
from .search import memory_search


def _estimate_tokens(obj: Any) -> int:
    """Rough token estimate: 1 token ≈ 4 characters of JSON-serialised text."""
    try:
        import json as _json

        return max(1, len(_json.dumps(obj, ensure_ascii=False)) // 4)
    except Exception:
        return 0


def memory_tree(config: SynapseConfig) -> dict[str, Any]:
    """Return a nested JSON directory tree for the configured vault."""
    vault = config.vault_path
    _ensure_vault_path(vault)
    return _tree_node(vault, vault)


def memory_get(config: SynapseConfig, key: str) -> dict[str, Any]:
    """Return parsed frontmatter and content for a dot-notation memory key."""
    vault = config.vault_path
    _ensure_vault_path(vault)
    file_path = key_to_path(vault, key)
    if not file_path.exists():
        raise ValueError(f"Memory key not found: {key}")
    if not file_path.is_file() or file_path.suffix.lower() != ".md":
        raise ValueError(f"Memory key does not resolve to a Markdown file: {key}")

    frontmatter, content = parse_memory_text(read_text(config, file_path))
    result = {
        "key": frontmatter.get("key", key),
        "file_path": str(file_path.relative_to(vault)).replace("\\", "/"),
        "frontmatter": frontmatter,
        "content": content,
    }
    result["_tokens"] = _estimate_tokens(result)
    return result


def memory_search_tool(config: SynapseConfig, query: str) -> list[dict[str, Any]]:
    return memory_search(config, query)


def memory_propose_update(config: SynapseConfig, patch: dict[str, Any]) -> dict[str, Any]:
    return propose_update(config, patch)


def memory_apply_update(config: SynapseConfig, patch_id: str) -> dict[str, Any]:
    return apply_update(config, patch_id)


def memory_reject_update(config: SynapseConfig, patch_id: str, reason: str = "") -> dict[str, Any]:
    return reject_update(config, patch_id, reason)


def memory_diff(config: SynapseConfig) -> list[dict[str, Any]]:
    return list_pending(config)


def memory_conflicts(config: SynapseConfig) -> list[dict[str, str]]:
    return detect_conflicts(config)


def memory_scan_project(config: SynapseConfig, path: str) -> dict[str, Any]:
    result = scan_and_extract(config, path)
    if "error" in result and "proposals" not in result:
        return result

    patch_ids: list[Any] = []
    errors: list[dict[str, str]] = []
    for proposal in result.get("proposals", []):
        try:
            r = propose_update(config, proposal)
            patch_ids.append(r["patch_id"])
        except Exception as exc:
            errors.append({"key": proposal.get("key", "?"), "error": str(exc)})

    return {
        "project_name": result.get("project_name", ""),
        "detected": result.get("detected", {}),
        "files_analyzed": result.get("file_count", 0),
        "function_nodes": result.get("function_nodes", 0),
        "stale_removed": result.get("stale_removed", 0),
        "patches_proposed": len(patch_ids),
        "patch_ids": patch_ids,
        "errors": errors,
        "next_step": "Call memory.diff to review, then memory.apply_update per patch_id.",
    }


def memory_ingest_text(
    config: SynapseConfig, text: str, label: str = "[pasted text]"
) -> dict[str, Any]:
    result = _ingest_text(config, text, label)
    if "error" in result:
        return result

    patch_ids: list[Any] = []
    errors: list[dict[str, str]] = []
    for proposal in result.get("proposals", []):
        try:
            r = propose_update(config, proposal)
            patch_ids.append(r["patch_id"])
        except Exception as exc:
            errors.append({"key": proposal.get("key", "?"), "error": str(exc)})

    return {
        "patches_proposed": len(patch_ids),
        "patch_ids": patch_ids,
        "errors": errors,
        "next_step": "Call memory.diff to review, then memory.apply_update per patch_id.",
    }


def memory_smart_merge(
    config: SynapseConfig, dry_run: bool = True, threshold: float = 0.93
) -> dict[str, Any]:
    return _smart_merge(config, dry_run=dry_run, threshold=threshold)


def memory_import_ai_export(
    config: SynapseConfig,
    path: str,
    owner_name: str | None = None,
    resume_failed: bool = False,
) -> dict[str, Any]:
    result = _import_ai_export(config, path, owner_name, resume_failed=resume_failed)
    if "error" in result and "proposals" not in result:
        return result

    patch_ids: list[Any] = []
    errors: list[dict[str, str]] = []
    for proposal in result.get("proposals", []):
        try:
            r = propose_update(config, proposal)
            patch_ids.append(r["patch_id"])
        except Exception as exc:
            errors.append({"key": proposal.get("key", "?"), "error": str(exc)})

    return {
        "provider": result.get("provider", "unknown"),
        "chunks_processed": result.get("chunks_processed", 0),
        "owner_detected": result.get("owner_detected"),
        "identity_warning": result.get("identity_warning"),
        "failed_chunks": result.get("failed_chunks", 0),
        "resume_file": result.get("resume_file"),
        "patches_proposed": len(patch_ids),
        "patch_ids": patch_ids,
        "errors": errors,
        "next_step": "Call memory.diff to review, then memory.apply_update per patch_id.",
    }


def memory_import_filtered_jsonl(
    config: SynapseConfig,
    filtered_jsonl_folder: str,
    blacklist_file: str | None = None,
    redflag_file: str | None = None,
    owner_name: str | None = None,
) -> dict[str, Any]:
    result = _import_filtered_jsonl(
        config, filtered_jsonl_folder, blacklist_file, redflag_file, owner_name
    )
    if "error" in result and "proposals" not in result:
        return result

    patch_ids: list[Any] = []
    errors: list[dict[str, str]] = []
    for proposal in result.get("proposals", []):
        try:
            r = propose_update(config, proposal)
            patch_ids.append(r["patch_id"])
        except Exception as exc:
            errors.append({"key": proposal.get("key", "?"), "error": str(exc)})

    return {
        "provider": result.get("provider", "gemma"),
        "model": result.get("model"),
        "chunks_processed": result.get("chunks_processed", 0),
        "failed_chunks": result.get("failed_chunks", 0),
        "owner_detected": result.get("owner_detected"),
        "patches_proposed": len(patch_ids),
        "patch_ids": patch_ids,
        "errors": errors,
        "next_step": "Call memory.diff to review, then memory.apply_update per patch_id.",
    }


def memory_import_synapse_summaries(
    config: SynapseConfig,
    summaries_folder: str,
    owner_name: str | None = None,
) -> dict[str, Any]:
    return _import_synapse_summaries(config, summaries_folder, owner_name)


def memory_organize_vault(config: SynapseConfig) -> dict[str, Any]:
    return organize_vault(config)


def memory_relink_all(config: SynapseConfig) -> dict[str, Any]:
    return relink_all(config)


def memory_start_watcher(config: SynapseConfig, path: str) -> dict[str, Any]:
    return start_watcher(config, path)


def memory_stop_watcher() -> dict[str, Any]:
    return stop_watcher()


def memory_watcher_status() -> dict[str, Any]:
    return watcher_status()


def memory_dedup(config: SynapseConfig, auto_clean: bool = False) -> dict[str, Any]:
    return _dedup(config, auto_clean=auto_clean)


def memory_list_folder(config: SynapseConfig, folder: str = "") -> dict[str, Any]:
    """Shallow listing of one vault folder — cheap alternative to memory_tree."""
    vault = config.vault_path
    _ensure_vault_path(vault)
    target = vault / folder if folder else vault
    if not target.exists() or not target.is_dir():
        raise ValueError(f"Folder not found: {folder!r}")
    files, folders = [], []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if item.name.startswith("_") or item.name.startswith("."):
            continue
        if item.is_dir():
            folders.append(item.name)
        elif item.suffix.lower() == ".md":
            files.append(path_to_key(vault, item))
    return {"folder": folder or ".", "subfolders": folders, "keys": files}


def memory_context(config: SynapseConfig) -> dict[str, Any]:
    """Return core identity context + vault health check in one call for conversation start."""
    result: dict[str, Any] = {}
    for key in ("identity.profile", "identity.communication", "identity.location"):
        try:
            result[key] = memory_get(config, key)
        except Exception:
            result[key] = None

    # Lightweight folder index — always present even on empty vault
    for folder in ("life", "work", "patterns", "projects"):
        try:
            result[f"_index.{folder}"] = memory_list_folder(config, folder)["keys"]
        except Exception:
            result[f"_index.{folder}"] = []

    # Run dedup automatically — Jaccard only, no API cost
    try:
        dedup = _dedup(config, auto_clean=False)
        health: dict[str, Any] = {
            "total_files": sum(
                1 for _ in config.vault_path.rglob("*.md") if not _.name.startswith("_")
            )
        }
        issues = []
        if dedup.get("stray_files"):
            issues.append(f"{len(dedup['stray_files'])} stray files")
        if dedup.get("thin_files"):
            issues.append(f"{len(dedup['thin_files'])} thin stubs")
        if dedup.get("duplicate_groups"):
            issues.append(f"{len(dedup['duplicate_groups'])} duplicate groups")
        health["issues"] = issues
        health["clean"] = len(issues) == 0
        result["_vault_health"] = health
    except Exception:
        pass

    result["_write_mode"] = config.write_mode
    result["_tokens"] = _estimate_tokens(result)
    return result


def memory_get_raw(config: SynapseConfig, chat_id: str) -> dict[str, Any]:
    return _get_raw(config, chat_id)


def memory_get_raw_chunks(
    config: SynapseConfig, chat_id: str, query: str, top_k: int = 3, window: int = 8
) -> dict[str, Any]:
    result = _get_raw_chunks(config, chat_id, query, top_k=top_k, window=window)
    if isinstance(result, dict):
        result["_tokens"] = _estimate_tokens(result)
    return result


def memory_search_raw(config: SynapseConfig, query: str, top_k: int = 10) -> list[dict[str, Any]]:
    return _search_raw_index(config, query, top_k=top_k)


def memory_code_search(
    config: SynapseConfig,
    query: str,
    project: str = "",
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Hybrid FTS5 + semantic search over code nodes indexed by memory_scan_project."""
    from .code_index import search_code, list_projects

    if not config.vault_path or not (config.vault_path / "_code_index.db").exists():
        return [{"error": "No code index found. Run memory_scan_project first."}]
    results = search_code(
        config.vault_path,
        config.gemini_api_key or None,
        query,
        project=project,
        limit=limit,
    )
    if not results:
        projects = list_projects(config.vault_path)
        return [{"info": "No results.", "indexed_projects": projects}]
    return results


def memory_code_stats(config: SynapseConfig, project: str = "") -> dict[str, Any]:
    """Stats for indexed code projects. Pass project slug to drill in."""
    from .code_index import list_projects, project_stats

    if not config.vault_path or not (config.vault_path / "_code_index.db").exists():
        return {"error": "No code index found. Run memory_scan_project first."}
    projects = list_projects(config.vault_path)
    if project:
        if project not in projects:
            return {"error": f"Project {project!r} not indexed.", "indexed_projects": projects}
        return {"project": project, **project_stats(config.vault_path, project)}
    return {"indexed_projects": projects}


def memory_save_chat(
    config: SynapseConfig,
    title: str,
    summary: str,
    key_facts: list[str],
    decisions: list[str],
    tags: list[str],
    keywords: str = "",
    categories: list[str] | None = None,
    chat_id: str | None = None,
) -> dict[str, Any]:
    return _save_chat_memory(
        config,
        title,
        summary,
        key_facts,
        decisions,
        tags,
        keywords=keywords,
        categories=categories,
        chat_id=chat_id,
    )


def memory_build_graph(config: SynapseConfig, top_k: int = 8) -> dict[str, Any]:
    return _build_topic_graph(config, top_k=top_k)


def memory_deep_search(
    config: SynapseConfig, query: str, depth: int = 2, top_k: int = 8
) -> list[dict[str, Any]]:
    return _deep_search(config, query, depth=depth, top_k=top_k)


def memory_auto(config: SynapseConfig, task: str) -> dict[str, Any]:
    """
    Smart retrieval dispatcher. Always loads context, searches active vault,
    and escalates to deep search only when no vault result clears the confidence
    threshold (score >= 0.7). One strong hit stops the chain; weak hits escalate.
    """
    from .search import CLAUDE_ANALYSIS_THRESHOLD

    result: dict[str, Any] = {}

    # Tier 1: always
    result["context"] = memory_context(config)

    # Tier 2: active vault search
    vault_hits = memory_search_tool(config, task)
    result["vault_results"] = vault_hits

    # Tier 3: escalate only when no hit is confident enough
    best_score = max((r.get("score", 0.0) for r in vault_hits), default=0.0)
    if best_score < CLAUDE_ANALYSIS_THRESHOLD:
        result["deep_results"] = _deep_search(config, task)

    result["_tokens"] = _estimate_tokens(result)
    return result


def memory_commit(config: SynapseConfig, patch: dict[str, Any]) -> dict[str, Any]:
    """
    Write a memory patch using the configured write_mode.
    - review (default): proposes a diff for human approval, same as memory_propose_update.
    - auto: proposes then immediately applies — no confirmation needed.
    """
    proposed = memory_propose_update(config, patch)
    if config.write_mode != "auto":
        return proposed
    patch_id = proposed.get("patch_id")
    if not patch_id:
        return proposed
    return memory_apply_update(config, patch_id)


def memory_triage(
    config: SynapseConfig,
    input_folder: str,
    output_folder: str = "",
    force_review: bool = False,
    openrouter_model: str = "",
    groq_model: str = "",
    workers: int = 3,
) -> dict[str, Any]:
    """Run the AI triage pipeline on a conversations_jsonl folder."""
    import os

    or_key = os.environ.get("OPENROUTER_API_KEY", "")
    groq_key = os.environ.get("GROQ_API_KEY", "")

    if not or_key:
        return {"error": "OPENROUTER_API_KEY not set. Add it to your .env file."}
    if not groq_key:
        return {"error": "GROQ_API_KEY not set. Add it to your .env file."}

    out = output_folder or str(config.root_path / "synapse_filtered_chats")

    kwargs: dict[str, Any] = {}
    if openrouter_model:
        kwargs["openrouter_model"] = openrouter_model
    if groq_model:
        kwargs["groq_model"] = groq_model

    return _run_triage(
        input_folder=input_folder,
        output_folder=out,
        openrouter_api_key=or_key,
        groq_api_key=groq_key,
        force_review=force_review,
        workers=workers,
        **kwargs,
    )


def rebuild_index(config: SynapseConfig) -> dict[str, str]:
    MemoryIndex(config.vault_path, lambda path: read_text(config, path)).rebuild(
        api_key=config.gemini_api_key or None
    )
    return {"status": "rebuilt", "db_path": str((config.vault_path / "_index.db").resolve())}


def _ensure_vault_path(vault: Path) -> None:
    if not vault.exists():
        raise ValueError(f"Vault path does not exist: {vault}")
    if not vault.is_dir():
        raise ValueError(f"Vault path is not a directory: {vault}")


def _tree_node(path: Path, vault: Path) -> dict[str, Any]:
    children = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            children.append(_tree_node(child, vault))
        elif child.is_file():
            children.append(
                {
                    "type": "file",
                    "name": child.name,
                    "path": str(child.relative_to(vault)).replace("\\", "/"),
                    "key": path_to_key(vault, child) if child.suffix.lower() == ".md" else None,
                }
            )

    return {
        "type": "folder",
        "name": path.name,
        "path": "." if path == vault else str(path.relative_to(vault)).replace("\\", "/"),
        "children": children,
    }

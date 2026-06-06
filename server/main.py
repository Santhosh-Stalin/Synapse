from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .functions import (
    memory_apply_update,
    memory_auto,
    memory_build_graph,
    memory_code_search,
    memory_code_stats,
    memory_commit,
    memory_save_chat,
    memory_conflicts,
    memory_get_raw,
    memory_get_raw_chunks,
    memory_search_raw,
    memory_context,
    memory_dedup,
    memory_deep_search,
    memory_diff,
    memory_get,
    memory_import_ai_export,
    memory_import_filtered_jsonl,
    memory_import_synapse_summaries,
    memory_ingest_text,
    memory_list_folder,
    memory_organize_vault,
    memory_smart_merge,
    memory_propose_update,
    memory_reject_update,
    memory_relink_all,
    memory_scan_project,
    memory_search_tool,
    memory_start_watcher,
    memory_stop_watcher,
    memory_tree,
    memory_triage,
    memory_watcher_status,
    rebuild_index,
)
from .weekly_report import generate_weekly_report


def _load_agent_instructions() -> str:
    here = Path(__file__).parent.parent
    md = here / "AGENT.md"
    return md.read_text(encoding="utf-8") if md.exists() else ""


app = FastMCP("Synapse", instructions=_load_agent_instructions())
config = load_config()


@app.tool(name="memory_context")
def context() -> dict:
    """Call this FIRST at the start of every conversation. Returns identity.profile + identity.communication + vault dedup health check in one call."""
    return memory_context(config)


@app.tool(name="memory_list")
def list_folder(folder: str = "") -> dict:
    """List keys in a vault folder (shallow, ~200 tokens). Pass folder name like 'projects' or '' for root."""
    return memory_list_folder(config, folder)


@app.tool(name="memory_tree")
def tree() -> dict:
    """EXPENSIVE: ~20k tokens. Use memory_list or memory_search instead. Only call if you need the complete vault structure."""
    return memory_tree(config)


@app.tool(name="memory_get")
def get(key: str) -> dict:
    """Returns parsed frontmatter and content for a memory key such as work_stack."""
    return memory_get(config, key)


@app.tool(name="memory_search")
def search(query: str) -> list[dict]:
    """Searches the SQLite FTS5 index across all memory files."""
    return memory_search_tool(config, query)


@app.tool(name="memory_propose_update")
def propose_update(patch: dict) -> dict:
    """Creates a pending memory patch and returns a diff preview without writing memory."""
    return memory_propose_update(config, patch)


@app.tool(name="memory_apply_update")
def apply_update(patch_id: str) -> dict:
    """Applies an approved pending patch, updates the index, and commits to git when available."""
    return memory_apply_update(config, patch_id)


@app.tool(name="memory_reject_update")
def reject_update(patch_id: str, reason: str = "") -> dict:
    """Rejects a pending patch and removes it from the pending queue."""
    return memory_reject_update(config, patch_id, reason)


@app.tool(name="memory_diff")
def diff() -> list[dict]:
    """Returns all pending patches awaiting approval."""
    return memory_diff(config)


@app.tool(name="memory_conflicts")
def conflicts() -> list[dict]:
    """Returns potential contradictions between memory files."""
    return memory_conflicts(config)


@app.tool(name="memory_rebuild_index")
def rebuild() -> dict:
    """Rebuilds the SQLite index from the vault."""
    return rebuild_index(config)


@app.tool(name="memory_organize")
def organize() -> dict:
    """Builds MOC index files for every vault folder so Obsidian graph view has a clear hub-and-spoke structure."""
    return memory_organize_vault(config)


@app.tool(name="memory_relink_all")
def relink_all() -> dict:
    """Recomputes triggers and related links for every memory file. Run after bulk imports."""
    return memory_relink_all(config)


@app.tool(name="memory_scan_project")
def scan_project(path: str) -> dict:
    """Scans a project directory with AI to extract file and function-level memory patches."""
    return memory_scan_project(config, path)


@app.tool(name="memory_import_ai_export")
def import_ai_export(path: str, owner_name: str = "", resume_failed: bool = False) -> dict:
    """Imports an AI provider data export and extracts memory patches. Pass resume_failed=true to process only chunks that failed in the previous run for this source."""
    return memory_import_ai_export(config, path, owner_name or None, resume_failed=resume_failed)


@app.tool(name="memory_import_filtered_jsonl")
def import_filtered_jsonl(
    filtered_jsonl_folder: str,
    blacklist_file: str = "",
    redflag_file: str = "",
    owner_name: str = "",
) -> dict:
    """Import pre-filtered ChatGPT conversations using Gemma (Gemini API). Skips any conversation ID in the blacklist or redflag file. Requires gemini_api_key in config."""
    return memory_import_filtered_jsonl(
        config,
        filtered_jsonl_folder,
        blacklist_file or None,
        redflag_file or None,
        owner_name or None,
    )


@app.tool(name="memory_import_synapse_summaries")
def import_synapse_summaries(summaries_folder: str, owner_name: str = "") -> dict:
    """Import synapse_ai_summaries/*.json directly into the vault. Each conversation becomes vault/chats/<id>.md. Category index pages (coding.md, life.md, study.md, projects.md, misc.md) are auto-generated with links to all relevant chats. No LLM call needed."""
    return memory_import_synapse_summaries(config, summaries_folder, owner_name or None)


@app.tool(name="memory_ingest_text")
def ingest_text(text: str, label: str = "[pasted text]") -> dict:
    """Paste any raw text and the configured inference provider will extract memory patches from it. Supports notes, bullet lists, conversation snippets, and other free-form text. Optional label names the source."""
    return memory_ingest_text(config, text, label)


@app.tool(name="memory_smart_merge")
def smart_merge(dry_run: bool = True, threshold: float = 0.93) -> dict:
    """Find and merge semantic duplicate memory files using embedding similarity plus the configured inference provider. dry_run=True reports pairs without changing files. dry_run=False executes merges."""
    return memory_smart_merge(config, dry_run=dry_run, threshold=threshold)


@app.tool(name="memory_deduplicate")
def deduplicate(auto_clean: bool = False) -> dict:
    """Scans vault for duplicate entries, stray files, and thin stubs. Pass auto_clean=true to delete stray/thin files automatically. Duplicate merges are always reported for manual review."""
    return memory_dedup(config, auto_clean=auto_clean)


@app.tool(name="memory_weekly_report")
def weekly_report() -> dict:
    """Generates the weekly Synapse report in the vault root."""
    return generate_weekly_report(config)


@app.tool(name="memory_start_watcher")
def start_watcher(path: str) -> dict:
    """Start the incremental file watcher for a project. Auto-extracts changed files with the configured inference provider."""
    return memory_start_watcher(config, path)


@app.tool(name="memory_stop_watcher")
def stop_watcher() -> dict:
    """Stop the running file watcher."""
    return memory_stop_watcher()


@app.tool(name="memory_watcher_status")
def watcher_status() -> dict:
    """Returns the current watcher state: queued files, processed count, last processed file."""
    return memory_watcher_status()


@app.tool(name="memory_get_raw")
def get_raw(chat_id: str) -> dict:
    """Retrieve the full raw conversation markdown from synapse_extracted for a given chat UUID or chats.<uuid> key. Returns the original message-by-message text with timestamps. WARNING: can be 35k+ tokens — use memory_get_raw_chunks when you have a query."""
    return memory_get_raw(config, chat_id)


@app.tool(name="memory_get_raw_chunks")
def get_raw_chunks(chat_id: str, query: str, top_k: int = 3, window: int = 8) -> dict:
    """Retrieve only the most query-relevant message windows from a raw conversation. Typical cost: 1-5k tokens instead of 35k. Pass the same query used to find the chat. top_k=number of windows to return, window=messages per window."""
    return memory_get_raw_chunks(config, chat_id, query, top_k=top_k, window=window)


@app.tool(name="memory_search_raw")
def search_raw(query: str, top_k: int = 10) -> list[dict]:
    """Search raw archive by conversation title. Faster than FTS5 for known titles. Returns conversation_id, title, date, message_count."""
    return memory_search_raw(config, query, top_k=top_k)


@app.tool(name="memory_build_graph")
def build_graph(top_k: int = 8) -> dict:
    """Build the topic graph over vault/chats/*.md nodes. Computes edges from tag/project/keyword overlap, writes vault/metadata/topic_graph.json, and updates each chat file with related wikilinks. Run once after import, then re-run after new imports."""
    return memory_build_graph(config, top_k=top_k)


@app.tool(name="memory_deep_search")
def deep_search(query: str, depth: int = 2, top_k: int = 8) -> list[dict]:
    """Graph-guided chat search. FTS5 finds entry nodes, graph traversal expands to related conversations, returns ranked summaries. Requires memory_build_graph to have been run first."""
    return memory_deep_search(config, query, depth=depth, top_k=top_k)


@app.tool(name="memory_code_search")
def code_search(query: str, project: str = "", limit: int = 8) -> list[dict]:
    """Hybrid FTS5 + semantic search over code nodes indexed by memory_scan_project. Pass project slug to scope to one codebase, or leave blank to search all. Returns matching functions/files with description, file path, line number, and call edges."""
    return memory_code_search(config, query, project=project, limit=limit)


@app.tool(name="memory_code_stats")
def code_stats(project: str = "") -> dict:
    """Stats for code projects indexed by memory_scan_project. Pass project slug to drill in, or leave blank to list all indexed projects."""
    return memory_code_stats(config, project=project)


@app.tool(name="memory_save_chat")
def save_chat(
    title: str,
    summary: str,
    key_facts: list[str],
    decisions: list[str],
    tags: list[str],
    keywords: str = "",
    categories: list[str] = [],
    chat_id: str = "",
) -> dict:
    """Save the current conversation as a chat summary in vault/chats/. Call this at the end of any session worth remembering. The result is immediately searchable via memory_deep_search and memory_search. Run memory_build_graph afterward to wire it into the topic graph."""
    return memory_save_chat(
        config,
        title,
        summary,
        key_facts,
        decisions,
        tags,
        keywords=keywords,
        categories=categories or None,
        chat_id=chat_id or None,
    )


@app.tool(name="memory_triage")
def triage(
    input_folder: str,
    output_folder: str = "",
    force_review: bool = False,
    openrouter_model: str = "",
    groq_model: str = "",
    workers: int = 3,
) -> dict:
    """AI-powered chat triage pipeline. Reads conversations_jsonl from input_folder, sends each to OpenRouter (Groq fallback) to decide keep_full/keep_short/skip/redflag_*, and writes filtered JSONL ready for memory_import_filtered_jsonl. Requires OPENROUTER_API_KEY and GROQ_API_KEY in .env. output_folder defaults to synapse_filtered_chats/ in the Synapse root."""
    return memory_triage(config, input_folder, output_folder, force_review, openrouter_model, groq_model, workers)


@app.tool(name="memory_auto")
def auto(task: str) -> dict:
    """Smart retrieval dispatcher. Call this instead of manually chaining context → search → deep_search. Loads context, searches the active vault, and escalates to deep search automatically when vault results are thin."""
    return memory_auto(config, task)


@app.tool(name="memory_commit")
def commit(patch: dict) -> dict:
    """Write a memory patch using the configured write_mode. In 'review' mode (default) proposes a diff for human approval. In 'auto' mode applies immediately without confirmation. Same patch format as memory_propose_update."""
    return memory_commit(config, patch)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()

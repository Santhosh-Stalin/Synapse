from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def _check_critical_imports() -> None:
    missing = []
    for module, package in [
        ("mcp", "mcp"),
        ("yaml", "PyYAML"),
        ("google.genai", "google-genai"),
    ]:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        pkgs = " ".join(missing)
        print(f"[Synapse] FATAL: Missing required packages: {pkgs}", file=sys.stderr)
        print(f"[Synapse] Fix: pip install {pkgs}", file=sys.stderr)
        sys.exit(1)


_check_critical_imports()

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
    memory_history,
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
    memory_format_claude_export,
    memory_full_import,
    memory_health,
    memory_index_status,
    memory_stop_job,
    memory_export_snapshot,
    memory_session_stats,
    memory_session_save,
    memory_vault_diff,
    memory_apply_all,
    memory_fix_frontmatter,
    memory_multi_search,
    memory_watch_vault,
    memory_ask,
    memory_triage,
    memory_watcher_status,
    rebuild_index,
    memory_ingest_file,
    memory_list_files,
    memory_preview_image,
    memory_ingest_image_save,
    memory_ingest_image_gemini,
    memory_quick_save_chat,
    memory_start_chat,
    memory_update_chat,
    memory_finalize_chat,
    memory_ingest_file_content,
    memory_ingest_image_content,
    memory_read_file,
)
from .weekly_report import generate_weekly_report


def _load_agent_instructions() -> str:
    here = Path(__file__).parent.parent
    md = here / "AGENT.md"
    return md.read_text(encoding="utf-8") if md.exists() else ""


app = FastMCP("Synapse", instructions=_load_agent_instructions())
config = load_config()

if not config.vault_path.exists():
    print(
        f"[Synapse] FATAL: vault_path does not exist: {config.vault_path}\n"
        "[Synapse] Fix vault_path in config.yaml or run: python setup.py",
        file=sys.stderr,
    )
    sys.exit(1)

# Auto-start vault watcher so external edits (Obsidian) are picked up immediately
try:
    from .functions import memory_watch_vault as _watch_vault
    _watch_vault(config, enable=True)
    print("[Synapse] Vault watcher started.", file=sys.stderr)
except Exception as _e:
    print(f"[Synapse] Vault watcher failed to start: {_e}", file=sys.stderr)


@app.tool(name="memory_context")
def context() -> dict:
    """Call this FIRST at the start of every conversation. Returns identity.profile + identity.communication + vault dedup health check in one call."""
    return memory_context(config)


@app.tool(name="memory_list")
def list_folder(folder: str = "") -> dict:
    """List keys in a vault folder (shallow, ~200 tokens). Pass folder name like 'projects' or '' for root."""
    return memory_list_folder(config, folder)


@app.tool(name="memory_tree")
def tree(confirm: bool = False) -> dict:
    """Returns the complete vault directory tree. Without confirm=True returns a cost warning (~20k tokens). Pass confirm=True only when you genuinely need the full structure — use memory_list or memory_search instead."""
    return memory_tree(config, confirm=confirm)


@app.tool(name="memory_get")
def get(key: str) -> dict:
    """Returns parsed frontmatter and content for a memory key such as work_stack."""
    return memory_get(config, key)


@app.tool(name="memory_history")
def history(key: str) -> dict:
    """Returns the full structured write history of a memory key: all events with timestamps, session IDs, freshness score, and retrieval/correction counts."""
    return memory_history(config, key)


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
def conflicts(auto_resolve: bool = False) -> list[dict]:
    """Returns potential contradictions between memory files. Pass auto_resolve=True to automatically propose deprecating the older file in each conflicting pair."""
    return memory_conflicts(config, auto_resolve=auto_resolve)


@app.tool(name="memory_rebuild_index")
def rebuild(background: bool = False) -> dict:
    """Rebuild the SQLite FTS5 + semantic index from scratch. Pass background=True to run in a thread and return immediately — poll memory_index_status for completion."""
    return rebuild_index(config, background=background)


@app.tool(name="memory_organize")
def organize() -> dict:
    """Builds MOC index files for every vault folder so Obsidian graph view has a clear hub-and-spoke structure."""
    return memory_organize_vault(config)


@app.tool(name="memory_relink_all")
def relink_all() -> dict:
    """Recomputes triggers and related links for every memory file. Run after bulk imports."""
    return memory_relink_all(config)


@app.tool(name="memory_scan_project")
def scan_project(path: str, exclude_dirs: list[str] = [], background: bool = True) -> dict:
    """Scans a project directory with AI to extract file and function-level memory patches. Runs in background by default — poll memory_index_status for progress. Pass exclude_dirs=["secrets","logs"] to skip extra folders beyond built-in exclusions (vault, .venv, .git, node_modules, config.yaml, .env etc. are always skipped). Set background=False to block until complete."""
    return memory_scan_project(config, path, exclude_dirs=exclude_dirs or None, background=background)


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
def start_watcher(path: str, exclude_dirs: list[str] = []) -> dict:
    """Start the incremental file watcher for a project. Auto-extracts changed files with the configured inference provider. Pass exclude_dirs to skip project-specific folders (e.g. ['vault','.backups'])."""
    return memory_start_watcher(config, path, exclude_dirs=exclude_dirs or None)


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
def build_graph(top_k: int = 8, background: bool = False) -> dict:
    """Build the topic graph over vault/chats/*.md nodes. Computes edges from tag/project/keyword overlap, writes vault/metadata/topic_graph.json, and updates each chat file with related wikilinks. Pass background=True to run in a thread — poll memory_index_status for completion."""
    return memory_build_graph(config, top_k=top_k, background=background)


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


@app.tool(name="memory_format_claude_export")
def format_claude_export(
    export_folder: str,
    output_folder: str = "",
    write_markdown: bool = True,
) -> dict:
    """Convert a Claude.ai data export folder (containing conversations.json) into monthly JSONL files with full conversation text. Output is ready to pass directly into memory_triage. output_folder defaults to synapse_extracted/ in the Synapse root."""
    return memory_format_claude_export(config, export_folder, output_folder, write_markdown)


@app.tool(name="memory_triage")
def triage(
    input_folder: str,
    output_folder: str = "",
    force_review: bool = False,
    openrouter_model: str = "",
    groq_model: str = "",
    workers: int = 3,
) -> dict:
    """AI-powered chat triage pipeline. Reads conversations_jsonl from input_folder, sends each to OpenRouter+Groq (preferred) or Gemini (fallback) to decide keep_full/keep_short/skip/redflag_*. Writes filtered JSONL ready for memory_import_filtered_jsonl. Prefers OPENROUTER_API_KEY+GROQ_API_KEY; falls back to GEMINI_API_KEY with a privacy warning. output_folder defaults to synapse_filtered_chats/ in the Synapse root."""
    return memory_triage(config, input_folder, output_folder, force_review, openrouter_model, groq_model, workers)


@app.tool(name="memory_full_import")
def full_import(
    export_folder: str,
    owner_name: str = "",
    skip_triage: bool = False,
    background_rebuild: bool = True,
) -> dict:
    """One-command Claude export pipeline: format → triage → import_filtered → rebuild_index → build_graph. Pass the Claude.ai export folder (containing conversations.json). Triage uses OPENROUTER_API_KEY+GROQ_API_KEY or falls back to GEMINI_API_KEY. Steps 4+5 run in background by default — poll memory_index_status for completion. Set skip_triage=True to bypass privacy filter."""
    return memory_full_import(config, export_folder, owner_name, skip_triage, background_rebuild)


@app.tool(name="memory_index_status")
def index_status() -> dict:
    """Poll the status of background jobs (rebuild_index, build_graph, full_import). Returns job name, status (running/done/failed), elapsed seconds, and result or error."""
    return memory_index_status()


@app.tool(name="memory_stop_job")
def stop_job(job_name: str = "") -> dict:
    """Cancel a running background job. Pass job_name to stop one job (rebuild_index, build_graph, full_import), or leave empty to cancel all running jobs. Also cancels any pending auto-rebuild timer."""
    return memory_stop_job(job_name)


@app.tool(name="memory_health")
def health(auto_fix: bool = False) -> dict:
    """Vault health dashboard: health score, file counts per folder, token estimate, index age, graph stats, pending patches, issues. auto_fix=True runs deduplicate + relink + organize automatically."""
    return memory_health(config, auto_fix=auto_fix)


@app.tool(name="memory_vault_diff")
def vault_diff(since: str = "", limit: int = 50) -> dict:
    """List vault files modified after a date (ISO: '2025-06-01'). Leave since empty to list all, newest first. Shows key, folder, and modification timestamp."""
    return memory_vault_diff(config, since=since, limit=limit)


@app.tool(name="memory_apply_all")
def apply_all(folder: str = "", dry_run: bool = False) -> dict:
    """Apply all pending patches at once. Filter by vault folder (e.g. 'life'). dry_run=True previews what would be applied without writing."""
    return memory_apply_all(config, folder=folder, dry_run=dry_run)


@app.tool(name="memory_fix_frontmatter")
def fix_frontmatter(dry_run: bool = True) -> dict:
    """Find vault files with missing required frontmatter fields (type, weight, confidence, scope) and propose patches to fix them. dry_run=True reports without writing."""
    return memory_fix_frontmatter(config, dry_run=dry_run)


@app.tool(name="memory_multi_search")
def multi_search(queries: list[str], top_k: int = 4) -> dict:
    """Fan-out search: run multiple queries in parallel and merge results. Results appearing across more queries rank higher. Returns deduplicated ranked list."""
    return memory_multi_search(config, queries=queries, top_k=top_k)


@app.tool(name="memory_watch_vault")
def watch_vault(enable: bool = True) -> dict:
    """Watch the vault for external edits (e.g. from Obsidian) and auto-rebuild the index when .md files change. enable=False stops the watcher."""
    return memory_watch_vault(config, enable=enable)


@app.tool(name="memory_ask")
def ask(question: str, top_k: int = 5) -> dict:
    """Natural language Q&A over the vault. Retrieves relevant memories + chats, then asks Gemini to answer grounded in that context with citations. Requires gemini_api_key."""
    return memory_ask(config, question=question, top_k=top_k)


@app.tool(name="memory_export_snapshot")
def export_snapshot(output_path: str = "") -> dict:
    """Zip the entire vault to a timestamped snapshot file. SQLite index files are excluded (they can be rebuilt). Pass output_path to control the destination."""
    return memory_export_snapshot(config, output_path)


@app.tool(name="memory_session_stats")
def session_stats() -> dict:
    """Token budget and tool usage for the current session. Shows tokens used, top tools called, keys read/written, and budget status (ok / moderate / high)."""
    return memory_session_stats()


@app.tool(name="memory_session_save")
def session_save() -> dict:
    """Generate a prefilled memory_save_chat template based on this session's vault activity. Fill in title/summary/key_facts/decisions, then call memory_save_chat with the result."""
    return memory_session_save(config)


@app.tool(name="memory_auto")
def auto(task: str) -> dict:
    """Smart retrieval dispatcher. Call this instead of manually chaining context → search → deep_search. Loads context, searches the active vault, and escalates to deep search automatically when vault results are thin."""
    return memory_auto(config, task)


@app.tool(name="memory_commit")
def commit(patch: dict) -> dict:
    """Write a memory patch using the configured write_mode. In 'review' mode (default) proposes a diff for human approval. In 'auto' mode applies immediately without confirmation. Same patch format as memory_propose_update."""
    return memory_commit(config, patch)


@app.tool(name="memory_ingest_file")
def ingest_file(file_path: str, key: str = "", title: str = "", tags: list[str] = []) -> dict:
    """Convert a file to markdown and store it in vault/files/. Supports PDF, DOCX, XLSX, CSV, HTML, TXT. For images, redirects to memory_preview_image first."""
    return memory_ingest_file(config, file_path=file_path, key=key, title=title, tags=tags)


@app.tool(name="memory_list_files")
def list_files() -> dict:
    """List all files ingested into vault/files/ — the dedicated folder for file-converted memories. Shows key, title, source filename, and ingestion date."""
    return memory_list_files(config)


@app.tool(name="memory_preview_image")
def preview_image(file_path: str):
    """Show an image so Claude can assess it directly. After viewing, Claude decides: (1) not worth saving → skip; (2) sensitive (passwords, IDs, medical, private) → call memory_ingest_image_save with your own markdown; (3) safe → call memory_ingest_image_gemini."""
    return memory_preview_image(file_path)


@app.tool(name="memory_ingest_image_save")
def ingest_image_save(file_path: str, markdown: str, key: str = "", title: str = "", tags: list[str] = []) -> dict:
    """Save an image using Claude's own markdown description — for sensitive images. No data sent to external APIs. Call after memory_preview_image when image contains sensitive info."""
    return memory_ingest_image_save(config, file_path=file_path, markdown=markdown, key=key, title=title, tags=tags)


@app.tool(name="memory_ingest_image_gemini")
def ingest_image_gemini(file_path: str, key: str = "", title: str = "", tags: list[str] = []) -> dict:
    """Extract markdown from a non-sensitive image using Gemini vision and save to vault/files/. Call after memory_preview_image when image is safe to send to Gemini."""
    return memory_ingest_image_gemini(config, file_path=file_path, key=key, title=title, tags=tags)


@app.tool(name="memory_quick_save_chat")
def quick_save_chat(title: str, summary: str, key_facts: list[str] = [], decisions: list[str] = []) -> dict:
    """One-shot fallback: create and immediately finalize a chat record. Use memory_start_chat + memory_update_chat + memory_finalize_chat for richer, more detailed records."""
    return memory_quick_save_chat(config, title=title, summary=summary, key_facts=key_facts, decisions=decisions)


@app.tool(name="memory_start_chat")
def start_chat(title: str, initial_topic: str = "") -> dict:
    """Call at the START of every conversation to create a living chat record with sections for facts, decisions, problems, code, files, references, and timeline. Returns chat_id — pass it to memory_update_chat and memory_finalize_chat."""
    return memory_start_chat(config, title=title, initial_topic=initial_topic)


@app.tool(name="memory_update_chat")
def update_chat(
    chat_id: str,
    key_facts: list[str] = [],
    decisions: list[str] = [],
    problems_solved: list[str] = [],
    technical_details: str = "",
    references: list[str] = [],
    next_steps: list[str] = [],
    timeline: list[str] = [],
    deep_summary: str = "",
    tags: list[str] = [],
) -> dict:
    """Update the living chat record mid-conversation whenever something notable happens: a decision, a solved problem, code written, a reference found, etc. All list fields are appended; deep_summary and technical_details replace their section."""
    return memory_update_chat(
        config, chat_id=chat_id,
        key_facts=key_facts or None,
        decisions=decisions or None,
        problems_solved=problems_solved or None,
        technical_details=technical_details,
        references=references or None,
        next_steps=next_steps or None,
        timeline=timeline or None,
        deep_summary=deep_summary,
        tags=tags or None,
    )


@app.tool(name="memory_finalize_chat")
def finalize_chat(chat_id: str, summary: str, tags: list[str] = []) -> dict:
    """Call at the END of every conversation. Writes the final deep summary, links ingested files back to this chat, marks status complete, and triggers index rebuild."""
    return memory_finalize_chat(config, chat_id=chat_id, summary=summary, tags=tags or None)


@app.tool(name="memory_ingest_file_content")
def ingest_file_content(filename: str, content: str, key: str = "", title: str = "", tags: list[str] = []) -> dict:
    """Save a file attached to the conversation directly from its content — no filesystem path needed. Use when a user drops any text-based file (PDF, DOCX, CSV, code, etc.) into the chat and Claude already has the text."""
    return memory_ingest_file_content(config, filename=filename, content=content, key=key, title=title, tags=tags or None)


@app.tool(name="memory_ingest_image_content")
def ingest_image_content(filename: str, description: str, key: str = "", title: str = "", tags: list[str] = [], sensitive: bool = False) -> dict:
    """Save an image attached to the conversation using Claude's own description — no path needed. Use when a user drops an image into the chat. Set sensitive=True if it contained passwords, IDs, medical data, or private info."""
    return memory_ingest_image_content(config, filename=filename, description=description, key=key, title=title, tags=tags or None, sensitive=sensitive)


@app.tool(name="memory_read_file")
def read_file(file_path: str) -> dict:
    """Convert a file to markdown and return it for Claude to read — no vault write. Saves tokens when analyzing documents. Supports PDF, DOCX, XLSX, CSV, HTML, TXT. Images excluded."""
    return memory_read_file(config, file_path=file_path)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()

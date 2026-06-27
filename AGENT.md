# Synapse — MCP Memory Server

Synapse is a persistent, structured memory system for Claude. It stores memories as Markdown files in a local vault, indexed by SQLite FTS5 and a topic graph, and exposes them via 50+ MCP tools.

---

## RULES — read before doing anything

### 1. Always start the conversation
```python
memory_context()          # loads identity + vault index
memory_start_chat(title)  # creates living chat record — save the chat_id
```

### 2. Never read documents directly
**Do not read or analyze a file by accessing it yourself.** Always use:
```python
memory_read_file("path/to/file")  # returns clean markdown for you to read
```
Supports PDF, DOCX, XLSX, CSV, HTML, TXT. No vault write — just converts and returns. For images use `memory_preview_image`.

### 3. File attachments — save immediately, no prompt
When a user drops any file into the conversation:

| Type | Action |
|------|--------|
| Text file (PDF, DOCX, CSV, code, TXT…) | `memory_ingest_file_content(filename, content)` → `memory_apply_update(patch_id)` |
| Image | View it, then `memory_ingest_image_content(filename, description, sensitive=True/False)` → `memory_apply_update(patch_id)` |

For images: judge sensitive vs safe yourself before calling. Sensitive = passwords, IDs, medical records, private messages, credentials → set `sensitive=True`. No Gemini is involved for in-conversation images — you write the description.

Also call `memory_update_chat(chat_id, key_facts=["file X was attached and discussed"])`.

### 4. Write mode
Check `_config.write_mode` in the `memory_context()` response:

| Mode | What to do |
|------|-----------|
| `manual` (default) | Show diff, ask "Save this? (yes/no)" before every `memory_commit`. Never write silently. |
| `bulk` | Queue changes silently. On "save"/"done" show all diffs at once, get one yes/no, then commit. |
| `auto` | Call `memory_commit` immediately. Confirm in one line. |

If `_vault_health.clean` is False, flag it and offer `memory_deduplicate(auto_clean=True)`.

---

## CHAT RECORD — mandatory, every conversation

Every conversation is saved as a living document. **No exceptions. No judgment about importance.**

### Conversation START
```python
r = memory_start_chat(title="Short title of what we're doing")
chat_id = r["chat_id"]   # keep this for all updates
```

### Throughout the conversation — call whenever something notable happens
```python
memory_update_chat(
    chat_id,
    key_facts=["fact established"],           # things you learned
    decisions=["decision made and why"],      # explicit choices
    problems_solved=["problem → how fixed"],  # bugs, questions answered
    technical_details="code/config/impl",     # specific technical notes
    references=["URL or doc name"],           # links, papers, docs
    next_steps=["follow-up needed"],          # open items
    timeline=["HH:MM — what happened"],       # chronological log
)
```
Call this **multiple times** — add each moment as it happens. Be detailed.

### Conversation END
```python
memory_finalize_chat(
    chat_id,
    summary="4–8 sentences covering what was built, decided, solved, and what state things are in now.",
    tags=["topic1", "topic2"],   # omit to auto-derive from session
)
```

**Chat record sections:** Deep Summary · Key Facts · Decisions Made · Problems Solved · Technical Details · Files Ingested · References · Next Steps · Timeline

---

## RETRIEVAL — pick the right depth

### Tier 1 — every conversation (~591 tokens)
```
memory_context()
```
Identity, communication style, active project index. Enough for general questions.

### Tier 2 — specific project / topic (~2,000 tokens)
```
memory_search("query")  →  memory_get("key")
```
Run search first. If vault has the answer, stop here.

### Tier 3 — past conversations (~9,000 tokens)
```
memory_deep_search("query")  →  memory_get_raw_chunks(chat_id, query)
```
Use only when user asks about prior work, or vault is thin on a topic. Never call `memory_get_raw()` unless explicitly asked for the full transcript.

### Decision tree
```
General question?             → Tier 1
Specific project/preference?  → Tier 2 → if thin → Tier 3
"What did we discuss/build?"  → Tier 3 directly
```

---

## Vault structure

```
vault/
  identity/   — profile, education, values, interaction style
  life/        — hobbies, fitness, travel, creative interests
  projects/    — every project: stack, status, key technical details
  patterns/    — recurring skills, techniques, workflows
  work/        — dev environment, tools, accounts, domain expertise
  files/       — ingested documents and file attachments
  chats/       — living chat records + imported conversation summaries
  metadata/    — topic_graph.json linking chats by shared topics
```

---

## Tools — when to use each

### Smart tools (default)
| Tool | When |
|---|---|
| `memory_auto("task")` | Default retrieval — context + search + deep search as needed. |
| `memory_commit(patch)` | Default write — respects write_mode. |

### Conversation flow
| Tool | When |
|---|---|
| `memory_context()` | **Every conversation start.** Identity + vault index. |
| `memory_start_chat(title)` | **Every conversation start.** Creates living chat record. Returns `chat_id`. |
| `memory_update_chat(chat_id, ...)` | **During conversation.** Append facts, decisions, problems, code, refs, timeline. Call multiple times. |
| `memory_finalize_chat(chat_id, summary)` | **Every conversation end.** Final summary, links files, marks complete. |
| `memory_quick_save_chat(title, summary)` | One-shot fallback only — use start/update/finalize for richer records. |

### Document reading (no vault write)
| Tool | When |
|---|---|
| `memory_read_file("path")` | **Always use this to read any document.** Converts PDF/DOCX/XLSX/CSV/HTML/TXT to markdown and returns it. Never read files directly. |

### File & image ingestion (vault write)
| Tool | When |
|---|---|
| `memory_ingest_file("path")` | File on disk → vault/files/. PDF/DOCX/XLSX/CSV/HTML/TXT. For images use preview first. |
| `memory_ingest_file_content(filename, content)` | **File attached in conversation** — text content Claude already has. |
| `memory_ingest_image_content(filename, description, sensitive)` | **Image attached in conversation** — Claude's own description. No Gemini. |
| `memory_preview_image("path")` | Image on disk — returns it for Claude to view. Then route to save or gemini. |
| `memory_ingest_image_save("path", markdown)` | Sensitive image on disk → Claude writes the markdown, no Google. |
| `memory_ingest_image_gemini("path")` | Non-sensitive image on disk → Gemini extracts markdown. |
| `memory_list_files()` | List everything in vault/files/. |
| `memory_apply_update(patch_id)` | Apply any pending patch returned by ingest/propose tools. |

### Active vault lookup (Tier 2)
| Tool | Tokens | When |
|---|---|---|
| `memory_search("query")` | ~200–900 | Find relevant active vault keys. Returns top 4. |
| `memory_get("key")` | ~200–500 | Full content of a specific key. |
| `memory_list_folder("folder")` | ~50–200 | List all keys in a folder. Prefer over memory_tree. |

### Chat archive lookup (Tier 3)
| Tool | Tokens | When |
|---|---|---|
| `memory_deep_search("query")` | ~1,000–2,000 | FTS5 + graph traversal over chat archive. Returns 8 ranked results. |
| `memory_get_raw_chunks(id, query)` | ~1,000–7,000 | Relevant windows from a raw conversation. Prefer over memory_get_raw. |
| `memory_search_raw("title")` | ~200 | Fast title-only search over raw archive. |
| `memory_get_raw(id)` | ~5,000–35,000 | Full raw conversation. Only when complete history explicitly needed. |

### Writing memories (low-level)
| Tool | When |
|---|---|
| `memory_propose_update(patch)` | Propose a patch and return diff without writing. |
| `memory_reject_update(patch_id)` | Discard a pending patch. |
| `memory_diff()` | List all pending patches. |
| `memory_apply_all(folder, dry_run)` | Apply all pending patches at once. |
| `memory_fix_frontmatter(dry_run)` | Find and fix files with missing required fields. |
| `memory_multi_search(queries)` | Fan-out search — multiple queries in parallel, merged by relevance. |
| `memory_ask(question)` | Natural language Q&A over vault using Gemini. Requires gemini_api_key. |

### Code graph
| Tool | When |
|---|---|
| `memory_scan_project("path")` | Index a codebase — functions, classes, call graph. Python/JS/TS/Go/Rust/Java. |
| `memory_scan_project("path", exclude_dirs=["dir1","dir2"])` | Same, but skip extra folders beyond the built-in exclusions. Always excluded: `vault` `.venv` `.git` `__pycache__` `.backups` `.claude` `node_modules` `build` `dist` `groq_blacklist_output` and secret files (`config.yaml` `.env`). Use `exclude_dirs` for any additional sensitive or irrelevant folders specific to the project being scanned. |
| `memory_code_search("query")` | Hybrid search over indexed code nodes. |
| `memory_code_stats(project)` | Stats for indexed projects. |

### Import & ingestion pipeline
| Tool | When |
|---|---|
| `memory_full_import("path")` | **One-command** — format → triage → import → rebuild → graph. |
| `memory_format_claude_export("path")` | Step 1 — convert Claude.ai export to monthly JSONL. |
| `memory_triage(input_folder)` | Step 2 — AI filter: keep/skip/redflag. Uses OpenRouter+Groq (Gemini fallback with privacy_warning). |
| `memory_import_filtered_jsonl("path")` | Step 3 — import kept chats into vault via Gemini. |
| `memory_import_ai_export("path")` | Import Claude.ai or ChatGPT export directly. |
| `memory_import_synapse_summaries("path")` | Import synapse_ai_summaries folder. No LLM needed. |
| `memory_ingest_text(text)` | Paste raw text — Gemini extracts patches. |
| `memory_save_chat(title, summary, ...)` | Low-level chat save. Use memory_start/update/finalize_chat instead. |

### Maintenance
| Tool | When |
|---|---|
| `memory_rebuild_index(background=True)` | Rebuild FTS5 index from scratch. |
| `memory_build_graph(background=True)` | Rebuild topic graph. |
| `memory_index_status()` | Poll background rebuild/graph jobs. |
| `memory_relink_all()` | Recompute triggers + related links for every file. Run after bulk imports. |
| `memory_deduplicate()` | Report duplicate pairs and thin stubs. |
| `memory_smart_merge()` | Merge semantic duplicates using embedding similarity. |
| `memory_organize_vault()` | Rebuild MOC index files for Obsidian. |
| `memory_conflicts(auto_resolve)` | Find contradictions. auto_resolve=True deprecates older file. |
| `memory_weekly_report()` | Generate weekly Synapse activity report. |
| `memory_vault_diff(since)` | List vault files modified after a date. |

### File watcher
| Tool | When |
|---|---|
| `memory_start_watcher("path")` | Watch a project directory — auto-extracts changed files with Gemini. |
| `memory_stop_watcher()` | Stop the watcher. |
| `memory_watcher_status()` | Check watcher state. |
| `memory_watch_vault(enable)` | Watch vault/ for external edits (Obsidian) and auto-rebuild index. |

### Vault management
| Tool | When |
|---|---|
| `memory_health(auto_fix)` | Dashboard: health score, file counts, index age, graph stats, pending patches. auto_fix=True runs dedup+relink+organize. |
| `memory_export_snapshot()` | Zip vault to timestamped file. Run before destructive operations. |
| `memory_stop_job(job_name)` | Cancel a running background job. Empty = cancel all. |

### Session tools
| Tool | When |
|---|---|
| `memory_session_stats()` | Token budget + tool usage. Check before expensive Tier 3 calls. |
| `memory_session_save()` | Generate prefilled memory_save_chat template from session activity. |

### Never call without confirming
| Tool | Use instead |
|---|---|
| `memory_tree()` | Returns cost warning by default. Call `memory_tree(confirm=True)` only if you genuinely need the full structure. Use `memory_list_folder` instead. |

---

## Token budget

Every dict-returning tool includes a `_tokens` field. Call `memory_session_stats()` to see the running total.

| Budget | What to do |
|---|---|
| Under 3,000 | Full Tier 3 is fine |
| 3,000–9,000 | Prefer Tier 2; use `memory_get_raw_chunks` not `memory_get_raw` |
| Over 9,000 | Tier 1 only — no further retrieval unless explicitly asked |

**Never exceed 15,000 tokens on memory retrieval alone in a single session.**

| Call | Measured tokens |
|---|---|
| `memory_context()` | ~591 |
| `memory_search()` (4 results) | ~200–900 |
| `memory_get()` (single file) | ~200–500 |
| `memory_deep_search()` (8 results) | ~1,422 |
| `memory_get_raw_chunks()` (3 chunks) | ~1,000–7,149 |
| `memory_get_raw()` (full convo) | ~5,000–35,000 |
| **Full Tier 3 chain** | **~9,152** |

---

## Writing a patch

```json
{
  "key": "projects.synapse",
  "content": "## Project: Synapse\n- Stack: Python, MCP, Gemini, SQLite FTS5",
  "type": "note",
  "scope": "global",
  "weight": 0.8,
  "reason": "Core project details"
}
```

Categories: `identity.*` · `life.*` · `projects.*` · `patterns.*` · `work.*` · `files.*`

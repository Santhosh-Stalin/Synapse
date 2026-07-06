# Synapse — MCP Memory Server

Synapse is a persistent, structured memory system for Claude. It stores memories as Markdown files in a local vault, indexed by SQLite FTS5 and a topic graph, and exposes them via 62 MCP tools.

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
| Text file (PDF, DOCX, DOC, CSV, TSV, XLSX, HTML, TXT…) | `memory_ingest_file_content(filename, content)` → `memory_apply_update(patch_id)` |
| Image | `memory_ingest_image_content(filename, description, sensitive=True/False)` → `memory_apply_update(patch_id)` |

**The two-step rule:** every `memory_ingest_*` and `memory_propose_update` returns a `patch_id`. You MUST call `memory_apply_update(patch_id)` immediately after — the file is not written until you do.

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
| `memory_read_file("path")` | **Always use this to read any document.** Converts to Markdown and returns — no vault write. Never read files directly. Supports PDF/DOCX/DOC/XLSX/XLS/CSV/TSV/HTML/TXT/MD. |

### File & image ingestion (vault write — always two steps)

**All ingestion tools return a `patch_id`. Always follow with `memory_apply_update(patch_id)` to actually write.**

| Tool | When |
|---|---|
| `memory_ingest_file("path")` | File on disk → proposes to `vault/files/`. Supports PDF/DOCX/DOC/XLSX/XLS/CSV/TSV/HTML/TXT. **Cannot be used for images** — use preview first. |
| `memory_ingest_file_content(filename, content)` | **File attached in conversation** — pass the text Claude received. filename used for metadata only. |
| `memory_ingest_image_content(filename, description, sensitive)` | **Image attached in conversation** — Claude's own Markdown description. No Gemini involved regardless of sensitive value. |
| `memory_preview_image("path")` | Image on disk — returns it for Claude to view inline. After viewing, route to `memory_ingest_image_save` (sensitive) or `memory_ingest_image_gemini` (safe). |
| `memory_ingest_image_save("path", markdown)` | Sensitive image on disk → Claude writes the Markdown, nothing sent externally. |
| `memory_ingest_image_gemini("path")` | Non-sensitive image on disk → Gemini `gemini-2.0-flash` extracts Markdown. Requires `gemini_api_key`. |
| `memory_list_files()` | List everything in `vault/files/`. |
| `memory_apply_update(patch_id)` | **Required second step** for all ingest and propose tools. |

### Active vault lookup (Tier 2)
| Tool | Tokens | When |
|---|---|---|
| `memory_search("query")` | ~200–900 | Find relevant active vault keys. Returns top 4. |
| `memory_get("key")` | ~200–500 | Full content of a specific key. |
| `memory_history("key")` | ~200 | Full write timeline: timestamps, session IDs, freshness, retrieval/correction counts. Use to check when something was last updated or what changed. |
| `memory_list("folder")` | ~50–200 | List all keys in a folder. Prefer over memory_tree. |

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
| `memory_propose_update(patch)` | Propose a patch and return diff without writing. Accepts `merge: "replace"/"append"/"prepend"` (default replace). Accepts `session_id` for provenance. |
| `memory_reject_update(patch_id)` | Discard a pending patch. |
| `memory_diff()` | List all pending patches. |
| `memory_apply_all(folder, dry_run)` | Apply all pending patches at once. |
| `memory_fix_frontmatter(dry_run)` | Find and fix files with missing required fields. |
| `memory_multi_search(queries)` | Fan-out search — multiple queries in parallel, merged by relevance. |
| `memory_ask(question)` | Natural language Q&A over vault using Gemini. Requires gemini_api_key. |

### Code graph
| Tool | When |
|---|---|
| `memory_scan_project("path")` | Index a codebase — functions, classes, call graph. Python/JS/TS/Go/Rust/Java. **Runs in background by default** (`background=True`) — poll `memory_index_status()` for progress. |
| `memory_scan_project("path", exclude_dirs=["dir1","dir2"])` | Same, but skip extra folders beyond the built-in exclusions. Always excluded: `vault` `.venv` `.git` `__pycache__` `.backups` `.claude` `node_modules` `build` `dist` `groq_blacklist_output` and secret files (`config.yaml` `.env`). Use `exclude_dirs` for any additional sensitive or irrelevant folders. Pass `background=False` to block until complete. |
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
| `memory_organize()` | Rebuild MOC index files for Obsidian. |
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
| `memory_tree()` | Returns cost warning by default. Call `memory_tree(confirm=True)` only if you genuinely need the full structure. Use `memory_list` instead. |

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
  "reason": "Core project details",
  "merge": "replace"
}
```

**`merge` field:**
- `"replace"` (default) — new content replaces existing body
- `"append"` — new content added after existing body (non-destructive)
- `"prepend"` — new content added before existing body

Use `"append"` when adding a fact to a file that already has good content you don't want to lose.

Categories: `identity.*` · `life.*` · `projects.*` · `patterns.*` · `work.*` · `files.*`

**Pending patch expiry:** Patches older than `pending_auto_expire_days` (config, default 90) are automatically pruned from `_pending.json` on every `load_pending` call. They do not accumulate indefinitely.

---

## Background jobs

`memory_rebuild_index` and `memory_build_graph` both accept `background=True`. When passed:
- They return immediately with `{"job": "...", "status": "started"}`
- The actual work runs in a daemon thread
- Poll `memory_index_status()` to check progress, step label (0–100%), and elapsed seconds

`memory_full_import` always runs rebuild+graph in background by default (`background_rebuild=True`).

`memory_finalize_chat` automatically schedules a debounced index rebuild 20 seconds after it runs. If more finalize calls happen within 20s, the timer resets — only one rebuild fires per burst. This means you don't need to manually call `memory_rebuild_index` after a normal conversation end.

`memory_stop_job(name)` cancels a background job cooperatively — it sets a stop flag and the job exits after its current step. Pass an empty string to cancel all running jobs plus any pending auto-rebuild timer.

---

## Vault watching (Obsidian)

`memory_watch_vault(enable=True)` watches `vault/*.md` for mtime changes every 5 seconds. When a change is detected (e.g. from an Obsidian edit), it schedules a debounced index rebuild after 10 seconds. Use `memory_watch_vault(enable=False)` to stop it. This is separate from `memory_start_watcher` which watches CODE files, not the vault.

---

## Provenance and memory quality

Every `memory_get` response includes a `provenance` block. Read it to understand how reliable and current a memory is:

```json
{
  "source_session": "5b246d3d...",
  "last_updated": "2026-07-06",
  "retrieval_count": 12,
  "correction_count": 1,
  "freshness": 0.87,
  "history": [
    {"date": "2026-06-28", "session_id": "abc12345", "text": "initial write"},
    {"date": "2026-07-06", "session_id": "5b246d3d", "text": "updated stack details"}
  ]
}
```

- **`freshness`** — `exp(-days_since_last_use / 90) × retrieval_boost × correction_penalty` where `retrieval_boost = min(1.5, 1 + 0.05×retrieval_count)` and `correction_penalty = max(0.5, 1 - 0.1×correction_count)`. Below 0.5 = stale.
- **`retrieval_count`** — incremented every time `memory_get` is called on this key.
- **`correction_count`** — incremented every `memory_apply_update`. High = volatile memory.
- **`history`** — `memory_get` returns the last 3 entries. Use `memory_history(key)` for the full timeline. Each history line is stamped `[sess:XXXXXXXX]` with the 8-char prefix of the server session that wrote it.
- **`conflict_warning`** — returned by `memory_propose_update` when new content contradicts existing files on the same topic. The conflict detector uses a scoped O(n) scan (same folder + overlapping triggers) during propose, and a full O(n²) vault scan for `memory_conflicts()`. Always surface conflict warnings to the user before applying.
- **`apply_update` counter behaviour** — when a patch is applied to an existing file, `retrieval_count` is preserved and `correction_count` is incremented by 1. `freshness` is recomputed. High `correction_count` (>5) means volatile memory — treat content with lower confidence.

### memory_context behaviour
- Automatically runs a Jaccard dedup scan on every call (no API cost). The `_vault_health` block reflects the result.
- Loads `identity.profile`, `identity.communication`, and `identity.location` simultaneously.
- The write mode is in `_write_mode` (not `_config.write_mode`).

### Search result fields
`memory_search` results include:
- **`score`** — blended BM25 + semantic (0–1)
- **`source`** — `"fts5"`, `"semantic"`, `"hybrid"`, or `"cloud"`
- **`full_content`** + **`requires_analysis: true`** — attached when `score ≥ 0.7` (non-chat files)
- **`content_preview`** — always 360 chars max

`memory_deep_search` results include:
- **`graph_score`** — FTS score × BFS decay (hop1=×0.4, hop2=×0.16)
- **`is_direct_hit`** — true if FTS matched directly, false if reached via graph
- **`projects`**, **`related`** — up to 4 related chat IDs

### Patch signal field
- `"high_signal"` (default) — writes immediately
- `"casual"` — requires ≥2 occurrences of the same key in the session before proposing
- `"manual"` / `"remember"` — sets `urgent: true`, high priority queue

---

## Tool parameter reference

| Tool | Key params / notes |
|---|---|
| `memory_propose_update` | `key`, `content`, `merge` (`"replace"`/`"append"`/`"prepend"`), `signal`, `session_id`, `trigger_reason` |
| `memory_commit` | Same patch format as `memory_propose_update`. Respects `write_mode`. |
| `memory_history` | `key` (required) → returns version, source_session, freshness, history list |
| `memory_start_chat` | `title` (required), `initial_topic` (optional context string) |
| `memory_update_chat` | `chat_id` (required), `key_facts`, `decisions`, `problems_solved`, `technical_details`, `references`, `next_steps`, `timeline`, `deep_summary`, `tags` — all optional, all appended |
| `memory_finalize_chat` | `chat_id`, `summary` (required), `tags` (list, optional) |
| `memory_quick_save_chat` | `title`, `summary` (required), `key_facts`, `decisions` (optional lists) |
| `memory_scan_project` | `path` (required), `exclude_dirs` (list), `background` (bool, **default True**) |
| `memory_format_claude_export` | `output_folder`, `write_markdown` (bool, default True) |
| `memory_import_ai_export` | `owner_name`, `resume_failed` (bool) |
| `memory_import_filtered_jsonl` | `blacklist_file`, `redflag_file`, `owner_name` |
| `memory_ingest_text` | `text`, `label` (source label in patch history) |
| `memory_smart_merge` | `threshold` (float, default 0.93), `dry_run` (bool, default True) |
| `memory_triage` | `input_folder`, `output_folder`, `force_review` (bool), `openrouter_model`, `groq_model`, `workers` (int, default 3) |
| `memory_get_raw_chunks` | `chat_id`, `query` (required), `top_k` (int, default 3), `window` (int, default 8) |
| `memory_deep_search` | `query` (required), `depth` (int, default 2), `top_k` (int, default 8) |
| `memory_build_graph` | `top_k` (int, default 8), `background` (bool, default False) |
| `memory_rebuild_index` | `background` (bool, default False) |
| `memory_reject_update` | `patch_id`, `reason` (string, logged to `_rejections.jsonl`) |
| `memory_deduplicate` | `auto_clean` (bool — deletes stray/thin files when True) |
| `memory_search_raw` | `query`, `top_k` (int, default 10) |
| `memory_code_search` | `query`, `limit` (int, default 8), `project` (scope to one codebase) |
| `memory_apply_all` | `folder` (filter by vault folder, optional), `dry_run` (bool, default False) |
| `memory_fix_frontmatter` | `dry_run` (bool, default True) |
| `memory_multi_search` | `queries` (list, required), `top_k` (int, default 4) |
| `memory_ask` | `question` (required), `top_k` (int, default 5). Requires `gemini_api_key`. |
| `memory_conflicts` | `auto_resolve` (bool — deprecates older file automatically) |
| `memory_full_import` | `export_folder` (required), `owner_name`, `skip_triage` (bool), `background_rebuild` (bool, default True) |
| `memory_vault_diff` | `since` (ISO date string), `limit` (int, default 50) |
| `memory_export_snapshot` | `output_path` (str, default = vault parent dir with timestamp) |
| `memory_list` | `folder` (str, default `""` = vault root) |
| `memory_ingest_file` | `file_path` (required), `key` (default `files.<stem>`), `title`, `tags` |
| `memory_ingest_file_content` | `filename`, `content` (required), `key`, `title`, `tags` |
| `memory_ingest_image_content` | `filename`, `description` (required), `key`, `title`, `tags`, `sensitive` (bool) |
| `memory_preview_image` | `file_path` (required) |
| `memory_ingest_image_save` | `file_path`, `markdown` (required), `key`, `title`, `tags` |
| `memory_ingest_image_gemini` | `file_path` (required), `key`, `title`, `tags` |
| `memory_start_watcher` | `path` (required), `exclude_dirs` (list of extra dirs to skip) |
| `memory_stop_job` | `job_name` (str, empty = cancel all) |

---

## Watcher auto-apply note

The file watcher (`memory_start_watcher`) always auto-applies patches immediately regardless of `write_mode`. Vault watcher (`memory_watch_vault`) only triggers index rebuilds, never writes.

---

## `memory_ask` behaviour

Retrieves vault hits + deep search hits, builds a context block, then calls Gemini (`gemini-2.0-flash`) to answer grounded in those sources. Returns `answer`, `citations` (list of keys used), and `context_sources` count. Returns error if `gemini_api_key` is not configured.

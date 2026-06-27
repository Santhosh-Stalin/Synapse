# Synapse

[![CI](https://github.com/Santhosh-Stalin/Synapse/actions/workflows/ci.yml/badge.svg)](https://github.com/Santhosh-Stalin/Synapse/actions/workflows/ci.yml)

A memory agent for Claude that reduces token usage.

Every Claude session starts from zero. Without Synapse, giving Claude context means pasting notes, history, and project details manually — burning thousands of tokens before any real work begins. Synapse stores that context as structured Markdown, retrieves only what's relevant, and keeps Claude's context window free for thinking.

---

## Token reduction — measured on a real vault

*Benchmark: 1,997 past conversations, 5 representative queries. Run `python Diagnostics/benchmark_tokens.py` on your own vault.*

| Scenario | Without Synapse | With Synapse | Saving |
|---|---|---|---|
| Load identity context | 1,604 tokens | 591 tokens | **63%** |
| Active projects query | 4,304 tokens | 1,186 tokens | **72%** |
| Coding patterns query | 6,585 tokens | 1,186 tokens | **82%** |
| Recover a past conversation | 1,370,000 tokens | 1,185 tokens | **99.9%** |

The last row is the point. A chat archive of 1,997 conversations is 1.37M tokens. Synapse retrieves what matters — 1,185 tokens — and leaves the rest on disk.

---

![Synapse vault graph — 1,997 conversations indexed and linked by topic](Graph.png)

## What it is

- **Memory agent** — Claude remembers across sessions, projects, and conversations
- **Living chat records** — every conversation is saved as a detailed, growing 9-section document updated throughout the session
- **File & document ingestion** — Claude converts PDF, DOCX, XLSX, CSV, HTML, and images into indexed Markdown and saves them to your vault
- **Smart document reading** — Claude reads any file through Synapse (token-efficient conversion) rather than accessing it directly
- **Image routing** — Claude views images inline and decides: sensitive (describes itself, no external API) or safe (Gemini extracts content)
- **Token-aware** — tiered retrieval stops as soon as confidence is high enough; every response carries a `_tokens` field so Claude tracks its own budget
- **Cross-provider** — import from ChatGPT, Claude.ai, or any conversation export; one vault for everything
- **Local and private** — plain Markdown files on your machine, no SaaS, no cloud sync
- **Code graph** — scan any project (Python, JS/TS, Go, Rust, Java) and ask Claude questions about it
- **MCP server** — 50+ tools exposed via the Model Context Protocol; works with Claude Desktop and Claude Code

---

## Quickstart in 5 minutes

**Option A — GUI installer (Windows)**

Run `install.bat` or launch `SynapseInstaller.exe`. A 5-step wizard guides you through API key setup, vault path, write mode, and MCP config. No terminal required.

**Option B — command line**

```bash
git clone https://github.com/Santhosh-Stalin/Synapse.git
cd Synapse
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
python setup.py     # enter Gemini key, vault path, write mode
```

Restart Claude Desktop. Synapse appears in the tools list. Say: `"Load my memory context"`.

---

## Full install

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python setup.py
```

Restart Claude Desktop. Synapse appears in the tools list automatically.

---

## API keys

| Key | Get it at | Required for |
|-----|-----------|--------------|
| `GEMINI_API_KEY` | aistudio.google.com/apikey | Everything — MCP server, search, import, scan, image extraction |
| `GROQ_API_KEY` | console.groq.com | Filtering pipeline only (`memory_triage`) |
| `OPENROUTER_API_KEY` | openrouter.ai/keys | Filtering pipeline only (`memory_triage`) |

Only the Gemini key is needed for daily use.

---

## How Claude uses Synapse

### Every conversation — two mandatory calls

```python
memory_context()          # loads identity + vault index
memory_start_chat(title)  # creates living chat record, returns chat_id
```

At the end of every conversation:
```python
memory_finalize_chat(chat_id, summary="...")
```

### The two default tools

```python
memory_auto("your question")   # retrieval — loads context, searches vault, escalates if needed
memory_commit(patch)           # writes — proposes diff in manual mode, applies directly in auto mode
```

### Retrieval tiers (what memory_auto does internally)

| Tier | Cost | When |
|---|---|---|
| 1 — Identity context | ~591 tokens | Every conversation |
| 2 — Active vault search | ~600–2,000 tokens | Topic or project questions |
| 3 — Deep search + raw chunks | ~2,000–9,000 tokens | "What did we discuss about X?" |

**Hard ceiling: never exceed 15,000 tokens on memory retrieval in one session.** Each response includes `_tokens` — Claude tracks a running total and downshifts tier when the budget fills.

---

## Living chat records

Every conversation is saved as a detailed, growing document — not a one-line summary.

```python
# Start
r = memory_start_chat(title="What we're doing")
chat_id = r["chat_id"]

# Update throughout — call multiple times
memory_update_chat(chat_id,
    key_facts=["..."],
    decisions=["..."],
    problems_solved=["..."],
    technical_details="...",
    references=["..."],
    next_steps=["..."],
    timeline=["HH:MM — event"],
)

# End
memory_finalize_chat(chat_id, summary="4–8 sentence summary")
```

Each chat document has 9 sections: **Deep Summary · Key Facts · Decisions Made · Problems Solved · Technical Details · Files Ingested · References · Next Steps · Timeline**

Files ingested during a conversation are automatically linked to the chat record.

---

## Document reading and file ingestion

### Reading a document (no vault write)

Claude never reads files directly. Instead:

```python
memory_read_file("/path/to/file.pdf")
# → returns clean markdown for Claude to analyze
```

Supports PDF, DOCX, XLSX, CSV, HTML, TXT. Token-efficient — converts only the content, skips formatting noise.

### Saving a file to the vault

**From disk:**
```python
memory_ingest_file("/path/to/report.pdf")
```

**From a conversation attachment** (Claude already has the content):
```python
memory_ingest_file_content("report.pdf", content)
```

Files are saved to `vault/files/` as indexed Markdown and linked to the current chat.

### Images

Claude views images inline via `memory_preview_image` and decides the routing:

| Situation | Action |
|---|---|
| Sensitive (passwords, IDs, medical, credentials) | Claude writes the description itself — no data sent to Gemini |
| Safe (diagrams, screenshots, documents) | `memory_ingest_image_gemini` — Gemini extracts content |
| Not worth saving | Skip |

**From a conversation attachment:**
```python
memory_ingest_image_content("screenshot.png", description="...", sensitive=False)
```

---

## Code graph — scanning projects

`memory_scan_project` indexes a code project so you can ask Claude questions about it directly.

```python
memory_scan_project("/path/to/your/project")

# Skip extra sensitive or irrelevant folders beyond the built-in exclusions
memory_scan_project("/path/to/your/project", exclude_dirs=["data", "secrets", "private"])

memory_code_search("how does authentication work")
memory_code_stats()
```

**Always excluded (built-in):** `vault` · `.venv` · `.git` · `__pycache__` · `.backups` · `.claude` · `node_modules` · `build` · `dist` · `groq_blacklist_output` · secret files (`config.yaml`, `.env`)

**Supported languages:** Python (AST), TypeScript/JavaScript, Go, Rust, Java (regex)

For each function and class, Synapse stores:
- **Source body** — exact code, up to 3,000 chars
- **Line numbers** — `lineno` and `lineno_end` for every node
- **AI description** — one-sentence summary generated by Gemini
- **Call graph** — which functions call which
- **Edges** — `contains`, `imports_from`, `calls`, `exports`

The graph is saved as `vault/projects/<name>/_graph.json` and incrementally updated on re-scan (only changed files are re-extracted).

---

## Importing from other AI providers

Synapse can import your full conversation history from any major provider and make it searchable alongside your vault.

```python
memory_import_ai_export("/path/to/export/")
```

**Supported:**
- **Claude.ai** — conversations, stored memories, projects
- **ChatGPT** — conversations, stored memories, user profile (including split exports)
- **Plain text / Markdown** — any folder of `.txt` or `.md` files

After import:
```python
memory_diff()                       # review extracted patches
memory_conflicts()                  # find contradictions
memory_smart_merge(dry_run=True)    # preview duplicate merges
memory_smart_merge(dry_run=False)   # execute merges
memory_relink_all()                 # rebuild wikilinks
memory_build_graph()                # rebuild topic graph
```

---

## Vault structure

```
vault/
  identity/    — who you are: profile, education, communication style
  life/        — hobbies, travel, fitness, creative interests
  projects/    — every project: stack, status, key decisions
  patterns/    — recurring skills, workflows, learning approaches
  work/        — career, tools, dev environment, domain expertise
  files/       — ingested documents and file attachments
  chats/       — living chat records + imported conversation summaries
  metadata/
    topic_graph.json   — weighted graph linking chats by shared topics
```

Each file is plain Markdown with YAML frontmatter:

```markdown
---
key: work.cybersecurity
type: note
weight: 0.9
triggers: [ctf, exploitation, pwn]
related: [work.python, projects.ctf_entrypoint]
---

## Technical Proficiency
- CTF: Binary exploitation, ROP chains, pwntools
- Web: SQLi, XSS, SSRF, JWT attacks

Related: [[work/python]] | [[projects/ctf_entrypoint]]
```

---

## All MCP tools

### Conversation flow

| Tool | Use |
|---|---|
| `memory_context()` | Identity + vault index. Every conversation start. |
| `memory_start_chat(title)` | Create living chat record. Returns `chat_id`. |
| `memory_update_chat(chat_id, ...)` | Append facts, decisions, problems, code, refs, timeline. Call multiple times. |
| `memory_finalize_chat(chat_id, summary)` | Final summary, links files, marks complete. |
| `memory_quick_save_chat(title, summary)` | One-shot fallback for short sessions. |

### Smart defaults

| Tool | Tokens | Use |
|---|---|---|
| `memory_auto(task)` | 591–9,000 | **Default retrieval.** Smart dispatcher — handles all tiers automatically. |
| `memory_commit(patch)` | — | **Default write.** Respects write_mode (manual/bulk/auto). |

### Document reading (no vault write)

| Tool | Use |
|---|---|
| `memory_read_file(path)` | Convert PDF/DOCX/XLSX/CSV/HTML/TXT to markdown and return it. Claude always uses this instead of reading files directly. |

### File & image ingestion

| Tool | Use |
|---|---|
| `memory_ingest_file(path)` | File on disk → vault/files/. Supports PDF/DOCX/XLSX/CSV/HTML/TXT. |
| `memory_ingest_file_content(filename, content)` | File attached in conversation — Claude passes the text it received. |
| `memory_ingest_image_content(filename, description, sensitive)` | Image attached in conversation — Claude writes the description. No Gemini. |
| `memory_preview_image(path)` | Returns image for Claude to view inline. Claude then routes to save or gemini. |
| `memory_ingest_image_save(path, markdown)` | Sensitive image on disk — Claude writes the markdown, nothing sent to Google. |
| `memory_ingest_image_gemini(path)` | Non-sensitive image on disk — Gemini extracts markdown. |
| `memory_list_files()` | List everything in vault/files/. |
| `memory_apply_update(patch_id)` | Apply a pending patch from any ingest or propose tool. |

### Retrieval

| Tool | Tokens | Use |
|---|---|---|
| `memory_search(query)` | 200–900 | FTS5 + semantic search over active vault |
| `memory_get(key)` | 200–500 | Full content of one memory file |
| `memory_list_folder(folder)` | 50–200 | Keys in a vault folder |
| `memory_multi_search(queries)` | 600–2,700 | Fan-out parallel search, merged by relevance |
| `memory_deep_search(query)` | 1,000–2,000 | Graph-guided search over chat archive |
| `memory_get_raw_chunks(id, query)` | 1,000–7,000 | Relevant windows from a raw conversation |
| `memory_search_raw(title)` | ~200 | Fast title search over raw archive |
| `memory_get_raw(id)` | 5,000–35,000 | Full raw conversation — avoid unless necessary |
| `memory_ask(question)` | varies | Natural language Q&A over vault using Gemini |
| `memory_tree()` | ~20,000 | Full vault tree — use `memory_list_folder` instead |

### Writing (low-level)

| Tool | Use |
|---|---|
| `memory_propose_update(patch)` | Propose a diff for manual approval |
| `memory_reject_update(patch_id)` | Discard a pending patch |
| `memory_diff()` | List pending patches |
| `memory_apply_all(folder, dry_run)` | Apply all pending patches at once |
| `memory_fix_frontmatter(dry_run)` | Find and fix files with missing required frontmatter fields |

### Code graph

| Tool | Use |
|---|---|
| `memory_scan_project(path)` | Index a code project (Python/JS/TS/Go/Rust/Java) |
| `memory_code_search(query)` | Hybrid search over indexed code nodes |
| `memory_code_stats(project)` | Stats for indexed projects |

### Import pipeline

| Tool | Use |
|---|---|
| `memory_full_import(path)` | One command — format → triage → import → rebuild → graph |
| `memory_format_claude_export(path)` | Step 1 — convert Claude.ai export to monthly JSONL |
| `memory_triage(input_folder)` | Step 2 — AI filter: keep/skip/redflag. Uses OpenRouter+Groq (Gemini fallback) |
| `memory_import_filtered_jsonl(path)` | Step 3 — import kept chats into vault via Gemini |
| `memory_import_ai_export(path)` | Import Claude.ai or ChatGPT export directly |
| `memory_import_synapse_summaries(path)` | Import pre-processed summary JSON (no LLM) |
| `memory_ingest_text(text)` | Extract patches from any pasted text via Gemini |
| `memory_save_chat(...)` | Low-level chat save — prefer start/update/finalize |

### Maintenance

| Tool | Use |
|---|---|
| `memory_rebuild_index()` | Rebuild FTS5 index from scratch |
| `memory_build_graph()` | Rebuild topic graph over vault/chats |
| `memory_relink_all()` | Recompute wikilinks for every file. Run after bulk imports. |
| `memory_deduplicate()` | Report stray files and thin stubs |
| `memory_smart_merge()` | Find and merge semantic duplicates |
| `memory_organize_vault()` | Rebuild MOC index files (Obsidian) |
| `memory_weekly_report()` | Generate weekly activity report |
| `memory_conflicts(auto_resolve)` | Find contradictions. auto_resolve=True deprecates older file. |
| `memory_vault_diff(since)` | List vault files modified after a date |
| `memory_health(auto_fix)` | Dashboard: health score, counts, index age, pending patches |
| `memory_export_snapshot()` | Zip vault to timestamped file |

### File watcher

| Tool | Use |
|---|---|
| `memory_start_watcher(path)` | Watch project folder, auto-extract on change |
| `memory_stop_watcher()` | Stop the running watcher |
| `memory_watcher_status()` | Check watcher state |
| `memory_watch_vault(enable)` | Watch vault/ for external edits (Obsidian) and auto-rebuild index |

### Session

| Tool | Use |
|---|---|
| `memory_session_stats()` | Token budget + tool usage for this session |
| `memory_session_save()` | Generate prefilled chat save template from session activity |
| `memory_stop_job(name)` | Cancel a running background job. Empty = cancel all. |
| `memory_index_status()` | Poll background rebuild/graph jobs |

---

## Write mode

Set during `setup.py` or change in `config.yaml`:

```yaml
write_mode: manual   # Claude proposes diff → you approve each one (default, safest)
write_mode: bulk     # queue silently, one approval at conversation end
write_mode: auto     # Claude writes directly, no confirmation
```

---

## Configuration

```yaml
vault_path: ./vault
git_enabled: true                       # auto-commit every write
encryption: false                       # Fernet at-rest encryption
cloud_search: false                     # Gemini LLM fallback for empty searches
weekly_report_day: monday
pending_auto_expire_days: 90
raw_archive_path: ./synapse_extracted   # raw conversation archive (optional)
write_mode: manual
gemini_api_key: ""                      # prefer .env or environment variable
```

---

## Obsidian integration

Open `vault/` as an Obsidian vault. Run `memory_organize_vault()` then `memory_relink_all()` to populate hub index files and wikilinks. Graph view shows a hub-and-spoke structure — one index node per folder, memory files as leaves, cross-links connecting related topics.

---

## Testing

```bash
# Full tool coverage — no API key needed
python -X utf8 Diagnostics/test_all_tools.py

# Stress test — scale, concurrency, edge cases
python -X utf8 Diagnostics/stress_test.py

# Code graph quality — scan a 10-file polyglot project end-to-end (requires Gemini key)
python -X utf8 Diagnostics/test_scan_calc.py
```

---

## Troubleshooting

**`No module named 'server.embeddings'`** — pull the latest and reinstall:
```bash
git pull && pip install -r requirements.txt
```
Then restart Claude Desktop and run `memory_rebuild_index()`.

**Tools don't appear in Claude Desktop** — restart after editing MCP config. Validate JSON syntax. Python path must point to `.venv`, not system Python.

**`gemini_api_key required`**
```bash
python setup.py
```

**Search returns nothing**
```python
memory_rebuild_index()
```

**`memory_deep_search` returns no results**
```python
memory_build_graph()
memory_rebuild_index()
```

**Slow import / 429 errors** — free tier Gemini is 15 RPM. Synapse uses 4 workers with auto-retry. A 20-file project takes 5–15 minutes.

**`memory_conflicts` floods with false positives** — make sure `chats/` is excluded. The conflict detector skips the chats folder automatically since v0.3.

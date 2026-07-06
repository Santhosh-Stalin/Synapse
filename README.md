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
- **File & document ingestion** — Claude converts PDF, DOCX, DOC, XLSX, XLS, CSV, TSV, HTML, and images into indexed Markdown and saves them to your vault
- **Smart document reading** — Claude reads any file through Synapse (token-efficient conversion) rather than accessing it directly
- **Image routing** — Claude views images inline and decides: sensitive (describes itself, no external API) or safe (Gemini extracts content)
- **Token-aware** — tiered retrieval stops as soon as confidence is high enough; every response carries a `_tokens` field so Claude tracks its own budget
- **Cross-provider** — import from ChatGPT, Claude.ai, or any conversation export; one vault for everything
- **Local and private** — plain Markdown files on your machine, no SaaS, no cloud sync
- **Code graph** — scan any project (Python, JS/TS, Go, Rust, Java) and ask Claude questions about it
- **MCP server** — 62 tools exposed via the Model Context Protocol; works with Claude Desktop and Claude Code

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

| Key | Provider | Free? | Required for |
|-----|----------|-------|--------------|
| `GEMINI_API_KEY` | Google | Yes (free tier) | Default extraction provider, semantic search, image extraction |
| `GROQ_API_KEY` | Groq | Yes | Triage pipeline + extraction (if `extraction_provider: groq`) |
| `CEREBRAS_API_KEY` | Cerebras | Yes | Triage pipeline fallback |
| `OPENROUTER_API_KEY` | OpenRouter | Yes (free models) | Triage pipeline + extraction (if `extraction_provider: openrouter`) |
| `OPENAI_API_KEY` | OpenAI | No (paid) | Extraction only (if `extraction_provider: openai`) |
| `ANTHROPIC_API_KEY` | Anthropic | No (paid) | Extraction only (if `extraction_provider: claude`) |

Only one extraction provider key is required. Gemini is the default.

---

## Privacy & data policy

> **Warning — Gemini free tier:** When `extraction_provider: gemini` (the default), every file scanned by `memory_scan_project` and the watcher is sent to the Google Gemini API. On the **free tier**, Google's terms permit using API requests for model training and improvement. Your code, notes, and file contents may be used to train future Google models.
>
> **To opt out of data training, choose one of:**
>
> | Provider | Cost | Training on data | Rate limit |
> |----------|------|-----------------|------------|
> | `groq` | Free | No | ~100 RPM (rotates 2 models: llama-3.3-70b + llama-4-scout) |
> | `openrouter` | Free models available | No | ~20 RPM free tier |
> | `openai` | Paid (gpt-4o-mini ~$0.15/1M tokens) | No | 500 RPM (tier 1) |
> | `claude` | Paid (haiku ~$0.80/1M tokens) | No | 50 RPM (tier 1) |
> | `gemini` paid | Paid | No | 1000 RPM |
>
> **Recommended free alternative:** Set `extraction_provider: groq` in `config.yaml` and add `GROQ_API_KEY` to your `.env`. Groq is fast, free, and explicitly states they do not train on API data.
>
> **Safe regardless of provider:** `vault/`, `.env`, `config.yaml`, `secrets.*` are never sent to any provider.

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

No vault write — converts and returns. Token-efficient: skips formatting noise.

### Saving a file to the vault

All file ingestion is a two-step flow — propose then apply:

**Step 1 — propose:**
```python
# From disk:
result = memory_ingest_file("/path/to/report.pdf")

# From a conversation attachment (Claude already has the text):
result = memory_ingest_file_content("report.pdf", content)
```

**Step 2 — apply:**
```python
memory_apply_update(result["patch_id"])
```

Files land in `vault/files/<stem>.md` with a metadata header:

```markdown
# report.pdf

**Source:** `report.pdf`
**Type:** PDF
**Ingested:** 2026-07-06

---

## Page 1

[converted content...]
```

### Supported file types

| Extension | Conversion method | Library |
|---|---|---|
| `.pdf` | Page-by-page text extraction | `pypdf` |
| `.docx` / `.doc` | Paragraph + heading style extraction | `python-docx` |
| `.html` / `.htm` | Converts to Markdown | `markdownify` (falls back to regex strip) |
| `.csv` / `.tsv` | Converts to Markdown table | stdlib `csv` |
| `.xlsx` / `.xls` | One Markdown table per sheet | `openpyxl` |
| `.txt` / `.md` | Read as-is | — |
| Images | See image routing below | — |

Missing a library? Each format gives a clear `pip install` error message.

### Images

Images cannot go through `memory_ingest_file` — they must be viewed first:

```python
memory_preview_image("/path/to/image.png")
# → Claude sees the image inline and decides routing
```

| Situation | Next call |
|---|---|
| Not worth saving (blank, generic icon) | Do nothing |
| Sensitive (passwords, IDs, medical, credentials, private chats) | `memory_ingest_image_save(path, markdown=<Claude's own description>)` — no external API |
| Safe (diagrams, screenshots, documents) | `memory_ingest_image_gemini(path)` — Gemini `gemini-2.0-flash` extracts Markdown |

**From a conversation attachment** (image dropped into chat — no file path):
```python
memory_ingest_image_content("screenshot.png", description="...", sensitive=False)
# → Claude's description saved directly, no Gemini involved
```

All image tools still return a `patch_id` — call `memory_apply_update(patch_id)` to write.

**Supported image formats:** `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`

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
| `memory_read_file(path)` | Convert PDF/DOCX/DOC/XLSX/XLS/CSV/TSV/HTML/TXT/MD to markdown and return it. Claude always uses this instead of reading files directly. |

### File & image ingestion

| Tool | Use |
|---|---|
| `memory_ingest_file(path)` | File on disk → vault/files/. Supports PDF/DOCX/DOC/XLSX/XLS/CSV/TSV/HTML/TXT. Images redirect to preview flow. |
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
| `memory_history(key)` | ~200 | Full write history: timestamps, session IDs, freshness score, retrieval/correction counts |
| `memory_timeline(key)` | ~300 | Intellectual history rendered as a markdown timeline — every edit, freshness bar, version trail, git commits if enabled |
| `memory_list(folder)` | 50–200 | Keys in a vault folder |
| `memory_multi_search(queries)` | 600–2,700 | Fan-out parallel search, merged by relevance |
| `memory_deep_search(query)` | 1,000–2,000 | Graph-guided search over chat archive |
| `memory_get_raw_chunks(id, query)` | 1,000–7,000 | Relevant windows from a raw conversation |
| `memory_search_raw(title)` | ~200 | Fast title search over raw archive |
| `memory_get_raw(id)` | 5,000–35,000 | Full raw conversation — avoid unless necessary |
| `memory_ask(question)` | varies | Natural language Q&A over vault using Gemini |
| `memory_tree()` | ~20,000 | Full vault tree — use `memory_list` instead |

### Writing (low-level)

| Tool | Use |
|---|---|
| `memory_propose_update(patch)` | Propose a diff for manual approval. Supports `merge` field: `"replace"` (default), `"append"`, `"prepend"` |
| `memory_reject_update(patch_id)` | Discard a pending patch |
| `memory_diff()` | List pending patches |
| `memory_apply_all(folder, dry_run)` | Apply all pending patches at once |
| `memory_fix_frontmatter(dry_run)` | Find and fix files with missing required frontmatter fields |

### Code graph

| Tool | Use |
|---|---|
| `memory_scan_project(path)` | Index a code project (Python/JS/TS/Go/Rust/Java). **Runs in background by default** — poll `memory_index_status()` for progress. Pass `background=False` to block. |
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
| `memory_organize()` | Rebuild MOC index files (Obsidian) |
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
extraction_provider: gemini             # gemini | cerebras | groq | openai | openrouter | claude
write_mode: manual                      # manual | bulk | auto
git_enabled: true                       # auto-commit every write
encryption: false                       # Fernet at-rest encryption (set SYNAPSE_FERNET_KEY in .env)
cloud_search: false                     # Gemini LLM fallback for empty FTS5+semantic searches
weekly_report_day: monday
pending_auto_expire_days: 90            # auto-prune pending patches older than N days on load
raw_archive_path: ./synapse_extracted   # raw conversation archive (optional)
gemini_api_key: ""                      # prefer .env or GEMINI_API_KEY / GOOGLE_API_KEY env var
groq_api_key: ""
cerebras_api_key: ""
```

**Environment variable overrides:** `EXTRACTION_PROVIDER` (overrides `config.yaml`), `GEMINI_API_KEY`, `GOOGLE_API_KEY` (Gemini alias), `GROQ_API_KEY`, `CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `SYNAPSE_FERNET_KEY`.

**Important:** `write_mode` is only read from `config.yaml` — there is no env override. `OPENROUTER_API_KEY` is not part of `SynapseConfig`; it must be set in `.env` or the shell for `memory_triage` to find it.

---

## AI provider configuration

| Provider | Key | Free? | No data training | Best for |
|---|---|---|---|---|
| `gemini` (default) | `GEMINI_API_KEY` | Yes (free tier) | **No on free tier** | Semantic search, image extraction, `memory_ask` |
| `cerebras` | `CEREBRAS_API_KEY` | Yes | Yes | Fast extraction, watcher, low latency |
| `groq` | `GROQ_API_KEY` | Yes | Yes | Triage, extraction fallback |
| `openrouter` | `OPENROUTER_API_KEY` | Free models | Yes | Triage primary model |
| `openai` | `OPENAI_API_KEY` | Paid | Yes | Extraction |
| `claude` | `ANTHROPIC_API_KEY` | Paid | Yes | Extraction |

**Provider model details:**
- **Cerebras** — primary: `zai-glm-4.7`, congestion fallback: `gpt-oss-120b`. 3 retries, 12s timeout.
- **Groq** — rotates: `llama-3.3-70b-versatile` + `meta-llama/llama-4-scout-17b-16e-instruct`. 5 retries.
- **Triage** — OpenRouter primary (`inclusionai/ring-2.6-1t:free`), Groq fallback (`llama-3.1-8b-instant`).
- **`import_filtered_jsonl`** — always uses Gemini `gemma-4-31b-it` regardless of `extraction_provider`.
- **`memory_ask`** — always uses Gemini `gemini-2.0-flash` regardless of `extraction_provider`.
- **`best_complete()`** (used internally) — tries Cerebras first (15s wall timeout), falls back to Groq.

**Semantic embeddings:** `gemini-embedding-001` (3072-dim). Only generated when `GEMINI_API_KEY` is set. Required for `memory_smart_merge` similarity and hybrid search. Without it, search falls back to FTS5-only.

---

## Security — what never gets sent to an LLM

**Directories always skipped:**
`node_modules`, `__pycache__`, `.venv`, `venv`, `.git`, `dist`, `build`, `.next`, `out`, `release`, `desktop-artifacts`, `.turbo`, `.cache`, `coverage`, `.pytest_cache`, `.claude`, `vault`, `.backups`

**Files always skipped:**
`.env`, `.env.local`, `.env.production`, `.env.staging`, `config.yaml`, `config.yml`, `secrets.yaml`, `secrets.json`

**Extensions always skipped:**
`.pyc`, `.lock`, `.ico`, image formats, `.woff`, `.ttf`, `.map`, `.db`, `.sqlite`, `.exe`, `.dll`, `.so`, `.bin`, `.zip`, `.tar`, `.gz`

**Scan limits:** 12,000 chars per file, 500,000 chars total per scan.

---

## Encryption

Set `encryption: true` in `config.yaml` and add `SYNAPSE_FERNET_KEY=<key>` to `.env`. Generate a key:

```python
from server.encryption import generate_key
print(generate_key())
```

Encrypted files are stored with a `SYNAPSE-FERNET\n` prefix — unencrypted files are read transparently. To export the vault as a password-protected zip:

```python
from server.encryption import encrypted_export
encrypted_export(config, output_path="vault_backup.zip", password="your-password")
```

---

## Memory quality features

### Provenance tracking

Every write records the server session ID, timestamp, and trigger reason in the file's `History:` section using `[sess:XXXXXXXX]` tags:

```
History:
- 2026-07-06 [sess:5b246d3d]: Merged from ChatGPT export
- 2026-07-07 [sess:a1c3e9f2]: Corrected extraction provider
```

`memory_history(key)` returns the full structured history:

```json
{
  "key": "work.stack",
  "version": 4,
  "source_session": "5b246d3d...",
  "retrieval_count": 12,
  "correction_count": 1,
  "freshness": 0.87,
  "history": [
    {"date": "2026-07-06", "session_id": "5b246d3d", "text": "Initial import"},
    {"date": "2026-07-07", "session_id": "a1c3e9f2", "text": "Corrected provider"}
  ],
  "history_count": 2
}
```

Four session IDs are stamped per server restart: `_SESSION_ID` (main), `_WATCHER_SESSION_ID`, `_MERGER_SESSION_ID`, `_IMPORTER_SESSION_ID`. These flow into `source_session` in each written file so every patch is traceable to the exact server instance that wrote it.

Freshness formula: `exp(-days / 90) × min(1.5, 1+0.05×retrievals) × max(0.5, 1-0.1×corrections)`

When a patch is applied to an existing file: `retrieval_count` is preserved, `correction_count` is incremented by 1, and `freshness` is recomputed.

### Contradiction detection

`memory_propose_update` scans existing vault files for factual contradictions before queuing a patch. When `scope_key` is provided (as it always is during `propose_update`), only files sharing the same top-level folder prefix or overlapping triggers are compared — O(n) instead of O(n²). Conflicting patches are flagged `urgent=True` and held in the review queue with a `conflict_warning` field.

50+ hard-coded contradiction pairs are checked covering: database preference, cloud stance, framework choice, AI provider, language preference, project lifecycle, auth method, mobile platform, encryption setting, git status, and extraction provider.

`memory_conflicts(auto_resolve=True)` performs a full O(n²) vault scan and can automatically deprecate the older of two conflicting files.

### Patch merge modes

`memory_propose_update` supports a `merge` field that controls how new content combines with existing file content:

| `merge` value | Behaviour |
|---|---|
| `"replace"` (default) | New content replaces existing body |
| `"append"` | New content added after existing body |
| `"prepend"` | New content added before existing body |

Example — add a fact to an existing memory without losing what's there:
```json
{"key": "work.stack", "content": "Also uses Rust for CLI tools.", "merge": "append"}
```

### Pending patch expiry

Pending patches older than `pending_auto_expire_days` (default: 90) are automatically pruned from `_pending.json` every time `load_pending` runs. Set to 0 to disable. Stale patches no longer accumulate silently.

### Intellectual history view

`memory_timeline(key)` renders a memory's full evolution as a formatted markdown document — useful for understanding how a belief or piece of knowledge changed over time:

```
# Timeline: `work.stack`

## Current state

| Field        | Value                    |
|---|---|
| Version      | 4                        |
| Last updated | 2026-07-06               |
| Source session | `5b246d3d`…            |
| Freshness    | `████████░░` 0.82        |
| Retrievals   | 12                       |
| Corrections  | 1                        |

## Edit history

### 1. 2026-06-01  `[sess:a1c3e9f2]`
> Initial import from ChatGPT export

### 2. 2026-06-14  `[sess:3d72d1fc]`
> Added Rust — confirmed via project scan

### 3. 2026-07-06  `[sess:5b246d3d]`
> Corrected extraction provider from gemini to cerebras

## Git commits

a1c3e9f work.stack: initial import
3d72d1f work.stack: added Rust
5b246d3d work.stack: corrected provider
```

The freshness bar (`█░`) gives an instant read on how stale the memory is. Git commits are appended automatically when `git_enabled: true`.

`memory_history(key)` returns the same data as structured JSON if you need to process it programmatically.

### Cross-model consensus

During incremental watcher scans, if a second provider key is available alongside the primary `extraction_provider`, both providers extract the file independently. Their outputs are compared using word-level Jaccard similarity:

- **Agreed (Jaccard ≥ 0.4):** logged silently — both providers converged.
- **Disagreed (Jaccard < 0.4):** the patch is flagged with a `consensus_flag` field, and both output snippets are stored for inspection.

All entries are written to `vault/projects/<slug>/_consensus.json` (capped at 500 entries, newest kept).

**Which provider acts as secondary:** Cerebras is preferred (if key present and primary isn't Cerebras), then Groq. If only one provider key is configured, consensus is skipped.

Example `_consensus.json` entry:
```json
{
  "ts": "2026-07-06T14:22:11+00:00",
  "file": "server/diff.py",
  "primary": "gemini",
  "secondary": "cerebras",
  "jaccard": 0.31,
  "agreed": false,
  "primary_snippet": "...",
  "secondary_snippet": "..."
}
```

This surfaces extraction disagreements as data rather than silently discarding one result — letting you spot files where the LLM is uncertain about the right memory structure.

---

## Vault internal files

| File | Purpose |
|---|---|
| `vault/_index.db` | SQLite FTS5 memories + embedding vectors |
| `vault/_code_index.db` | Code graph nodes, edges, and code embeddings |
| `vault/_pending.json` | Pending patch queue |
| `vault/_rejections.jsonl` | Append-only log of rejected patches |
| `vault/_weekly.md` | Last weekly report |
| `vault/_import_resume.json` | Failed import chunks (for `resume_failed=True`) |
| `vault/metadata/topic_graph.json` | Weighted graph linking chats by topic |
| `vault/projects/<slug>/_graph.json` | Code graph + file hashes for incremental scans |
| `vault/projects/<slug>/_consensus.json` | Cross-provider disagreement log |

---

## CLI — terminal pipeline

Install `typer` (`pip install typer`) then run pipeline commands directly from the terminal without MCP:

```bash
# One-command full pipeline: format → triage → import → rebuild → graph
python -m server.cli full-import /path/to/claude-export/

# Run just the triage step
python -m server.cli triage synapse_extracted/conversations_jsonl/ -o synapse_filtered/

# Format a Claude export to JSONL
python -m server.cli format-claude /path/to/export/ --no-markdown

# Rebuild the FTS5 index (optionally in background)
python -m server.cli rebuild-index
python -m server.cli rebuild-index --background

# Check background job progress
python -m server.cli index-status

# Build the topic graph
python -m server.cli build-graph --background
```

---

## Background jobs

`memory_rebuild_index` and `memory_build_graph` both accept `background=True`. When set, they return immediately and run in a daemon thread. Poll `memory_index_status()` for progress (0–100%), current step, and elapsed time.

`memory_finalize_chat` automatically schedules a debounced index rebuild 20 seconds after it runs. Subsequent finalize calls within 20 seconds reset the timer — only one rebuild fires per burst.

`memory_stop_job(name)` cancels a job cooperatively. Empty string cancels all running jobs plus any pending auto-rebuild timer.

---

## Vault watching (Obsidian sync)

`memory_watch_vault(enable=True)` polls `vault/*.md` every 5 seconds for mtime changes (e.g. from Obsidian edits) and auto-schedules an index rebuild when any change is detected. Call `memory_watch_vault(enable=False)` to stop it.

---

## Obsidian integration

Open `vault/` as an Obsidian vault. Run `memory_organize()` then `memory_relink_all()` to populate hub index files and wikilinks. Graph view shows a hub-and-spoke structure — one index node per folder, memory files as leaves, cross-links connecting related topics.

---

## Testing

```bash
# Full tool coverage — 187 checks, no API key needed
python -X utf8 Diagnostics/test_all_tools.py

# Core unit tests (pytest)
python -m pytest Diagnostics/test_core.py -q

# Stress test — scale, concurrency, edge cases
python -X utf8 Diagnostics/stress_test.py

# Code graph quality — scan a 10-file polyglot project end-to-end (requires Gemini key)
python -X utf8 Diagnostics/test_scan_calc.py

# Validate manifest.json matches registered tools in main.py (no API key needed)
python Diagnostics/check_manifest.py

# Regenerate manifest.json after adding or removing tools
python Diagnostics/check_manifest.py --update

# Benchmark: context tokens loaded with Synapse vs cold (no API key needed)
python Diagnostics/benchmark_with_without.py

# Rate-limit benchmarks (require API keys)
python Diagnostics/benchmark_groq_limits.py
python Diagnostics/benchmark_cerebras_limits.py

# Token reduction benchmark on a real vault (requires Gemini key)
python Diagnostics/benchmark_tokens.py
```

---

## Self-update

`updater.py` manages version upgrades without manual git operations:

```bash
# Check if a newer release is available
python updater.py --check

# Download and apply the latest release
python updater.py

# Force update even if already on latest version
python updater.py --force

# Roll back to the previous version (stored in .backups/)
python updater.py --rollback
```

The updater: fetches the latest GitHub release zip → backs up current code to `.backups/` → extracts the release (skipping `vault/`, `config.yaml`, `.env`, `.venv`) → runs `pip install -r requirements.txt` → runs `test_core.py` to verify → rolls back automatically on failure. The `.backups/` folder is excluded from git and never pushed.

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

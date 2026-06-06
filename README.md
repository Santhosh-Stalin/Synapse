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
- **Token-aware** — tiered retrieval stops as soon as confidence is high enough; every response carries a `_tokens` field so Claude tracks its own budget
- **Cross-provider** — import from ChatGPT, Claude.ai, or any conversation export; one vault for everything
- **Local and private** — plain Markdown files on your machine, no SaaS, no cloud sync
- **Code graph** — scan any project (Python, JS/TS, Go, Rust, Java) and ask Claude questions about it
- **MCP server** — 34 tools exposed via the Model Context Protocol; works with Claude Desktop and Claude Code

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
| `GEMINI_API_KEY` | aistudio.google.com/apikey | Everything — MCP server, search, import, scan |
| `GROQ_API_KEY` | console.groq.com | Filtering pipeline only (`Diagnostics/Triage.py`) |
| `OPENROUTER_API_KEY` | openrouter.ai/keys | Filtering pipeline only (`Diagnostics/Triage.py`) |

Only the Gemini key is needed for daily use.

---

## How Claude uses Synapse

### The two entry points

```python
memory_auto("your question")   # retrieval — loads context, searches vault, escalates if needed
memory_commit(patch)           # writes — proposes diff in review mode, applies directly in auto mode
```

`memory_auto` replaces the manual chain of `memory_context → memory_search → memory_deep_search`. It stops at the first confident result (score ≥ 0.7) so it never fetches more than necessary.

`memory_commit` behaviour is set during `setup.py`:
- **review mode** (default) — Claude proposes a diff, you approve before anything is written
- **auto mode** — Claude writes directly, no confirmation

### Retrieval tiers (what memory_auto does internally)

| Tier | Cost | When |
|---|---|---|
| 1 — Identity context | ~591 tokens | Every conversation |
| 2 — Active vault search | ~600–2,000 tokens | Topic or project questions |
| 3 — Deep search + raw chunks | ~2,000–9,000 tokens | "What did we discuss about X?" |

**Hard ceiling: never exceed 15,000 tokens on memory retrieval in one session.** Each response includes `_tokens` — Claude tracks a running total and downshifts tier when the budget fills.

### Token budget guidance for Claude

- Under 3,000 tokens spent: Tier 3 is fine
- 3,000–9,000 tokens spent: prefer Tier 2, use `memory_get_raw_chunks` not `memory_get_raw`
- Over 9,000 tokens spent: Tier 1 only, no further retrieval unless explicitly asked

---

## Code graph — scanning projects

`memory_scan_project` indexes a code project so you can ask Claude questions about it directly.

```python
memory_scan_project("/path/to/your/project")
memory_code_search("how does authentication work")
memory_code_stats()
```

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
  chats/       — summarised past conversations (searchable archive)
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

## All 34 MCP tools

### Retrieval

| Tool | Tokens | Use |
|---|---|---|
| `memory_auto(task)` | 591–9,000 | **Default.** Smart dispatcher — handles all tiers automatically |
| `memory_context()` | ~591 | Identity + vault health. Called by memory_auto |
| `memory_search(query)` | 200–900 | FTS5 + semantic search over active vault |
| `memory_get(key)` | 200–500 | Full content of one memory file |
| `memory_list_folder(folder)` | 50–200 | Keys in a vault folder |
| `memory_tree()` | ~20,000 | Full vault tree — avoid, use list_folder instead |
| `memory_deep_search(query)` | 1,000–2,000 | Graph-guided search over chat archive |
| `memory_get_raw_chunks(id, query)` | 1,000–7,000 | Relevant windows from a raw conversation |
| `memory_search_raw(title)` | ~200 | Fast title search over raw archive |
| `memory_get_raw(id)` | 5,000–35,000 | Full raw conversation — avoid unless necessary |

### Writing

| Tool | Use |
|---|---|
| `memory_commit(patch)` | **Default.** Write using configured mode (review or auto) |
| `memory_propose_update(patch)` | Propose a diff for manual approval |
| `memory_apply_update(patch_id)` | Apply an approved patch |
| `memory_reject_update(patch_id)` | Discard a pending patch |
| `memory_diff()` | List pending patches |

### Code graph

| Tool | Use |
|---|---|
| `memory_scan_project(path)` | Index a code project (Python/JS/TS/Go/Rust/Java) |
| `memory_code_search(query)` | Hybrid search over indexed code nodes |
| `memory_code_stats(project)` | Stats for indexed projects |

### Maintenance

| Tool | Use |
|---|---|
| `memory_import_ai_export(path)` | Import Claude.ai or ChatGPT export |
| `memory_triage(input_folder)` | AI triage — filter conversations into keep/skip/redflag before import. Needs `OPENROUTER_API_KEY` + `GROQ_API_KEY` |
| `memory_import_filtered_jsonl(path)` | Import kept chats from `memory_triage` output |
| `memory_import_synapse_summaries(path)` | Import pre-processed summary JSON (no LLM) |
| `memory_ingest_text(text)` | Extract patches from any pasted text |
| `memory_save_chat(...)` | Save a structured chat summary to vault |
| `memory_build_graph()` | Rebuild topic graph over vault/chats |
| `memory_relink_all()` | Recompute wikilinks for every file |
| `memory_rebuild_index()` | Rebuild FTS5 index from scratch |
| `memory_deduplicate()` | Report stray files and thin stubs |
| `memory_smart_merge()` | Find and merge semantic duplicates |
| `memory_organize_vault()` | Rebuild MOC index files (Obsidian) |
| `memory_weekly_report()` | Generate weekly activity report |
| `memory_conflicts()` | Find contradictions across vault files |
| `memory_start_watcher(path)` | Watch project folder, auto-extract on change |
| `memory_stop_watcher()` | Stop the running watcher |
| `memory_watcher_status()` | Check watcher state |

---

## Write mode

Set during `setup.py` or change in `config.yaml`:

```yaml
write_mode: review   # Claude proposes diff → you approve (default, safer)
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
pending_auto_expire_days: 7
raw_archive_path: ./synapse_extracted   # raw conversation archive (optional)
write_mode: review
gemini_api_key: ""                      # prefer .env or environment variable
```

---

## Obsidian integration

Open `vault/` as an Obsidian vault. Run `memory_organize_vault()` then `memory_relink_all()` to populate hub index files and wikilinks. Graph view shows a hub-and-spoke structure — one index node per folder, memory files as leaves, cross-links connecting related topics.

---

## Testing

```bash
# Full tool coverage — 70 checks across all 34 tools, no API key needed
python -X utf8 Diagnostics/test_all_tools.py

# Stress test — scale, concurrency, edge cases (28 checks)
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

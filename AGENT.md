# Synapse — MCP Memory Server

Synapse is a persistent, structured memory system for Claude. It stores memories as Markdown files in a local vault, indexed by SQLite FTS5 and a topic graph, and exposes them via 34 MCP tools.

---

## FIRST THING EVERY CONVERSATION

**Call `memory_auto(task)` for any retrieval question** — it loads context, searches the active vault, and escalates to deep search automatically. You do not need to chain `memory_context → memory_search → memory_deep_search` manually; `memory_auto` does it for you.

For write operations use **`memory_commit(patch)`** — behaviour depends on `write_mode` in config:

| Mode | What Claude must do |
|------|---------------------|
| `manual` (default) | Show the diff for **each** write and ask "Save this? (yes/no)" before calling `memory_commit`. Never write silently. |
| `bulk` | Queue all proposed changes silently during the conversation. When the user says "save" / "commit" / "done", show **all** pending diffs at once and ask for a single yes/no approval, then call `memory_commit` for each approved patch. |
| `auto` | Call `memory_commit` immediately. No confirmation. Briefly confirm what was saved (one line). |

Check the mode in the context response (`_config.write_mode`). If the field is missing, treat it as `manual`.

If `_vault_health.clean` is False in the context response, flag it and offer to run `memory_deduplicate(auto_clean=True)`.

---

## TIERED RETRIEVAL — pick the right depth

Do not blindly run all tools. Match retrieval depth to what the query actually needs.

### Tier 1 — Every conversation (always, ~591 tokens)
```
memory_context()
```
Covers identity, communication style, location, active project index. Sufficient for general questions, advice, and anything that doesn't reference past work.

### Tier 2 — Project or topic questions (~2,000 tokens total)
```
memory_context()  +  memory_search("query")  +  memory_get("key")
```
Use when the question is about a specific project, skill, or preference. Run `memory_search` first — if the active vault has the answer, stop here.

### Tier 3 — "What did we work on / discuss before?" (~9,000 tokens total)
```
memory_context()  +  memory_deep_search("query")  +  memory_get_raw_chunks(chat_id, query)
```
Use only when:
- The user explicitly asks about a past conversation
- The active vault doesn't have enough detail on a project
- You need to recover specific decisions, code, or reasoning from prior sessions

`memory_deep_search` returns ranked chat summaries. Pick the most relevant `chat_id`, then call `memory_get_raw_chunks` with the same query (~1–7k tokens instead of the full ~35k).

**Never call `memory_get_raw()` unless the user explicitly asks for the complete history of a chat.**

### Decision tree
```
Is the question simple / general?
  → Tier 1 only

Does it reference a specific project, skill, or preference?
  → Tier 2 (check active vault first)
  → If vault is thin on that topic → Tier 3

Does it ask "what did we discuss / what was the code / what did we decide"?
  → Tier 3 directly
```

---

## Vault structure

```
vault/
  identity/   — who the user is: profile, education, philosophy, interaction style
  life/        — hobbies, fitness, travel, photography, creative interests
  projects/    — every project: stack, status, key technical details
  patterns/    — recurring skills: techniques, workflows, prompting habits
  work/        — dev environment, tools, accounts, domain expertise
  chats/       — summarised past conversations (passive archive, searchable)
  metadata/    — topic_graph.json linking chats by shared topics/projects
```

Each active vault file has YAML frontmatter (`key`, `type`, `triggers`, `related`) and Markdown content.

---

## Tools — when to use each

### Smart tools (use these by default)
| Tool | When |
|---|---|
| `memory_auto("task")` | **Default retrieval.** Loads context + vault search + deep search if needed. |
| `memory_commit(patch)` | **Default write.** Proposes diff in `review`, applies immediately in `auto`. |

### Conversation start
| Tool | Tokens | When |
|---|---|---|
| `memory_context()` | ~591 | First call every conversation. Identity + active project index. |

### Active vault lookup (Tier 2)
| Tool | Tokens | When |
|---|---|---|
| `memory_search("query")` | ~200–900 | Find relevant active vault keys. Returns top 4. |
| `memory_get("some.key")` | ~200–500 | Full content of a specific key. |
| `memory_list_folder("projects")` | ~50–200 | List all keys in a folder. Use instead of memory_tree. |

### Chat archive lookup (Tier 3)
| Tool | Tokens | When |
|---|---|---|
| `memory_deep_search("query")` | ~1,000–2,000 | FTS5 + graph traversal over chat archive. Returns 8 ranked results. |
| `memory_get_raw_chunks(id, query)` | ~1,000–7,000 | Relevant message windows from a raw conversation. Prefer over memory_get_raw. |
| `memory_search_raw("title")` | ~200 | Fast title-only search over raw archive. |
| `memory_get_raw(id)` | ~5,000–35,000 | Full raw conversation. Only when complete history is explicitly needed. |

### Writing memories (low-level)
| Tool | When |
|---|---|
| `memory_propose_update(patch)` | Propose a patch and return a diff without writing. |
| `memory_apply_update(patch_id)` | Apply an approved pending patch. |
| `memory_reject_update(patch_id)` | Discard a pending patch. |
| `memory_diff()` | List all pending patches awaiting approval. |

### Code graph
| Tool | When |
|---|---|
| `memory_scan_project("path")` | Index a code project — extracts functions, classes, call graph. Supports Python, JS/TS, Go, Rust, Java. |
| `memory_code_search("query")` | Hybrid search over indexed code nodes (FTS5 + semantic). |
| `memory_code_stats(project)` | Stats for indexed projects: file count, function nodes, edges. |

### Import & ingestion
| Tool | When |
|---|---|
| `memory_import_ai_export("path")` | Import Claude.ai or ChatGPT data export. |
| `memory_import_filtered_jsonl("path")` | Import pre-filtered JSONL conversation folder. |
| `memory_import_synapse_summaries("path")` | Import synapse_ai_summaries JSON folder. No LLM needed. |
| `memory_ingest_text(text)` | Paste raw text — Gemini extracts patches from it. |
| `memory_save_chat(title, summary, ...)` | Save a structured chat summary to vault/chats. |

### Maintenance
| Tool | When |
|---|---|
| `memory_build_graph()` | Build/rebuild topic graph over vault/chats. Run after new imports. |
| `memory_relink_all()` | Recompute triggers + related links for every vault file. Run after bulk imports. |
| `memory_rebuild_index()` | Rebuild the SQLite FTS5 index from scratch. Run if search feels stale. |
| `memory_deduplicate()` | Report stray files, thin stubs, and duplicate pairs. |
| `memory_smart_merge()` | Find and merge semantic duplicates using embedding similarity. |
| `memory_organize_vault()` | Rebuild MOC index files for every folder (Obsidian). |
| `memory_conflicts()` | Find contradictions between memory files. Skips chats/ automatically. |
| `memory_weekly_report()` | Generate the weekly Synapse activity report. |

### File watcher
| Tool | When |
|---|---|
| `memory_start_watcher("path")` | Watch a project directory — auto-extracts changed files with Gemini. |
| `memory_stop_watcher()` | Stop the running watcher. |
| `memory_watcher_status()` | Check watcher state. |

### Never use
| Tool | Why |
|---|---|
| `memory_tree()` | ~20k tokens. Use `memory_list_folder` instead. |

---

## Token tracking

Every dict-returning tool includes a `_tokens` field. Track a running session total:

- Under 3,000 tokens: full Tier 3 is fine
- 3,000–9,000 tokens: prefer Tier 2, use `memory_get_raw_chunks` not `memory_get_raw`
- Over 9,000 tokens: Tier 1 only, no further retrieval unless the user explicitly asks

**Never exceed 15,000 tokens on memory retrieval alone in a single session.**

---

## Token cost reference

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

Categories: `identity.*` · `life.*` · `projects.*` · `patterns.*` · `work.*`

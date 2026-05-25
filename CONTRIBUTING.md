# Contributing to Synapse

## Setup

```bash
git clone https://github.com/Santhosh-Stalin/Synapse.git
cd Synapse
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install black pytest
```

## Running tests

```bash
# Full tool coverage — 70 checks across all 34 tools, no API key needed (~10s)
python -X utf8 Diagnostics/test_all_tools.py

# Stress test — scale (500 files), concurrency (20 threads), edge cases (28 checks, ~15s)
python -X utf8 Diagnostics/stress_test.py

# Code graph quality — polyglot scan (Python/JS/TS/Go/Rust), requires Gemini key (~3–4 min)
python -X utf8 Diagnostics/test_scan_calc.py
```

All tool coverage and stress tests run without a Gemini API key. No vault, no network.

## Code style

Formatted with [black](https://github.com/psf/black) at line length 100.

```bash
black server/ Diagnostics/ installer/ setup.py run_server.py --line-length 100
```

## Adding a new MCP tool

1. Add the function to `server/functions.py`
2. Register it in `server/main.py` with `@app.tool(name="memory_<name>")`
3. Add it to the tool table in `AGENT.md` so Claude knows when to use it
4. Add it to the tool table in `README.md`
5. Add at least one check in `Diagnostics/test_all_tools.py`

## Adding a new language to the code graph

1. Add regex patterns and an `_extract_<lang>()` function in `server/graph.py`
2. Wire it into `extract_code_graph()` — add the extension to the dispatch block
3. Return nodes with: `id`, `label`, `file`, `type`, `parent`, `source`, `lineno`, `lineno_end`
4. Test with `python -X utf8 Diagnostics/test_scan_calc.py` (add a sample file for the new language)

## Adding a new import provider

1. Add detection logic in `server/ai_importer.py` → `_detect_provider()`
2. Write a `_preprocess_<provider>()` function that returns `{label: text}` chunks
3. Wire it into the dispatch block at the bottom of `import_ai_export()`
4. Test with a real export from that provider

## What not to touch

- `vault/` — personal memory data, gitignored, never committed
- `config.yaml` — gitignored (contains personal vault path and API key); use `config.example.yaml` as reference
- `_pending.json`, `_index.db`, `_code_index.db` — runtime files, not source
- `vault/_graph.json` — generated per-project by scan_project, not source

## Submitting changes

Open a pull request against `master`. CI runs the test suite automatically on every push.

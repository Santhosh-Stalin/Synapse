"""
Scan test: fake calculator project across 10 file types.
Checks:
  - Every file type is picked up by the scanner
  - Gemini returns meaningful (non-empty, non-generic) descriptions
  - Function nodes are extracted from AST
  - Patches are proposed with real content
  - Code index is populated

Run: python Diagnostics/test_scan_calc.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.scanner import scan_and_extract, scan_project

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

# ── Fake calculator project ───────────────────────────────────────────────────

CALC_FILES = {
    "calc.py": """\
\"\"\"Core calculator logic.\"\"\"

HISTORY_LIMIT = 100
DEFAULT_PRECISION = 10

class Calculator:
    \"\"\"Stateful calculator with history.\"\"\"

    def __init__(self, precision: int = DEFAULT_PRECISION):
        self.precision = precision
        self.history: list[str] = []

    def add(self, a: float, b: float) -> float:
        \"\"\"Add two numbers and record to history.\"\"\"
        result = round(a + b, self.precision)
        self._record(f"{a} + {b} = {result}")
        return result

    def subtract(self, a: float, b: float) -> float:
        result = round(a - b, self.precision)
        self._record(f"{a} - {b} = {result}")
        return result

    def multiply(self, a: float, b: float) -> float:
        result = round(a * b, self.precision)
        self._record(f"{a} * {b} = {result}")
        return result

    def divide(self, a: float, b: float) -> float:
        if b == 0:
            raise ZeroDivisionError("Cannot divide by zero")
        result = round(a / b, self.precision)
        self._record(f"{a} / {b} = {result}")
        return result

    def _record(self, entry: str) -> None:
        self.history.append(entry)
        if len(self.history) > HISTORY_LIMIT:
            self.history.pop(0)

    def clear_history(self) -> None:
        \"\"\"Clear calculation history.\"\"\"
        self.history.clear()
""",

    "api.js": """\
const express = require('express');
const app = express();
const PORT = 3000;

app.use(express.json());

// POST /calculate — evaluate a math expression
app.post('/calculate', (req, res) => {
  const { expression } = req.body;
  if (!expression) return res.status(400).json({ error: 'expression required' });
  try {
    // Safe eval using Function constructor
    const result = new Function('return ' + expression)();
    res.json({ result, expression });
  } catch (err) {
    res.status(422).json({ error: 'Invalid expression', detail: err.message });
  }
});

// GET /history — return last 20 calculations from in-memory store
const history = [];
app.get('/history', (req, res) => res.json(history.slice(-20)));

app.listen(PORT, () => console.log(`Calculator API running on :${PORT}`));
""",

    "App.tsx": """\
import React, { useState } from 'react';

interface CalcState {
  display: string;
  history: string[];
}

const App: React.FC = () => {
  const [state, setState] = useState<CalcState>({ display: '0', history: [] });

  const handleKey = (key: string) => {
    setState(prev => ({ ...prev, display: prev.display === '0' ? key : prev.display + key }));
  };

  const handleEquals = async () => {
    const res = await fetch('/calculate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expression: state.display }),
    });
    const data = await res.json();
    setState(prev => ({
      display: String(data.result),
      history: [...prev.history, `${state.display} = ${data.result}`],
    }));
  };

  return (
    <div className="calculator">
      <div className="display">{state.display}</div>
      {['7','8','9','/','4','5','6','*','1','2','3','-','0','.','=','+'].map(k => (
        <button key={k} onClick={k === '=' ? handleEquals : () => handleKey(k)}>{k}</button>
      ))}
    </div>
  );
};

export default App;
""",

    "styles.css": """\
/* Calculator layout */
.calculator {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  max-width: 320px;
  margin: 40px auto;
  background: #1a1a1a;
  padding: 16px;
  border-radius: 12px;
}

.display {
  grid-column: span 4;
  background: #0d0d0d;
  color: #e8e8e8;
  font-size: 28px;
  text-align: right;
  padding: 12px 16px;
  border-radius: 8px;
  min-height: 56px;
  overflow: hidden;
  text-overflow: ellipsis;
}

button {
  background: #2a2a2a;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 16px;
  font-size: 18px;
  cursor: pointer;
  transition: background 0.15s;
}

button:hover { background: #3a3a3a; }
""",

    "schema.sql": """\
-- Calculator history database schema

CREATE TABLE IF NOT EXISTS calculations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    expression  TEXT    NOT NULL,
    result      REAL    NOT NULL,
    user_id     INTEGER REFERENCES users(id),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    UNIQUE NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_calculations_user ON calculations(user_id);
CREATE INDEX idx_calculations_created ON calculations(created_at);
""",

    "config.yaml": """\
app:
  name: calculator
  version: "1.0.0"
  port: 3000

calculator:
  precision: 10
  history_limit: 100
  allow_expressions: true

database:
  path: ./calc.db
  pool_size: 5

logging:
  level: info
  file: ./logs/calc.log
""",

    "README.md": """\
# Calculator

A full-stack calculator with Python backend, Express API, and React frontend.

## Stack
- **Backend**: Python `Calculator` class (`calc.py`) — add, subtract, multiply, divide with history
- **API**: Node.js/Express (`api.js`) — POST `/calculate`, GET `/history`
- **Frontend**: React/TypeScript (`App.tsx`) — grid layout, async fetch to API
- **Database**: SQLite (`schema.sql`) — persists calculations per user

## Setup
```bash
pip install -r requirements.txt
npm install
npm start
```

## Config
All settings in `config.yaml`. Default port: 3000, precision: 10 decimal places.
""",

    "data.json": """\
{
  "operators": ["+", "-", "*", "/"],
  "constants": {
    "pi": 3.141592653589793,
    "e": 2.718281828459045,
    "phi": 1.618033988749895
  },
  "test_cases": [
    {"expression": "2 + 2", "expected": 4},
    {"expression": "10 / 3", "expected": 3.3333333333},
    {"expression": "2 ** 10", "expected": 1024}
  ]
}
""",

    "utils.go": """\
package main

import (
    "fmt"
    "math"
    "strconv"
)

// Round rounds a float64 to n decimal places.
func Round(val float64, precision int) float64 {
    ratio := math.Pow(10, float64(precision))
    return math.Round(val*ratio) / ratio
}

// FormatResult formats a calculation result for display.
// Returns integer string if no fractional part, else decimal string.
func FormatResult(val float64) string {
    if val == math.Trunc(val) {
        return strconv.FormatInt(int64(val), 10)
    }
    return fmt.Sprintf("%.10g", val)
}
""",

    "calc.rs": """\
/// Calculator error types
#[derive(Debug)]
pub enum CalcError {
    DivisionByZero,
    Overflow,
}

/// Perform a safe arithmetic operation
pub fn calculate(a: f64, op: char, b: f64) -> Result<f64, CalcError> {
    match op {
        '+' => Ok(a + b),
        '-' => Ok(a - b),
        '*' => Ok(a * b),
        '/' => {
            if b == 0.0 {
                Err(CalcError::DivisionByZero)
            } else {
                Ok(a / b)
            }
        }
        _ => Err(CalcError::Overflow),
    }
}

/// Format a float result, removing trailing zeros
pub fn format_result(val: f64) -> String {
    if val.fract() == 0.0 {
        format!("{}", val as i64)
    } else {
        format!("{:.10}", val).trim_end_matches('0').to_string()
    }
}
""",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def check(label: str, ok: bool, detail: str = ""):
    icon = PASS if ok else FAIL
    print(f"  {icon}  {label:<50} {detail}")
    return ok


def warn(label: str, detail: str = ""):
    print(f"  {WARN}  {label:<50} {detail}")


def is_meaningful(text: str) -> bool:
    """Return True if description looks like real content, not a placeholder."""
    if not text or len(text) < 20:
        return False
    garbage = {"n/a", "none", "todo", "placeholder", "description", "summary"}
    if text.lower().strip() in garbage:
        return False
    return True


# ── Main test ─────────────────────────────────────────────────────────────────

def main():
    print("\n== Synapse scan_project test — Calculator project ==\n")

    # Load real config (picks up GEMINI_API_KEY from .env)
    config = load_config(ROOT / "config.yaml")
    if not config.gemini_api_key:
        print(f"  {FAIL}  No GEMINI_API_KEY — AI extraction will be skipped")
        print("         Set GEMINI_API_KEY in .env and retry")
        return

    print(f"  Gemini key: ...{config.gemini_api_key[-6:]}")
    print(f"  Vault: {config.vault_path}\n")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        project_dir = Path(tmp) / "calculator"
        project_dir.mkdir()

        # Write all files
        for fname, content in CALC_FILES.items():
            (project_dir / fname).write_text(content, encoding="utf-8")

        print(f"Created {len(CALC_FILES)} files: {', '.join(CALC_FILES)}\n")

        # ── 1. Raw scan ───────────────────────────────────────────────────────
        print("── 1. Raw file scan ──────────────────────────────────────────")
        raw = scan_project(str(project_dir))
        found_files = set(raw["files"].keys())
        for fname in CALC_FILES:
            check(f"  {fname} picked up", fname in found_files)
        print()

        # ── 2. AI extraction ──────────────────────────────────────────────────
        print("── 2. AI extraction (Gemini) ─────────────────────────────────")
        t0 = time.perf_counter()
        result = scan_and_extract(config, str(project_dir))
        elapsed = time.perf_counter() - t0

        if "error" in result and "proposals" not in result:
            print(f"  {FAIL}  scan_and_extract failed: {result['error']}")
            return

        print()
        check("No top-level error", "error" not in result or "proposals" in result)
        check("Files extracted > 0", result.get("files_extracted", 0) > 0,
              str(result.get("files_extracted", 0)))
        check("Patches proposed > 0", len(result.get("proposals", [])) > 0,
              f"{len(result.get('proposals', []))} patches")
        check("Function nodes extracted", result.get("function_nodes", 0) > 0,
              f"{result.get('function_nodes', 0)} nodes")
        # Free tier = 15 RPM — 10 files + function batches takes ~3 min
        check(f"Completed in < 300s", elapsed < 300, f"{elapsed:.1f}s")
        print()

        # ── 3. Patch quality ──────────────────────────────────────────────────
        print("── 3. Patch content quality ──────────────────────────────────")
        proposals = result.get("proposals", [])
        meaningful = 0
        empty = 0
        for p in proposals:
            content = p.get("content", "")
            key = p.get("key", "?")
            if is_meaningful(content):
                meaningful += 1
            else:
                empty += 1
                warn(f"  Thin patch: {key}", repr(content[:80]))

        check("All patches have meaningful content",
              empty == 0, f"{meaningful} meaningful, {empty} thin/empty")

        # Show sample patches
        print()
        print("  Sample patches generated:")
        for p in proposals[:3]:
            print(f"\n  key: {p.get('key', '?')}")
            preview = p.get("content", "")[:300].replace("\n", "\n  ")
            print(f"  {preview}")
            if len(p.get("content", "")) > 300:
                print("  [...]")

        # ── 4. File type coverage ─────────────────────────────────────────────
        print("\n── 4. File type coverage in patches ──────────────────────────")
        patched_keys = {p.get("key", "") for p in proposals}
        print(f"  Patch keys: {sorted(patched_keys)}")

        # ── 5. Graph file written ─────────────────────────────────────────────
        print("\n── 5. Knowledge graph (_graph.json) ──────────────────────────")
        graph_path = config.vault_path / "projects" / "calculator" / "_graph.json"
        if graph_path.exists():
            graph = json.loads(graph_path.read_text())
            nodes = graph.get("nodes", [])
            edges = graph.get("edges", [])
            fns = [n for n in nodes if n.get("type") in ("function", "class")]
            check("Graph file written", True, str(graph_path))
            check("Graph has nodes", len(nodes) > 0, f"{len(nodes)} nodes")
            check("Graph has function/class nodes", len(fns) > 0,
                  f"{len(fns)} functions/classes")
            check("Graph has edges", len(edges) > 0, f"{len(edges)} edges")

            print("\n  Function nodes:")
            for n in fns[:8]:
                desc = n.get("description", "")[:80]
                print(f"    [{n.get('type')}] {n['id']}: {desc or '(no description)'}")
        else:
            check("Graph file written", False, "not found — may need re-run")

        print(f"\n{'='*60}")
        print(f"  Done in {elapsed:.1f}s")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    main()

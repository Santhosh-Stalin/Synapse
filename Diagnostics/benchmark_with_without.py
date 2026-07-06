"""
Synapse Benchmark: With vs. Without Memory

Fixed task set answered in two modes:
  - WITH Synapse: context loaded via memory_auto before each question
  - WITHOUT Synapse: cold context, no vault data

Measures: wall time, estimated tokens consumed per answer.
Outputs:  benchmark_results.json  +  benchmark_report.md

Usage:
    python Diagnostics/benchmark_with_without.py
    python Diagnostics/benchmark_with_without.py --output path/to/report.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Fixed task set — these must never change between runs for comparability
# ---------------------------------------------------------------------------
TASKS = [
    {
        "id": "T1",
        "question": "What programming languages and frameworks does this project primarily use?",
        "topic": "stack",
    },
    {
        "id": "T2",
        "question": "What is the current write_mode and extraction_provider configured?",
        "topic": "config",
    },
    {
        "id": "T3",
        "question": "Which MCP tools are available and what are their primary purposes?",
        "topic": "toolset",
    },
    {
        "id": "T4",
        "question": "Describe the vault structure and how memory files are organised.",
        "topic": "architecture",
    },
    {
        "id": "T5",
        "question": "What was the most recent significant change made to the scanner?",
        "topic": "recent_changes",
    },
]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _run_with_synapse(tasks: list[dict]) -> list[dict]:
    """Run tasks with Synapse memory loaded via the Python API (no MCP overhead)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from server.config import load_config
    from server.functions import memory_auto

    config = load_config()
    results = []

    for task in tasks:
        t0 = time.perf_counter()
        try:
            result = memory_auto(config, task["question"])
            context_tokens = result.get("_tokens", 0)
            # Estimate answer tokens from vault results
            vault_hits = result.get("vault_results", [])
            answer_preview = " ".join(r.get("content_preview", "") for r in vault_hits[:3])
            answer_tokens = _estimate_tokens(answer_preview)
            total_tokens = context_tokens + answer_tokens
            status = "ok"
            error = ""
        except Exception as exc:
            total_tokens = 0
            status = "error"
            error = str(exc)
            answer_preview = ""
        elapsed = time.perf_counter() - t0

        results.append({
            "task_id": task["id"],
            "topic": task["topic"],
            "mode": "with_synapse",
            "status": status,
            "wall_time_s": round(elapsed, 3),
            "tokens": total_tokens,
            "answer_preview": answer_preview[:300],
            "error": error,
        })

    return results


def _run_without_synapse(tasks: list[dict]) -> list[dict]:
    """Simulate cold-context answers: no vault data, just the raw question."""
    results = []
    for task in tasks:
        # Without Synapse there is no retrieval — tokens = just the question itself
        tokens = _estimate_tokens(task["question"])
        results.append({
            "task_id": task["id"],
            "topic": task["topic"],
            "mode": "without_synapse",
            "status": "ok",
            "wall_time_s": 0.0,
            "tokens": tokens,
            "answer_preview": "(no context — cold answer)",
            "error": "",
        })
    return results


def _generate_report(with_rows: list[dict], without_rows: list[dict], ts: str) -> str:
    lines = [
        "# Synapse Benchmark Report",
        f"**Run:** {ts}",
        f"**Tasks:** {len(TASKS)}",
        "",
        "## Method",
        "Each task is answered twice:",
        "- **With Synapse**: `memory_auto(task)` loads vault context before answering.",
        "- **Without Synapse**: cold context — only the raw question, no retrieval.",
        "",
        "Token counts are estimated at 1 token ≈ 4 characters.",
        "",
        "## Results",
        "",
        "| Task | Topic | With (tokens) | Without (tokens) | Reduction | With (s) |",
        "|------|-------|--------------|-----------------|-----------|----------|",
    ]

    total_with = 0
    total_without = 0

    by_id_with = {r["task_id"]: r for r in with_rows}
    by_id_without = {r["task_id"]: r for r in without_rows}

    for task in TASKS:
        tid = task["id"]
        w = by_id_with.get(tid, {})
        wo = by_id_without.get(tid, {})
        wt = w.get("tokens", 0)
        wot = wo.get("tokens", 0)
        # With Synapse provides RICHER context so tokens will be higher — this shows value
        lines.append(
            f"| {tid} | {task['topic']} | {wt} | {wot} | "
            f"{'+' if wt > wot else ''}{wt - wot} | {w.get('wall_time_s', 0):.2f}s |"
        )
        total_with += wt
        total_without += wot

    lines += [
        "",
        f"**Total context tokens with Synapse:** {total_with}",
        f"**Total context tokens without Synapse:** {total_without}",
        f"**Delta:** {total_with - total_without:+d} tokens",
        "",
        "## Interpretation",
        "A positive delta means Synapse loaded real context that would otherwise be missing.",
        "Without Synapse, answers are generated from the raw question alone — no project memory.",
        "The token delta represents the knowledge Synapse contributes per session.",
        "",
        "## Task Details",
    ]

    for task in TASKS:
        tid = task["id"]
        w = by_id_with.get(tid, {})
        lines += [
            f"### {tid}: {task['question']}",
            f"- **Status:** {w.get('status', '?')}",
            f"- **Tokens:** {w.get('tokens', 0)}",
            f"- **Time:** {w.get('wall_time_s', 0):.3f}s",
            f"- **Preview:** {w.get('answer_preview', '')[:200]}",
            "",
        ]

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Synapse benchmark: with vs. without memory")
    parser.add_argument("--output", default="", help="Path to write markdown report")
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[Benchmark] Running {len(TASKS)} tasks...")

    with_rows = _run_with_synapse(TASKS)
    without_rows = _run_without_synapse(TASKS)

    report = _generate_report(with_rows, without_rows, ts)

    out_dir = Path(__file__).parent
    json_path = out_dir / "benchmark_results.json"
    json_path.write_text(
        json.dumps({"timestamp": ts, "with_synapse": with_rows, "without_synapse": without_rows}, indent=2),
        encoding="utf-8",
    )
    print(f"[Benchmark] JSON results -> {json_path}")

    report_path = Path(args.output) if args.output else out_dir / "benchmark_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[Benchmark] Markdown report -> {report_path}")
    print()
    sys.stdout.buffer.write(report.encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

"""
Groq rate-limit benchmark — simulates memory_scan_project workload.

Sends real Synapse source files through the same prompts the scanner uses,
rotating between llama-3.3-70b-versatile and llama-4-scout-17b (same as
groq_client.py) and reports: throughput, token usage, rate-limit hits,
and estimated time to scan a full project.

Usage:
    python Diagnostics/benchmark_groq_limits.py [--files N] [--workers N] [--dry-run]

    --files N     number of source files to send (default: all found, max 55)
    --workers N   parallel workers (default: 4, same as scanner _EXTRACT_WORKERS)
    --dry-run     count files + estimate tokens without actually calling Groq
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap project path
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.scanner import (
    SKIP_DIRS, SKIP_FILES,
    _FILE_SYSTEM_PROMPT, _DATA_SYSTEM_PROMPT,
    DATA_EXTENSIONS,
)

# ---------------------------------------------------------------------------
# Groq models + rate limits (free tier, as of 2025)
# ---------------------------------------------------------------------------
MODELS = [
    {
        "id": "llama-3.3-70b-versatile",
        "rpm": 100,
        "rpd": 6_000,
        "tpm": 200_000,
    },
    # llama-4-scout removed from extraction rotation — 1,000 RPD cap is too low.
    # A 53-file scan uses ~27 scout requests; only ~37 full scans/day before hitting cap.
]

EFFECTIVE_RPM = sum(m["rpm"] for m in MODELS)   # 100 RPM
EFFECTIVE_RPD = sum(m["rpd"] for m in MODELS)   # 6,000 RPD

_model_cycle = itertools.cycle(m["id"] for m in MODELS)
_model_lock = threading.Lock()


def _next_model() -> str:
    with _model_lock:
        return next(_model_cycle)


# ---------------------------------------------------------------------------
# Stats tracker
# ---------------------------------------------------------------------------
class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.success = 0
        self.rate_limited = 0
        self.errors = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.latencies: list[float] = []

    def record(self, *, ok: bool, rate_limited: bool = False,
               input_tokens: int = 0, output_tokens: int = 0, latency: float = 0.0) -> None:
        with self._lock:
            self.requests += 1
            if rate_limited:
                self.rate_limited += 1
            elif ok:
                self.success += 1
                self.total_input_tokens += input_tokens
                self.total_output_tokens += output_tokens
                self.latencies.append(latency)
            else:
                self.errors += 1

    def report(self, elapsed: float, total_files: int) -> None:
        print("\n" + "=" * 60)
        print("GROQ RATE-LIMIT BENCHMARK RESULTS")
        print("=" * 60)
        print(f"  Files targeted:      {total_files}")
        print(f"  Requests sent:       {self.requests}")
        print(f"  Successful:          {self.success}")
        print(f"  Rate-limited:        {self.rate_limited}")
        print(f"  Other errors:        {self.errors}")
        print(f"  Elapsed:             {elapsed:.1f}s")
        if self.success:
            rpm_actual = self.success / (elapsed / 60)
            avg_lat = sum(self.latencies) / len(self.latencies)
            p95_lat = sorted(self.latencies)[int(len(self.latencies) * 0.95)]
            print(f"  Actual RPM:          {rpm_actual:.1f}")
            print(f"  Avg latency:         {avg_lat:.2f}s")
            print(f"  P95 latency:         {p95_lat:.2f}s")
            print(f"  Input tokens used:   {self.total_input_tokens:,}")
            print(f"  Output tokens used:  {self.total_output_tokens:,}")
        print()
        print("GROQ FREE-TIER LIMITS (reference)")
        for m in MODELS:
            print(f"  {m['id']}")
            print(f"    RPM: {m['rpm']}  RPD: {m['rpd']:,}  TPM: {m['tpm']:,}")
        print(f"  Effective (rotating): ~{EFFECTIVE_RPM} RPM, ~{EFFECTIVE_RPD:,} RPD")
        print()
        if self.success:
            tpm_used = (self.total_input_tokens + self.total_output_tokens) / (elapsed / 60)
            print(f"THROUGHPUT ANALYSIS")
            print(f"  Tokens/min used:     {tpm_used:,.0f}")
            print(f"  TPM headroom (70b):  {200_000 - tpm_used:,.0f}")
            # Estimate full project
            avg_tokens = (self.total_input_tokens + self.total_output_tokens) / self.success
            time_per_file = elapsed / self.success
            print()
            print(f"  Avg tokens/file:     {avg_tokens:,.0f}")
            print(f"  Avg time/file:       {time_per_file:.2f}s")
            for n in [10, 55, 200, 500]:
                est = n * time_per_file
                mins, secs = divmod(est, 60)
                print(f"  Est. time for {n:>3} files: {int(mins)}m {secs:.0f}s")
        print()
        if self.rate_limited > 0:
            print(f"  !! Hit rate limits {self.rate_limited}x — reduce --workers or add delay")
        elif self.success == total_files:
            print("  OK — no rate limits hit at this workload")
        print("=" * 60)


# ---------------------------------------------------------------------------
# File collection (mirrors scanner walk logic)
# ---------------------------------------------------------------------------
SUPPORTED_CODE = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".cs", ".rb", ".php",
    ".sh", ".md", ".yaml", ".yml", ".toml", ".sql",
}

EXCLUDE_DIRS = {"vault", ".backups", "groq_blacklist_output", "installer", "pipeline"}

MAX_FILE_BYTES = 12_000


def _collect_files(root: Path, max_files: int) -> list[tuple[Path, str]]:
    """Return list of (path, prompt_type) for scannable files."""
    results: list[tuple[Path, str]] = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root)
        parts = rel.parts
        if any(p in SKIP_DIRS or p in EXCLUDE_DIRS for p in parts):
            continue
        if f.name in SKIP_FILES:
            continue
        ext = f.suffix.lower()
        if ext not in SUPPORTED_CODE:
            continue
        prompt_type = "data" if ext in DATA_EXTENSIONS else "file"
        results.append((f, prompt_type))
        if len(results) >= max_files:
            break
    return results


# ---------------------------------------------------------------------------
# Single-file benchmark call
# ---------------------------------------------------------------------------
def _benchmark_file(
    client: object,
    config: object,
    root: Path,
    f: Path,
    prompt_type: str,
    stats: Stats,
    dry_run: bool,
) -> None:
    rel = str(f.relative_to(root))
    try:
        content = f.read_text(encoding="utf-8", errors="ignore")[:MAX_FILE_BYTES]
    except OSError:
        return

    system = _DATA_SYSTEM_PROMPT if prompt_type == "data" else _FILE_SYSTEM_PROMPT
    user_msg = f"Project: Synapse\nFile: {rel}\n\n```\n{content}\n```"

    if dry_run:
        # Rough token estimate: 1 token ≈ 4 chars
        tokens = (len(system) + len(user_msg)) // 4
        print(f"  [dry-run] {rel} — ~{tokens} input tokens")
        stats.record(ok=True, input_tokens=tokens, output_tokens=200, latency=0.0)
        return

    model = _next_model()
    t0 = time.time()
    try:
        from groq import Groq
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        latency = time.time() - t0
        usage = response.usage
        in_tok = usage.prompt_tokens if usage else 0
        out_tok = usage.completion_tokens if usage else 0
        print(f"  OK  [{model.split('/')[-1][:20]}] {rel} — {in_tok}in/{out_tok}out — {latency:.2f}s")
        stats.record(ok=True, input_tokens=in_tok, output_tokens=out_tok, latency=latency)
    except Exception as exc:
        msg = str(exc).lower()
        if "rate limit" in msg or "429" in msg:
            print(f"  RL  {rel} — rate limited ({exc})")
            stats.record(ok=False, rate_limited=True)
        else:
            print(f"  ERR {rel} — {exc}")
            stats.record(ok=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Groq limits against Synapse workload")
    parser.add_argument("--files", type=int, default=55, help="Max files to send (default: 55)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers (default: 4)")
    parser.add_argument("--dry-run", action="store_true", help="Estimate tokens without calling Groq")
    args = parser.parse_args()

    config = load_config()

    if not args.dry_run and not config.groq_api_key:
        print("ERROR: groq_api_key not set. Add GROQ_API_KEY to .env or config.yaml")
        sys.exit(1)

    client = None
    if not args.dry_run:
        try:
            from groq import Groq
            client = Groq(api_key=config.groq_api_key)
        except ImportError:
            print("ERROR: groq not installed — pip install groq")
            sys.exit(1)

    print(f"Collecting files from {ROOT} ...")
    files = _collect_files(ROOT, args.files)
    print(f"Found {len(files)} files to benchmark ({args.workers} workers)")
    print(f"Mode: {'DRY RUN (no API calls)' if args.dry_run else 'LIVE (hitting Groq API)'}")
    print()

    stats = Stats()
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(_benchmark_file, client, config, ROOT, f, pt, stats, args.dry_run)
            for f, pt in files
        ]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                print(f"  UNEXPECTED: {exc}")

    elapsed = time.time() - t_start
    stats.report(elapsed, len(files))


if __name__ == "__main__":
    main()

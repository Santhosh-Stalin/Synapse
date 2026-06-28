"""
Cerebras rate-limit benchmark — simulates memory_scan_project workload.

Sends real Synapse source files through the same prompts the scanner uses,
using the same cerebras_client.py logic (qwen-3-235b primary, gpt-oss-120b fallback).

Usage:
    python Diagnostics/benchmark_cerebras_limits.py [--files N] [--workers N] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.scanner import SKIP_DIRS, SKIP_FILES, _FILE_SYSTEM_PROMPT, _DATA_SYSTEM_PROMPT, DATA_EXTENSIONS

# ---------------------------------------------------------------------------
# Cerebras free tier limits (published)
# ---------------------------------------------------------------------------
MODELS = [
    {
        "id": "zai-glm-4.7",
        "rpm": 30,
        "tpm": 60_000,
        "tph": 1_000_000,
        "note": "primary",
    },
    {
        "id": "gpt-oss-120b",
        "rpm": 30,
        "tpm": 60_000,
        "tph": 1_000_000,
        "note": "fallback — fast",
    },
]

SUPPORTED_CODE = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".cs", ".rb", ".php",
    ".sh", ".md", ".yaml", ".yml", ".toml", ".sql",
}
EXCLUDE_DIRS = {"vault", ".backups", "groq_blacklist_output", "installer", "pipeline"}
MAX_FILE_BYTES = 12_000


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.success = 0
        self.rate_limited = 0
        self.timeouts = 0
        self.fallbacks = 0
        self.errors = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.latencies: list[float] = []

    def record(self, *, ok: bool, rate_limited: bool = False, timeout: bool = False,
               fallback: bool = False, input_tokens: int = 0, output_tokens: int = 0,
               latency: float = 0.0) -> None:
        with self._lock:
            self.requests += 1
            if rate_limited:
                self.rate_limited += 1
            elif timeout:
                self.timeouts += 1
            elif ok:
                self.success += 1
                if fallback:
                    self.fallbacks += 1
                self.total_input_tokens += input_tokens
                self.total_output_tokens += output_tokens
                self.latencies.append(latency)
            else:
                self.errors += 1

    def report(self, elapsed: float, total_files: int) -> None:
        print("\n" + "=" * 60)
        print("CEREBRAS RATE-LIMIT BENCHMARK RESULTS")
        print("=" * 60)
        print(f"  Files targeted:      {total_files}")
        print(f"  Requests sent:       {self.requests}")
        print(f"  Successful:          {self.success}")
        print(f"    of which fallback: {self.fallbacks} (used gpt-oss-120b)")
        print(f"  Rate-limited:        {self.rate_limited}")
        print(f"  Timeouts:            {self.timeouts}")
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
        print("CEREBRAS FREE-TIER LIMITS (published)")
        for m in MODELS:
            print(f"  {m['id']} ({m['note']})")
            print(f"    RPM: {m['rpm']}  TPM: {m['tpm']:,}  TPH: {m['tph']:,}")
        print()

        if self.success:
            total_tokens = self.total_input_tokens + self.total_output_tokens
            tpm_used = total_tokens / (elapsed / 60)
            avg_tokens = total_tokens / self.success
            time_per_file = elapsed / self.success
            print("THROUGHPUT ANALYSIS")
            print(f"  Tokens/min used:     {tpm_used:,.0f}  (limit: 60,000 TPM)")
            print(f"  TPM headroom:        {60_000 - tpm_used:,.0f}")
            print(f"  Tokens used today:   {total_tokens:,}  (hourly limit: 1,000,000)")
            print(f"  Avg tokens/file:     {avg_tokens:,.0f}")
            print(f"  Avg time/file:       {time_per_file:.2f}s")
            print()
            for n in [10, 55, 200, 500]:
                est = n * time_per_file
                mins, secs = divmod(est, 60)
                est_tokens = n * avg_tokens
                print(f"  Est. for {n:>3} files:  {int(mins)}m {secs:.0f}s  (~{est_tokens:,.0f} tokens)")

        print()
        if self.rate_limited > 0:
            print(f"  !! Rate limited {self.rate_limited}x — reduce --workers")
        elif self.timeouts > 0:
            print(f"  !! {self.timeouts} timeouts — model may be under load")
        elif self.success == total_files:
            print("  OK — completed all files, no rate limits")
        print("=" * 60)


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------
def _collect_files(root: Path, max_files: int) -> list[tuple[Path, str]]:
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
# Single file call
# ---------------------------------------------------------------------------
def _benchmark_file(
    client: object,
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
        tokens = (len(system) + len(user_msg)) // 4
        print(f"  [dry-run] {rel} — ~{tokens} input tokens")
        stats.record(ok=True, input_tokens=tokens, output_tokens=200, latency=0.0)
        return

    t0 = time.time()
    used_fallback = False
    try:
        from server.cerebras_client import _MODEL_PRIMARY, _MODEL_FAST, _MAX_RETRIES, _QUEUE_RETRY_DELAY, _REQUEST_TIMEOUT

        model = _MODEL_PRIMARY
        response = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.2,
                    max_tokens=1024,
                    timeout=_REQUEST_TIMEOUT,
                )
                break
            except Exception as exc:
                msg = str(exc).lower()
                if "timed out" in msg or "timeout" in msg:
                    raise RuntimeError(f"Timeout after {_REQUEST_TIMEOUT:.0f}s") from exc
                if "queue" in msg or "429" in msg or "too_many_requests" in msg:
                    if model == _MODEL_PRIMARY:
                        model = _MODEL_FAST
                        used_fallback = True
                        print(f"  FB  {rel} — queue full, switching to fast model", flush=True)
                    else:
                        wait = _QUEUE_RETRY_DELAY * (attempt + 1)
                        print(f"  RL  {rel} — queue full, retrying in {wait:.0f}s", flush=True)
                        time.sleep(wait)
                else:
                    raise

        latency = time.time() - t0
        if response is None:
            stats.record(ok=False, rate_limited=True)
            return

        usage = response.usage
        in_tok = usage.prompt_tokens if usage else 0
        out_tok = usage.completion_tokens if usage else 0
        label = "FB " if used_fallback else "OK "
        print(f"  {label} [{model.split('/')[-1][:22]}] {rel} — {in_tok}in/{out_tok}out — {latency:.2f}s")
        stats.record(ok=True, fallback=used_fallback, input_tokens=in_tok,
                     output_tokens=out_tok, latency=latency)

    except RuntimeError as exc:
        if "timeout" in str(exc).lower():
            print(f"  TO  {rel} — {exc}")
            stats.record(ok=False, timeout=True)
        else:
            print(f"  ERR {rel} — {exc}")
            stats.record(ok=False)
    except Exception as exc:
        msg = str(exc).lower()
        if "rate limit" in msg or "429" in msg:
            print(f"  RL  {rel} — rate limited: {exc}")
            stats.record(ok=False, rate_limited=True)
        else:
            print(f"  ERR {rel} — {exc}")
            stats.record(ok=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Cerebras limits against Synapse workload")
    parser.add_argument("--files", type=int, default=53)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()

    if not args.dry_run and not config.cerebras_api_key:
        print("ERROR: cerebras_api_key not set. Add CEREBRAS_API_KEY to .env or config.yaml")
        sys.exit(1)

    client = None
    if not args.dry_run:
        try:
            from cerebras.cloud.sdk import Cerebras
            client = Cerebras(api_key=config.cerebras_api_key)
        except ImportError:
            print("ERROR: cerebras-cloud-sdk not installed — pip install cerebras-cloud-sdk")
            sys.exit(1)

    print(f"Collecting files from {ROOT} ...")
    files = _collect_files(ROOT, args.files)
    print(f"Found {len(files)} files ({args.workers} workers)")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE (hitting Cerebras API)'}")
    print(f"Models: zai-glm-4.7 (primary) -> gpt-oss-120b (fallback)")
    print()

    stats = Stats()
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(_benchmark_file, client, ROOT, f, pt, stats, args.dry_run)
            for f, pt in files
        ]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                print(f"  UNEXPECTED: {exc}")

    stats.report(time.time() - t_start, len(files))


if __name__ == "__main__":
    main()

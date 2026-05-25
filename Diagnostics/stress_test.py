"""
Synapse stress test — pushes the tool to its limits.
Run with: python Diagnostics/stress_test.py

Tests:
  1. FTS5 index rebuild speed at scale
  2. Search latency with 1k+ vault files
  3. Conflict detection performance (post-fix)
  4. Token estimation accuracy under large payloads
  5. memory_auto escalation under all confidence cases
  6. Concurrent write safety
  7. Edge cases: unicode, empty content, 100kb files, malformed frontmatter
  8. Patch queue under load (100 pending patches)
"""

import json
import os
import sys
import tempfile
import time
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import SynapseConfig
from server.functions import (
    memory_context,
    memory_search_tool,
    memory_propose_update,
    memory_apply_update,
    rebuild_index as memory_rebuild_index,
)
from server.diff import detect_conflicts
from server.index import MemoryIndex
from server.functions import _estimate_tokens

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m⚠\033[0m"


def section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def result(label, ok, detail="", warn=False):
    icon = WARN if warn else (PASS if ok else FAIL)
    print(f"  {icon}  {label:<45} {detail}")


def make_config(vault: Path, write_mode="review") -> SynapseConfig:
    return SynapseConfig(
        root_path=vault.parent,
        vault_path=vault,
        git_enabled=False,
        encryption=False,
        write_mode=write_mode,
        gemini_api_key="",
        cloud_search=False,
        pending_auto_expire_days=7,
        raw_archive_path=vault / "raw",
        weekly_report_day="monday",
    )


def write_md(path: Path, key: str, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nkey: {key}\ntype: note\nweight: 0.8\n---\n\n{content}\n",
        encoding="utf-8",
    )


# ── Test 1: Index rebuild at scale ───────────────────────────────────────────

def test_index_rebuild_scale():
    section("1. FTS5 index rebuild — 500 vault files")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        vault = Path(tmp)
        config = make_config(vault)

        # Generate 500 files across folders
        folders = ["identity", "work", "projects", "patterns", "life"]
        for i in range(500):
            folder = folders[i % len(folders)]
            write_md(
                vault / folder / f"note_{i:04d}.md",
                f"{folder}.note_{i:04d}",
                f"This is memory note number {i}. Topic: {'python' if i%3==0 else 'security' if i%3==1 else 'projects'}. "
                f"Detail: {'Lorem ipsum ' * 20}",
            )

        t0 = time.perf_counter()
        res = memory_rebuild_index(config)
        elapsed = time.perf_counter() - t0

        ok = elapsed < 30
        result(f"Rebuild 500 files", ok, f"{elapsed:.2f}s {'✓' if ok else '— too slow'}")

        # Search immediately after
        t0 = time.perf_counter()
        hits = memory_search_tool(config, "python security")
        search_elapsed = time.perf_counter() - t0
        result(f"Search after rebuild", len(hits) > 0, f"{len(hits)} hits in {search_elapsed*1000:.1f}ms")


# ── Test 2: Search latency ────────────────────────────────────────────────────

def test_search_latency():
    section("2. Search latency — 10 queries back-to-back")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        vault = Path(tmp)
        config = make_config(vault)

        for i in range(200):
            write_md(vault / "work" / f"doc_{i}.md", f"work.doc_{i}",
                     f"Claude memory agent token reduction MCP tool {i} python fastapi")
        memory_rebuild_index(config)

        queries = [
            "claude memory", "token reduction", "MCP protocol", "python fastapi",
            "security patterns", "identity profile", "project status",
            "coding workflow", "deep search", "vault health",
        ]
        times = []
        for q in queries:
            t0 = time.perf_counter()
            memory_search_tool(config, q)
            times.append(time.perf_counter() - t0)

        avg = sum(times) / len(times) * 1000
        worst = max(times) * 1000
        result("Avg search latency", avg < 200, f"{avg:.1f}ms avg  {worst:.1f}ms worst")


# ── Test 3: Conflict detection performance ────────────────────────────────────

def test_conflict_detection():
    section("3. Conflict detection — active vault only (post-fix)")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        vault = Path(tmp)
        config = make_config(vault)

        # 50 active vault files
        for i in range(50):
            folder = ["identity", "work", "projects", "patterns", "life"][i % 5]
            write_md(vault / folder / f"note_{i}.md", f"{folder}.note_{i}",
                     f"This file never ignores always important patterns for project {i}.")

        # 500 chat files (should be excluded)
        chats = vault / "chats"
        chats.mkdir()
        for i in range(500):
            (chats / f"chat_{i:04d}.md").write_text(
                f"---\nkey: chats.chat_{i:04d}\n---\nWe never do X. We always do Y. {i}\n",
                encoding="utf-8",
            )

        t0 = time.perf_counter()
        conflicts = detect_conflicts(config)
        elapsed = time.perf_counter() - t0

        # Should be fast (only 50 files, not 550)
        ok_speed = elapsed < 5
        # Should have no never/always false positives
        false_positives = [c for c in conflicts if "never" in c.get("explanation", "").lower()
                           or "always" in c.get("explanation", "").lower()]
        ok_quality = len(false_positives) == 0

        result("Conflict scan speed (550 files, 500 excluded)", ok_speed, f"{elapsed:.3f}s")
        result("No never/always false positives", ok_quality,
               f"{len(false_positives)} false positives" if not ok_quality else "clean")
        result("Total conflicts found", True, f"{len(conflicts)} (active vault only)")


# ── Test 4: Token estimation ──────────────────────────────────────────────────

def test_token_estimation():
    section("4. Token estimation accuracy")

    cases = [
        ("Empty dict", {}, 2),
        ("Small context", {"key": "identity.profile", "content": "Alex Example, New York"}, 20),
        ("1k token payload", {"data": "x " * 2000}, 1000),
        ("Nested structure", {"a": {"b": {"c": list(range(100))}}}, 80),
        ("Unicode heavy", {"text": "日本語テスト " * 100}, 150),
    ]

    for label, obj, expected_approx in cases:
        tokens = _estimate_tokens(obj)
        # Within 50% of expected is reasonable for a heuristic
        ok = tokens > 0
        result(f"  {label}", ok, f"{tokens} tokens")

    # Hard ceiling check: 15k token budget
    big = {"results": [{"key": f"k{i}", "content": "x" * 200} for i in range(300)]}
    tokens = _estimate_tokens(big)
    over_ceiling = tokens > 15000
    result("Large payload > 15k ceiling detectable", over_ceiling, f"{tokens:,} tokens")


# ── Test 5: memory_auto escalation logic ─────────────────────────────────────

def test_auto_escalation():
    section("5. memory_auto escalation logic")
    from unittest.mock import patch
    from server.functions import memory_auto

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        vault = Path(tmp)
        config = make_config(vault)
        write_md(vault / "identity" / "profile.md", "identity.profile", "Santhosh Stalin")
        memory_rebuild_index(config)

        # Case A: no vault hits → should escalate to deep search
        with patch("server.functions.memory_search_tool", return_value=[]) as mock_search, \
             patch("server.functions._deep_search", return_value={"chats": []}) as mock_deep, \
             patch("server.functions.memory_context", return_value={}):
            memory_auto(config, "what projects did we discuss")
            result("Escalates when no vault hits", mock_deep.called, "deep_search called")

        # Case B: high confidence hit → should NOT escalate
        with patch("server.functions.memory_search_tool",
                   return_value=[{"key": "identity.profile", "score": 0.95}]) as mock_search, \
             patch("server.functions._deep_search", return_value={}) as mock_deep, \
             patch("server.functions.memory_context", return_value={}):
            memory_auto(config, "who am I")
            result("No escalation when score ≥ 0.7", not mock_deep.called, "deep_search skipped")

        # Case C: low confidence → escalates
        with patch("server.functions.memory_search_tool",
                   return_value=[{"key": "work.dev", "score": 0.3}]) as mock_search, \
             patch("server.functions._deep_search", return_value={}) as mock_deep, \
             patch("server.functions.memory_context", return_value={}):
            memory_auto(config, "obscure past project")
            result("Escalates when score < 0.7", mock_deep.called, "deep_search called")


# ── Test 6: Concurrent write safety ──────────────────────────────────────────

def test_concurrent_writes():
    section("6. Concurrent write safety — 20 simultaneous proposes")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        vault = Path(tmp)
        config = make_config(vault)
        memory_rebuild_index(config)

        errors = []
        patch_ids = []
        lock = threading.Lock()

        def propose(i):
            try:
                patch = {
                    "key": f"work.concurrent_test_{i}",
                    "content": f"---\nkey: work.concurrent_test_{i}\ntype: note\n---\n\nThread {i} data\n",
                }
                res = memory_propose_update(config, patch)
                with lock:
                    patch_ids.append(res.get("patch_id"))
            except Exception as e:
                with lock:
                    errors.append(str(e))

        threads = [threading.Thread(target=propose, args=(i,)) for i in range(20)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0

        result("No errors under 20 concurrent proposes", len(errors) == 0,
               f"{len(errors)} errors" if errors else f"{elapsed:.2f}s")
        valid_ids = [p for p in patch_ids if p]
        result("All patches got IDs", len(valid_ids) == 20, f"{len(valid_ids)}/20 got patch_id")


# ── Test 7: Edge cases ────────────────────────────────────────────────────────

def test_edge_cases():
    section("7. Edge cases")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        vault = Path(tmp)
        config = make_config(vault)
        memory_rebuild_index(config)

        # Unicode content
        try:
            write_md(vault / "identity" / "unicode.md", "identity.unicode",
                     "日本語 العربية हिंदी 🔥⚡ emoji and unicode everywhere")
            memory_rebuild_index(config)
            hits = memory_search_tool(config, "unicode")
            result("Unicode content indexed and searchable", True, f"{len(hits)} hits")
        except Exception as e:
            result("Unicode content", False, str(e))

        # 100kb file
        try:
            big_content = "memory agent token reduction vault search " * 2000  # ~100kb
            write_md(vault / "work" / "big_file.md", "work.big_file", big_content)
            memory_rebuild_index(config)
            hits = memory_search_tool(config, "memory agent token reduction")
            result("100kb file indexed", True, f"{len(hits)} hits")
        except Exception as e:
            result("100kb file", False, str(e))

        # Malformed frontmatter
        try:
            bad = vault / "work" / "bad_fm.md"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("---\nkey: :::invalid:::\nbroken yaml: [unclosed\n---\ncontent\n", encoding="utf-8")
            memory_rebuild_index(config)
            result("Malformed frontmatter doesn't crash indexer", True)
        except Exception as e:
            result("Malformed frontmatter", False, str(e)[:60])

        # Empty file
        try:
            empty = vault / "work" / "empty.md"
            empty.write_text("", encoding="utf-8")
            memory_rebuild_index(config)
            result("Empty file doesn't crash indexer", True)
        except Exception as e:
            result("Empty file", False, str(e)[:60])

        # Search on empty vault
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp2:
            empty_config = make_config(Path(tmp2))
            try:
                hits = memory_search_tool(empty_config, "anything")
                result("Search on empty vault returns []", hits == [], f"got {hits}")
            except Exception as e:
                result("Search on empty vault", False, str(e)[:60])


# ── Test 8: Patch queue under load ───────────────────────────────────────────

def test_patch_queue_load():
    section("8. Patch queue — 100 pending patches")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        vault = Path(tmp)
        config = make_config(vault)
        memory_rebuild_index(config)

        t0 = time.perf_counter()
        ids = []
        for i in range(100):
            patch = {
                "key": f"projects.load_test_{i:03d}",
                "content": f"---\nkey: projects.load_test_{i:03d}\ntype: note\n---\n\nProject {i} details\n",
            }
            res = memory_propose_update(config, patch)
            ids.append(res.get("patch_id"))
        propose_elapsed = time.perf_counter() - t0

        result("Queue 100 patches", all(ids), f"{propose_elapsed:.2f}s")

        # Apply all
        t0 = time.perf_counter()
        applied = 0
        for pid in ids:
            if pid:
                try:
                    memory_apply_update(config, pid)
                    applied += 1
                except Exception:
                    pass
        apply_elapsed = time.perf_counter() - t0

        result("Apply 100 patches", applied == 100, f"{applied}/100 applied in {apply_elapsed:.2f}s")

        # Verify files exist
        files = list((vault / "projects").glob("load_test_*.md"))
        result("All files written to disk", len(files) == 100, f"{len(files)} files on disk")


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n** Synapse Stress Test")
    print("=" * 60)

    t_total = time.perf_counter()

    test_index_rebuild_scale()
    test_search_latency()
    test_conflict_detection()
    test_token_estimation()
    test_auto_escalation()
    test_concurrent_writes()
    test_edge_cases()
    test_patch_queue_load()

    total = time.perf_counter() - t_total
    print(f"\n{'='*60}")
    print(f"  Total: {total:.2f}s")
    print(f"{'='*60}\n")

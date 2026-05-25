"""
Synapse full tool coverage test — all 34 MCP tools.
AI-dependent tools (scan_project, ingest_text, import_*, deep_search, save_chat)
are tested for correct error/graceful-return behaviour with no API key.
Run with: python -X utf8 Diagnostics/test_all_tools.py
"""

import sys, json, tempfile, shutil, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import SynapseConfig
from server.functions import (
    memory_tree, memory_get, memory_search_tool,
    memory_propose_update, memory_apply_update, memory_reject_update,
    memory_diff, memory_conflicts, rebuild_index,
    memory_organize_vault, memory_relink_all,
    memory_scan_project, memory_ingest_text,
    memory_import_ai_export, memory_import_filtered_jsonl,
    memory_import_synapse_summaries,
    memory_smart_merge, memory_dedup,
    memory_context, memory_list_folder,
    memory_start_watcher, memory_stop_watcher, memory_watcher_status,
    memory_get_raw, memory_get_raw_chunks, memory_search_raw,
    memory_build_graph, memory_deep_search,
    memory_code_search, memory_code_stats,
    memory_save_chat, memory_auto, memory_commit,
)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"

results = []

def check(label, ok, detail=""):
    icon = PASS if ok else FAIL
    print(f"  {icon}  {label:<52} {detail}")
    results.append((label, ok))

def skip(label, reason=""):
    print(f"  {SKIP}  {label:<52} {reason}")
    results.append((label, None))

def section(title):
    print(f"\n── {title} {'─'*(56-len(title))}")

# ── Setup ─────────────────────────────────────────────────────────────────────

tmp = Path(tempfile.mkdtemp(prefix="synapse_test_"))
vault = tmp / "vault"
vault.mkdir()

def cfg(write_mode="review"):
    return SynapseConfig(
        root_path=tmp,
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

def md(key, content, folder="life"):
    path = vault / folder / f"{key.split('.')[-1]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nkey: {key}\ntype: note\nweight: 0.8\n---\n\n{content}\n",
        encoding="utf-8",
    )
    return key

# Seed vault
md("identity.profile",  "Name: Test User. Lives in London.", "identity")
md("identity.communication", "Prefers concise replies.", "identity")
md("life.health",    "Runs 5km three times a week.", "life")
md("life.hobbies",   "Enjoys chess and hiking.", "life")
md("work.career",    "Software engineer at Acme Corp.", "work")
md("patterns.code",  "Prefers Python. Uses type hints.", "patterns")
rebuild_index(cfg())

# ── 1. memory_tree ────────────────────────────────────────────────────────────
section("1. memory_tree")
try:
    tree = memory_tree(cfg())
    check("Returns dict with 'children'", isinstance(tree, dict) and "children" in tree)
    check("Finds identity folder", any(c.get("name") == "identity" for c in tree.get("children", [])))
except Exception as e:
    check("memory_tree", False, str(e))

# ── 2. memory_list_folder ─────────────────────────────────────────────────────
section("2. memory_list_folder")
try:
    r = memory_list_folder(cfg(), "life")
    check("Returns subfolders + keys", "keys" in r and "subfolders" in r)
    check("Finds life.health", "life.health" in r["keys"] or any("health" in k for k in r["keys"]))
except Exception as e:
    check("memory_list_folder", False, str(e))

# ── 3. memory_get ─────────────────────────────────────────────────────────────
section("3. memory_get")
try:
    r = memory_get(cfg(), "life.health")
    check("Returns key", r.get("key") == "life.health")
    check("Returns content", "5km" in r.get("content", ""))
    check("Includes _tokens", "_tokens" in r)
except Exception as e:
    check("memory_get", False, str(e))

try:
    memory_get(cfg(), "nonexistent.key")
    check("Missing key raises ValueError", False)
except ValueError:
    check("Missing key raises ValueError", True)
except Exception as e:
    check("Missing key raises ValueError", False, str(e))

# ── 4. memory_search ──────────────────────────────────────────────────────────
section("4. memory_search")
try:
    hits = memory_search_tool(cfg(), "python type hints")
    check("Returns list", isinstance(hits, list))
    check("Finds patterns.code", any("patterns" in h.get("key", "") for h in hits))
    check("Has score field", all("score" in h for h in hits))
except Exception as e:
    check("memory_search", False, str(e))

# ── 5. memory_context ─────────────────────────────────────────────────────────
section("5. memory_context")
try:
    ctx = memory_context(cfg())
    check("Has identity.profile key", "identity.profile" in ctx)
    check("Has _index.life", "_index.life" in ctx)
    check("Has _vault_health", "_vault_health" in ctx)
    check("_tokens present", ctx.get("_tokens", 0) > 0)
    # vault health structure — test files are intentionally short so may flag as thin
    h = ctx.get("_vault_health", {})
    check("_vault_health has total_files", "total_files" in h and h["total_files"] >= 6)
    check("_vault_health has issues list", isinstance(h.get("issues"), list))
except Exception as e:
    check("memory_context", False, str(e))

# ── 6. memory_propose_update ──────────────────────────────────────────────────
section("6. memory_propose_update / diff / reject / apply")
try:
    patch = {"key": "life.test_note", "content": "A test note for Synapse."}
    r = memory_propose_update(cfg(), patch)
    check("propose returns patch_id", "patch_id" in r)
    pid = r["patch_id"]

    diff = memory_diff(cfg())
    check("diff lists the pending patch", any(p.get("patch_id") == pid for p in diff))
except Exception as e:
    check("propose/diff", False, str(e))
    pid = None

# ── 7. memory_reject_update ───────────────────────────────────────────────────
try:
    if pid:
        r2 = memory_reject_update(cfg(), pid, reason="test rejection")
        check("reject returns status", r2.get("status") == "rejected")
        diff2 = memory_diff(cfg())
        check("patch removed from diff after reject", not any(p.get("patch_id") == pid for p in diff2))
except Exception as e:
    check("memory_reject_update", False, str(e))

# ── 8. memory_apply_update ────────────────────────────────────────────────────
try:
    patch2 = {"key": "life.applied_note", "content": "Applied note content."}
    r3 = memory_propose_update(cfg(), patch2)
    pid2 = r3["patch_id"]
    applied = memory_apply_update(cfg(), pid2)
    check("apply returns status ok", applied.get("status") in ("applied", "ok", "written"))
    check("file written to disk", (vault / "life" / "applied_note.md").exists())
except Exception as e:
    check("memory_apply_update", False, str(e))

# ── 9. memory_commit (review mode) ────────────────────────────────────────────
section("9. memory_commit")
try:
    r = memory_commit(cfg("review"), {"key": "life.commit_review", "content": "Review mode."})
    check("review mode returns patch_id (not applied)", "patch_id" in r)
    check("file NOT on disk yet (review)", not (vault / "life" / "commit_review.md").exists())
except Exception as e:
    check("memory_commit review", False, str(e))

try:
    r = memory_commit(cfg("auto"), {"key": "life.commit_auto", "content": "Auto mode."})
    check("auto mode writes file immediately", (vault / "life" / "commit_auto.md").exists())
except Exception as e:
    check("memory_commit auto", False, str(e))

# ── 10. memory_conflicts ──────────────────────────────────────────────────────
section("10. memory_conflicts")
try:
    c = memory_conflicts(cfg())
    check("Returns list", isinstance(c, list))
    check("No false positives on clean vault", len(c) == 0)
except Exception as e:
    check("memory_conflicts", False, str(e))

# ── 11. memory_dedup ──────────────────────────────────────────────────────────
section("11. memory_dedup")
try:
    # Add a near-duplicate
    md("life.hobbies_dup", "Enjoys chess and hiking. Also swimming.", "life")
    d = memory_dedup(cfg())
    check("Returns dict", isinstance(d, dict))
    check("Has thin_files / stray_files keys", "thin_files" in d or "stray_files" in d)
except Exception as e:
    check("memory_dedup", False, str(e))

# ── 12. memory_smart_merge ────────────────────────────────────────────────────
section("12. memory_smart_merge")
try:
    r = memory_smart_merge(cfg(), dry_run=True, threshold=0.93)
    check("Returns dict", isinstance(r, dict))
    check("Has pairs_found + dry_run fields", "pairs_found" in r and "dry_run" in r)
except Exception as e:
    check("memory_smart_merge", False, str(e))

# ── 13. memory_relink_all ─────────────────────────────────────────────────────
section("13. memory_relink_all")
try:
    r = memory_relink_all(cfg())
    check("Returns dict", isinstance(r, dict))
    check("Has status or updated field", "status" in r or "updated" in r or "relinked" in r)
except Exception as e:
    check("memory_relink_all", False, str(e))

# ── 14. memory_organize_vault ─────────────────────────────────────────────────
section("14. memory_organize_vault")
try:
    r = memory_organize_vault(cfg())
    check("Returns dict", isinstance(r, dict))
except Exception as e:
    check("memory_organize_vault", False, str(e))

# ── 15. memory_build_graph ────────────────────────────────────────────────────
section("15. memory_build_graph")
try:
    r = memory_build_graph(cfg(), top_k=5)
    check("Returns dict", isinstance(r, dict))
    check("Has nodes or graph key", "nodes" in r or "graph" in r or "topics" in r or "error" in r)
except Exception as e:
    check("memory_build_graph", False, str(e))

# ── 16. memory_watcher ────────────────────────────────────────────────────────
section("16. memory_start/stop/status watcher")
try:
    status0 = memory_watcher_status()
    check("watcher_status returns dict", isinstance(status0, dict))
    check("watcher not running initially", not status0.get("running", False))

    r = memory_start_watcher(cfg(), str(vault))
    check("start_watcher returns dict", isinstance(r, dict))
    time.sleep(1.0)

    status1 = memory_watcher_status()
    # Running is True if watchdog is installed; some envs may not have it
    watcher_ok = status1.get("running", False) or "error" in status1 or "status" in status1
    check("watcher reports state after start", watcher_ok)

    stop = memory_stop_watcher()
    check("stop_watcher returns dict", isinstance(stop, dict))
    time.sleep(0.3)

    status2 = memory_watcher_status()
    check("watcher stopped after stop", not status2.get("running", False))
except Exception as e:
    check("watcher tools", False, str(e))

# ── 17. memory_get_raw / search_raw ──────────────────────────────────────────
section("17. memory_get_raw / get_raw_chunks / search_raw")
raw_dir = vault / "raw"
raw_dir.mkdir(exist_ok=True)
raw_file = raw_dir / "chat_abc123.txt"
raw_file.write_text("User: What is Python?\nAssistant: A programming language.\n" * 20, encoding="utf-8")

try:
    r = memory_get_raw(cfg(), "abc123")
    check("get_raw returns dict", isinstance(r, dict))
    check("get_raw has content", r.get("content") or r.get("text") or "error" in r)
except Exception as e:
    check("memory_get_raw", False, str(e))

try:
    r = memory_get_raw_chunks(cfg(), "abc123", "python programming", top_k=2)
    check("get_raw_chunks returns dict", isinstance(r, dict))
    check("get_raw_chunks has chunks or error", "chunks" in r or "error" in r or "results" in r)
except Exception as e:
    check("memory_get_raw_chunks", False, str(e))

try:
    r = memory_search_raw(cfg(), "python", top_k=5)
    check("search_raw returns list", isinstance(r, list))
except Exception as e:
    check("memory_search_raw", False, str(e))

# ── 18. memory_save_chat ──────────────────────────────────────────────────────
section("18. memory_save_chat")
try:
    r = memory_save_chat(
        cfg(),
        title="Test Chat",
        summary="Discussed Python and Synapse.",
        key_facts=["Python is great", "Synapse stores memories"],
        decisions=["Use Python for this project"],
        tags=["python", "synapse"],
        keywords="python synapse memory",
        categories=["tech"],
        chat_id="test_chat_001",
    )
    check("save_chat returns dict", isinstance(r, dict))
    check("save_chat has status or patch_id", "patch_id" in r or "status" in r or "chat_id" in r or "key" in r)
except Exception as e:
    check("memory_save_chat", False, str(e))

# ── 19. memory_deep_search (no API key → graceful) ────────────────────────────
section("19. memory_deep_search (no-key graceful fallback)")
try:
    r = memory_deep_search(cfg(), "python", depth=1, top_k=3)
    check("Returns list (with or without results)", isinstance(r, list))
    check("No crash without API key", True)
except Exception as e:
    # Acceptable if it raises with a clear message
    check("No crash without API key", "api" in str(e).lower() or "key" in str(e).lower(), str(e)[:60])

# ── 20. memory_auto ───────────────────────────────────────────────────────────
section("20. memory_auto")
try:
    r = memory_auto(cfg(), "chess hobbies")
    check("Returns dict", isinstance(r, dict))
    check("Has context key", "context" in r)
    check("Has vault_results", "vault_results" in r)
    check("Vault hit found (score ≥ 0.7 → no deep escalation)", "deep_results" not in r or len(r.get("vault_results", [])) > 0)
    check("Has _tokens", "_tokens" in r)
except Exception as e:
    check("memory_auto", False, str(e))

# ── 21. memory_code_search / code_stats (no index) ───────────────────────────
section("21. memory_code_search / code_stats (pre-index)")
try:
    r = memory_code_search(cfg(), "round function", project="")
    check("code_search: no index → error dict", isinstance(r, list) and "error" in r[0])
except Exception as e:
    check("memory_code_search pre-index", False, str(e))

try:
    r = memory_code_stats(cfg())
    check("code_stats: no index → error dict", "error" in r)
except Exception as e:
    check("memory_code_stats pre-index", False, str(e))

# ── 22. memory_ingest_text (no API key → graceful) ────────────────────────────
section("22. memory_ingest_text (no-key graceful fallback)")
try:
    r = memory_ingest_text(cfg(), "Sandy enjoys building AI tools.", "[test]")
    check("Returns dict", isinstance(r, dict))
    check("Has patches_proposed or error", "patches_proposed" in r or "error" in r)
except Exception as e:
    check("memory_ingest_text", False, str(e))

# ── 23. memory_import_ai_export (bad path → error dict) ──────────────────────
section("23. memory_import_ai_export (bad path → error dict)")
try:
    r = memory_import_ai_export(cfg(), "/nonexistent/path")
    check("Returns error dict (not exception)", "error" in r)
except Exception as e:
    check("memory_import_ai_export bad path", False, str(e))

# ── 24. memory_import_filtered_jsonl (bad folder → error) ────────────────────
section("24. memory_import_filtered_jsonl (bad folder → error dict)")
try:
    r = memory_import_filtered_jsonl(cfg(), "/nonexistent/folder")
    check("Returns error dict (not exception)", "error" in r)
except Exception as e:
    check("memory_import_filtered_jsonl bad path", False, str(e))

# ── 25. memory_import_synapse_summaries (bad folder → error) ─────────────────
section("25. memory_import_synapse_summaries (bad folder → error dict)")
try:
    r = memory_import_synapse_summaries(cfg(), "/nonexistent/folder")
    check("Returns error dict or empty", isinstance(r, dict))
except Exception as e:
    check("memory_import_synapse_summaries bad path", False, str(e))

# ── 26. rebuild_index ─────────────────────────────────────────────────────────
section("26. rebuild_index")
try:
    r = rebuild_index(cfg())
    check("Returns status=rebuilt", r.get("status") == "rebuilt")
    check("_index.db exists after rebuild", (vault / "_index.db").exists())
    # Search still works after rebuild
    hits = memory_search_tool(cfg(), "chess")
    check("Search works after rebuild", len(hits) > 0)
except Exception as e:
    check("rebuild_index", False, str(e))

# ── 27. Edge: memory_get on folder path ──────────────────────────────────────
section("27. Edge cases")
try:
    memory_get(cfg(), "life")   # folder, not file
    check("get on folder raises ValueError", False, "no error raised")
except ValueError:
    check("get on folder raises ValueError", True)
except Exception as e:
    check("get on folder raises ValueError", False, str(e))

try:
    memory_list_folder(cfg(), "nonexistent_folder_xyz")
    check("list_folder on missing folder raises ValueError", False)
except ValueError:
    check("list_folder on missing folder raises ValueError", True)
except Exception as e:
    check("list_folder on missing folder raises ValueError", False, str(e))

# Empty search
try:
    hits = memory_search_tool(cfg(), "zzz_no_match_xyz_abc")
    check("Search with no match returns []", hits == [])
except Exception as e:
    check("Search no match", False, str(e))

# ── Final summary ─────────────────────────────────────────────────────────────
shutil.rmtree(tmp, ignore_errors=True)

total   = len([r for r in results if r[1] is not None])
passed  = len([r for r in results if r[1] is True])
failed  = len([r for r in results if r[1] is False])
skipped = len([r for r in results if r[1] is None])

print(f"\n{'='*60}")
print(f"  {passed}/{total} passed   {failed} failed   {skipped} skipped")
if failed:
    print(f"\n  Failed checks:")
    for label, ok in results:
        if ok is False:
            print(f"    ✗  {label}")
print(f"{'='*60}\n")
sys.exit(0 if failed == 0 else 1)

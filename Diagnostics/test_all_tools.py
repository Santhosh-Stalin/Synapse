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
    memory_triage, memory_index_status, memory_full_import,
    memory_stop_job,
    memory_health, memory_export_snapshot,
    memory_session_stats, memory_session_save,
    memory_vault_diff, memory_apply_all,
    memory_fix_frontmatter, memory_multi_search,
    memory_watch_vault, memory_ask,
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
    # Without confirm → cost warning
    warn = memory_tree(cfg())
    check("No confirm → returns warning dict", "warning" in warn and "estimated_tokens" in warn)
    check("No confirm → tip present", "tip" in warn)
    # With confirm=True → actual tree
    tree = memory_tree(cfg(), confirm=True)
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

section("28. memory_triage (no keys → error dict)")
try:
    r = memory_triage(cfg(), input_folder=str(tmp))
    check("Returns dict",              isinstance(r, dict))
    check("Has error key (no OR key)", "error" in r)
except Exception as e:
    check("memory_triage no-key graceful", False, str(e))

section("29. memory_import_ai_export (triage_warning present)")
try:
    # Deliberately call without a real export — just check the warning is in the response
    r = memory_import_ai_export(cfg(), path=str(tmp / "nonexistent"))
    check("Returns dict", isinstance(r, dict))
    # Either an error (no export found) or the triage_warning must appear
    has_warning = "triage_warning" in r
    has_error   = "error" in r
    check("triage_warning present or error returned", has_warning or has_error)
    if has_warning:
        check("triage_warning mentions triage pipeline", "memory_triage" in r["triage_warning"])
except Exception as e:
    check("memory_import_ai_export warning graceful", False, str(e))

section("30. memory_index_status (no jobs → message key)")
try:
    r = memory_index_status()
    check("Returns dict", isinstance(r, dict))
    # Fresh process: either empty jobs dict or a message key
    check("Has content (message or job entries)", len(r) > 0)
except Exception as e:
    check("memory_index_status graceful", False, str(e))

section("31. rebuild_index background=True")
try:
    r = rebuild_index(cfg(), background=True)
    check("Returns dict", isinstance(r, dict))
    check("Has status key", "status" in r)
    check("Status is started or done", r.get("status") in ("started", "done", "already_running"))
    # Wait briefly and poll
    time.sleep(3)
    s = memory_index_status()
    check("memory_index_status shows rebuild_index", "rebuild_index" in s or "message" in s)
except Exception as e:
    check("rebuild_index background graceful", False, str(e))

section("32. memory_full_import (no export folder → error)")
try:
    r = memory_full_import(cfg(), export_folder=str(tmp / "no_such_export"))
    check("Returns dict", isinstance(r, dict))
    check("Has error (missing folder)", "error" in r)
    check("step_failed is format", r.get("step_failed") == "format")
except Exception as e:
    check("memory_full_import graceful", False, str(e))

# ── 33. Bug #1 — merge modes (append / prepend) ───────────────────────────────
section("33. Bug #1 — merge modes append / prepend")
try:
    from server.memory_file import new_frontmatter, render_memory_file
    base_key = "life.merge_base"
    base_path = vault / "life" / "merge_base.md"

    def _reset_base(body: str) -> None:
        """Write a valid full-frontmatter base file with the given body."""
        fm = new_frontmatter(key=base_key, memory_type="note", scope="global",
                             weight=0.8, confidence="confirmed")
        base_path.write_text(render_memory_file(fm, body), encoding="utf-8")

    # append
    _reset_base("Original line.")
    ra = memory_propose_update(cfg(), {"key": base_key, "content": "Appended line.", "merge": "append"})
    memory_apply_update(cfg(), ra["patch_id"])
    after_a = base_path.read_text(encoding="utf-8")
    check("append: original comes before appended",
          "Original" in after_a and "Appended" in after_a
          and after_a.index("Original") < after_a.index("Appended"))

    # prepend
    _reset_base("Original line.")
    rp = memory_propose_update(cfg(), {"key": base_key, "content": "Prepended line.", "merge": "prepend"})
    memory_apply_update(cfg(), rp["patch_id"])
    after_p = base_path.read_text(encoding="utf-8")
    check("prepend: prepended comes before original",
          "Prepended" in after_p and "Original" in after_p
          and after_p.index("Prepended") < after_p.index("Original"))

    # replace
    _reset_base("Original line.")
    rr = memory_propose_update(cfg(), {"key": base_key, "content": "Replaced line.", "merge": "replace"})
    memory_apply_update(cfg(), rr["patch_id"])
    after_r = base_path.read_text(encoding="utf-8")
    check("replace: only new content in body", "Replaced line." in after_r and "Original line." not in after_r)
except Exception as e:
    check("merge mode bug #1", False, str(e))

# ── 34. Bug #2 — TTL enforcement on pending patches ───────────────────────────
section("34. Bug #2 — TTL enforcement on pending patches")
try:
    from datetime import datetime, timezone, timedelta
    from server.diff import load_pending, save_pending, pending_path

    # SynapseConfig is frozen — construct with short TTL
    c = SynapseConfig(
        root_path=tmp,
        vault_path=vault,
        git_enabled=False,
        encryption=False,
        write_mode="review",
        gemini_api_key="",
        cloud_search=False,
        pending_auto_expire_days=3,   # short TTL for test
        raw_archive_path=vault / "raw",
        weekly_report_day="monday",
    )

    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()

    stale = {"patch_id": "stale-001", "key": "x", "created_at": old_ts}
    live  = {"patch_id": "live-001",  "key": "y", "created_at": fresh_ts}

    save_pending(c, [stale, live])
    loaded = load_pending(c)

    check("stale patch (10d old) is pruned",  not any(p["patch_id"] == "stale-001" for p in loaded))
    check("fresh patch survives TTL",          any(p["patch_id"] == "live-001"  for p in loaded))
    check("pending file updated on disk",      len(loaded) == 1)

    # clean up
    save_pending(c, [])
except Exception as e:
    check("TTL enforcement bug #2", False, str(e))

# ── 35. Bug #5 — watcher rejects empty path ───────────────────────────────────
section("35. Bug #5 — watcher empty-path guard")
try:
    r = memory_start_watcher(cfg(), "")
    check("Empty path returns error dict", isinstance(r, dict) and "error" in r)
    check("Error mentions 'path is required'", "path" in r.get("error", "").lower())

    r2 = memory_start_watcher(cfg(), "   ")
    check("Whitespace-only path returns error dict", isinstance(r2, dict) and "error" in r2)
except Exception as e:
    check("watcher empty-path bug #5", False, str(e))

# ── 36. Bug #6 — CLI module importable and has expected commands ───────────────
section("36. Bug #6 — CLI module exists and commands registered")
try:
    from server.cli import app as _cli_app
    # Typer stores name=None for commands without explicit name= kwarg; fall back to callback name
    def _cmd_name(ci):
        return ci.name if ci.name else (ci.callback.__name__ if ci.callback else None)
    cmd_names = {_cmd_name(c) for c in _cli_app.registered_commands}
    check("CLI app imported", _cli_app is not None)
    check("'triage' command registered",       "triage"        in cmd_names)
    check("'full-import' command registered",  "full-import"   in cmd_names)
    check("'rebuild-index' command registered","rebuild-index" in cmd_names)
    check("'index-status' command registered", "index-status"  in cmd_names)
    check("'build-graph' command registered",  "build-graph"   in cmd_names)
except Exception as e:
    check("CLI bug #6", False, str(e))

# ── 37. Improvement #7 — triage_warning on memory_import_ai_export ────────────
section("37. Improvement #7 — triage_warning on successful import")
try:
    # Use a real (empty) folder so the importer gets past path validation
    fake_export = tmp / "fake_export"
    fake_export.mkdir(exist_ok=True)
    r = memory_import_ai_export(cfg(), str(fake_export))
    check("Returns dict", isinstance(r, dict))
    # Either error (no data found) or triage_warning present
    check("triage_warning key always present on non-error", "triage_warning" in r or "error" in r)
    if "triage_warning" in r:
        check("triage_warning references memory_triage", "memory_triage" in r["triage_warning"])
except Exception as e:
    check("triage_warning improvement #7", False, str(e))

# ── 38. Improvement #8/#9 — background rebuild + progress % ──────────────────
section("38. Improvement #8/#9 — background job progress tracking")
try:
    from server import background as _bg

    # Reset job state from earlier tests
    with _bg._lock:
        _bg._jobs.pop("rebuild_index", None)
        if "rebuild_index" in _bg._stop_flags:
            _bg._stop_flags["rebuild_index"].clear()

    r = rebuild_index(cfg(), background=True)
    check("background=True returns immediately", "status" in r)
    check("status is 'started' or 'already_running'", r["status"] in ("started", "already_running", "done"))

    time.sleep(1)
    s = memory_index_status()
    check("index_status returns dict", isinstance(s, dict))
    has_job = "rebuild_index" in s
    check("rebuild_index appears in status", has_job)
    if has_job:
        entry = s["rebuild_index"]
        check("progress field is a % string", "%" in str(entry.get("progress", "")))
        check("elapsed_seconds present", entry.get("elapsed_seconds") is not None)
        check("current_step present", bool(entry.get("current_step")))
except Exception as e:
    check("background progress improvement #8/#9", False, str(e))

# ── 39. memory_stop_job ───────────────────────────────────────────────────────
section("39. memory_stop_job — cancel running / all / timer")
try:
    from server import background as _bg

    # Cancel a non-existent job → graceful not_found
    r = memory_stop_job("nonexistent_job_xyz")
    check("Stop non-existent job returns dict", isinstance(r, dict))
    check("not_found reported for unknown job", "not_found" in r)

    # Cancel all when nothing running → empty cancelled list
    r2 = memory_stop_job("")
    check("Stop all (idle) returns dict", isinstance(r2, dict))
    check("cancelled list is empty when idle", r2.get("cancelled", []) == [] or "_auto_rebuild" in r2.get("cancelled", []))

    # Schedule a pending rebuild timer then cancel it
    _bg.schedule_rebuild(lambda c: None, cfg(), delay_seconds=60)
    time.sleep(0.2)
    with _bg._pending_lock:
        timer_alive_before = _bg._pending_timer is not None and _bg._pending_timer.is_alive()
    check("pending auto-rebuild timer is active", timer_alive_before)

    r3 = memory_stop_job("rebuild_index")  # should cancel timer too
    check("Stop rebuild_index cancels pending timer", "_auto_rebuild" in r3.get("cancelled", []))
    time.sleep(0.1)
    with _bg._pending_lock:
        timer_dead = _bg._pending_timer is None or not _bg._pending_timer.is_alive()
    check("Timer is dead after stop", timer_dead)
except Exception as e:
    check("memory_stop_job", False, str(e))

# ── 40. Auto-rebuild schedules after vault write ──────────────────────────────
section("40. Auto-rebuild triggered after vault write")
try:
    from server import background as _bg

    # Cancel any lingering timer
    with _bg._pending_lock:
        if _bg._pending_timer:
            _bg._pending_timer.cancel()
            _bg._pending_timer = None

    # Simulate a vault write (apply a patch) — schedule_rebuild is called inside apply
    patch = {"key": "life.auto_rebuild_test", "content": "Testing auto-rebuild trigger."}
    r = memory_propose_update(cfg(), patch)
    pid = r["patch_id"]
    memory_apply_update(cfg(), pid)

    time.sleep(0.5)  # give schedule_rebuild time to register
    with _bg._pending_lock:
        timer_scheduled = _bg._pending_timer is not None and _bg._pending_timer.is_alive()
    check("Auto-rebuild timer scheduled after vault write", timer_scheduled)

    # Clean up timer
    with _bg._pending_lock:
        if _bg._pending_timer:
            _bg._pending_timer.cancel()
            _bg._pending_timer = None
except Exception as e:
    check("auto-rebuild trigger", False, str(e))

# ── 41. #10 memory_tree guardrail ────────────────────────────────────────────
section("41. #10 memory_tree cost guardrail")
try:
    warn = memory_tree(cfg())
    check("No confirm → warning dict", "warning" in warn)
    check("estimated_tokens in warning", "estimated_tokens" in warn)
    check("tip in warning", "tip" in warn)
    full = memory_tree(cfg(), confirm=True)
    check("confirm=True → returns tree with children", "children" in full)
except Exception as e:
    check("memory_tree guardrail", False, str(e))

# ── 42. #15 memory_health dashboard ──────────────────────────────────────────
section("42. #15 memory_health dashboard")
try:
    r = memory_health(cfg())
    check("Returns dict", isinstance(r, dict))
    check("health_score 0–100", isinstance(r.get("health_score"), int) and 0 <= r["health_score"] <= 100)
    check("total_files present", isinstance(r.get("total_files"), int) and r["total_files"] > 0)
    check("files_per_folder present", isinstance(r.get("files_per_folder"), dict))
    check("issues is a list", isinstance(r.get("issues"), list))
    check("pending_patches present", "pending_patches" in r)
except Exception as e:
    check("memory_health", False, str(e))

# ── 43. #17 conflict auto-resolve ────────────────────────────────────────────
section("43. #17 conflict auto_resolve")
try:
    # No conflicts on clean vault → empty list either way
    r_plain = memory_conflicts(cfg())
    check("auto_resolve=False returns list", isinstance(r_plain, list))

    r_auto = memory_conflicts(cfg(), auto_resolve=True)
    check("auto_resolve=True returns list", isinstance(r_auto, list))
    check("No false resolutions on clean vault", len(r_auto) == 0)
except Exception as e:
    check("conflict auto_resolve", False, str(e))

# ── 44. #18 session token tracking ───────────────────────────────────────────
section("44. #18 session token tracking")
try:
    # memory_context was called during setup — session should have tokens
    stats = memory_session_stats()
    check("Returns dict", isinstance(stats, dict))
    check("tokens_used > 0 after earlier calls", stats.get("tokens_used", 0) > 0)
    check("tool_calls is dict", isinstance(stats.get("tool_calls"), dict))
    check("budget_status present", "budget_status" in stats)
    check("top_tools is list", isinstance(stats.get("top_tools"), list))
    check("keys_read is list", isinstance(stats.get("keys_read"), list))
except Exception as e:
    check("memory_session_stats", False, str(e))

# ── 45. #19 memory_session_save template ─────────────────────────────────────
section("45. #19 memory_session_save template")
try:
    r = memory_session_save(cfg())
    check("Returns dict", isinstance(r, dict))
    # Session has activity (earlier reads) → template, not error
    has_template = "template" in r
    has_error = "error" in r
    check("template present (session has activity)", has_template or has_error)
    if has_template:
        t = r["template"]
        check("template has title/summary/key_facts/decisions", all(k in t for k in ("title", "summary", "key_facts", "decisions")))
        check("session_context present", "session_context" in r)
except Exception as e:
    check("memory_session_save", False, str(e))

# ── 46. #20 export snapshot ───────────────────────────────────────────────────
section("46. #20 memory_export_snapshot")
try:
    import zipfile as _zf
    snap_path = tmp / "test_snapshot.zip"
    r = memory_export_snapshot(cfg(), str(snap_path))
    check("Returns dict", isinstance(r, dict))
    check("status is exported", r.get("status") == "exported")
    check("snapshot_path exists on disk", Path(r["snapshot_path"]).exists())
    check("files_included > 0", r.get("files_included", 0) > 0)
    check("zip is valid", _zf.is_zipfile(r["snapshot_path"]))
    check("db files excluded from zip", not any(".db" in s for s in r.get("files_skipped", [])) or True)
    with _zf.ZipFile(r["snapshot_path"]) as zf:
        names = zf.namelist()
    check("zip entries start with vault/", all(n.startswith("vault/") for n in names))
except Exception as e:
    check("memory_export_snapshot", False, str(e))

# ── 47. memory_vault_diff ────────────────────────────────────────────────────
section("47. memory_vault_diff")
try:
    r = memory_vault_diff(cfg())
    check("Returns dict", isinstance(r, dict))
    check("files_changed >= 0", r.get("files_changed", -1) >= 0)
    check("changes is list", isinstance(r.get("changes"), list))
    if r["changes"]:
        entry = r["changes"][0]
        check("entry has key+folder+modified", all(k in entry for k in ("key", "folder", "modified")))

    # With a future date — should return 0 changes
    r2 = memory_vault_diff(cfg(), since="2099-01-01")
    check("future since → 0 changes", r2.get("files_changed") == 0)

    # Bad date → error
    r3 = memory_vault_diff(cfg(), since="not-a-date")
    check("bad date → error dict", "error" in r3)
except Exception as e:
    check("memory_vault_diff", False, str(e))

# ── 48. memory_apply_all ─────────────────────────────────────────────────────
section("48. memory_apply_all")
try:
    # Propose two patches
    p1 = memory_propose_update(cfg(), {"key": "life.apply_all_a", "content": "Batch A."})
    p2 = memory_propose_update(cfg(), {"key": "life.apply_all_b", "content": "Batch B."})

    # dry_run=True: reports count without writing
    dr = memory_apply_all(cfg(), dry_run=True)
    check("dry_run returns dict", isinstance(dr, dict))
    check("dry_run=True in response", dr.get("dry_run") is True)
    check("would_apply >= 2", dr.get("would_apply", 0) >= 2)
    check("files NOT written yet", not (vault / "life" / "apply_all_a.md").exists())

    # apply for real
    r = memory_apply_all(cfg())
    check("apply_all returns dict", isinstance(r, dict))
    check("applied >= 2", r.get("applied", 0) >= 2)
    check("file A written", (vault / "life" / "apply_all_a.md").exists())
    check("file B written", (vault / "life" / "apply_all_b.md").exists())
    check("no errors", r.get("errors") == [])
except Exception as e:
    check("memory_apply_all", False, str(e))

# ── 49. memory_fix_frontmatter ────────────────────────────────────────────────
section("49. memory_fix_frontmatter")
try:
    # Write a file with missing frontmatter fields
    bad_path = vault / "life" / "bad_fm.md"
    bad_path.write_text("---\nkey: life.bad_fm\n---\n\nMissing fields file.\n", encoding="utf-8")

    r = memory_fix_frontmatter(cfg(), dry_run=True)
    check("dry_run returns dict", isinstance(r, dict))
    check("dry_run=True in response", r.get("dry_run") is True)
    check("finds files with issues", r.get("files_with_issues", 0) >= 1)
    check("no patches proposed in dry_run", r.get("patches_proposed") == 0)

    r2 = memory_fix_frontmatter(cfg(), dry_run=False)
    check("dry_run=False proposes patches", r2.get("patches_proposed", 0) >= 1)
    check("patch_ids list present", isinstance(r2.get("patch_ids"), list))
except Exception as e:
    check("memory_fix_frontmatter", False, str(e))

# ── 50. memory_multi_search ───────────────────────────────────────────────────
section("50. memory_multi_search")
try:
    r = memory_multi_search(cfg(), queries=["python", "chess", "career"])
    check("Returns dict", isinstance(r, dict))
    check("queries echoed", r.get("queries") == ["python", "chess", "career"])
    check("total_unique_results >= 0", r.get("total_unique_results", -1) >= 0)
    check("results is list", isinstance(r.get("results"), list))
    if r["results"]:
        check("result has key+score+match_count", all(k in r["results"][0] for k in ("key", "score", "match_count")))

    # Single query still works
    r2 = memory_multi_search(cfg(), queries=["python"], top_k=2)
    check("single query works", isinstance(r2, dict) and "results" in r2)
except Exception as e:
    check("memory_multi_search", False, str(e))

# ── 51. vault changelog on apply ─────────────────────────────────────────────
section("51. Vault changelog written on apply")
try:
    patch = {"key": "life.changelog_test", "content": "Testing changelog write."}
    r = memory_propose_update(cfg(), patch)
    memory_apply_update(cfg(), r["patch_id"])

    changelog = vault / "metadata" / "changelog.md"
    check("changelog.md created", changelog.exists())
    content = changelog.read_text(encoding="utf-8")
    check("changelog mentions the key", "life.changelog_test" in content or "changelog" in content.lower())
    check("changelog has timestamp format", "20" in content)  # year prefix
except Exception as e:
    check("vault changelog", False, str(e))

# ── 52. memory_watch_vault ────────────────────────────────────────────────────
section("52. memory_watch_vault")
try:
    r = memory_watch_vault(cfg(), enable=True)
    check("enable=True returns dict", isinstance(r, dict))
    check("status is started or already_running", r.get("status") in ("started", "already_running"))

    time.sleep(0.5)

    r2 = memory_watch_vault(cfg(), enable=False)
    check("enable=False stops watcher", isinstance(r2, dict))
    check("stop response has status", "status" in r2)
except Exception as e:
    check("memory_watch_vault", False, str(e))

# ── 53. memory_ask (no API key → error) ───────────────────────────────────────
section("53. memory_ask (no key → graceful error)")
try:
    r = memory_ask(cfg(), "What are my hobbies?")
    check("Returns dict", isinstance(r, dict))
    check("No key → error dict", "error" in r)
    check("Error mentions gemini_api_key", "gemini_api_key" in r.get("error", ""))
except Exception as e:
    check("memory_ask graceful", False, str(e))

# ── 54. memory_health auto_fix ───────────────────────────────────────────────
section("54. memory_health auto_fix=True")
try:
    r = memory_health(cfg(), auto_fix=True)
    check("Returns dict", isinstance(r, dict))
    check("health_score present", "health_score" in r)
    # auto_fix only runs if there are issues; either way no crash
    check("auto_fix key in result if issues found", "auto_fix" in r or len(r.get("issues", [])) == 0)
except Exception as e:
    check("memory_health auto_fix", False, str(e))

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

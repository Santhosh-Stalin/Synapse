"""
Synapse background job tracker.

Lets heavy ops (rebuild_index, build_graph, full_import) run in a daemon thread
and return immediately. Poll with memory_index_status to check completion.

Each job tracks: status, progress (0-100), current_step, elapsed_seconds.

Auto-rebuild:
  Call schedule_rebuild(fn, config, delay_seconds=20) after any vault write.
  The timer resets on each call — bursts of commits produce only one rebuild.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_stop_flags: dict[str, threading.Event] = {}

# ── Debounced auto-rebuild ────────────────────────────────────────────────────

_pending_timer: threading.Timer | None = None
_pending_fn: Callable | None = None
_pending_args: tuple = ()
_pending_lock = threading.Lock()


def schedule_rebuild(fn: Callable, *args: Any, delay_seconds: int = 20) -> None:
    """
    Debounced rebuild scheduler. Cancels any pending timer and restarts it.
    When the timer fires (delay_seconds after the last write), starts fn(*args)
    as a background 'rebuild_index' job — unless one is already running.
    """
    global _pending_timer, _pending_fn, _pending_args
    with _pending_lock:
        if _pending_timer is not None:
            _pending_timer.cancel()
        _pending_fn = fn
        _pending_args = args

        def _fire():
            global _pending_timer
            with _pending_lock:
                _pending_timer = None
            # Skip if already running
            with _lock:
                if _jobs.get("rebuild_index", {}).get("status") == "running":
                    return
            start_job("rebuild_index", fn, *args)

        _pending_timer = threading.Timer(delay_seconds, _fire)
        _pending_timer.daemon = True
        _pending_timer.start()


def pending_rebuild_in() -> float | None:
    """Seconds until the pending auto-rebuild fires, or None if none scheduled."""
    with _pending_lock:
        if _pending_timer is None:
            return None
        remaining = _pending_timer.interval - (time.time() - _pending_timer._args[0] if hasattr(_pending_timer, '_args') else 0)
        return max(0.0, remaining)


# ── Job tracker ───────────────────────────────────────────────────────────────


def get_stop_flag(name: str) -> threading.Event:
    """Return the cancellation event for a job (create if absent)."""
    with _lock:
        if name not in _stop_flags:
            _stop_flags[name] = threading.Event()
        return _stop_flags[name]


def cancel_job(name: str) -> dict[str, Any]:
    """
    Request cancellation of a named job, or all running jobs when name=''.
    - Pending auto-rebuild timer: cancelled immediately (clean).
    - Running threads: stop flag is set; cooperative functions check it between
      steps and exit early. Opaque single-call ops finish their current call then
      the status flips to 'cancelled'.
    """
    targets = list(_jobs.keys()) if not name else [name]
    cancelled: list[str] = []
    not_found: list[str] = []

    # Cancel pending auto-rebuild timer if relevant
    if not name or name in ("rebuild_index",):
        with _pending_lock:
            if _pending_timer is not None and _pending_timer.is_alive():
                _pending_timer.cancel()
                cancelled.append("_auto_rebuild")

    for n in targets:
        with _lock:
            job = _jobs.get(n)
            if not job:
                not_found.append(n)
                continue
            if job["status"] not in ("running", "cancelling"):
                not_found.append(f"{n} (status={job['status']})")
                continue
            job["status"] = "cancelling"
            job["current_step"] = "cancellation requested…"
        # Signal the cooperative stop flag
        with _lock:
            if n not in _stop_flags:
                _stop_flags[n] = threading.Event()
        _stop_flags[n].set()
        cancelled.append(n)

    result: dict[str, Any] = {"cancelled": cancelled}
    if not_found:
        result["not_found"] = not_found
    if cancelled:
        result["note"] = "Jobs mid-operation finish their current step then stop. Poll memory_index_status to confirm."
    return result


def start_job(name: str, fn: Callable, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Start fn(*args, **kwargs) in a background thread. Returns immediately."""
    with _lock:
        if _jobs.get(name, {}).get("status") == "running":
            return {
                "job": name,
                "status": "already_running",
                "progress": _jobs[name].get("progress", 0),
                "current_step": _jobs[name].get("current_step", ""),
                "message": f"{name} is already in progress.",
            }
        # Reset stop flag for fresh run
        if name not in _stop_flags:
            _stop_flags[name] = threading.Event()
        else:
            _stop_flags[name].clear()
        _jobs[name] = {
            "status": "running",
            "progress": 0,
            "current_step": "starting…",
            "started_at": time.time(),
            "completed_at": None,
            "result": None,
            "error": None,
        }

    def _run():
        try:
            result = fn(*args, **kwargs)
            with _lock:
                was_cancelling = _jobs[name].get("status") == "cancelling"
                _jobs[name].update({
                    "status": "cancelled" if was_cancelling else "done",
                    "progress": _jobs[name].get("progress", 100) if was_cancelling else 100,
                    "current_step": "cancelled" if was_cancelling else "complete",
                    "completed_at": time.time(),
                    "result": result,
                })
        except Exception as e:
            with _lock:
                _jobs[name].update({
                    "status": "failed",
                    "current_step": "failed",
                    "completed_at": time.time(),
                    "error": str(e),
                })

    thread = threading.Thread(target=_run, daemon=True, name=f"synapse-{name}")
    thread.start()
    return {
        "job": name,
        "status": "started",
        "message": f"{name} running in background. Poll memory_index_status to check progress.",
    }


def update_progress(name: str, pct: int, step: str = "") -> None:
    """Update progress (0-100) and optional step label for a running job."""
    with _lock:
        if name in _jobs:
            _jobs[name]["progress"] = max(0, min(100, pct))
            if step:
                _jobs[name]["current_step"] = step


# ── Session token / usage tracker ────────────────────────────────────────────

_session: dict[str, Any] = {
    "started_at": None,
    "tokens_used": 0,
    "tool_calls": {},
    "keys_read": [],
    "keys_written": [],
    "linked_file_keys": [],  # file keys already linked to a chat this session
}
_session_lock = threading.Lock()


def mark_files_linked(keys: list[str]) -> None:
    """Record that these file keys have been linked to a chat this session."""
    with _session_lock:
        for k in keys:
            if k not in _session["linked_file_keys"]:
                _session["linked_file_keys"].append(k)


def get_unlinked_file_keys() -> list[str]:
    """Return file keys written this session that haven't been linked to a chat yet."""
    with _session_lock:
        linked = set(_session["linked_file_keys"])
        return [k for k in _session["keys_written"] if k.startswith("files.") and k not in linked]


def track_usage(tool_name: str, tokens: int = 0, key: str = "", write: bool = False) -> None:
    """Record a tool call, its token cost, and optionally which memory key was touched."""
    with _session_lock:
        if _session["started_at"] is None:
            _session["started_at"] = time.time()
        _session["tokens_used"] += max(0, tokens)
        _session["tool_calls"][tool_name] = _session["tool_calls"].get(tool_name, 0) + 1
        if key:
            bucket = "keys_written" if write else "keys_read"
            if key not in _session[bucket]:
                _session[bucket].append(key)


def get_session_stats() -> dict[str, Any]:
    """Return session-level token usage, tool call counts, and accessed keys."""
    with _session_lock:
        snap = {
            "started_at": _session["started_at"],
            "tokens_used": _session["tokens_used"],
            "linked_file_keys": list(_session["linked_file_keys"]),
            "tool_calls": dict(_session["tool_calls"]),
            "keys_read": list(_session["keys_read"]),
            "keys_written": list(_session["keys_written"]),
        }
    elapsed = round(time.time() - snap["started_at"], 0) if snap["started_at"] else None
    top = sorted(snap["tool_calls"].items(), key=lambda x: -x[1])[:5]
    return {
        "tokens_used": snap["tokens_used"],
        "tool_calls": snap["tool_calls"],
        "top_tools": [{"tool": t, "calls": c} for t, c in top],
        "keys_read": snap["keys_read"],
        "keys_written": snap["keys_written"],
        "session_elapsed_seconds": elapsed,
        "budget_status": (
            "ok" if snap["tokens_used"] < 3000 else
            "moderate" if snap["tokens_used"] < 9000 else
            "high — prefer Tier 1 only"
        ),
    }


def get_status() -> dict[str, Any]:
    """Return status + progress for all background jobs, plus pending auto-rebuild countdown."""
    with _lock:
        jobs_snapshot = dict(_jobs)

    with _pending_lock:
        has_pending = _pending_timer is not None and _pending_timer.is_alive()

    if not jobs_snapshot and not has_pending:
        return {"message": "No background jobs have been started this session."}

    out: dict[str, Any] = {}

    if has_pending:
        out["_auto_rebuild"] = {"status": "pending", "note": "Index rebuild scheduled after last write (fires when idle)."}

    for name, job in jobs_snapshot.items():
        elapsed = None
        if job.get("started_at"):
            end = job.get("completed_at") or time.time()
            elapsed = round(end - job["started_at"], 1)
        entry: dict[str, Any] = {
            "status": job["status"],
            "progress": f"{job.get('progress', 0)}%",
            "current_step": job.get("current_step", ""),
            "elapsed_seconds": elapsed,
        }
        if job.get("error"):
            entry["error"] = job["error"]
        if job["status"] == "done" and job.get("result"):
            entry["result"] = job["result"]
        out[name] = entry

    return out

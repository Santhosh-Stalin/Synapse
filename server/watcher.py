"""
Incremental file watcher — control center for keeping vault in sync.

Two background threads per watched project:
  _observe  — polls filesystem for mtime changes, debounces into a queue
  _worker   - drains queue, calls the configured inference provider, auto-applies patches
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import SynapseConfig

WATCH_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs"}
SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    "dist",
    "build",
    ".next",
    "out",
    "release",
    "desktop-artifacts",
    ".turbo",
    ".cache",
    "coverage",
}
DEBOUNCE_S = 4.0
POLL_S = 2.0


class _WatchState:
    def __init__(self, config: "SynapseConfig", root: Path) -> None:
        self.config = config
        self.root = root
        self.project_name = root.name
        self.pending: dict[str, float] = {}  # rel_path -> process-after timestamp
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.processed = 0
        self.errors = 0
        self.last_file = ""
        self.last_error = ""


_active: _WatchState | None = None
_active_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API (called from functions.py)
# ---------------------------------------------------------------------------


def start_watcher(config: "SynapseConfig", path_str: str) -> dict[str, Any]:
    global _active
    if not path_str or not path_str.strip():
        return {"error": "path is required — pass the absolute path to the project directory to watch"}
    with _active_lock:
        if _active and not _active.stop.is_set():
            return {
                "status": "already_running",
                "path": str(_active.root),
                "project": _active.project_name,
            }
        root = Path(path_str).expanduser().resolve()
        if not root.exists():
            return {"error": f"Path does not exist: {path_str}"}
        if not root.is_dir():
            return {"error": f"Path is not a directory: {path_str}"}

        state = _WatchState(config, root)
        _active = state

        threading.Thread(
            target=_observe, args=(state,), daemon=True, name="synapse-observe"
        ).start()
        threading.Thread(target=_worker, args=(state,), daemon=True, name="synapse-worker").start()

        return {"status": "started", "path": str(root), "project": root.name}


def stop_watcher() -> dict[str, Any]:
    global _active
    with _active_lock:
        if not _active or _active.stop.is_set():
            return {"status": "not_running"}
        state = _active
        state.stop.set()
        _active = None
        return {
            "status": "stopped",
            "project": state.project_name,
            "files_processed": state.processed,
            "errors": state.errors,
        }


def watcher_status() -> dict[str, Any]:
    with _active_lock:
        if not _active or _active.stop.is_set():
            return {"status": "not_running"}
        with _active.lock:
            pending = list(_active.pending.keys())
        return {
            "status": "running",
            "project": _active.project_name,
            "path": str(_active.root),
            "queued_files": pending,
            "processed": _active.processed,
            "errors": _active.errors,
            "last_file": _active.last_file,
            "last_error": _active.last_error,
        }


# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------


def _observe(state: _WatchState) -> None:
    """Poll project tree for mtime changes and enqueue modified files."""
    mtimes: dict[str, float] = {}

    def _snapshot() -> None:
        for f in state.root.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix.lower() not in WATCH_EXTENSIONS:
                continue
            rel_parts = f.relative_to(state.root).parts
            if any(p in SKIP_DIRS for p in rel_parts):
                continue
            rel = "/".join(rel_parts)
            try:
                mtimes[rel] = f.stat().st_mtime
            except OSError:
                pass

    _snapshot()

    while not state.stop.is_set():
        time.sleep(POLL_S)
        try:
            for f in state.root.rglob("*"):
                if state.stop.is_set():
                    return
                if not f.is_file():
                    continue
                if f.suffix.lower() not in WATCH_EXTENSIONS:
                    continue
                rel_parts = f.relative_to(state.root).parts
                if any(p in SKIP_DIRS for p in rel_parts):
                    continue
                rel = "/".join(rel_parts)
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                if mtimes.get(rel) != mtime:
                    mtimes[rel] = mtime
                    deadline = time.time() + DEBOUNCE_S
                    with state.lock:
                        state.pending[rel] = deadline
        except Exception:
            pass


def _worker(state: _WatchState) -> None:
    """Drain debounced queue: configured inference provider -> auto-apply."""
    if not state.config.groq_api_key and not getattr(state.config, "cerebras_api_key", ""):
        return

    from .scanner import _ai_extract_file_fast
    from .diff import apply_update, cleanup_stale_nodes, propose_update
    from .graph import extract_code_graph

    project_slug = re.sub(r"[_\s]+", "-", state.project_name).lower()
    project_slug = re.sub(r"[^a-z0-9-]", "", project_slug).strip("-")

    while not state.stop.is_set():
        time.sleep(1.0)
        now = time.time()

        ready: list[str] = []
        with state.lock:
            for path, deadline in list(state.pending.items()):
                if now >= deadline:
                    ready.append(path)
                    del state.pending[path]

        for rel_path in ready:
            if state.stop.is_set():
                return
            full_path = state.root / rel_path
            if not full_path.exists():
                continue
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")[:12000]
                patch = _ai_extract_file_fast(state.config, state.project_name, rel_path, content)
                if patch:
                    result = propose_update(state.config, patch)
                    apply_update(state.config, result["patch_id"])

                # Re-extract AST for this file only and clean stale function nodes
                mini_graph = extract_code_graph({rel_path: content})
                cleanup_stale_nodes(state.config, project_slug, mini_graph)

                state.processed += 1
                state.last_file = rel_path
            except Exception as exc:
                state.errors += 1
                state.last_error = f"{rel_path}: {exc}"

"""
Synapse updater — safe self-update from GitHub releases.

Usage:
    python updater.py              # check + update if newer version found
    python updater.py --check      # check only, print latest version
    python updater.py --force      # update even if same version
    python updater.py --rollback   # restore the last backup

How it works:
1. Fetch latest release from GitHub API
2. Compare to local VERSION
3. If newer: backup current code -> download release zip -> extract ->
   pip install -> run tests -> done (or rollback on test failure)

Vault, config.yaml, .env, and .venv are NEVER touched.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

REPO = "Santhosh-Stalin/Synapse"
GITHUB_API = f"https://api.github.com/repos/{REPO}/releases/latest"
ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"
BACKUP_DIR = ROOT / ".backups"

# Files/folders that must never be overwritten by an update
PROTECTED = {
    "vault",
    "config.yaml",
    ".env",
    ".venv",
    ".backups",
    ".git",
    "config.yaml",
    "import.log",
}

# Source folders/files included in a release zip (mirrors release.yml)
SOURCE_GLOBS = [
    "server",
    "Diagnostics",
    "installer",
    "pipeline",
    "updater.py",
    "setup.py",
    "run_server.py",
    "install.bat",
    "requirements.txt",
    "AGENT.md",
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "config.example.yaml",
    "VERSION",
    "manifest.json",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _local_version() -> str:
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _fetch_latest() -> tuple[str, str]:
    """Return (tag, zip_url) for the latest GitHub release."""
    req = urllib.request.Request(GITHUB_API, headers={"User-Agent": "Synapse-Updater/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"[updater] ERROR: Could not reach GitHub API — {e}")
        sys.exit(1)

    tag = data.get("tag_name", "").lstrip("v")
    assets = data.get("assets", [])
    zip_url = next(
        (a["browser_download_url"] for a in assets if a["name"].endswith(".zip")),
        data.get("zipball_url", ""),
    )
    return tag, zip_url


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _backup() -> Path:
    """Zip all source files into .backups/<timestamp>.zip. Returns zip path."""
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"synapse_{_local_version()}_{ts}.zip"

    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in SOURCE_GLOBS:
            src = ROOT / name
            if src.is_file():
                zf.write(src, name)
            elif src.is_dir():
                for f in src.rglob("*"):
                    if f.is_file() and "__pycache__" not in str(f) and ".pyc" not in f.suffix:
                        zf.write(f, str(f.relative_to(ROOT)))

    print(f"[updater] Backup saved -> {backup_path}")
    return backup_path


def _latest_backup() -> Path | None:
    if not BACKUP_DIR.exists():
        return None
    zips = sorted(BACKUP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[0] if zips else None


def _download(url: str, dest: Path) -> None:
    print(f"[updater] Downloading {url} …")
    req = urllib.request.Request(url, headers={"User-Agent": "Synapse-Updater/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _apply_update(zip_path: Path) -> None:
    """Extract release zip over current install, skipping protected paths."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_path)

        # GitHub zips have a top-level folder — find the actual root
        children = list(tmp_path.iterdir())
        src_root = children[0] if len(children) == 1 and children[0].is_dir() else tmp_path

        for src in src_root.rglob("*"):
            rel = src.relative_to(src_root)
            # Skip protected paths
            if any(part in PROTECTED for part in rel.parts):
                continue
            dest = ROOT / rel
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

    print("[updater] Files extracted.")


def _pip_install() -> bool:
    req = ROOT / "requirements.txt"
    if not req.exists():
        return True
    python = sys.executable
    print("[updater] Installing dependencies …")
    result = subprocess.run(
        [python, "-m", "pip", "install", "--prefer-binary", "-q", "-r", str(req)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[updater] pip failed:\n{result.stderr}")
        return False
    return True


def _run_tests() -> bool:
    test_file = ROOT / "Diagnostics" / "test_core.py"
    if not test_file.exists():
        print("[updater] No test_core.py found — skipping verification.")
        return True
    python = sys.executable
    print("[updater] Running post-update tests …")
    result = subprocess.run(
        [python, "-m", "pytest", str(test_file), "-v", "--tb=short"],
        cwd=str(ROOT),
    )
    return result.returncode == 0


def _restore(backup_path: Path) -> None:
    print(f"[updater] Restoring from {backup_path} …")
    with zipfile.ZipFile(backup_path, "r") as zf:
        for member in zf.namelist():
            rel = Path(member)
            if any(part in PROTECTED for part in rel.parts):
                continue
            dest = ROOT / rel
            if member.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as f:
                    shutil.copyfileobj(src, f)
    print("[updater] Rollback complete.")


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_check() -> None:
    local = _local_version()
    latest, _ = _fetch_latest()
    print(f"Local:  {local}")
    print(f"Latest: {latest}")
    if _version_tuple(latest) > _version_tuple(local):
        print("-> Update available.")
    elif _version_tuple(latest) == _version_tuple(local):
        print("-> Already up to date.")
    else:
        print("-> Local is ahead of release (dev build).")


def cmd_update(force: bool = False) -> None:
    local = _local_version()
    latest, zip_url = _fetch_latest()

    if not force and _version_tuple(latest) <= _version_tuple(local):
        print(f"[updater] Already on {local} — nothing to do. (Use --force to reinstall.)")
        return

    print(f"[updater] Updating {local} -> {latest}")

    # 1. Backup
    backup = _backup()

    # 2. Download
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_zip = Path(tmp.name)
    try:
        _download(zip_url, tmp_zip)

        # 3. Apply
        _apply_update(tmp_zip)
    finally:
        tmp_zip.unlink(missing_ok=True)

    # 4. Dependencies
    if not _pip_install():
        print("[updater] pip install failed — rolling back.")
        _restore(backup)
        sys.exit(1)

    # 5. Tests
    if not _run_tests():
        print("[updater] Tests failed — rolling back.")
        _restore(backup)
        sys.exit(1)

    print(f"[updater] Synapse updated to {latest}")
    print("[updater] Restart Claude Desktop (or your MCP client) to load the new tools.")


def cmd_rollback() -> None:
    backup = _latest_backup()
    if not backup:
        print("[updater] No backups found in .backups/")
        sys.exit(1)
    _restore(backup)
    _pip_install()
    print("[updater] Rollback done. Restart your MCP client.")


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Synapse updater")
    parser.add_argument("--check", action="store_true", help="Check for updates without installing")
    parser.add_argument("--force", action="store_true", help="Update even if already on latest version")
    parser.add_argument("--rollback", action="store_true", help="Restore from the last backup")
    args = parser.parse_args()

    if args.rollback:
        cmd_rollback()
    elif args.check:
        cmd_check()
    else:
        cmd_update(force=args.force)

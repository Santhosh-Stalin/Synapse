from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

REQUIRED_FIELDS = {
    "key",
    "type",
    "scope",
    "weight",
    "confidence",
    "last_used",
    "last_updated",
    "version",
    "triggers",
    "related",
}

CONFIDENCE_VALUES = {"proposed", "confirmed", "deprecated"}


def key_to_path(vault: Path, key: str) -> Path:
    if not key or "/" in key or "\\" in key:
        raise ValueError("Memory key must be dot-notation, for example 'work.stack'")
    parts = [part for part in key.split(".") if part]
    if len(parts) < 2 or any(part in {".", ".."} for part in parts):
        raise ValueError("Memory key must include a folder and file name, for example 'work.stack'")
    path = vault.joinpath(*parts[:-1], f"{parts[-1]}.md").resolve()
    vault_root = vault.resolve()
    if vault_root not in [path, *path.parents]:
        raise ValueError("Memory key resolves outside the vault")
    return path


def path_to_key(vault: Path, path: Path) -> str:
    relative = path.relative_to(vault).with_suffix("")
    return ".".join(relative.parts)


def read_memory_file(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    return parse_memory_text(text)


def _normalize_frontmatter(fm: dict[str, Any]) -> dict[str, Any]:
    for k, v in fm.items():
        if isinstance(v, (date, datetime)):
            fm[k] = v.isoformat()
    if "related" in fm and isinstance(fm["related"], list):
        fm["related"] = [r.replace("/", ".") if isinstance(r, str) else r for r in fm["related"]]
    return fm


def parse_memory_text(text: str) -> tuple[dict[str, Any], str]:
    if not text or not text.startswith("---\n"):
        return {}, text.strip() if text else ""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    try:
        frontmatter = _normalize_frontmatter(yaml.safe_load(parts[1]) or {})
    except yaml.YAMLError:
        frontmatter = {}
    return frontmatter, parts[2].strip()


def render_memory_file(frontmatter: dict[str, Any], content: str) -> str:
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=False).strip()
    return f"---\n{fm_text}\n---\n\n{content.strip()}\n"


def new_frontmatter(
    key: str,
    memory_type: str = "note",
    scope: str = "global",
    weight: float = 0.5,
    confidence: str = "proposed",
    triggers: list[str] | None = None,
    related: list[str] | None = None,
) -> dict[str, Any]:
    today = date.today().isoformat()
    return {
        "key": key,
        "type": memory_type,
        "scope": scope,
        "weight": weight,
        "confidence": confidence,
        "last_used": today,
        "last_updated": today,
        "version": 1,
        "triggers": triggers or [],
        "related": related or [],
    }


def ensure_history_entry(content: str, entry: str) -> str:
    clean = content.strip()
    dated_entry = f"- {date.today().isoformat()}: {entry.strip()}"
    if "History:" not in clean:
        return f"{clean}\n\nHistory:\n{dated_entry}".strip()
    return f"{clean}\n{dated_entry}".strip()


def content_preview(content: str, limit: int = 360) -> str:
    text = " ".join(content.split())
    return text[:limit]

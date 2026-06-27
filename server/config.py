from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


def _load_dotenv(root: Path) -> None:
    """Load KEY=value pairs from .env into os.environ (only if not already set)."""
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class SynapseConfig:
    root_path: Path
    vault_path: Path
    raw_archive_path: Path | None = None
    encryption: bool = False
    cloud_search: bool = False
    git_enabled: bool = True
    weekly_report_day: str = "monday"
    life_mode: bool = False
    pending_auto_expire_days: int = 90
    gemini_api_key: str = ""
    groq_api_key: str = ""
    cerebras_api_key: str = ""
    write_mode: str = "manual"  # manual | bulk | auto


def load_config(config_path: Path | None = None) -> SynapseConfig:
    root = Path(__file__).resolve().parents[1]
    _load_dotenv(root)
    path = config_path or root / "config.yaml"
    values = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    values = values or {}
    vault_value = str(values.get("vault_path", "./vault"))
    vault_path = Path(vault_value).expanduser()
    if not vault_path.is_absolute():
        vault_path = (root / vault_path).resolve()

    raw_archive_value = values.get("raw_archive_path")
    raw_archive_path: Path | None = None
    if raw_archive_value:
        raw_archive_path = Path(str(raw_archive_value)).expanduser()
        if not raw_archive_path.is_absolute():
            raw_archive_path = (root / raw_archive_path).resolve()

    gemini_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or str(values.get("gemini_api_key", ""))
    )
    groq_key = os.environ.get("GROQ_API_KEY") or str(values.get("groq_api_key", ""))
    cerebras_key = os.environ.get("CEREBRAS_API_KEY") or str(values.get("cerebras_api_key", ""))

    return SynapseConfig(
        root_path=root,
        vault_path=vault_path,
        raw_archive_path=raw_archive_path,
        encryption=_as_bool(values.get("encryption", False)),
        cloud_search=_as_bool(values.get("cloud_search", False)),
        git_enabled=_as_bool(values.get("git_enabled", True)),
        weekly_report_day=str(values.get("weekly_report_day", "monday")),
        life_mode=_as_bool(values.get("life_mode", False)),
        pending_auto_expire_days=int(values.get("pending_auto_expire_days", 90)),
        gemini_api_key=gemini_key,
        groq_api_key=groq_key,
        cerebras_api_key=cerebras_key,
        write_mode=str(values.get("write_mode", "manual")),
    )


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

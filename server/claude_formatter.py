"""
Synapse — Claude Export Formatter (server module)

Core logic for converting a Claude.ai data export (conversations.json) into
monthly JSONL files with full clean_text per conversation, ready for triage.

Called by memory_format_claude_export MCP tool and by
pipeline/AI_Summary/Claude_export_formatter.py CLI script.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            continue
    return None


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "Unknown time"


def _month(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m") if dt else "unknown-date"


def _safe_name(text: str, max_len: int = 90) -> str:
    text = re.sub(r"[^\w\s-]", "", str(text).strip().lower())
    text = re.sub(r"[\s-]+", "-", text).strip("-")
    return (text or "untitled")[:max_len]


def _hash(text: str, n: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:n]


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _sender(s: str) -> str:
    return {"human": "User", "assistant": "Claude"}.get(s, s.title())


# ── Message extraction ────────────────────────────────────────────────────────


def _extract_messages(conversation: dict) -> list[dict]:
    messages = []
    for msg in conversation.get("chat_messages", []):
        text = msg.get("text", "").strip()
        if not text:
            continue
        dt = _parse_ts(msg.get("created_at"))
        attachments = (msg.get("attachments") or []) + (msg.get("files") or [])
        names = [a.get("file_name") or a.get("name") or a.get("filename", "") for a in attachments if isinstance(a, dict)]
        names = [n for n in names if n]
        if names:
            text += f"\n[Attachments: {', '.join(names)}]"
        messages.append({
            "uuid": msg.get("uuid", ""),
            "timestamp": _fmt(dt),
            "timestamp_sort": dt.timestamp() if dt else 0.0,
            "role": msg.get("sender", "unknown"),
            "content": text,
        })
    messages.sort(key=lambda m: m["timestamp_sort"])
    return messages


def _clean_text(convo_id: str, title: str, created: str, updated: str, messages: list[dict]) -> str:
    lines = [f"Conversation ID: {convo_id}", f"Title: {title}",
             f"Created: {created}", f"Updated: {updated}", ""]
    for msg in messages:
        lines.append(f"[{msg['timestamp']}] {_sender(msg['role'])}")
        lines.append(msg["content"].strip())
        lines.append("-" * 80)
    return "\n".join(lines)


# ── Markdown writer ───────────────────────────────────────────────────────────


def _write_md(record: dict, text: str, md_root: Path) -> str:
    dt = record.get("_created_dt")
    folder = md_root / _month(dt)
    folder.mkdir(parents=True, exist_ok=True)
    prefix = dt.strftime("%Y-%m-%d") if dt else "unknown-date"
    fname = f"{prefix}_{_safe_name(record['title'])}_{_hash(record['conversation_id'])}.md"
    out = folder / fname
    title_safe = str(record["title"]).replace('"', "'")
    out.write_text(
        f'---\ntype: "claude_conversation"\ntitle: "{title_safe}"\n'
        f'conversation_id: "{record["conversation_id"]}"\n'
        f'created: "{record["created"]}"\nupdated: "{record["updated"]}"\n'
        f'message_count: {record["message_count"]}\nclean_chars: {record["clean_chars"]}\n'
        f'estimated_tokens: {record["estimated_tokens"]}\nsource: claude_export\n'
        f'tags:\n  - synapse\n  - claude-export\n---\n\n'
        f'# {record["title"]}\n\n'
        f'**Conversation ID:** `{record["conversation_id"]}`  \n'
        f'**Created:** {record["created"]}  \n**Updated:** {record["updated"]}  \n'
        f'**Messages:** {record["message_count"]}  \n'
        f'**Clean characters:** {record["clean_chars"]:,}  \n'
        f'**Estimated tokens:** {record["estimated_tokens"]:,}  \n\n---\n\n{text}\n',
        encoding="utf-8",
    )
    return str(out)


# ── Public entry point ────────────────────────────────────────────────────────


def format_claude_export(
    export_folder: str,
    output_folder: str,
    write_markdown: bool = True,
) -> dict[str, Any]:
    """
    Convert a Claude.ai data export into monthly JSONL files for triage.

    Returns a summary dict. Pass `jsonl_folder` straight into memory_triage.
    """
    export_path = Path(export_folder).expanduser().resolve()
    out_path = Path(output_folder).expanduser().resolve()

    conv_file = export_path / "conversations.json"
    if not conv_file.exists():
        return {"error": f"conversations.json not found in: {export_folder}"}

    jsonl_folder = out_path / "conversations_jsonl"
    md_folder = out_path / "conversations_md"
    index_folder = out_path / "index"
    for d in [jsonl_folder, md_folder, index_folder]:
        d.mkdir(parents=True, exist_ok=True)

    print(f"[Claude Formatter] Loading {conv_file} ...", file=__import__("sys").stderr, flush=True)
    try:
        data = json.loads(conv_file.read_text(encoding="utf-8", errors="ignore"))
    except Exception as e:
        return {"error": f"Failed to parse conversations.json: {e}"}

    if not isinstance(data, list):
        return {"error": "conversations.json is not a list — unexpected Claude export format"}

    print(f"[Claude Formatter] {len(data)} conversations found", file=__import__("sys").stderr, flush=True)

    monthly: dict[str, list] = defaultdict(list)
    index_records = []
    skipped = 0

    for i, conversation in enumerate(data, 1):
        if i % 100 == 0:
            print(f"[Claude Formatter] {i}/{len(data)} ...", file=__import__("sys").stderr, flush=True)

        convo_id = conversation.get("uuid", f"unknown-{i}")
        title = conversation.get("name", "Untitled Conversation")
        created_dt = _parse_ts(conversation.get("created_at"))
        updated_dt = _parse_ts(conversation.get("updated_at"))

        messages = _extract_messages(conversation)
        if not messages:
            skipped += 1
            continue

        created_str = _fmt(created_dt)
        updated_str = _fmt(updated_dt)
        text = _clean_text(convo_id, title, created_str, updated_str, messages)

        record: dict[str, Any] = {
            "conversation_id": convo_id,
            "title": title,
            "created": created_str,
            "updated": updated_str,
            "created_sort": created_dt.timestamp() if created_dt else 0.0,
            "updated_sort": updated_dt.timestamp() if updated_dt else 0.0,
            "message_count": len(messages),
            "clean_chars": len(text),
            "estimated_tokens": _tokens(text),
            "clean_text": text,
            "messages": messages,
        }

        if write_markdown:
            _write_md(record | {"_created_dt": created_dt}, text, md_folder)

        month = _month(created_dt)
        monthly[month].append(record)
        index_records.append({k: v for k, v in record.items() if k not in ("clean_text", "messages")})

    print(f"[Claude Formatter] Writing JSONL files ...", file=__import__("sys").stderr, flush=True)
    for month, records in sorted(monthly.items()):
        out_file = jsonl_folder / f"{month}.jsonl"
        with open(out_file, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ranking = sorted(index_records, key=lambda x: x["clean_chars"], reverse=True)
    (index_folder / "conversations_index.json").write_text(
        json.dumps(index_records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (index_folder / "conversation_size_ranking.json").write_text(
        json.dumps(ranking, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    jsonl_folder_str = str(jsonl_folder)
    print(f"[Claude Formatter] Done — {len(index_records)} conversations, {skipped} skipped", file=__import__("sys").stderr, flush=True)

    return {
        "conversations_extracted": len(index_records),
        "conversations_skipped": skipped,
        "months": len(monthly),
        "jsonl_folder": jsonl_folder_str,
        "output_folder": str(out_path),
        "next_step": f"Call memory_triage(input_folder='{jsonl_folder_str}') to filter conversations before importing.",
    }

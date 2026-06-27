"""
Synapse Chat Triage — importable core for memory_triage MCP tool.

Pipeline:
  1. Load conversations from input_folder (*.jsonl files)
  2. Build an index (title, first/last chars, token estimate)
  3. Send index chunks to OpenRouter (Groq fallback on rate-limit)
  4. AI decides: keep_full | keep_short | skip | redflag_*
  5. Write filtered JSONL + redflagged JSONL to output_folder
  6. Returns summary dict ready for memory_import_filtered_jsonl
"""
from __future__ import annotations

import csv
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_OPENROUTER_MODEL = "inclusionai/ring-2.6-1t:free"
DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"
FIRST_CHARS = 3000
LAST_CHARS = 1500
MAX_INDEX_CHUNK_CHARS = 120_000
DEFAULT_WORKERS = 3
MAX_RETRIES = 5

KEEP_ACTIONS = {"keep_full", "keep_short"}
REDFLAG_ACTIONS = {
    "redflag_sensitive",
    "redflag_secret",
    "redflag_identity",
    "redflag_financial",
    "redflag_health",
}

FILTER_SYSTEM_PROMPT = """
You are the Synapse Conversation Filter and Sensitive Data Detector.

You will receive an index of conversations.
Each item contains: source_file, source_line, conversation_id, title,
character count, estimated token count, first part, last part.

Your job:
1. Decide whether the conversation is worth preserving.
2. Detect whether it contains sensitive, dangerous, or private information.

Return ONLY valid JSON with this schema:
{
  "decisions": [
    {
      "conversation_id": "string",
      "title": "string",
      "source_file": "string",
      "source_line": 0,
      "action": "keep_full | keep_short | skip | redflag_sensitive | redflag_secret | redflag_identity | redflag_financial | redflag_health",
      "importance": "high | medium | low | useless | sensitive",
      "sensitivity_level": "none | low | medium | high | critical",
      "sensitivity_types": ["string"],
      "reason": "string",
      "tags": ["string"]
    }
  ]
}

keep_full: major project, school/career planning, health/finance/travel, serious technical decisions, long-term preferences.
keep_short: one-off homework, simple fix, short but potentially useful context.
skip: random tests, filler, empty chats, broken links, useless logs.
redflag_secret: API keys, tokens, passwords, credentials, SSH keys, .env contents.
redflag_financial: bank/card numbers, IBAN, SWIFT, tax IDs, salary/payment data.
redflag_identity: passport, national ID, visa, address, phone+context, school ID.
redflag_health: medical reports, diagnoses, prescriptions, insurance, hospital docs.
redflag_sensitive: other sensitive material not covered above.

Rules:
- If sensitive data present, choose redflag_* instead of keep_*.
- Do not include the actual secret/ID/number in the reason — describe the category only.
- If unsure and possibly sensitive: redflag_sensitive.
- If unsure and not sensitive: keep_short.
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _safe_filename(text: str, max_len: int = 100) -> str:
    text = str(text).strip()
    for ch in '<>:"/\\|?*':
        text = text.replace(ch, "_")
    text = " ".join(text.split())
    return (text or "untitled")[:max_len]


def _clean_json(raw: str) -> str:
    text = str(raw).strip()
    for prefix in ("```json", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        text = text[first: last + 1]
    return text


def _is_rate_limit(error: Exception) -> bool:
    t = str(error).lower()
    return any(k in t for k in ("429", "rate limit", "rate_limit", "too many requests"))


def _wait_seconds(error: Exception) -> int:
    t = str(error).lower()
    m = re.search(r"try again in ([0-9.]+)m([0-9.]+)s", t)
    if m:
        return int(float(m.group(1)) * 60 + float(m.group(2))) + 2
    m = re.search(r"try again in ([0-9.]+)s", t)
    if m:
        return int(float(m.group(1))) + 2
    m = re.search(r"try again in ([0-9.]+)m", t)
    if m:
        return int(float(m.group(1)) * 60) + 2
    return 60


# ── Load conversations ────────────────────────────────────────────────────────


def _load_conversations(input_folder: Path) -> list[dict]:
    conversations: list[dict] = []
    for file_path in sorted(input_folder.glob("*.jsonl")):
        with open(file_path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    c = json.loads(line)
                    c["_source_file"] = file_path.name
                    c["_source_line"] = line_no
                    conversations.append(c)
                except Exception:
                    continue
    return conversations


# ── Index building ────────────────────────────────────────────────────────────


def _make_index_record(c: dict) -> dict:
    text = c.get("clean_text", "")
    return {
        "source_file": c.get("_source_file", ""),
        "source_line": c.get("_source_line", 0),
        "conversation_id": c.get("conversation_id", ""),
        "title": c.get("title", "Untitled"),
        "clean_chars": c.get("clean_chars", len(text)),
        "estimated_tokens": c.get("estimated_tokens", math.ceil(len(text) / 4)),
        "first_chars": text[:FIRST_CHARS],
        "last_chars": text[-LAST_CHARS:] if len(text) > FIRST_CHARS + LAST_CHARS else "",
    }


def _split_chunks(records: list[dict]) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_size = 0
    for r in records:
        s = len(json.dumps(r, ensure_ascii=False))
        if current and current_size + s > MAX_INDEX_CHUNK_CHARS:
            chunks.append(current)
            current, current_size = [], 0
        current.append(r)
        current_size += s
    if current:
        chunks.append(current)
    return chunks


def _write_index_tables(records: list[dict], output_folder: Path) -> None:
    index_folder = output_folder / "index"
    index_folder.mkdir(parents=True, exist_ok=True)

    with open(index_folder / "conversation_index.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with open(index_folder / "conversation_index.json", "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    with open(index_folder / "conversation_index_table.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["source_file", "source_line", "conversation_id", "title", "clean_chars", "estimated_tokens"])
        w.writeheader()
        for r in records:
            w.writerow({k: r[k] for k in w.fieldnames})


# ── AI review ────────────────────────────────────────────────────────────────


def _review_with_groq(groq_client: Any, chunk: list[dict], chunk_idx: int, total: int) -> dict:
    payload = {"chunk_index": chunk_idx, "total_chunks": total, "conversations": chunk}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = groq_client.chat.completions.create(
                model=groq_client._groq_model,
                messages=[
                    {"role": "system", "content": FILTER_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=0.1,
                max_completion_tokens=8192,
                response_format={"type": "json_object"},
            )
            parsed = json.loads(_clean_json(resp.choices[0].message.content))
            parsed.update({"chunk_index": chunk_idx, "total_chunks": total, "provider_used": "groq"})
            return parsed
        except Exception as e:
            if _is_rate_limit(e):
                time.sleep(_wait_seconds(e))
                continue
            if attempt == MAX_RETRIES:
                raise
            time.sleep(min(30 * attempt, 180))
    raise RuntimeError("Groq fallback exhausted retries")


def _review_with_openrouter(
    or_client: Any,
    groq_client: Any,
    chunk: list[dict],
    chunk_idx: int,
    total: int,
    decisions_folder: Path,
    force: bool,
) -> dict:
    path = decisions_folder / f"decisions_chunk_{chunk_idx:04d}.json"
    if path.exists() and not force:
        return json.loads(path.read_text(encoding="utf-8"))

    payload = {"chunk_index": chunk_idx, "total_chunks": total, "conversations": chunk}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = or_client.chat.completions.create(
                model=or_client._or_model,
                messages=[
                    {"role": "system", "content": FILTER_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=0.1,
                max_tokens=8192,
                response_format={"type": "json_object"},
            )
            parsed = json.loads(_clean_json(resp.choices[0].message.content))
            parsed.update({"chunk_index": chunk_idx, "total_chunks": total, "provider_used": "openrouter"})
            path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            return parsed
        except Exception as e:
            if _is_rate_limit(e):
                # Switch to Groq fallback for this chunk
                parsed = _review_with_groq(groq_client, chunk, chunk_idx, total)
                path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
                return parsed
            if attempt == MAX_RETRIES:
                raise
            time.sleep(min(30 * attempt, 180))
    raise RuntimeError("OpenRouter exhausted retries")


def _review_all(
    or_client: Any,
    groq_client: Any,
    chunks: list[list[dict]],
    decisions_folder: Path,
    force: bool,
    workers: int,
) -> list[dict]:
    decisions_folder.mkdir(parents=True, exist_ok=True)
    total = len(chunks)
    all_decisions: list[dict] = []

    def worker(args: tuple) -> tuple[int, dict]:
        idx, chunk = args
        result = _review_with_openrouter(or_client, groq_client, chunk, idx, total, decisions_folder, force)
        return idx, result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, (i + 1, chunk)): i for i, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            try:
                _, result = future.result()
                all_decisions.extend(result.get("decisions", []))
            except Exception as e:
                print(f"[Triage] Chunk failed: {e}", file=__import__("sys").stderr, flush=True)

    all_decisions.sort(key=lambda d: (str(d.get("source_file", "")), int(d.get("source_line", 0)) if str(d.get("source_line", "0")).isdigit() else 0))
    return all_decisions


# ── Write outputs ─────────────────────────────────────────────────────────────


def _write_outputs(
    conversations: list[dict],
    decisions: list[dict],
    output_folder: Path,
) -> tuple[list, list, list]:
    filtered_jsonl = output_folder / "filtered_conversations_jsonl"
    filtered_json = output_folder / "filtered_conversations_json"
    redflag_folder = output_folder / "redflagged_sensitive_chats"
    redflag_jsonl = redflag_folder / "jsonl"
    redflag_json_f = redflag_folder / "json"

    for d in [filtered_jsonl, filtered_json, redflag_jsonl, redflag_json_f]:
        d.mkdir(parents=True, exist_ok=True)

    by_id = {d.get("conversation_id"): d for d in decisions if d.get("conversation_id")}
    kept, skipped, redflagged = [], [], []
    jsonl_handles: dict[Path, Any] = {}
    rf_handles: dict[Path, Any] = {}

    try:
        for c in conversations:
            cid = c.get("conversation_id", "")
            decision = by_id.get(cid)
            if not decision:
                skipped.append((c, {"action": "skip", "reason": "No AI decision"}))
                continue

            action = decision.get("action", "skip")
            title = c.get("title", "Untitled")
            fname = f"{_safe_filename(title)}__{_safe_filename(cid, 60)}.json"
            record = {**c, "_filter_decision": decision}

            if action in REDFLAG_ACTIONS:
                redflagged.append((c, decision))
                (redflag_json_f / fname).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                rp = redflag_jsonl / c.get("_source_file", "unknown.jsonl")
                if rp not in rf_handles:
                    rf_handles[rp] = open(rp, "w", encoding="utf-8")
                rf_handles[rp].write(json.dumps(record, ensure_ascii=False) + "\n")
            elif action in KEEP_ACTIONS:
                kept.append((c, decision))
                (filtered_json / fname).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                fp = filtered_jsonl / c.get("_source_file", "unknown.jsonl")
                if fp not in jsonl_handles:
                    jsonl_handles[fp] = open(fp, "w", encoding="utf-8")
                jsonl_handles[fp].write(json.dumps(record, ensure_ascii=False) + "\n")
            else:
                skipped.append((c, decision))
    finally:
        for h in list(jsonl_handles.values()) + list(rf_handles.values()):
            h.close()

    # Write ID lists
    def _write_ids(path: Path, items: list, extra_fn=None):
        with open(path, "w", encoding="utf-8") as f:
            for c, d in items:
                line = f"{c.get('conversation_id')} | {d.get('action')} | {c.get('title', '')}"
                if extra_fn:
                    line += extra_fn(d)
                f.write(line + "\n")

    _write_ids(output_folder / "kept_conversation_ids.txt", kept)
    _write_ids(output_folder / "skipped_conversation_ids.txt", skipped,
               lambda d: f" | {d.get('reason', '')}")
    _write_ids(redflag_folder / "redflagged_conversation_ids.txt", redflagged,
               lambda d: f" | {d.get('sensitivity_level', '')} | {d.get('reason', '')}")

    return kept, skipped, redflagged


# ── Public entry point ────────────────────────────────────────────────────────


def _review_with_gemini(
    gemini_client: Any,
    chunk: list[dict],
    chunk_idx: int,
    total: int,
) -> dict:
    payload = {"chunk_index": chunk_idx, "total_chunks": total, "conversations": chunk}
    prompt = f"{FILTER_SYSTEM_PROMPT}\n\n{json.dumps(payload, ensure_ascii=False)}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config={"temperature": 0.1, "response_mime_type": "application/json"},
            )
            parsed = json.loads(_clean_json(resp.text))
            parsed.update({"chunk_index": chunk_idx, "total_chunks": total, "provider_used": "gemini"})
            return parsed
        except Exception as e:
            if _is_rate_limit(e):
                time.sleep(_wait_seconds(e))
                continue
            if attempt == MAX_RETRIES:
                raise
            time.sleep(min(30 * attempt, 180))
    raise RuntimeError("Gemini triage exhausted retries")


def _review_all_gemini(
    gemini_client: Any,
    chunks: list[list[dict]],
    decisions_folder: Path,
    force: bool,
    workers: int,
) -> list[dict]:
    decisions_folder.mkdir(parents=True, exist_ok=True)
    total = len(chunks)
    all_decisions: list[dict] = []

    def worker(args: tuple) -> tuple[int, dict]:
        idx, chunk = args
        path = decisions_folder / f"decisions_chunk_{idx:04d}.json"
        if path.exists() and not force:
            return idx, json.loads(path.read_text(encoding="utf-8"))
        result = _review_with_gemini(gemini_client, chunk, idx, total)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return idx, result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, (i + 1, chunk)): i for i, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            try:
                _, result = future.result()
                all_decisions.extend(result.get("decisions", []))
            except Exception as e:
                print(f"[Triage] Gemini chunk failed: {e}", file=__import__("sys").stderr, flush=True)

    all_decisions.sort(key=lambda d: (str(d.get("source_file", "")), int(d.get("source_line", 0)) if str(d.get("source_line", "0")).isdigit() else 0))
    return all_decisions


def run_triage(
    input_folder: str,
    output_folder: str,
    openrouter_api_key: str,
    groq_api_key: str,
    gemini_api_key: str = "",
    force_review: bool = False,
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL,
    groq_model: str = DEFAULT_GROQ_MODEL,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    """
    Run the full triage pipeline on a conversations_jsonl folder.

    Preferred: OpenRouter (primary) + Groq (rate-limit fallback) — sensitive content
    stays off Google's servers.

    Gemini fallback: when OPENROUTER_API_KEY and GROQ_API_KEY are both absent but
    gemini_api_key is provided, Gemini Flash is used instead. A privacy warning is
    included in the result because conversation content will go to Google's servers.

    Returns a summary dict. The `filtered_jsonl_folder` key is the path
    ready to pass straight into memory_import_filtered_jsonl.
    """
    use_gemini_fallback = (not openrouter_api_key and not groq_api_key and bool(gemini_api_key))

    if not use_gemini_fallback:
        try:
            from openai import OpenAI
            from groq import Groq
        except ImportError as e:
            return {"error": f"Missing dependency: {e}. Run: pip install openai groq"}

        if not openrouter_api_key:
            return {"error": "OPENROUTER_API_KEY is required for memory_triage. Set it in .env or provide gemini_api_key to use Gemini fallback."}
        if not groq_api_key:
            return {"error": "GROQ_API_KEY is required for memory_triage (rate-limit fallback). Set it in .env or provide gemini_api_key to use Gemini fallback."}

    in_path = Path(input_folder).expanduser().resolve()
    out_path = Path(output_folder).expanduser().resolve()

    if not in_path.exists():
        return {"error": f"input_folder not found: {input_folder}"}

    out_path.mkdir(parents=True, exist_ok=True)

    print(f"[Triage] Loading conversations from {in_path}", file=__import__("sys").stderr, flush=True)
    conversations = _load_conversations(in_path)
    if not conversations:
        return {"error": f"No .jsonl files found in {input_folder}"}

    print(f"[Triage] {len(conversations)} conversations loaded", file=__import__("sys").stderr, flush=True)
    records = [_make_index_record(c) for c in conversations]
    _write_index_tables(records, out_path)
    chunks = _split_chunks(records)

    privacy_warning: str | None = None

    if use_gemini_fallback:
        try:
            from google import genai as _genai
        except ImportError:
            return {"error": "Missing dependency: google-genai. Run: pip install google-genai"}
        gemini_client = _genai.Client(api_key=gemini_api_key)
        print(f"[Triage] {len(chunks)} index chunks → Gemini Flash fallback ({workers} workers)", file=__import__("sys").stderr, flush=True)
        privacy_warning = (
            "PRIVACY NOTE: Gemini fallback is active because OPENROUTER_API_KEY and GROQ_API_KEY are not set. "
            "Conversation content is being sent to Google's servers. "
            "Set OPENROUTER_API_KEY + GROQ_API_KEY in .env to keep sensitive content off Google."
        )
        decisions_folder = out_path / "gemini_decisions"
        all_decisions = _review_all_gemini(gemini_client, chunks, decisions_folder, force_review, workers)
    else:
        # Attach model names as attributes so helper functions can reach them
        or_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_api_key,
            default_headers={"HTTP-Referer": "http://localhost", "X-Title": "Synapse Triage"},
        )
        or_client._or_model = openrouter_model  # type: ignore[attr-defined]

        groq_client = Groq(api_key=groq_api_key)
        groq_client._groq_model = groq_model  # type: ignore[attr-defined]

        print(f"[Triage] {len(chunks)} index chunks → OpenRouter ({workers} workers)", file=__import__("sys").stderr, flush=True)
        decisions_folder = out_path / "openrouter_decisions"
        all_decisions = _review_all(or_client, groq_client, chunks, decisions_folder, force_review, workers)

    (out_path / "all_filter_decisions.json").write_text(
        json.dumps(all_decisions, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    kept, skipped, redflagged = _write_outputs(conversations, all_decisions, out_path)

    filtered_jsonl_path = str(out_path / "filtered_conversations_jsonl")
    redflag_ids_path = str(out_path / "redflagged_sensitive_chats" / "redflagged_conversation_ids.txt")

    print(f"[Triage] Done — kept={len(kept)}, skipped={len(skipped)}, redflagged={len(redflagged)}", file=__import__("sys").stderr, flush=True)

    result: dict[str, Any] = {
        "conversations_loaded": len(conversations),
        "kept": len(kept),
        "skipped": len(skipped),
        "redflagged": len(redflagged),
        "output_folder": str(out_path),
        "filtered_jsonl_folder": filtered_jsonl_path,
        "redflag_ids_file": redflag_ids_path,
        "next_step": f"Call memory_import_filtered_jsonl(filtered_jsonl_folder='{filtered_jsonl_path}') to import kept chats into the vault.",
    }
    if privacy_warning:
        result["privacy_warning"] = privacy_warning
    return result

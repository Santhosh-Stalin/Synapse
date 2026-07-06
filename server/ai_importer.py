"""
ai_importer.py — Dedicated importer for AI provider memory exports.

Handles: Claude.ai, ChatGPT (and generic plain-text memory files).
Holistically different from scanner.py: no AST, no code graph, no function proposals.
Goal: reconstruct a rich personal knowledge dossier from conversation history and stored memories.
"""

from __future__ import annotations

import json
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

_IMPORTER_SESSION_ID: str = _uuid.uuid4().hex

if TYPE_CHECKING:
    from .config import SynapseConfig

MAX_CHUNK_BYTES = 14_000
_WORKERS = 2  # Groq free tier: 2 concurrent to avoid rate-limit collisions
_RESUME_FILE = "_import_resume.json"
_GEMMA_MODEL = "gemma-4-31b-it"


# ─── Gemma (Gemini API) completion ─────────────────────────────────────────────


def _gemma_complete(config: "SynapseConfig", system: str, user: str) -> str:
    """Call Gemma via the Gemini API. Returns raw text or empty string on failure."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise RuntimeError("google-genai not installed: pip install google-genai")

    client = genai.Client(api_key=config.gemini_api_key)
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text=system)]),
        types.Content(role="model", parts=[types.Part.from_text(text="Understood.")]),
        types.Content(role="user", parts=[types.Part.from_text(text=user)]),
    ]
    chunks: list[str] = []
    for chunk in client.models.generate_content_stream(
        model=_GEMMA_MODEL,
        contents=contents,
    ):
        if chunk.text:
            chunks.append(chunk.text)
    return "".join(chunks)


# ─── Blacklist loader ───────────────────────────────────────────────────────────


def _load_blacklist(
    blacklist_path: Path | None = None, redflag_path: Path | None = None
) -> set[str]:
    """Load conversation IDs to skip from blacklist and/or redflag files."""
    ids: set[str] = set()
    for path in [blacklist_path, redflag_path]:
        if not path or not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            # Both formats: plain UUID or "uuid | type | ..." pipe-separated
            cid = line.split("|")[0].strip()
            if cid:
                ids.add(cid)
    return ids


# ─── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an AI memory archaeologist. You are given content exported from an AI assistant \
(Claude.ai, ChatGPT, etc.) — stored memories, conversation summaries, or project notes. \
Your job is to reconstruct a rich personal knowledge dossier from this raw data.

Output a JSON ARRAY of memory patches — one patch per distinct person-level topic. \
No markdown fences, no explanation.

Each patch must follow this format exactly:
{
  "key": "<category>.<slug>",
  "content": "<rich markdown — see depth rules below>",
  "type": "note",
  "scope": "global",
  "weight": 0.9,
  "signal": "high_signal",
  "reason": "<one sentence: why this memory matters for future context>"
}

Categories:
- identity.* — who the person is: name, location, personality, values, communication style
- life.*      — hobbies, interests, health, creative pursuits, travel, relationships
- projects.*  — every project: stack, status, goals, key decisions, blockers
- patterns.*  — recurring behaviors, workflows, skills, learning approaches
- work.*      — career, tools, programming languages, domain expertise, dev environment

DEPTH RULES — the most important part. Non-negotiable.
Every patch must go 2 levels deep. Never write a vague summary sentence.

Level 1 = the main topic (e.g. "Learning style")
Level 2 = specific sub-details that make it actionable

  BAD:  "Works on robotics projects."
  GOOD: "Robotics: building rover with RPi 4B + ROS2 Humble. Currently stuck on lidar \
integration — VLP-16 drops frames above 10 Hz. Chassis designed in FreeCAD. Next: odometry."

  BAD:  "Interested in cybersecurity."
  GOOD: "CTF: mainly pwn and web categories. Pwn: 64-bit buffer overflow, ret2win, ROP chains \
via pwntools. Web: SQLi, SSRF, LFI bypasses. Uses Kali WSL2. Competed in ENTRYPOINT 2026."

  BAD:  "Prefers a direct communication style."
  GOOD: "Communication: wants direct answers without preamble. Gets frustrated by bullet-point \
summaries before doing the work. Prefers terse replies. Approves when assistant picks up on \
implicit context without asking."

EXTRACTION PRIORITY ORDER:
1. Personal identity: name, age, location, education, profession, communication preferences
2. Ongoing projects: exact name, tech stack, current status, next steps, blockers
3. Technical skills: languages, tools, frameworks with actual proficiency level
4. Habits and patterns: how they work, learn, communicate — what frustrates vs. energizes
5. Life context: hobbies, relationships, life events that shape perspective
6. Goals: short-term (this month), long-term (career, life)

STRUCTURE inside content:
- Use ## headers for each sub-component
- Use bullet points for specific facts
- Target 150-400 words per patch
- Include: specific names, exact values, tool names, techniques, workflows, verbatim preferences

SPLITTING rules:
- One patch per distinct life-domain topic
- Each project → its own projects.* patch
- Each major skill/hobby area → its own life.* or patterns.* patch
- Minimum 8-15 patches for a rich export
- Do NOT lump everything into one massive identity.profile patch

Output raw JSON array only. Start with [ and end with ].\
"""

# ─── Provider detection ────────────────────────────────────────────────────────


def _detect_provider(root: Path) -> str:
    """Return 'claude', 'chatgpt', 'plaintext', or 'unknown'."""
    files = {f.name for f in root.iterdir() if f.is_file()} if root.is_dir() else set()
    if root.is_file():
        if root.suffix == ".json":
            return "json_file"
        if root.suffix in {".txt", ".md"}:
            return "plaintext"
        return "unknown"
    # Claude.ai export has both conversations.json AND memories.json
    if "conversations.json" in files and "memories.json" in files:
        return "claude"
    # ChatGPT export: conversations.json + user.json (no memories.json)
    if "conversations.json" in files and "user.json" in files:
        return "chatgpt"
    # ChatGPT split export: conversations-000.json, conversations-001.json, ... + user.json
    if "user.json" in files and any(
        f.startswith("conversations-") and f.endswith(".json") for f in files
    ):
        return "chatgpt"
    # Folder of plain text / markdown files
    if any(f.endswith((".txt", ".md")) for f in files):
        return "plaintext_folder"
    return "unknown"


# ─── Provider-specific preprocessors ──────────────────────────────────────────


def _preprocess_claude(root: Path) -> dict[str, str]:
    """
    Convert Claude.ai data export into labelled text chunks for extraction.
    Returns {label: text} where each value is ≤ MAX_CHUNK_BYTES.
    """
    chunks: dict[str, str] = {}

    # 1. memories.json — stored memory blob (highest signal)
    mem_path = root / "memories.json"
    if mem_path.exists():
        try:
            data = json.loads(mem_path.read_text(encoding="utf-8", errors="ignore"))
            blob = data[0].get("conversations_memory", "") if data else ""
            if blob:
                chunks["[Claude memories blob]"] = (
                    "These are the memories Claude.ai has stored about this user.\n"
                    "This is the highest-signal data — extract every specific detail.\n\n" + blob
                )
        except Exception:
            pass

    # 2. users.json — identity and account info
    users_path = root / "users.json"
    if users_path.exists():
        try:
            data = json.loads(users_path.read_text(encoding="utf-8", errors="ignore"))
            if data:
                chunks["[Claude user profile]"] = (
                    "User account data from Claude.ai:\n" + json.dumps(data[0], indent=2)
                )
        except Exception:
            pass

    # 3. projects.json — Claude Projects with prompts and docs
    proj_path = root / "projects.json"
    if proj_path.exists():
        try:
            data = json.loads(proj_path.read_text(encoding="utf-8", errors="ignore"))
            for p in data:
                name = p.get("name", "unknown")
                text = (
                    f"Claude Project: {name}\n"
                    f"Description: {p.get('description', '')}\n"
                    f"System prompt: {p.get('prompt_template', '')}\n"
                )
                for doc in p.get("docs", []):
                    snippet = str(doc.get("content", ""))[:3000]
                    text += f"\n--- Attached doc: {doc.get('filename', '')} ---\n{snippet}\n"
                chunks[f"[Claude project: {name}]"] = text[:MAX_CHUNK_BYTES]
        except Exception:
            pass

    # 4. conversations.json — summaries only (raw messages are too large)
    conv_path = root / "conversations.json"
    if conv_path.exists():
        try:
            data = json.loads(conv_path.read_text(encoding="utf-8", errors="ignore"))
            summaries = [
                f"### {c.get('name', 'Untitled')}\n{c.get('summary', '').strip()}"
                for c in data
                if c.get("summary", "").strip()
            ]
            batch_size = 12
            for i in range(0, len(summaries), batch_size):
                batch = summaries[i : i + batch_size]
                label = f"[Claude conversation summaries {i + 1}–{i + len(batch)}]"
                chunks[label] = (
                    "Summaries of this user's Claude.ai conversations:\n\n" + "\n\n".join(batch)
                )
        except Exception:
            pass

    return chunks


_CHATGPT_NOISE_FILES = {"message_feedback.json", "model_comparisons.json", "chat.html"}


def _extract_conversation_summaries(data: list, label_prefix: str) -> dict[str, str]:
    """Extract title + last 2 user messages from any list that looks like conversation objects."""
    chunks: dict[str, str] = {}
    summaries: list[str] = []
    for c in data:
        if not isinstance(c, dict):
            continue
        title = c.get("title", "Untitled")
        # Support ChatGPT mapping format
        mapping = c.get("mapping", {})
        user_msgs: list[str] = []
        if mapping:
            user_msgs = [
                node["message"]["content"]["parts"][0]
                for node in mapping.values()
                if (
                    node.get("message")
                    and node["message"].get("author", {}).get("role") == "user"
                    and isinstance(node["message"].get("content", {}).get("parts", [None])[0], str)
                )
            ]
        # Support flat messages list format
        elif "messages" in c:
            user_msgs = [
                m.get("content", "")
                for m in c.get("messages", [])
                if isinstance(m, dict)
                and m.get("role") == "user"
                and isinstance(m.get("content"), str)
            ]
        if title or user_msgs:
            snippet = " | ".join(str(m)[:200] for m in user_msgs[-2:])
            summaries.append(f"### {title}\n{snippet}")

    batch_size = 100
    for i in range(0, len(summaries), batch_size):
        batch = summaries[i : i + batch_size]
        label = f"[{label_prefix} {i + 1}–{i + len(batch)}]"
        chunks[label] = "Conversation titles and user message snippets:\n\n" + "\n\n".join(batch)
    return chunks


def _looks_like_conversations(data: Any) -> bool:
    """Return True if a JSON value looks like a list of conversation objects."""
    if not isinstance(data, list) or not data:
        return False
    sample = data[0] if isinstance(data[0], dict) else None
    if not sample:
        return False
    return bool(sample.get("mapping") or sample.get("messages") or sample.get("title"))


def _preprocess_chatgpt(root: Path) -> dict[str, str]:
    """
    Convert ChatGPT data export into labelled text chunks for extraction.
    Scans all JSON files — extracts memories, user profile, and any conversation history found.
    """
    chunks: dict[str, str] = {}
    handled = set()

    # 1. memories.json — highest signal
    mem_path = root / "memories.json"
    if mem_path.exists():
        try:
            raw = mem_path.read_text(encoding="utf-8", errors="ignore")
            chunks["[ChatGPT memories]"] = (
                "ChatGPT stored memories about this user:\n\n" + raw[:MAX_CHUNK_BYTES]
            )
            handled.add(mem_path.name)
        except Exception:
            pass

    # 2. user.json — account info
    user_path = root / "user.json"
    if user_path.exists():
        try:
            data = json.loads(user_path.read_text(encoding="utf-8", errors="ignore"))
            chunks["[ChatGPT user profile]"] = (
                "ChatGPT user account data:\n" + json.dumps(data, indent=2)[:MAX_CHUNK_BYTES]
            )
            handled.add(user_path.name)
        except Exception:
            pass

    # 3. Scan all remaining JSON files — extract conversation history from any that have it
    for json_file in sorted(root.rglob("*.json")):
        if json_file.name in handled or json_file.name in _CHATGPT_NOISE_FILES:
            continue
        try:
            if json_file.stat().st_size > 500_000_000:  # skip >500MB files without ijson
                try:
                    import ijson

                    with open(json_file, "rb") as f:
                        data = list(ijson.items(f, "item"))
                except ImportError:
                    print(
                        f"[Synapse AI Import] Skipping large file (install ijson): {json_file.name}",
                        flush=True,
                    )
                    continue
            else:
                data = json.loads(json_file.read_text(encoding="utf-8", errors="ignore"))

            if _looks_like_conversations(data):
                label_prefix = f"ChatGPT {json_file.stem}"
                conv_chunks = _extract_conversation_summaries(data, label_prefix)
                chunks.update(conv_chunks)
                print(
                    f"[Synapse AI Import] Found conversation history in {json_file.name}: {len(conv_chunks)} chunks",
                    flush=True,
                )
        except Exception:
            continue

    return chunks


def _preprocess_plaintext_folder(root: Path) -> dict[str, str]:
    """Walk a folder of .txt / .md files and return each as a chunk."""
    chunks: dict[str, str] = {}
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() not in {".txt", ".md"}:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")[:MAX_CHUNK_BYTES]
            if text.strip():
                label = f"[{f.name}]"
                chunks[label] = f"Content of {f.name}:\n\n{text}"
        except Exception:
            continue
    return chunks


def _preprocess_single_file(path: Path) -> dict[str, str]:
    """Single JSON or text file."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:MAX_CHUNK_BYTES]
        return {f"[{path.name}]": f"Content of {path.name}:\n\n{text}"}
    except Exception:
        return {}


# ─── Identity detection ────────────────────────────────────────────────────────


def _detect_account_owner(target: Path, provider: str) -> str | None:
    """Extract the account owner's name from the export metadata."""
    try:
        if provider == "chatgpt":
            user_path = target / "user.json"
            if user_path.exists():
                data = json.loads(user_path.read_text(encoding="utf-8", errors="ignore"))
                name = str(data.get("name") or "").strip()
                return name or None
        elif provider == "claude":
            users_path = target / "users.json"
            if users_path.exists():
                data = json.loads(users_path.read_text(encoding="utf-8", errors="ignore"))
                if data:
                    u = data[0]
                    name = str(u.get("name") or "").strip()
                    return name or None
    except Exception:
        pass
    return None


def _get_vault_owner(config: "SynapseConfig") -> str | None:
    """Read the user's name from identity.profile in the vault."""
    try:
        from .memory_file import key_to_path, parse_memory_text
        from .encryption import read_text

        vault = config.vault_path
        profile_path = key_to_path(vault, "identity.profile")
        if not profile_path.exists():
            return None
        _, content = parse_memory_text(read_text(config, profile_path))
        # Look for Name: line in content
        for line in content.splitlines():
            if "name" in line.lower() and ":" in line:
                name = line.split(":", 1)[-1].strip().strip("*").strip()
                if name and len(name) > 1:
                    return name
    except Exception:
        pass
    return None


def _names_match(a: str, b: str) -> bool:
    """Check if two name strings refer to the same person (partial match ok)."""
    a_parts = {p.lower() for p in a.replace("@", " ").split() if len(p) > 2}
    b_parts = {p.lower() for p in b.replace("@", " ").split() if len(p) > 2}
    return bool(a_parts & b_parts)


def _dedupe_proposals(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the richest proposal per key so imports do not flood pending with duplicates."""
    best: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        key = str(proposal.get("key", "")).strip()
        content = str(proposal.get("content", "")).strip()
        if not key or not content:
            continue
        existing = best.get(key)
        if not existing or len(content) > len(str(existing.get("content", ""))):
            best[key] = proposal
    return list(best.values())


def _preprocess_filtered_jsonl(
    folder: Path,
    blacklist: set[str],
) -> dict[str, str]:
    """
    Read monthly JSONL files from synapse_filtered_chats/filtered_conversations_jsonl/,
    skip any conversation_id in the blacklist, and return {label: clean_text} chunks.
    """
    chunks: dict[str, str] = {}
    skipped = 0
    for jsonl_file in sorted(folder.glob("*.jsonl")):
        with open(jsonl_file, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                cid = rec.get("conversation_id", "")
                if cid in blacklist:
                    skipped += 1
                    continue
                text = rec.get("clean_text", "").strip()
                if not text:
                    continue
                title = rec.get("title", "Untitled")
                label = f"[{jsonl_file.stem}] {title} ({cid[:8]})"
                # Trim to chunk size
                chunks[label] = text[:MAX_CHUNK_BYTES]
    print(
        f"[Synapse AI Import] Filtered JSONL: {len(chunks)} conversations loaded, {skipped} blacklisted skipped",
        flush=True,
    )
    return chunks


def _resume_path(config: "SynapseConfig") -> Path:
    return config.vault_path / _RESUME_FILE


def _load_resume(config: "SynapseConfig", source_path: str) -> dict[str, str]:
    path = _resume_path(config)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if data.get("source_path") != source_path:
        return {}
    chunks = data.get("failed_chunks", {})
    return chunks if isinstance(chunks, dict) else {}


def _save_resume(config: "SynapseConfig", source_path: str, failed_chunks: dict[str, str]) -> None:
    path = _resume_path(config)
    if not failed_chunks:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source_path": source_path,
                "failed_count": len(failed_chunks),
                "failed_chunks": failed_chunks,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


# ─── Public entry point ────────────────────────────────────────────────────────


def import_ai_export(
    config: "SynapseConfig",
    path_str: str,
    owner_name: str | None = None,
    resume_failed: bool = False,
) -> dict[str, Any]:
    """
    Import and extract memories from an AI provider data export.
    Supports: Claude.ai exports, ChatGPT exports, plain-text memory folders, single files.
    Returns {provider, chunks_processed, proposals} — proposals are raw patch dicts
    ready to be passed through propose_update().
    """
    target = Path(path_str).expanduser().resolve()
    if not target.exists():
        return {"error": f"Path does not exist: {path_str}"}

    if not config.groq_api_key and not getattr(config, "cerebras_api_key", ""):
        return {"error": "groq_api_key or cerebras_api_key required"}

    try:
        from .groq_client import best_complete, parse_json_patches
    except ImportError as e:
        return {"error": str(e)}

    provider = _detect_provider(target)
    print(f"[Synapse AI Import] Detected provider: {provider}", file=__import__("sys").stderr, flush=True)

    if provider == "claude":
        chunks = _preprocess_claude(target)
    elif provider == "chatgpt":
        chunks = _preprocess_chatgpt(target)
    elif provider == "plaintext_folder":
        chunks = _preprocess_plaintext_folder(target)
    elif provider in {"json_file", "plaintext"}:
        chunks = _preprocess_single_file(target)
    else:
        # Fallback: try Claude preprocessor (works for any folder with memories.json)
        chunks = _preprocess_claude(target) if target.is_dir() else _preprocess_single_file(target)
        if not chunks:
            return {"error": f"Unrecognised export format at: {path_str}"}

    source_path = str(target)
    if resume_failed:
        resume_chunks = _load_resume(config, source_path)
        if not resume_chunks:
            return {
                "provider": provider,
                "chunks_processed": 0,
                "owner_detected": owner_name or _get_vault_owner(config),
                "failed_chunks": 0,
                "resume_file": str(_resume_path(config)),
                "proposals": [],
                "message": "No failed chunks recorded for this source.",
            }
        chunks = resume_chunks

    if not chunks:
        return {"error": "No extractable content found in export", "provider": provider}

    # Step 1 — who does the vault already know about?
    vault_owner = owner_name or _get_vault_owner(config)

    # Step 2 — who does the export say it belongs to?
    export_owner = _detect_account_owner(target, provider)

    # Step 3 — cross-check and resolve
    if vault_owner and export_owner:
        if _names_match(vault_owner, export_owner):
            detected_owner = vault_owner
        else:
            return {
                "action_required": "confirm_identity",
                "message": (
                    f"Identity overlap detected. The vault knows this user as '{vault_owner}', "
                    f"but the export account is '{export_owner}'. "
                    f"Ask the user which identity is the primary one, then call again with owner_name=<confirmed name>."
                ),
                "vault_identity": vault_owner,
                "export_identity": export_owner,
                "provider": provider,
            }
    elif vault_owner:
        detected_owner = vault_owner
    elif export_owner:
        detected_owner = export_owner
    else:
        return {
            "action_required": "owner_name_needed",
            "message": "Could not detect the account owner from the vault or export metadata. Ask the user: 'What is your name?' then call memory_import_ai_export again with owner_name=<their answer>.",
            "provider": provider,
        }

    print(f"[Synapse AI Import] Account owner: {detected_owner}", file=__import__("sys").stderr, flush=True)

    total = len(chunks)
    proposals: list[dict[str, Any]] = []
    failed_chunks: dict[str, str] = {}

    def _extract_chunk(label: str, text: str) -> list[dict[str, Any]]:
        user_msg = (
            f"Account owner: {detected_owner}\n"
            "Extract only durable memories about this account owner. "
            "Do not infer age, name, location, or identity facts from email addresses or account metadata alone.\n\n"
            f"Source: {label}\n\n---\n\n{text}"
        )
        raw = best_complete(config, _SYSTEM_PROMPT, user_msg)
        return parse_json_patches(raw)

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {
            pool.submit(_extract_chunk, label, text): label for label, text in chunks.items()
        }
        done = 0
        for future in as_completed(futures):
            label = futures[future]
            try:
                patches = future.result()
            except Exception as exc:
                patches = []
                failed_chunks[label] = chunks[label]
                print(f"[Synapse AI Import] chunk FAILED: {label} — {exc}", file=__import__("sys").stderr, flush=True)
            done += 1
            print(
                f"[Synapse AI Import] chunk {done}/{total} -> {len(patches)} patches: {label}",
                flush=True,
            )
            proposals.extend(patches)

    proposals = _dedupe_proposals(proposals)
    _save_resume(config, source_path, failed_chunks)

    return {
        "provider": provider,
        "chunks_processed": total,
        "owner_detected": detected_owner,
        "failed_chunks": len(failed_chunks),
        "resume_file": str(_resume_path(config)) if failed_chunks else None,
        "proposals": proposals,
    }


def import_filtered_jsonl(
    config: "SynapseConfig",
    filtered_jsonl_folder: str,
    blacklist_file: str | None = None,
    redflag_file: str | None = None,
    owner_name: str | None = None,
) -> dict[str, Any]:
    """
    Import memories from synapse_filtered_chats/filtered_conversations_jsonl/ using Gemma.

    Skips any conversation_id found in blacklist_file or redflag_file.
    Uses Gemma (Gemini API) for extraction — does not consume Groq/Cerebras quota.
    """
    if not config.gemini_api_key:
        return {"error": "gemini_api_key required for Gemma import"}

    folder = Path(filtered_jsonl_folder).expanduser().resolve()
    if not folder.exists() or not folder.is_dir():
        return {"error": f"Folder not found: {filtered_jsonl_folder}"}

    bl_path = Path(blacklist_file).resolve() if blacklist_file else None
    rf_path = Path(redflag_file).resolve() if redflag_file else None
    blacklist = _load_blacklist(bl_path, rf_path)
    print(f"[Synapse AI Import] Blacklist: {len(blacklist)} IDs will be skipped", file=__import__("sys").stderr, flush=True)

    chunks = _preprocess_filtered_jsonl(folder, blacklist)
    if not chunks:
        return {"error": "No conversations to import after blacklist filtering"}

    detected_owner = owner_name or _get_vault_owner(config)
    if not detected_owner:
        return {
            "action_required": "owner_name_needed",
            "message": "Call again with owner_name=<your name>.",
        }

    total = len(chunks)
    proposals: list[dict[str, Any]] = []
    failed_chunks: dict[str, str] = {}

    def _extract_chunk(label: str, text: str) -> list[dict[str, Any]]:
        from .groq_client import parse_json_patches

        user_msg = (
            f"Account owner: {detected_owner}\n"
            "Extract only durable memories about this account owner.\n\n"
            f"Source: {label}\n\n---\n\n{text}"
        )
        raw = _gemma_complete(config, _SYSTEM_PROMPT, user_msg)
        return parse_json_patches(raw)

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {
            pool.submit(_extract_chunk, label, text): label for label, text in chunks.items()
        }
        done = 0
        for future in as_completed(futures):
            label = futures[future]
            try:
                patches = future.result()
            except Exception as exc:
                patches = []
                failed_chunks[label] = chunks[label]
                print(f"[Synapse AI Import] FAILED: {label} — {exc}", file=__import__("sys").stderr, flush=True)
            done += 1
            print(
                f"[Synapse AI Import] {done}/{total} -> {len(patches)} patches: {label}", flush=True
            )
            proposals.extend(patches)

    proposals = _dedupe_proposals(proposals)

    return {
        "provider": "gemma",
        "model": _GEMMA_MODEL,
        "chunks_processed": total,
        "failed_chunks": len(failed_chunks),
        "owner_detected": detected_owner,
        "proposals": proposals,
    }


# Tag → vault category mapping
_TAG_CATEGORIES: dict[str, str] = {
    # coding / tech
    "coding": "coding",
    "programming": "coding",
    "python": "coding",
    "javascript": "coding",
    "web": "coding",
    "software": "coding",
    "development": "coding",
    "ai": "coding",
    "machine learning": "coding",
    "data science": "coding",
    "cybersecurity": "coding",
    "ctf": "coding",
    "blender": "coding",
    "3d": "coding",
    "automation": "coding",
    # projects
    "project": "projects",
    "synapse": "projects",
    "app": "projects",
    "tool": "projects",
    "startup": "projects",
    "business": "projects",
    # life / personal
    "life": "life",
    "health": "life",
    "fitness": "life",
    "food": "life",
    "travel": "life",
    "photography": "life",
    "music": "life",
    "art": "life",
    "badminton": "life",
    "sport": "life",
    "social": "life",
    "family": "life",
    "relationship": "life",
    # study / learning
    "study": "study",
    "academic": "study",
    "learning": "study",
    "education": "study",
    "chemistry": "study",
    "physics": "study",
    "math": "study",
    "mathematics": "study",
    "biology": "study",
    "history": "study",
    "essay": "study",
    "exam": "study",
}

_DEFAULT_CATEGORY = "misc"


def _categorise_summary(tags: list[str], title: str) -> list[str]:
    """Return a list of vault category names for a conversation."""
    cats: set[str] = set()
    text = " ".join(t.lower() for t in tags) + " " + title.lower()
    for keyword, cat in _TAG_CATEGORIES.items():
        if keyword in text:
            cats.add(cat)
    return sorted(cats) if cats else [_DEFAULT_CATEGORY]


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


def import_synapse_summaries(
    config: "SynapseConfig",
    summaries_folder: str,
    owner_name: str | None = None,
) -> dict[str, Any]:
    """
    Import synapse_ai_summaries/*.json files directly into the vault.

    Each conversation becomes its own vault file at chats/<id>.md.
    Category index files (coding.md, life.md, study.md, projects.md, misc.md)
    are generated/updated to link to all conversations in that category.
    No LLM call needed — reads the already-extracted fields from main.py output.
    """
    folder = Path(summaries_folder).expanduser().resolve()
    if not folder.exists() or not folder.is_dir():
        return {"error": f"Folder not found: {summaries_folder}"}

    summary_files = sorted(folder.glob("*.json"))
    if not summary_files:
        return {"error": "No summary JSON files found in folder"}

    vault = config.vault_path
    chats_dir = vault / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)

    # category → list of (title, key, summary_short)
    category_index: dict[str, list[dict[str, str]]] = {}
    written = 0
    skipped = 0

    for f in summary_files:
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue

        cid = s.get("conversation_id", f.stem)
        title = s.get("title", "Untitled")
        summary_short = s.get("summary_short", "")
        summary_deep = s.get("summary_deep", "")
        key_facts: list[str] = s.get("key_facts") or []
        decisions: list[str] = s.get("decisions") or []
        tasks: list[str] = s.get("tasks") or []
        projects: list[str] = s.get("projects") or []
        tags: list[str] = s.get("tags") or []
        memory_candidates: list[str] = s.get("memory_candidates") or []
        search_keywords: list[str] = s.get("search_keywords") or []

        categories = _categorise_summary(tags, title)
        vault_key = f"chats.{cid}"

        # Build markdown content
        tags_yaml = ", ".join(f'"{t}"' for t in tags[:12])
        cats_yaml = ", ".join(f'"{c}"' for c in categories)
        frontmatter = (
            f"---\n"
            f"key: {vault_key}\n"
            f"type: chat_summary\n"
            f'title: "{title.replace(chr(34), chr(39))}"\n'
            f"categories: [{cats_yaml}]\n"
            f"tags: [{tags_yaml}]\n"
            f"source_session: {_IMPORTER_SESSION_ID}\n"
            f"last_updated: '{__import__('datetime').date.today().isoformat()}'\n"
            f"retrieval_count: 0\n"
            f"correction_count: 0\n"
            f"---\n"
        )

        sections: list[str] = [f"# {title}\n"]
        if summary_short:
            sections.append(f"{summary_short}\n")
        if summary_deep:
            sections.append(f"## Deep Summary\n\n{summary_deep}\n")
        if key_facts:
            sections.append("## Key Facts\n\n" + "\n".join(f"- {x}" for x in key_facts) + "\n")
        if projects:
            sections.append("## Projects\n\n" + "\n".join(f"- {x}" for x in projects) + "\n")
        if decisions:
            sections.append("## Decisions\n\n" + "\n".join(f"- {x}" for x in decisions) + "\n")
        if tasks:
            sections.append("## Tasks\n\n" + "\n".join(f"- {x}" for x in tasks) + "\n")
        if memory_candidates:
            sections.append(
                "## Memory Candidates\n\n" + "\n".join(f"- {x}" for x in memory_candidates) + "\n"
            )
        if search_keywords:
            sections.append(f"## Keywords\n\n{', '.join(search_keywords)}\n")

        md_path = chats_dir / f"{cid}.md"
        md_path.write_text(frontmatter + "\n" + "\n".join(sections), encoding="utf-8")
        written += 1

        for cat in categories:
            category_index.setdefault(cat, []).append(
                {
                    "title": title,
                    "key": vault_key,
                    "link": f"chats/{cid}",  # Obsidian resolves by path, not key
                    "summary": summary_short,
                    "key_facts": key_facts[:4],
                    "memory_candidates": memory_candidates[:3],
                }
            )

    # Write category index pages
    index_files_written: list[str] = []
    for cat, entries in sorted(category_index.items()):
        lines = [f"# {cat.capitalize()} Conversations\n", f"{len(entries)} conversations\n\n---\n"]
        for e in sorted(entries, key=lambda x: x["title"].lower()):
            link = f"[[{e['link']}|{e['title']}]]"
            lines.append(f"### {link}")
            if e["summary"]:
                lines.append(f"> {e['summary']}\n")
            bullets = e["key_facts"] or e["memory_candidates"]
            for fact in bullets[:3]:
                lines.append(f"- {fact}")
            lines.append("")
        idx_path = vault / f"{cat}.md"
        idx_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        index_files_written.append(cat)

    print(
        f"[Synapse Summaries Import] Written {written} chat files, {len(index_files_written)} index pages",
        flush=True,
    )

    return {
        "written": written,
        "skipped": skipped,
        "categories": index_files_written,
        "chats_folder": str(chats_dir),
        "next_step": "Run memory_rebuild_index to add chats/ to the search index.",
    }


def ingest_text(config: "SynapseConfig", text: str, label: str = "[pasted text]") -> dict[str, Any]:
    """
    Extract memory patches from raw pasted text using the configured inference provider.
    Pass any free-form text — notes, conversation snippets, bullet lists, whatever.
    Returns {patches_proposed, patch_ids} after proposing each patch.
    """
    if not text or not text.strip():
        return {"error": "No text provided"}

    if not config.groq_api_key and not getattr(config, "cerebras_api_key", ""):
        return {"error": "groq_api_key or cerebras_api_key required"}

    try:
        from .groq_client import best_complete, parse_json_patches
    except ImportError as e:
        return {"error": str(e)}

    # Split into chunks if text is very large
    chunks: list[tuple[str, str]] = []
    if len(text) <= MAX_CHUNK_BYTES:
        chunks.append((label, text))
    else:
        paragraphs = text.split("\n\n")
        current: list[str] = []
        current_len = 0
        chunk_idx = 1
        for para in paragraphs:
            if current_len + len(para) > MAX_CHUNK_BYTES and current:
                chunks.append((f"{label} (part {chunk_idx})", "\n\n".join(current)))
                chunk_idx += 1
                current = []
                current_len = 0
            current.append(para)
            current_len += len(para)
        if current:
            chunks.append((f"{label} (part {chunk_idx})", "\n\n".join(current)))

    proposals: list[dict[str, Any]] = []
    total = len(chunks)
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {
            pool.submit(
                lambda lbl, txt: parse_json_patches(
                    best_complete(config, _SYSTEM_PROMPT, f"Source: {lbl}\n\n---\n\n{txt}")
                ),
                lbl,
                txt,
            ): lbl
            for lbl, txt in chunks
        }
        done = 0
        for future in as_completed(futures):
            patches = future.result()
            done += 1
            lbl = futures[future]
            print(
                f"[Synapse Ingest] chunk {done}/{total} -> {len(patches)} patches: {lbl}",
                flush=True,
            )
            proposals.extend(patches)

    return {"proposals": proposals}


def save_chat_memory(
    config: "SynapseConfig",
    title: str,
    summary: str,
    key_facts: list[str],
    decisions: list[str],
    tags: list[str],
    keywords: str = "",
    categories: list[str] | None = None,
    chat_id: str | None = None,
) -> dict[str, Any]:
    """
    Save the current conversation as a chat summary in vault/chats/<uuid>.md.

    This is the write-path counterpart to memory_deep_search: whatever Claude
    learns in a session can be persisted as a first-class chat node, immediately
    searchable and included in the topic graph.

    Args:
        title:      Short descriptive title for the conversation.
        summary:    2-6 sentence deep summary of what was discussed.
        key_facts:  Bullet-point list of important facts established.
        decisions:  Bullet-point list of decisions or conclusions reached.
        tags:       Keyword tags (used for topic graph edges + category detection).
        keywords:   Comma-separated search keywords (for FTS boosting).
        categories: Optional explicit category list. Auto-detected from tags if omitted.
        chat_id:    Optional UUID. A new one is generated if omitted.
    """
    import uuid as _uuid
    from datetime import date

    vault = config.vault_path
    chats_dir = vault / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)

    cid = (chat_id or str(_uuid.uuid4())).strip()
    vault_key = f"chats.{cid}"
    today = date.today().isoformat()

    # Auto-detect categories from tags if not provided
    if not categories:
        categories = _categorise_summary(tags, title)

    # Build frontmatter
    tags_yaml = "\n".join(f"- {t}" for t in tags) if tags else "[]"
    cats_yaml = "\n".join(f"- {c}" for c in categories) if categories else "- misc"
    frontmatter = (
        f"---\n"
        f"key: {vault_key}\n"
        f"type: chat_summary\n"
        f"title: {title}\n"
        f"created: {today}\n"
        f"source: claude_session\n"
        f"source_session: {_IMPORTER_SESSION_ID}\n"
        f"last_updated: '{today}'\n"
        f"retrieval_count: 0\n"
        f"correction_count: 0\n"
        f"categories:\n{cats_yaml}\n"
        f"tags:\n{tags_yaml}\n"
        f"related: []\n"
        f"---\n"
    )

    # Build body
    sections: list[str] = [f"# {title}\n"]
    sections.append(f"## Deep Summary\n\n{summary.strip()}\n")
    if key_facts:
        sections.append("## Key Facts\n\n" + "\n".join(f"- {f}" for f in key_facts) + "\n")
    if decisions:
        sections.append("## Decisions\n\n" + "\n".join(f"- {d}" for d in decisions) + "\n")
    if keywords:
        sections.append(f"## Keywords\n\n{keywords.strip()}\n")

    md_path = chats_dir / f"{cid}.md"
    md_path.write_text(frontmatter + "\n" + "\n".join(sections), encoding="utf-8")

    # Update FTS5 index immediately so it's searchable right away
    try:
        from .index import MemoryIndex
        from .encryption import read_text

        idx = MemoryIndex(vault, lambda p: read_text(config, p))
        idx.upsert_file(md_path)
    except Exception:
        pass

    return {
        "chat_id": cid,
        "key": vault_key,
        "file": str(md_path.relative_to(vault)).replace("\\", "/"),
        "categories": categories,
        "message": (
            f"Saved as chats/{cid}.md. " "Run memory_build_graph to wire it into the topic graph."
        ),
    }

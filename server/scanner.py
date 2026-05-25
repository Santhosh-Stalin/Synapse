from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .file_readers import SUPPORTED_EXTENSIONS, extract_text as _extract_file_text
from .graph import extract_code_graph

if TYPE_CHECKING:
    from .config import SynapseConfig

SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    "dist",
    "build",
    ".next",
    "release",
    "desktop-artifacts",
    "out",
    ".turbo",
    ".cache",
    "coverage",
}
SKIP_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".lock",
    ".ico",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".map",
    ".db",
    ".sqlite",
    ".exe",
    ".dll",
    ".so",
    ".bin",
    ".zip",
    ".tar",
    ".gz",
}
PRIORITY_NAMES = {
    "README.md",
    "readme.md",
    "README.txt",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "next.config.ts",
    "next.config.js",
    "vite.config.ts",
    "vite.config.js",
    "tsconfig.json",
    "config.yaml",
    "config.yml",
    "main.py",
    "server.py",
    "app.py",
    "index.ts",
    "index.js",
    "index.tsx",
}
PRIORITY_PATTERNS = [
    re.compile(
        r"^(main|index|app|server|entry|config|settings)\.(py|ts|js|tsx|jsx|yaml|yml|json)$"
    ),
    re.compile(r"^README", re.IGNORECASE),
    re.compile(r"^(electron|src)/(main|preload)\.(cjs|js|ts)$"),
]
SOURCE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".cs"}
EXTENDED_CODE_EXTENSIONS = {
    # JavaScript / TypeScript
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    # C / C++
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".h",
    ".hpp",
    # Other languages
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".kts",
    ".dart",
    ".scala",
    ".r",
    ".lua",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".sql",
    ".ex",
    ".exs",
    ".hs",
    ".clj",
    ".fs",
    ".fsx",
    ".vim",
    ".toml",
    ".ini",
    ".cfg",
}
PLAIN_TEXT_EXTENSIONS = {".txt"}
DATA_EXTENSIONS = {".json", ".md", ".txt", ".yaml", ".yml"}

MAX_FILE_BYTES = 12_000
MAX_TOTAL_BYTES = 500_000  # raised from 100KB to 500KB
_EXTRACT_WORKERS = 4
_FN_BATCH_SIZE = 8
_GEMMA_MODEL = "gemma-4-31b-it"


# ---------------------------------------------------------------------------
# Gemma client (mirrors ai_importer._gemma_complete)
# ---------------------------------------------------------------------------


def _gemma_complete(config: "SynapseConfig", system: str, user: str) -> str:
    if not config.gemini_api_key:
        raise RuntimeError("gemini_api_key required for code extraction")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise RuntimeError("google-genai not installed: pip install google-genai")

    client = genai.Client(api_key=config.gemini_api_key)
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text=system)]),
        types.Content(
            role="model",
            parts=[types.Part.from_text(text="Understood. Provide the file to analyze.")],
        ),
        types.Content(role="user", parts=[types.Part.from_text(text=user)]),
    ]
    chunks: list[str] = []
    for chunk in client.models.generate_content_stream(model=_GEMMA_MODEL, contents=contents):
        if chunk.text:
            chunks.append(chunk.text)
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Incremental hash tracking
# ---------------------------------------------------------------------------


def _file_hash(path: Path) -> str:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _load_hashes(graph_path: Path) -> dict[str, str]:
    """Load {rel_path: md5} from existing _graph.json."""
    if not graph_path.exists():
        return {}
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        return data.get("file_hashes", {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Public scan API
# ---------------------------------------------------------------------------


def scan_project(path_str: str) -> dict[str, Any]:
    """Raw file scan — returns tree + file contents, no AI extraction."""
    root = Path(path_str).expanduser().resolve()
    if not root.exists():
        return {"error": f"Path does not exist: {path_str}"}
    if not root.is_dir():
        return {"error": f"Path is not a directory: {path_str}"}

    result: dict[str, Any] = {
        "project_name": root.name,
        "root": str(root),
        "tree": _build_tree(root),
        "files": {},
        "detected": _detect_project(root),
    }

    budget = MAX_TOTAL_BYTES

    for candidate in _walk_priority(root):
        rel = str(candidate.relative_to(root)).replace("\\", "/")
        if rel in result["files"]:
            continue
        text = _read_capped(candidate, MAX_FILE_BYTES)
        if text is None:
            continue
        result["files"][rel] = text
        budget -= len(text)
        if budget <= 0:
            break

    if budget > 0:
        for candidate in _walk_source(root):
            rel = str(candidate.relative_to(root)).replace("\\", "/")
            if rel in result["files"]:
                continue
            ext = candidate.suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                text = f"[{ext.lstrip('.')} binary: {rel}]"
            else:
                text = _read_capped(candidate, min(MAX_FILE_BYTES, budget))
            if text is None:
                continue
            result["files"][rel] = text
            budget -= len(text)
            if budget <= 0:
                break

    return result


def scan_and_extract(config: "SynapseConfig", path_str: str) -> dict[str, Any]:
    """
    Scan project + Gemma-powered extraction.
    Incremental: only re-extracts files whose content changed since last scan.
    Indexes results into vault/_code_index.db for semantic search.
    """
    root = Path(path_str).expanduser().resolve()
    raw = scan_project(path_str)
    if "error" in raw:
        return raw

    if not config.gemini_api_key:
        return {**raw, "proposals": [], "error": "gemini_api_key required for code extraction"}

    project_name = raw["project_name"]
    project_slug = re.sub(r"[_\s]+", "-", project_name).lower()
    project_slug = re.sub(r"[^a-z0-9-]", "", project_slug).strip("-")
    project_key = f"projects.{project_slug}"

    # Incremental: load previous hashes
    graph_path = config.vault_path / "projects" / project_slug / "_graph.json"
    old_hashes = _load_hashes(graph_path)
    new_hashes: dict[str, str] = {}
    for rel in raw["files"]:
        p = root / rel
        if p.exists():
            new_hashes[rel] = _file_hash(p)

    changed = {rel for rel, h in new_hashes.items() if old_hashes.get(rel) != h}
    unchanged = set(raw["files"]) - changed
    skipped = len(unchanged)

    files_to_extract = {rel: content for rel, content in raw["files"].items() if rel in changed}

    # --- File-level AI extraction (Gemma) ---
    proposals: list[dict[str, Any]] = []
    total = len(files_to_extract)

    def _extract(item: tuple[str, str]) -> list[dict[str, Any]]:
        rel_path, content = item
        ext = Path(rel_path).suffix.lower()
        if ext in SUPPORTED_EXTENSIONS:
            real_path = root / rel_path
            rich_text = _extract_file_text(real_path)
            if not rich_text:
                return []
            header = f"[{ext.lstrip('.')} file: {rel_path}]\n\n"
            return _ai_extract_data_file(config, project_name, rel_path, header + rich_text)
        if (
            ext in DATA_EXTENSIONS
            or ext in PLAIN_TEXT_EXTENSIONS
            or ext in EXTENDED_CODE_EXTENSIONS
        ):
            return _ai_extract_data_file(config, project_name, rel_path, content)
        patch = _ai_extract_file(config, project_name, rel_path, content)
        return [patch] if patch else []

    with ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS) as pool:
        futures = {pool.submit(_extract, item): item[0] for item in files_to_extract.items()}
        done = 0
        for future in as_completed(futures):
            patches = future.result()
            done += 1
            print(
                f"[Synapse] {done}/{total} -> {len(patches)} patches: {futures[future]}", flush=True
            )
            proposals.extend(patches)

    # --- AST code graph (all files, not just changed) ---
    code_graph = extract_code_graph(raw["files"])

    # --- Function descriptions via Gemma (only for changed files' nodes) ---
    changed_file_ids = {
        re.sub(r"[_\s]+", "-", Path(rel).stem).lower().replace("_", "-") for rel in changed
    }
    fn_nodes = [
        n
        for n in code_graph.get("nodes", [])
        if n.get("type") in ("function", "class") and n.get("parent", "") in changed_file_ids
    ]

    fn_descriptions: dict[str, str] = {}
    batches = [fn_nodes[i : i + _FN_BATCH_SIZE] for i in range(0, len(fn_nodes), _FN_BATCH_SIZE)]

    with ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS) as pool:
        batch_futures = {
            pool.submit(_ai_describe_functions, config, project_name, b): i
            for i, b in enumerate(batches)
        }
        for future in as_completed(batch_futures):
            fn_descriptions.update(future.result())

    fn_proposals = _make_function_proposals(project_key, code_graph, fn_descriptions)
    proposals.extend(fn_proposals)

    # --- Persist graph + hashes ---
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_data = {**code_graph, "file_hashes": new_hashes}
    graph_path.write_text(json.dumps(graph_data, indent=2), encoding="utf-8")

    # --- Index into code_index.db (all nodes, fresh descriptions) ---
    from .diff import cleanup_stale_nodes

    cleanup_result = cleanup_stale_nodes(config, project_slug, code_graph)

    try:
        from .code_index import index_project

        # Build full description map: function descriptions + file-level (blank for non-functions)
        all_descriptions: dict[str, str] = {**fn_descriptions}
        index_project(
            config.vault_path, config.gemini_api_key, project_slug, code_graph, all_descriptions
        )
    except Exception as exc:
        print(f"[Synapse] code_index error: {exc}", flush=True)

    return {
        "project_name": project_name,
        "root": raw["root"],
        "detected": raw["detected"],
        "file_count": len(raw["files"]),
        "files_extracted": total,
        "files_skipped_unchanged": skipped,
        "function_nodes": len(fn_proposals),
        "stale_removed": cleanup_result["count"],
        "proposals": proposals,
    }


# ---------------------------------------------------------------------------
# AI extraction helpers (Gemma)
# ---------------------------------------------------------------------------

_DATA_SYSTEM_PROMPT = """\
You are a personal memory extractor. Analyze this data file and output a JSON ARRAY of memory patches — one patch per distinct topic. No markdown fences, no explanation.

Each patch must follow this format:
{
  "key": "<category>.<slug>",
  "content": "<rich markdown — see depth rules below>",
  "type": "note",
  "scope": "global",
  "weight": 0.8,
  "signal": "high_signal",
  "reason": "<one sentence: why this memory matters>"
}

Categories: identity.* | life.* | projects.* | patterns.* | work.*

DEPTH RULES — every patch must go 2 levels deep. Never write a vague one-liner.
Level 1 = topic. Level 2 = specific sub-components with exact names, values, techniques.
BAD:  "Uses various tools for security work."
GOOD: "Binary exploitation: 64-bit buffer overflows at offset 72, ret2win, ROP chains via pwntools."

Structure: ## headers + bullets, 150–400 words, specific names/values always.
Output raw JSON array only. Start with [ and end with ].\
"""

_FILE_SYSTEM_PROMPT = """\
You are a developer knowledge extractor. Analyze the source file and output exactly one JSON object — nothing else. No markdown fences, no explanation.

Required format:
{
  "key": "projects.<project_slug>.<file_slug>",
  "content": "<markdown, 4-12 sentences>",
  "type": "code",
  "scope": "global",
  "weight": 0.8,
  "signal": "high_signal",
  "reason": "<one sentence: why this file matters architecturally>"
}

Rules — go 2 levels deep, never generic:
- Level 1: what the file does. Level 2: HOW — name the exact mechanism.
- Name every exported function/class with its real name and actual role.
- Name hardcoded values: ports, paths, env vars, constants, timeouts.
- Name real imports from this project and why they're used.
- Name real external libraries and WHY (not just "uses Flask" but what route/purpose).
- If trivial (re-export or stub), set weight 0.3 and write 1 sentence.
BAD:  "Handles authentication for the app."
GOOD: "Exports useAuth hook: reads Firebase currentUser, wraps onAuthStateChanged, returns {user, loading, signOut}."

project_slug: exact project name, lowercase, hyphens.
file_slug: from path, hyphens (electron/main.cjs → electron-main).
Output raw JSON only.\
"""

_FN_BATCH_PROMPT = """\
You are a code analyst. For each function below you receive: id, signature, file, docstring (truncated), and source body.

Write a description that names SPECIFIC details — actual values, actual calls, actual side effects. Never paraphrase the function name.

Rules:
- Name what it CALLS (other functions, APIs, libraries) with real names
- Name what it RETURNS or WRITES (type, shape, where it goes)
- Name any CONSTANTS, PORTS, PATHS, or CONFIG values it uses
- If it has side effects (writes file, mutates state, sends request), say so explicitly
- 1-2 sentences MAX. Dense, specific, no fluff.

BAD: "Initializes the bridge process to facilitate communication."
GOOD: "Spawns loophole-bridge.exe on the port returned by findFreePort, storing the handle in bridgeProcess; restarts on exit with 1s delay."

Output a JSON array — nothing else. No markdown fences.
Format: [{"id": "<node_id>", "description": "<specific description>"}]
Output raw JSON only.\
"""


def _ai_describe_functions(
    config: "SynapseConfig", project_name: str, nodes: list[dict[str, Any]]
) -> dict[str, str]:
    if not nodes:
        return {}

    fn_lines = "\n".join(
        f"- id: {n['id']}\n"
        f"  sig: {n.get('signature') or n.get('label', n['id'])}\n"
        f"  file: {n.get('file', '')}\n"
        f"  doc: {n.get('docstring', '')[:100]}\n"
        f"  body:\n```\n{n.get('source', '')[:1500]}\n```"
        for n in nodes
    )
    user_msg = f"Project: {project_name}\n\nFunctions:\n{fn_lines}"

    try:
        raw = _gemma_complete(config, _FN_BATCH_PROMPT, user_msg)
    except Exception:
        return {}

    text = re.sub(r"^```[a-z]*\n?", "", raw)
    text = re.sub(r"\n?```$", "", text).strip()
    if not text.startswith("["):
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            return {}
        text = m.group()

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        return {}

    return {
        str(item["id"]): str(item["description"])
        for item in items
        if isinstance(item, dict) and item.get("id") and item.get("description")
    }


def _ai_extract_data_file(
    config: "SynapseConfig", project_name: str, rel_path: str, content: str
) -> list[dict[str, Any]]:
    user_msg = f"Project: {project_name}\nFile: {rel_path}\n\n```\n{content}\n```"
    try:
        raw = _gemma_complete(config, _DATA_SYSTEM_PROMPT, user_msg)
        return _parse_patches(raw)
    except Exception:
        return []


def _ai_extract_file(
    config: "SynapseConfig", project_name: str, rel_path: str, content: str
) -> dict[str, Any] | None:
    ext = Path(rel_path).suffix.lower()
    prompt = _DATA_SYSTEM_PROMPT if ext in DATA_EXTENSIONS else _FILE_SYSTEM_PROMPT
    user_msg = f"Project: {project_name}\nFile: {rel_path}\n\n```\n{content}\n```"
    try:
        raw = _gemma_complete(config, prompt, user_msg)
        return _parse_patch(raw) if raw else None
    except Exception:
        return None


def _ai_extract_file_fast(
    config: "SynapseConfig", project_name: str, rel_path: str, content: str
) -> dict[str, Any] | None:
    """Fast single-patch extraction for the incremental watcher."""
    return _ai_extract_file(config, project_name, rel_path, content)


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


def _parse_patch(raw: str) -> dict[str, Any] | None:
    text = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    text = re.sub(r"\n?```$", "", text).strip()
    if not text.startswith("{"):
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        text = m.group()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    key = str(data.get("key", "")).strip()
    content = str(data.get("content", "")).strip()
    if not key or not content:
        return None
    return {
        "key": key,
        "content": content,
        "type": str(data.get("type", "code")),
        "scope": str(data.get("scope", "global")),
        "weight": float(data.get("weight", 0.8)),
        "signal": str(data.get("signal", "high_signal")),
        "reason": str(data.get("reason", "Extracted from project scan.")),
    }


def _parse_patches(raw: str) -> list[dict[str, Any]]:
    text = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    text = re.sub(r"\n?```$", "", text).strip()
    if not text.startswith("["):
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            p = _parse_patch(raw)
            return [p] if p else []
        text = m.group()
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        p = _parse_patch(raw)
        return [p] if p else []
    if not isinstance(items, list):
        p = _parse_patch(raw)
        return [p] if p else []
    result = []
    for item in items:
        key = str(item.get("key", "")).strip()
        content = str(item.get("content", "")).strip()
        if not key or not content:
            continue
        result.append(
            {
                "key": key,
                "content": content,
                "type": str(item.get("type", "note")),
                "scope": str(item.get("scope", "global")),
                "weight": float(item.get("weight", 0.8)),
                "signal": str(item.get("signal", "high_signal")),
                "reason": str(item.get("reason", "")),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Function/class proposals
# ---------------------------------------------------------------------------


def _make_function_proposals(
    project_key: str, graph: dict[str, Any], descriptions: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    edges_from: dict[str, list[dict[str, Any]]] = {}
    for edge in graph.get("edges", []):
        edges_from.setdefault(edge["source"], []).append(edge)

    for node in graph.get("nodes", []):
        ntype = node.get("type")
        if ntype not in ("function", "class"):
            continue

        file_id = node.get("parent", "")
        node_id = node["id"]
        fn_slug = node_id[len(file_id) + 1 :] if node_id.startswith(file_id + "-") else node_id
        vault_key = f"{project_key}.{file_id}.{fn_slug}"

        parent_key = f"{project_key}.{file_id}"
        related: list[str] = [parent_key]
        for edge in edges_from.get(node_id, []):
            if edge.get("relation") == "calls":
                related.append(f"{project_key}.{edge['target']}")
        related = list(dict.fromkeys(related))[:5]

        sig = node.get("signature") or node.get("label", node_id)
        doc = node.get("docstring", "").strip()
        lineno = node.get("lineno", "?")
        rel_file = node.get("file", "")
        ai_desc = (descriptions or {}).get(node_id, "")

        lines = [f"**{sig}**"]
        if ai_desc:
            flagged = _is_vague(ai_desc, sig)
            lines.append(f"\n[LOW DETAIL] {ai_desc}" if flagged else f"\n{ai_desc}")
        elif doc:
            lines.append(f"\n{doc}")
        lines.append(f"\nDefined in `{rel_file}` at line {lineno}.")

        proposals.append(
            {
                "key": vault_key,
                "content": "\n".join(lines),
                "type": "code",
                "scope": "global",
                "weight": 0.6,
                "signal": "high_signal",
                "reason": f"{ntype} node from {rel_file}",
                "related": related,
            }
        )

    return proposals


def _is_vague(description: str, signature: str) -> bool:
    # Specific markers: backticks, paths (slash or letter.letter), camelCase
    if "`" in description or "/" in description or "\\" in description:
        return False
    if re.search(r"[a-zA-Z]\.[a-zA-Z]", description):  # path-like dot, not sentence period
        return False
    if re.search(r"[a-z][A-Z]", description):
        return False
    if re.search(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", description):
        return False
    if re.search(r"\b\d+\b", description):
        return False
    fn_name = re.sub(r"[^a-zA-Z0-9]", "", signature.split("(")[0].split()[-1]).lower()
    words = re.findall(r"[a-z]+", description.lower())
    fn_parts = set(re.findall(r"[a-z]+", fn_name))
    if fn_parts:
        overlap = sum(1 for w in words if w in fn_parts and len(w) > 3)
        if overlap / len(fn_parts) >= 0.6:
            return True
    return len(description.strip()) < 60


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _build_tree(root: Path, depth: int = 0, max_depth: int = 3) -> list[dict[str, Any]]:
    if depth >= max_depth:
        return []
    try:
        items = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return []
    entries = []
    for item in items:
        if item.name.startswith(".") or item.name in SKIP_DIRS:
            continue
        if item.is_dir():
            entries.append(
                {
                    "name": item.name,
                    "type": "dir",
                    "children": _build_tree(item, depth + 1, max_depth),
                }
            )
        elif item.suffix.lower() not in SKIP_EXTENSIONS:
            entries.append({"name": item.name, "type": "file"})
    return entries


def _detect_project(root: Path) -> dict[str, Any]:
    detected: dict[str, Any] = {"type": "unknown", "languages": [], "frameworks": []}
    ext_counts: dict[str, int] = {}
    for f in root.rglob("*"):
        if f.is_file() and not _in_skip_dir(f, root):
            ext = f.suffix.lower()
            if ext in SOURCE_EXTENSIONS:
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
    if ext_counts:
        detected["languages"] = sorted(ext_counts, key=lambda e: -ext_counts[e])

    if (root / "package.json").exists():
        detected["frameworks"].append("node")
        pkg = _read_capped(root / "package.json", 8000) or ""
        for fw in ("next", "electron", "react", "vite", "firebase"):
            if f'"{fw}"' in pkg:
                detected["frameworks"].append(fw)
    if (root / "requirements.txt").exists() or any(root.glob("*.py")):
        detected["frameworks"].append("python")
    if (root / "pyproject.toml").exists():
        detected["frameworks"].append("python")
    if (root / "Cargo.toml").exists():
        detected["frameworks"].append("rust")
    if detected["frameworks"]:
        detected["type"] = detected["frameworks"][0]
    return detected


def _walk_priority(root: Path):
    seen: set[str] = set()
    for f in root.rglob("*"):
        if not f.is_file() or _in_skip_dir(f, root):
            continue
        rel = str(f.relative_to(root)).replace("\\", "/")
        name = f.name
        depth = len(f.relative_to(root).parts)
        is_priority = (
            name in PRIORITY_NAMES
            or any(p.match(rel) for p in PRIORITY_PATTERNS)
            or (depth <= 2 and name in PRIORITY_NAMES)
        )
        if is_priority and rel not in seen:
            seen.add(rel)
            yield f


def _walk_source(root: Path):
    all_exts = (
        SOURCE_EXTENSIONS
        | DATA_EXTENSIONS
        | SUPPORTED_EXTENSIONS
        | EXTENDED_CODE_EXTENSIONS
        | PLAIN_TEXT_EXTENSIONS
    )
    candidates = []
    for f in root.rglob("*"):
        if not f.is_file() or _in_skip_dir(f, root):
            continue
        ext = f.suffix.lower()
        if ext not in all_exts:
            continue
        depth = len(f.relative_to(root).parts)
        size = f.stat().st_size
        type_score = 0 if ext in SOURCE_EXTENSIONS else 1
        candidates.append((type_score * 10_000_000 + depth * 10000 + size, f))
    candidates.sort(key=lambda x: x[0])
    for _, f in candidates:
        yield f


def _read_capped(path: Path, limit: int) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception:
        return None


def _in_skip_dir(path: Path, root: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.relative_to(root).parts)

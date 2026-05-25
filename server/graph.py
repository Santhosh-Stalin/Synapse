"""
Structural code graph extractor.
Parses Python (AST), TypeScript/JS, Go, and Rust (regex) to produce:
  - file nodes
  - function/class nodes with source body
  - edges: contains | imports_from | calls | exports
No LLM required. Pure static analysis.
Supported: .py (AST), .ts/.tsx/.js/.jsx/.cjs/.mjs (regex), .go (regex), .rs (regex)
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_code_graph(files: dict[str, str]) -> dict[str, Any]:
    """
    Given {rel_path: content}, return a graph dict:
    {
        "nodes": [...],
        "edges": [...]
    }
    Each function/class node includes "source" (body text, up to 3000 chars).
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: set[str] = set()

    for rel_path, content in files.items():
        file_id = path_to_node_id(rel_path)
        if file_id not in node_ids:
            nodes.append(
                {"id": file_id, "label": Path(rel_path).name, "file": rel_path, "type": "file"}
            )
            node_ids.add(file_id)

        ext = Path(rel_path).suffix.lower()
        if ext == ".py":
            _extract_python(file_id, rel_path, content, nodes, edges, node_ids)
        elif ext in {".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs"}:
            _extract_typescript(file_id, rel_path, content, nodes, edges, node_ids)
        elif ext == ".go":
            _extract_go(file_id, rel_path, content, nodes, edges, node_ids)
        elif ext == ".rs":
            _extract_rust(file_id, rel_path, content, nodes, edges, node_ids)

    # Build a set of all known node IDs for edge filtering
    known_ids = {n["id"] for n in nodes}

    # Filter: keep call edges only where both endpoints exist (reduces external-lib noise)
    # Keep all other edge types (imports_from, contains, exports) regardless
    edges = [
        e
        for e in edges
        if e["relation"] != "calls" or (e["source"] in known_ids and e["target"] in known_ids)
    ]

    return {"nodes": nodes, "edges": edges}


def path_to_node_id(rel_path: str) -> str:
    """
    electron/main.cjs  -> electron-main
    lib/server/bridge.ts -> lib-server-bridge
    server_control.py  -> server-control
    """
    p = Path(rel_path)
    parts = [*p.parts[:-1], p.stem]
    slug = "-".join(parts)
    slug = re.sub(r"[_\s]+", "-", slug)
    slug = re.sub(r"[^a-zA-Z0-9\-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-").lower()
    return slug


def node_id_to_vault_key(project_key: str, node_id: str) -> str:
    return f"{project_key}.{node_id.replace('-', '.')}"


def function_vault_key(project_key: str, file_id: str, fn_slug: str) -> str:
    return f"{project_key}.{file_id}.{fn_slug}"


def file_edges(graph: dict[str, Any], file_id: str) -> list[dict[str, Any]]:
    return [e for e in graph["edges"] if e["source"] == file_id or e["target"] == file_id]


def related_file_ids(graph: dict[str, Any], file_id: str, limit: int = 5) -> list[str]:
    weights = {"imports_from": 3, "calls": 2, "exports": 1, "contains": 0}
    scores: dict[str, float] = {}
    file_node_ids = {n["id"] for n in graph["nodes"] if n.get("type") == "file"}

    for e in graph["edges"]:
        src, tgt, rel = e["source"], e["target"], e.get("relation", "")
        w = weights.get(rel, 1)
        if src == file_id and tgt in file_node_ids and tgt != file_id:
            scores[tgt] = scores.get(tgt, 0) + w
        elif tgt == file_id and src in file_node_ids and src != file_id:
            scores[src] = scores.get(src, 0) + w

    return sorted(scores, key=lambda k: -scores[k])[:limit]


# ---------------------------------------------------------------------------
# Python extractor (AST-based)
# ---------------------------------------------------------------------------


def _extract_python(
    file_id: str,
    rel_path: str,
    content: str,
    nodes: list,
    edges: list,
    node_ids: set,
) -> None:
    try:
        tree = ast.parse(content, filename=rel_path)
    except SyntaxError:
        return

    # Collect all top-level and nested definitions
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mod_id = _mod_to_id(node.module)
            edges.append({"source": file_id, "target": mod_id, "relation": "imports_from"})

        elif isinstance(node, ast.Import):
            for alias in node.names:
                mod_id = _mod_to_id(alias.name)
                edges.append({"source": file_id, "target": mod_id, "relation": "imports_from"})

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_slug = _name_to_slug(node.name)
            fn_id = f"{file_id}-{fn_slug}"
            if fn_id not in node_ids:
                sig = _py_signature(node)
                doc = ast.get_docstring(node) or ""
                source = _get_source(content, node)
                nodes.append(
                    {
                        "id": fn_id,
                        "label": f"{node.name}()",
                        "file": rel_path,
                        "type": "function",
                        "parent": file_id,
                        "signature": sig,
                        "docstring": doc[:200],
                        "source": source,
                        "lineno": node.lineno,
                        "lineno_end": getattr(node, "end_lineno", node.lineno),
                    }
                )
                node_ids.add(fn_id)
                edges.append({"source": file_id, "target": fn_id, "relation": "contains"})

                # Extract call edges from function body
                for child in ast.walk(node):
                    if child is node:
                        continue
                    if isinstance(child, ast.Call):
                        called_id = _resolve_call(child, file_id)
                        if called_id and called_id != fn_id:
                            edges.append(
                                {"source": fn_id, "target": called_id, "relation": "calls"}
                            )

        elif isinstance(node, ast.ClassDef):
            cls_slug = _name_to_slug(node.name)
            cls_id = f"{file_id}-{cls_slug}"
            if cls_id not in node_ids:
                source = _get_source(content, node)
                nodes.append(
                    {
                        "id": cls_id,
                        "label": node.name,
                        "file": rel_path,
                        "type": "class",
                        "parent": file_id,
                        "source": source[:1500],
                        "lineno": node.lineno,
                        "lineno_end": getattr(node, "end_lineno", node.lineno),
                    }
                )
                node_ids.add(cls_id)
                edges.append({"source": file_id, "target": cls_id, "relation": "contains"})


def _resolve_call(call_node: ast.Call, file_id: str) -> str | None:
    """Try to resolve a Call node to a node ID in the same file."""
    func = call_node.func
    if isinstance(func, ast.Name):
        return f"{file_id}-{_name_to_slug(func.id)}"
    if isinstance(func, ast.Attribute):
        # e.g. self.foo() → file_id-foo; obj.method() → file_id-method
        return f"{file_id}-{_name_to_slug(func.attr)}"
    return None


def _get_source(content: str, node: ast.AST) -> str:
    """Extract source text for a node using ast.get_source_segment (Python 3.8+)."""
    try:
        src = ast.get_source_segment(content, node) or ""
        return src[:3000]
    except Exception:
        return ""


def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = []
    for arg in node.args.args:
        if arg.annotation:
            type_str = ast.unparse(arg.annotation)
            args.append(f"{arg.arg}: {type_str}")
        else:
            args.append(arg.arg)

    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")

    ret = ""
    if node.returns:
        ret = f" -> {ast.unparse(node.returns)}"

    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}def {node.name}({', '.join(args)}){ret}"


def _mod_to_id(module: str) -> str:
    return re.sub(r"[._\s]+", "-", module).lower()


def _name_to_slug(name: str) -> str:
    return re.sub(r"_+", "-", name).lower()


# ---------------------------------------------------------------------------
# TypeScript / JavaScript extractor (regex-based)
# ---------------------------------------------------------------------------

# Named function declarations (exported or not)
_TS_FN = re.compile(r"""(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[(<]""")
# Arrow / const exports: export const foo = ..., export const foo: React.FC = ...
_TS_ARROW = re.compile(r"""export\s+(?:const|let|var)\s+(\w+)(?:\s*:\s*[\w<>\[\]|&., ]+)?\s*[=]""")
# Non-exported const/let arrow functions: const handleKey = (key: string) => ...
# re.MULTILINE so ^ anchors at each line start — m.start() lands on the line itself, not the preceding \n
_TS_CONST_FN = re.compile(
    r"""^[ \t]*(?:const|let)\s+(\w+)\s*(?::\s*[\w<>\[\]|&., ]+)?\s*=\s*(?:async\s+)?\(""",
    re.MULTILINE,
)
# Class declarations
_TS_CLASS = re.compile(r"""(?:export\s+)?(?:abstract\s+)?class\s+(\w+)""")
# Default export function
_TS_DEFAULT_FN = re.compile(r"""export\s+default\s+(?:async\s+)?function\s+(\w+)""")
# Import statements
_TS_IMPORT = re.compile(r"""from\s+['"](@?[\w/@.-]+)['"]""")
_TS_REQUIRE = re.compile(r"""require\s*\(\s*['"](@?[\w/@.-]+)['"]\s*\)""")
# Call expressions: functionName(
_TS_CALL = re.compile(r"""(?<!\w)(\w+)\s*\(""")


def _extract_typescript(
    file_id: str,
    rel_path: str,
    content: str,
    nodes: list,
    edges: list,
    node_ids: set,
) -> None:
    _SKIP_KW = {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "typeof",
        "instanceof",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "in",
        "of",
        "from",
        "import",
        "export",
        "class",
        "function",
        "async",
        "await",
        "yield",
        "super",
        "this",
    }

    # --- imports ---
    for pattern in (_TS_IMPORT, _TS_REQUIRE):
        for m in pattern.finditer(content):
            raw = m.group(1)
            mod_id = raw.lstrip("@").replace("/", "-").replace("_", "-").replace(".", "-").lower()
            mod_id = re.sub(r"-+", "-", mod_id).strip("-")
            edges.append({"source": file_id, "target": mod_id, "relation": "imports_from"})

    # --- named functions ---
    for m in _TS_FN.finditer(content):
        fn_name = m.group(1)
        if fn_name in _SKIP_KW:
            continue
        _add_ts_node(
            file_id, rel_path, content, fn_name, "function", m.start(), nodes, edges, node_ids
        )

    # --- default export function ---
    for m in _TS_DEFAULT_FN.finditer(content):
        fn_name = m.group(1)
        if fn_name not in _SKIP_KW:
            _add_ts_node(
                file_id, rel_path, content, fn_name, "function", m.start(), nodes, edges, node_ids
            )

    # --- arrow / const exports ---
    for m in _TS_ARROW.finditer(content):
        name = m.group(1)
        if name not in _SKIP_KW:
            _add_ts_node(
                file_id, rel_path, content, name, "function", m.start(), nodes, edges, node_ids
            )

    # --- non-exported const arrow functions ---
    for m in _TS_CONST_FN.finditer(content):
        name = m.group(1)
        if name not in _SKIP_KW:
            _add_ts_node(
                file_id, rel_path, content, name, "function", m.start(), nodes, edges, node_ids
            )

    # --- classes ---
    for m in _TS_CLASS.finditer(content):
        cls_name = m.group(1)
        if cls_name not in _SKIP_KW:
            _add_ts_node(
                file_id, rel_path, content, cls_name, "class", m.start(), nodes, edges, node_ids
            )

    # --- call edges (best-effort) ---
    known_fns = {n["id"] for n in nodes if n.get("parent") == file_id}
    for m in _TS_CALL.finditer(content):
        name = m.group(1)
        if name in _SKIP_KW or not name[0].islower():
            continue
        target_id = f"{file_id}-{_name_to_slug(name)}"
        if target_id in known_fns:
            # We don't know which source function this comes from in a regex pass,
            # so emit file-level call edge as approximation
            edges.append({"source": file_id, "target": target_id, "relation": "calls"})


def _add_ts_node(
    file_id: str,
    rel_path: str,
    content: str,
    name: str,
    ntype: str,
    pos: int,
    nodes: list,
    edges: list,
    node_ids: set,
) -> None:
    slug = _name_to_slug(name)
    node_id = f"{file_id}-{slug}"
    if node_id in node_ids:
        return
    lineno = content[:pos].count("\n") + 1
    # Extract approximate body: from this position to end of next matching brace block
    body = _extract_ts_body(content, pos)
    nodes.append(
        {
            "id": node_id,
            "label": f"{name}()" if ntype == "function" else name,
            "file": rel_path,
            "type": ntype,
            "parent": file_id,
            "source": body[:3000],
            "lineno": lineno,
            "lineno_end": lineno + body.count("\n"),
        }
    )
    node_ids.add(node_id)
    relation = "exports" if "export" in content[max(0, pos - 10) : pos + 10] else "contains"
    edges.append({"source": file_id, "target": node_id, "relation": relation})


def _find_opening_brace(content: str, start: int) -> int:
    """
    Scan the signature line(s) starting at `start` and return the position of
    the opening { that begins this function's body.  Returns -1 when:
      - an => appears before any { on the same statement (expression-body arrow)
      - a blank line or same-indent definition appears before any {
    Scans at most 10 lines so we don't bleed into the next function.
    """
    lines = content[start:].splitlines(keepends=True)
    if not lines:
        return -1
    base_indent = len(lines[0]) - len(lines[0].lstrip())
    pos = start
    for i, line in enumerate(lines[:10]):
        # First { wins — that's the body opener
        if "{" in line:
            return pos + line.index("{")
        # => without { on the same line → expression body (const f = x => x*2)
        if "=>" in line:
            return -1
        pos += len(line)
        # After the opening line: a blank line or a non-indented definition
        # means the signature ended without a body (type alias, interface, etc.)
        if i > 0:
            stripped = line.lstrip()
            if not stripped:
                return -1  # blank line — end of statement
            if len(line) - len(stripped) <= base_indent and stripped:
                return -1  # back to same indent — new definition
    return -1


def _extract_ts_body(content: str, start: int) -> str:
    """
    Find the opening brace after start and extract the balanced block.
    Falls back to _estimate_body_end() when:
      - no opening brace exists (expression-body arrow fns, interfaces)
      - brace never closes within the scan window (very long functions)
    """
    # Find the opening brace by scanning the signature line(s) only.
    # Stops as soon as it knows what kind of construct this is:
    #   hits "{"         → brace-body (regular fn or brace-arrow)
    #   hits "=>" sans { → expression-body arrow (const f = x => x * 2)
    #   hits blank line  → end of signature, no body found
    brace_start = _find_opening_brace(content, start)
    if brace_start == -1:
        return content[start : _estimate_body_end(content, start)]
    depth = 0
    for i in range(brace_start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return content[start : i + 1]
    # Brace opened but never closed (truncated file or parse error)
    return content[start : _estimate_body_end(content, start)]


def _estimate_body_end(content: str, start: int) -> int:
    """
    Rough estimator: scan forward from start and stop at the first line that
    looks like a new top-level definition at the same or lower indentation,
    or after MAX_LINES lines, whichever comes first.
    """
    MAX_LINES = 60
    # Patterns that signal a new top-level construct
    _TOP_DEF = re.compile(
        r"""^(?:export\s+)?(?:default\s+)?(?:async\s+)?"""
        r"""(?:function|class|const|let|var|type|interface|enum|"""
        r"""fn|pub\s+fn|pub\s+struct|pub\s+enum|struct|func)\s""",
        re.MULTILINE,
    )
    lines = content[start:].splitlines(keepends=True)
    if not lines:
        return start
    # Determine base indentation from the first non-empty line
    base_indent = len(lines[0]) - len(lines[0].lstrip())
    # pos always points to the end of the last line we're keeping
    # Start by including lines[0] (the definition line itself)
    pos = start + len(lines[0])
    for i, line in enumerate(lines[1:], 1):
        if i >= MAX_LINES:
            break
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        # A non-blank line at same or lower indent that starts a new definition
        if stripped and indent <= base_indent and _TOP_DEF.match(stripped):
            break
        pos += len(line)
    return min(pos, start + 3000)


# ---------------------------------------------------------------------------
# Go extractor (regex-based)
# ---------------------------------------------------------------------------

# func Name(...) or func (recv *Type) Name(...)
_GO_FN = re.compile(r"""func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(""")
# type Name struct { ... }
_GO_STRUCT = re.compile(r"""type\s+(\w+)\s+struct\s*\{""")
# import "pkg" or import ( "pkg" )
_GO_IMPORT = re.compile(r'''import\s+(?:\w+\s+)?["']([^"']+)["']''')


def _extract_go(
    file_id: str,
    rel_path: str,
    content: str,
    nodes: list,
    edges: list,
    node_ids: set,
) -> None:
    _SKIP = {"init", "main", "String", "Error"}

    # imports
    for m in _GO_IMPORT.finditer(content):
        mod_id = m.group(1).replace("/", "-").replace("_", "-").lower()
        mod_id = re.sub(r"-+", "-", mod_id).strip("-")
        edges.append({"source": file_id, "target": mod_id, "relation": "imports_from"})

    # structs
    for m in _GO_STRUCT.finditer(content):
        name = m.group(1)
        slug = _name_to_slug(name)
        node_id = f"{file_id}-{slug}"
        if node_id in node_ids:
            continue
        lineno = content[: m.start()].count("\n") + 1
        body = _extract_ts_body(content, m.start())
        nodes.append(
            {
                "id": node_id,
                "label": name,
                "file": rel_path,
                "type": "class",
                "parent": file_id,
                "source": body[:3000],
                "lineno": lineno,
                "lineno_end": lineno + body.count("\n"),
            }
        )
        node_ids.add(node_id)
        edges.append({"source": file_id, "target": node_id, "relation": "contains"})

    # functions
    for m in _GO_FN.finditer(content):
        name = m.group(1)
        if name in _SKIP:
            continue
        slug = _name_to_slug(name)
        node_id = f"{file_id}-{slug}"
        if node_id in node_ids:
            continue
        lineno = content[: m.start()].count("\n") + 1
        body = _extract_ts_body(content, m.start())
        nodes.append(
            {
                "id": node_id,
                "label": f"{name}()",
                "file": rel_path,
                "type": "function",
                "parent": file_id,
                "source": body[:3000],
                "lineno": lineno,
                "lineno_end": lineno + body.count("\n"),
            }
        )
        node_ids.add(node_id)
        edges.append({"source": file_id, "target": node_id, "relation": "contains"})


# ---------------------------------------------------------------------------
# Rust extractor (regex-based)
# ---------------------------------------------------------------------------

# pub fn name<...>( or fn name(
_RS_FN = re.compile(r"""(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*[<(]""")
# pub struct Name or struct Name
_RS_STRUCT = re.compile(r"""(?:pub\s+)?struct\s+(\w+)""")
# pub enum Name or enum Name
_RS_ENUM = re.compile(r"""(?:pub\s+)?enum\s+(\w+)""")
# use path::to::module;
_RS_USE = re.compile(r"""use\s+([\w:]+)""")


def _extract_rust(
    file_id: str,
    rel_path: str,
    content: str,
    nodes: list,
    edges: list,
    node_ids: set,
) -> None:
    _SKIP = {"new", "fmt", "from", "into", "default", "clone", "drop"}

    # imports
    for m in _RS_USE.finditer(content):
        raw = m.group(1)
        mod_id = raw.replace("::", "-").replace("_", "-").lower()
        mod_id = re.sub(r"-+", "-", mod_id).strip("-")
        edges.append({"source": file_id, "target": mod_id, "relation": "imports_from"})

    # structs
    for m in _RS_STRUCT.finditer(content):
        name = m.group(1)
        slug = _name_to_slug(name)
        node_id = f"{file_id}-{slug}"
        if node_id in node_ids:
            continue
        lineno = content[: m.start()].count("\n") + 1
        body = _extract_ts_body(content, m.start())
        nodes.append(
            {
                "id": node_id,
                "label": name,
                "file": rel_path,
                "type": "class",
                "parent": file_id,
                "source": body[:3000],
                "lineno": lineno,
                "lineno_end": lineno + body.count("\n"),
            }
        )
        node_ids.add(node_id)
        edges.append({"source": file_id, "target": node_id, "relation": "contains"})

    # enums
    for m in _RS_ENUM.finditer(content):
        name = m.group(1)
        slug = _name_to_slug(name)
        node_id = f"{file_id}-{slug}"
        if node_id in node_ids:
            continue
        lineno = content[: m.start()].count("\n") + 1
        body = _extract_ts_body(content, m.start())
        nodes.append(
            {
                "id": node_id,
                "label": name,
                "file": rel_path,
                "type": "class",
                "parent": file_id,
                "source": body[:3000],
                "lineno": lineno,
                "lineno_end": lineno + body.count("\n"),
            }
        )
        node_ids.add(node_id)
        edges.append({"source": file_id, "target": node_id, "relation": "contains"})

    # functions
    for m in _RS_FN.finditer(content):
        name = m.group(1)
        if name in _SKIP:
            continue
        slug = _name_to_slug(name)
        node_id = f"{file_id}-{slug}"
        if node_id in node_ids:
            continue
        lineno = content[: m.start()].count("\n") + 1
        body = _extract_ts_body(content, m.start())
        nodes.append(
            {
                "id": node_id,
                "label": f"{name}()",
                "file": rel_path,
                "type": "function",
                "parent": file_id,
                "source": body[:3000],
                "lineno": lineno,
                "lineno_end": lineno + body.count("\n"),
            }
        )
        node_ids.add(node_id)
        edges.append({"source": file_id, "target": node_id, "relation": "contains"})

"""
Compatibility Manifest Validator

Parses server/main.py @app.tool decorators and compares against manifest.json.
Exits 0 if they match, 1 if any tool is added/removed/renamed.

Usage:
    python Diagnostics/check_manifest.py
    python Diagnostics/check_manifest.py --manifest path/to/manifest.json
    python Diagnostics/check_manifest.py --update  # regenerate manifest.json from current code
"""
from __future__ import annotations

import ast
import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _extract_tools_from_code() -> list[dict]:
    src = (ROOT / "server" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    tools = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            is_tool = (isinstance(func, ast.Attribute) and func.attr == "tool") or \
                      (isinstance(func, ast.Name) and func.id == "tool")
            if not is_tool:
                continue
            tool_name = ""
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    tool_name = kw.value.value
            if not tool_name:
                tool_name = node.name
            params = [a.arg for a in node.args.args if a.arg != "self"]
            tools.append({"name": tool_name, "params": params})
    return sorted(tools, key=lambda t: t["name"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=str(ROOT / "manifest.json"))
    parser.add_argument("--update", action="store_true", help="Regenerate manifest.json from code")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    live_tools = _extract_tools_from_code()

    if args.update:
        existing = {}
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        existing["tools"] = live_tools
        manifest_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        print(f"[manifest] Updated {manifest_path} with {len(live_tools)} tools.")
        return

    if not manifest_path.exists():
        print(f"[manifest] ERROR: {manifest_path} not found. Run with --update to create it.")
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared_tools = {t["name"]: t["params"] for t in manifest.get("tools", [])}
    live_tool_map = {t["name"]: t["params"] for t in live_tools}

    added = sorted(set(live_tool_map) - set(declared_tools))
    removed = sorted(set(declared_tools) - set(live_tool_map))
    changed = sorted(
        n for n in set(live_tool_map) & set(declared_tools)
        if live_tool_map[n] != declared_tools[n]
    )

    ok = not added and not removed and not changed

    if added:
        print(f"[manifest] ADDED (not in manifest): {added}")
    if removed:
        print(f"[manifest] REMOVED (in manifest but not in code): {removed}")
    if changed:
        for name in changed:
            print(f"[manifest] CHANGED: {name}")
            print(f"  manifest: {declared_tools[name]}")
            print(f"  code:     {live_tool_map[name]}")

    if ok:
        print(f"[manifest] OK — {len(live_tools)} tools match manifest.json ({manifest.get('version', '?')})")
    else:
        print(f"\n[manifest] FAIL — run with --update to regenerate manifest.json")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
Synapse CLI — run pipeline commands from the terminal.

Usage:
  python -m server.cli triage <input_folder>
  python -m server.cli full-import <export_folder>
  python -m server.cli format-claude <export_folder>
  python -m server.cli rebuild-index
  python -m server.cli index-status
"""
from __future__ import annotations

import json
import sys

try:
    import typer
except ImportError:
    print("typer not installed. Run: pip install typer", file=sys.stderr)
    sys.exit(1)

app = typer.Typer(help="Synapse memory pipeline CLI", no_args_is_help=True)


def _cfg():
    from .config import load_config
    return load_config()


@app.command()
def triage(
    input_folder: str = typer.Argument(..., help="Path to conversations_jsonl folder"),
    output_folder: str = typer.Option("", "--output", "-o", help="Output folder (default: synapse_filtered_chats/)"),
    force_review: bool = typer.Option(False, "--force", "-f", help="Re-run AI review even if cached"),
    workers: int = typer.Option(3, "--workers", "-w", help="Parallel worker threads"),
):
    """AI triage: keep/skip/redflag conversations before importing."""
    from .functions import memory_triage
    result = memory_triage(_cfg(), input_folder, output_folder, force_review, workers=workers)
    print(json.dumps(result, indent=2))
    if "error" in result:
        raise typer.Exit(1)


@app.command(name="full-import")
def full_import(
    export_folder: str = typer.Argument(..., help="Path to Claude.ai export folder (contains conversations.json)"),
    owner_name: str = typer.Option("", "--owner", help="Override detected owner name"),
    skip_triage: bool = typer.Option(False, "--skip-triage", help="Skip triage step (no privacy filter)"),
    sync: bool = typer.Option(False, "--sync", help="Run rebuild-index and build-graph synchronously (slower)"),
):
    """One-command pipeline: format → triage → import → rebuild-index → build-graph."""
    from .functions import memory_full_import
    result = memory_full_import(_cfg(), export_folder, owner_name, skip_triage, not sync)
    print(json.dumps(result, indent=2))
    if "error" in result:
        raise typer.Exit(1)


@app.command(name="format-claude")
def format_claude(
    export_folder: str = typer.Argument(..., help="Path to Claude.ai export folder"),
    output_folder: str = typer.Option("", "--output", "-o", help="Output folder"),
    no_markdown: bool = typer.Option(False, "--no-markdown", help="Skip writing Markdown preview files"),
):
    """Convert Claude.ai export (conversations.json) to monthly JSONL files for triage."""
    from .functions import memory_format_claude_export
    result = memory_format_claude_export(_cfg(), export_folder, output_folder, not no_markdown)
    print(json.dumps(result, indent=2))
    if "error" in result:
        raise typer.Exit(1)


@app.command(name="rebuild-index")
def rebuild_index_cmd(
    background: bool = typer.Option(False, "--background", "-b", help="Run in background thread"),
):
    """Rebuild the SQLite FTS5 + semantic index from scratch."""
    from .functions import rebuild_index
    result = rebuild_index(_cfg(), background=background)
    print(json.dumps(result, indent=2))


@app.command(name="index-status")
def index_status_cmd():
    """Show background job status and progress."""
    from .functions import memory_index_status
    print(json.dumps(memory_index_status(), indent=2))


@app.command(name="build-graph")
def build_graph_cmd(
    background: bool = typer.Option(False, "--background", "-b", help="Run in background thread"),
):
    """Build the topic graph over vault/chats/ nodes."""
    from .functions import memory_build_graph
    result = memory_build_graph(_cfg(), background=background)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    app()

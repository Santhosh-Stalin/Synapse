"""
Synapse — Claude Export Formatter (CLI)

Core logic lives in server/claude_formatter.py.

Usage:
    cd <synapse-root>
    python pipeline/AI_Summary/Claude_export_formatter.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.claude_formatter import format_claude_export

EXPORT_FOLDER = r"C:\Users\Sandy\Documents\claude_memories"
OUTPUT_FOLDER = str(ROOT / "synapse_extracted")


def main():
    result = format_claude_export(
        export_folder=EXPORT_FOLDER,
        output_folder=OUTPUT_FOLDER,
        write_markdown=True,
    )
    if "error" in result:
        print(f"ERROR: {result['error']}", flush=True)
        sys.exit(1)
    print(f"\nDone. {result['conversations_extracted']} conversations across {result['months']} months.")
    print(f"JSONL folder: {result['jsonl_folder']}")
    print(f"\nNext step: {result['next_step']}")


if __name__ == "__main__":
    main()

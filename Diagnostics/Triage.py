"""
Synapse Chat Triage — CLI entry point.

Reads config from environment and runs the triage pipeline.
Core logic lives in server/triage.py.

Usage:
    cd <synapse-root>
    set OPENROUTER_API_KEY=...
    set GROQ_API_KEY=...
    python Diagnostics/Triage.py
"""
import os
import sys
from pathlib import Path

# Allow running from repo root or Diagnostics/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.triage import run_triage

EXTRACTED_FOLDER = ROOT / "synapse_extracted"
CONVERSATIONS_JSONL_FOLDER = EXTRACTED_FOLDER / "conversations_jsonl"
OUTPUT_FOLDER = ROOT / "synapse_filtered_chats"


def main():
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    groq_key = os.environ.get("GROQ_API_KEY", "")

    if not openrouter_key:
        print("ERROR: Set OPENROUTER_API_KEY environment variable before running.", flush=True)
        sys.exit(1)
    if not groq_key:
        print("ERROR: Set GROQ_API_KEY environment variable before running.", flush=True)
        sys.exit(1)

    result = run_triage(
        input_folder=str(CONVERSATIONS_JSONL_FOLDER),
        output_folder=str(OUTPUT_FOLDER),
        openrouter_api_key=openrouter_key,
        groq_api_key=groq_key,
    )

    if "error" in result:
        print(f"ERROR: {result['error']}", flush=True)
        sys.exit(1)

    print("\nTRIAGE COMPLETE", flush=True)
    print(f"  Kept:       {result['kept']}", flush=True)
    print(f"  Skipped:    {result['skipped']}", flush=True)
    print(f"  Redflagged: {result['redflagged']}", flush=True)
    print(f"  Output:     {result['output_folder']}", flush=True)
    print(f"\nNext step: {result['next_step']}", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Indexer hook for spickzettel-search.

  index_hook.py current   (Stop / PreCompact / SessionEnd): index the current transcript
  index_hook.py all       (SessionStart, async): incremental backfill of all transcripts
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import sessiondb  # noqa: E402


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "current"
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    conn, _ = sessiondb.connect()
    try:
        if mode == "all":
            sessiondb.index_all(conn)
        elif data.get("transcript_path"):
            sessiondb.index_file(conn, Path(data["transcript_path"]).expanduser())
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # indexing must never disturb the session
        print(f"spickzettel-search: indexing failed: {exc}", file=sys.stderr)
    sys.exit(0)

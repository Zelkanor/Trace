#!/usr/bin/env python3
"""Explicitly initialize the TRACE SQLite database (the only place it is created).

    uv run --package api python scripts/init_db.py

Uses TRACE_DB_PATH from the API settings, resolved against the repository root.
"""

from pathlib import Path

from app.persistence.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    raise SystemExit(main(default_workspace_root=REPO_ROOT))


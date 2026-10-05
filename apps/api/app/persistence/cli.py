"""Explicit database initialization command.

    uv run --package api python scripts/init_db.py
    python -m app.persistence.cli --workspace-root /app      # inside the API container

Creates the database file and parent directory if absent, enables WAL, and
creates the initial schema (version 1) on a new database. Idempotent for an
already-initialized database; refuses (without modifying) any newer, unversioned
or incompatible one. No network, model, or document activity.
"""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from app.core.config import get_settings
from app.persistence.database import PersistenceError, initialize_database


def main(argv: list[str] | None = None, *, default_workspace_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=default_workspace_root,
        help="absolute workspace root that relative TRACE_DB_PATH values resolve against",
    )
    args = parser.parse_args(argv)

    root: Path | None = args.workspace_root
    if root is None or not root.is_absolute():
        print("error: an absolute --workspace-root is required", file=sys.stderr)
        return 2

    try:
        database = initialize_database(get_settings(), root)
    except ValidationError as exc:
        print(f"error: invalid settings: {exc.error_count()} problem(s); see apps/api/.env", file=sys.stderr)
        return 2
    except PersistenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        print(f"database ready: {database.path} (schema version {database.schema_version}, WAL)")
    finally:
        database.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


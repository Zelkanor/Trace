#!/usr/bin/env python3
"""Export the shared JSON Schema artifacts from the Pydantic contract models.

    uv run --package api python scripts/export_contracts.py          # write
    uv run --package api python scripts/export_contracts.py --check  # verify only

The schemas are generated from the models alone: no settings, environment
values, clocks or machine paths are involved. `--check` never writes.
"""

import argparse
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.domain.schemas.records import DocumentRecord, EvidenceSpan, SourceRecord, canonical_json

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "packages" / "shared" / "contracts"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

CONTRACTS: dict[str, type[BaseModel]] = {
    "source.schema.json": SourceRecord,
    "document.schema.json": DocumentRecord,
    "evidence-span.schema.json": EvidenceSpan,
}


def build_schemas() -> dict[str, str]:
    """Return ``{filename: file text}`` with sorted keys and a trailing newline."""
    files: dict[str, str] = {}
    for filename, model in CONTRACTS.items():
        schema: dict[str, Any] = {"$schema": JSON_SCHEMA_DIALECT, **model.model_json_schema()}
        files[filename] = canonical_json(schema, indent=2) + "\n"
    return files


def _write(output_dir: Path, files: dict[str, str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, text in files.items():
        (output_dir / filename).write_bytes(text.encode("utf-8"))
        print(f"wrote {filename}")


def _check(output_dir: Path, files: dict[str, str]) -> int:
    stale = []
    for filename, text in files.items():
        path = output_dir / filename
        if not path.is_file() or path.read_bytes() != text.encode("utf-8"):
            stale.append(filename)
    if stale:
        print(f"contract schemas out of date: {', '.join(stale)}", file=sys.stderr)
        print("run: uv run --package api python scripts/export_contracts.py", file=sys.stderr)
        return 1
    print("contract schemas up to date")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="fail if schemas differ; do not write files"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)

    files = build_schemas()
    if args.check:
        return _check(args.output_dir, files)
    _write(args.output_dir, files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]  # apps/api/tests/api/integration -> repository root
SCRIPT = REPO_ROOT / "scripts" / "export_contracts.py"
CHECKED_IN = REPO_ROOT / "packages" / "shared" / "contracts"
FILENAMES = ["source.schema.json", "document.schema.json", "evidence-span.schema.json"]


@pytest.fixture(scope="module")
def exporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_contracts", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_export_is_deterministic(exporter: ModuleType, tmp_path: Path) -> None:
    assert exporter.build_schemas() == exporter.build_schemas()

    first, second = tmp_path / "one", tmp_path / "two"
    assert exporter.main(["--output-dir", str(first)]) == 0
    assert exporter.main(["--output-dir", str(second)]) == 0
    for name in FILENAMES:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_export_does_not_depend_on_environment(
    exporter: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = exporter.build_schemas()
    monkeypatch.setenv("SEC_USER_AGENT", "private-agent@example.test")
    monkeypatch.setenv("TRACE_DB_PATH", "/private/path.sqlite3")
    monkeypatch.chdir(tmp_path)
    after = exporter.build_schemas()
    assert before == after

    blob = "".join(after.values())
    for private in (str(REPO_ROOT), str(Path.home()), "private-agent", "/private/", "sqlite"):
        assert private not in blob


def test_checked_in_schemas_are_current(exporter: ModuleType) -> None:
    assert exporter.main(["--check"]) == 0


def test_check_mode_fails_without_rewriting(
    exporter: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert exporter.main(["--check", "--output-dir", str(tmp_path)]) == 1  # missing files
    assert list(tmp_path.iterdir()) == []

    assert exporter.main(["--output-dir", str(tmp_path)]) == 0
    drifted = tmp_path / "document.schema.json"
    drifted.write_text("{}\n")
    assert exporter.main(["--check", "--output-dir", str(tmp_path)]) == 1
    assert drifted.read_text() == "{}\n"
    assert "document.schema.json" in capsys.readouterr().err


def test_exported_schemas_describe_the_contracts(exporter: ModuleType) -> None:
    schemas = {name: json.loads(text) for name, text in exporter.build_schemas().items()}

    for schema in schemas.values():
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_version"]["const"] == "trace-core-v0.1"
        assert "schema_version" in schema["required"]

    source = schemas["source.schema.json"]
    assert source["required"] == ["schema_version", "source_id", "source_family"]
    assert source["$defs"]["SourceFamily"]["enum"] == [
        "SEC_REGULATORY",
        "ISSUER_OFFICIAL",
        "NEWS_WIRE",
        "NEWS_OUTLET",
        "REPLAY_SOCIAL",
        "UNKNOWN",
    ]
    assert {"type": "null"} in source["properties"]["independence_group_id"]["anyOf"]

    document = schemas["document.schema.json"]
    assert set(document["required"]) == {
        "schema_version",
        "document_id",
        "source_id",
        "published_at",
        "observed_at",
        "retrieved_at",
        "raw_text",
        "content_sha256",
        "is_replay",
        "is_synthetic",
    }
    assert {"type": "null"} in document["properties"]["published_at"]["anyOf"]
    assert not any(
        {"type": "null"} in document["properties"][field].get("anyOf", [])
        for field in ("observed_at", "retrieved_at")
    )

    span = schemas["evidence-span.schema.json"]
    assert set(span["required"]) == {
        "schema_version",
        "document_id",
        "document_content_sha256",
        "start_char",
        "end_char",
        "quote",
    }
    assert span["properties"]["start_char"]["type"] == "integer"


def test_exported_files_are_sorted_utf8_with_final_newline(exporter: ModuleType) -> None:
    for name, text in exporter.build_schemas().items():
        assert text.endswith("}\n") and not text.endswith("\n\n")
        assert "\r" not in text
        assert json.dumps(json.loads(text), sort_keys=True, indent=2, ensure_ascii=False) + "\n" == text
        assert not re.search(r"[A-Za-z]:\\\\|/home/|/Users/", text), name


def test_script_runs_as_a_command() -> None:
    import subprocess

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr

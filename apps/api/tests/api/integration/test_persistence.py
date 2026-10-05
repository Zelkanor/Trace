"""Persistence integration tests. Every database is an isolated temporary file."""

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import PrivateAttr
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import Settings
from app.domain.schemas.enums import RunMode, SourceFamily
from app.domain.schemas.records import (
    SCHEMA_VERSION,
    DocumentRecord,
    SourceRecord,
    compute_content_sha256,
)
from app.persistence import (
    DB_SCHEMA_VERSION,
    Database,
    EvidenceStore,
    MissingReference,
    PersistenceUnavailable,
    RecordConflict,
    SchemaVersionError,
    StoredRecordInvalid,
    initialize_database,
)
from app.persistence.models import Base, StoredTimestampError, format_utc, parse_utc

T0 = datetime(2024, 3, 1, 12, 0, 0, 123456, tzinfo=UTC)
EXPECTED_TABLES = {"ingestion_runs", "sources", "documents"}


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


def make_source(source_id: str = "src-1", **overrides: object) -> SourceRecord:
    fields: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_id": source_id,
        "source_family": SourceFamily.NEWS_WIRE,
    }
    fields.update(overrides)
    return SourceRecord(**fields)


def make_document(
    document_id: str = "doc-1",
    source_id: str = "src-1",
    raw_text: str = "Acme Corp announced a recall.",
    **overrides: object,
) -> DocumentRecord:
    fields: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "document_id": document_id,
        "source_id": source_id,
        "source_url": f"https://example.test/{document_id}",
        "published_at": T0 - timedelta(hours=1),
        "observed_at": T0,
        "retrieved_at": T0 + timedelta(minutes=1),
        "raw_text": raw_text,
        "content_sha256": compute_content_sha256(raw_text),
        "is_replay": True,
        "is_synthetic": False,
    }
    fields.update(overrides)
    return DocumentRecord(**fields)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"  # deliberately not created


@pytest.fixture
def db_settings() -> Settings:
    return Settings(_env_file=None, app_env="test", api_port=3001, web_origin="http://localhost:3000")


@pytest.fixture
def make_db(db_settings: Settings, workspace: Path) -> Iterator[Callable[[], Database]]:
    opened: list[Database] = []

    def _make() -> Database:
        database = initialize_database(db_settings, workspace)
        opened.append(database)
        return database

    yield _make
    for database in opened:
        database.engine.dispose()


@pytest.fixture
def db(make_db: Callable[[], Database]) -> Database:
    return make_db()


@pytest.fixture
def store(db: Database) -> EvidenceStore:
    return EvidenceStore(db)


_run_counter = 0


def ingest(
    store: EvidenceStore,
    sources: list[SourceRecord],
    documents: list[DocumentRecord],
    run_id: str | None = None,
):
    global _run_counter
    _run_counter += 1
    return store.persist_batch_and_complete(
        run_id=run_id or f"run-{_run_counter}",
        mode=RunMode.REPLAY,
        started_at=T0,
        finished_at=T0 + timedelta(seconds=5),
        sources=sources,
        documents=documents,
    )


def scalar(db: Database, sql: str) -> object:
    with db.engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def counts(db: Database) -> dict[str, int]:
    return {t: int(scalar(db, f"SELECT count(*) FROM {t}")) for t in sorted(EXPECTED_TABLES)}  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Initialization
# --------------------------------------------------------------------------- #


def test_import_does_not_create_database(tmp_path: Path) -> None:
    code = (
        "import app.persistence, app.persistence.models, app.persistence.repositories, "
        "app.persistence.cli, app.core.config"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={"TRACE_DB_PATH": str(tmp_path / "x" / "trace.sqlite3"), "PATH": ""},
        check=True,
    )
    assert list(tmp_path.iterdir()) == []


def test_nonexistent_parent_is_created_only_by_explicit_initialization(
    db_settings: Settings, workspace: Path
) -> None:
    path = db_settings.resolved_db_path(workspace)
    assert not workspace.exists()  # resolving the path creates nothing
    database = initialize_database(db_settings, workspace)
    try:
        assert path.is_file() and path.parent.is_dir()
    finally:
        database.engine.dispose()


def test_relative_workspace_root_is_rejected(db_settings: Settings) -> None:
    with pytest.raises(ValueError):
        initialize_database(db_settings, "relative/root")


def test_database_initializes_with_wal(db: Database) -> None:
    assert db.schema_version == DB_SCHEMA_VERSION == 1
    with db.engine.connect() as conn:
        tables = {
            name
            for (name,) in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
        }
    assert tables == EXPECTED_TABLES
    assert scalar(db, "PRAGMA user_version") == 1
    assert str(scalar(db, "PRAGMA journal_mode")).lower() == "wal"
    assert scalar(db, "PRAGMA integrity_check") == "ok"
    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []


def test_foreign_keys_enabled_on_new_connection(db: Database, store: EvidenceStore) -> None:
    # Two distinct pooled connections, both created by the application engine.
    first, second = db.engine.connect(), db.engine.connect()
    try:
        for conn in (first, second):
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
            assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
    finally:
        first.close()
        second.close()

    store.create_run("run-fk", RunMode.REPLAY, T0)
    document = make_document()
    with db.engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO documents VALUES (:id, :sv, 'missing-src', NULL, NULL, :ts, :ts, "
                "'x', :h, 1, 0, 'run-fk')"
            ),
            {
                "id": document.document_id,
                "sv": SCHEMA_VERSION,
                "ts": format_utc(T0),
                "h": compute_content_sha256("x"),
            },
        )


def test_initialization_is_idempotent(make_db: Callable[[], Database], db: Database) -> None:
    ingest(EvidenceStore(db), [make_source()], [make_document()])
    again = make_db()
    assert EvidenceStore(again).get_document("doc-1") == make_document()


def test_unsupported_newer_schema_version_fails_without_modification(
    db_settings: Settings, workspace: Path
) -> None:
    path = db_settings.resolved_db_path(workspace)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE precious (x)")
        raw.execute("INSERT INTO precious VALUES (1)")
        raw.execute("PRAGMA user_version = 2")
    raw.close()
    before = path.read_bytes()

    with pytest.raises(SchemaVersionError, match="newer"):
        initialize_database(db_settings, workspace)

    assert path.read_bytes() == before
    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT x FROM precious").fetchall() == [(1,)]
        assert check.execute("PRAGMA user_version").fetchone() == (2,)
    finally:
        check.close()


def test_unversioned_existing_database_is_not_relabelled(
    db_settings: Settings, workspace: Path
) -> None:
    path = db_settings.resolved_db_path(workspace)
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE foreign_app (x)")
    raw.commit()
    raw.close()
    before = path.read_bytes()

    with pytest.raises(SchemaVersionError, match="refusing to relabel"):
        initialize_database(db_settings, workspace)

    assert path.read_bytes() == before
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA user_version").fetchone() == (0,)
        assert check.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        check.close()


def test_version_one_database_with_wrong_tables_is_rejected(
    db_settings: Settings, workspace: Path
) -> None:
    path = db_settings.resolved_db_path(workspace)
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE sources (source_id TEXT PRIMARY KEY)")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    with pytest.raises(SchemaVersionError, match="missing tables"):
        initialize_database(db_settings, workspace)


def test_unopenable_database_is_reported_as_unavailable(
    db_settings: Settings, workspace: Path
) -> None:
    path = db_settings.resolved_db_path(workspace)
    path.mkdir(parents=True)  # a directory where the file should be
    with pytest.raises(PersistenceUnavailable):
        initialize_database(db_settings, workspace)

    (workspace / "blocker").write_text("not a directory")
    blocked = Settings(
        _env_file=None,
        app_env="test",
        api_port=3001,
        web_origin="http://localhost:3000",
        trace_db_path="blocker/inner/trace.sqlite3",
    )
    with pytest.raises(PersistenceUnavailable):
        initialize_database(blocked, workspace)


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #


def test_document_round_trip_after_restart(
    make_db: Callable[[], Database], db: Database, store: EvidenceStore
) -> None:
    source = make_source(
        "src-1",
        origin_id="origin-9",
        quoted_source_id="not-yet-ingested",  # not a local foreign key
        independence_group_id="group-A",
        source_tier=2,
    )
    document = make_document()
    result = ingest(store, [source], [document], run_id="run-restart")
    assert (result.documents_received, result.documents_inserted, result.documents_reused) == (1, 1, 0)

    assert store.get_source("src-1") == source
    assert store.get_document("doc-1") == document

    db.engine.dispose()  # "restart": close everything, reopen the file
    reopened = EvidenceStore(make_db())
    assert reopened.get_source("src-1") == source
    assert reopened.get_document("doc-1") == document
    assert reopened.list_documents() == [document]
    run = reopened.get_run("run-restart")
    assert run is not None and run.status == "COMPLETED"
    assert reopened.get_source("missing") is None
    assert reopened.get_document("missing") is None


def test_unknown_values_remain_null(db: Database, store: EvidenceStore) -> None:
    document = make_document(published_at=None, source_url=None)
    ingest(store, [make_source()], [document])

    stored = store.get_document("doc-1")
    assert stored is not None and stored.published_at is None and stored.source_url is None
    assert scalar(db, "SELECT published_at IS NULL FROM documents") == 1
    assert scalar(db, "SELECT independence_group_id IS NULL FROM sources") == 1
    assert scalar(db, "SELECT source_tier IS NULL FROM sources") == 1
    assert store.get_source("src-1") == make_source()  # unknown independence preserved


@pytest.mark.parametrize(
    "raw_text",
    [
        "",
        "  leading and trailing whitespace \n",
        "line1\r\nline2\rline3\nline4\u2028end",
        "Zürich · 東京 · 😀 · e\u0301 (combining) · \u202eRTL",
        "embedded\x00nul and \ttab",
    ],
)
def test_text_and_hash_survive_exactly(store: EvidenceStore, db: Database, raw_text: str) -> None:
    document = make_document(raw_text=raw_text)
    ingest(store, [make_source()], [document])
    stored = store.get_document("doc-1")
    assert stored is not None
    assert stored.raw_text == raw_text
    assert stored.content_sha256 == compute_content_sha256(raw_text)
    assert stored == document


def test_timestamps_are_stored_as_fixed_width_utc_text(db: Database, store: EvidenceStore) -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    document = make_document(
        observed_at=datetime(2024, 3, 1, 17, 30, 0, tzinfo=offset),  # = 12:00:00Z, no micros
        published_at=None,
    )
    ingest(store, [make_source()], [document], run_id="run-ts")
    assert scalar(db, "SELECT observed_at FROM documents") == "2024-03-01T12:00:00.000000Z"
    assert scalar(db, "SELECT started_at FROM ingestion_runs") == "2024-03-01T12:00:00.123456Z"
    stored = store.get_document("doc-1")
    assert stored is not None
    assert stored.observed_at == datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    assert stored.observed_at.utcoffset() == timedelta(0)

    with pytest.raises(IntegrityError), db.engine.begin() as conn:  # CHECK rejects other formats
        conn.execute(text("UPDATE documents SET observed_at = '2024-03-01 12:00:00'"))


# --------------------------------------------------------------------------- #
# Idempotency, conflicts, preserved provenance
# --------------------------------------------------------------------------- #


def test_identical_document_is_reused(db: Database, store: EvidenceStore) -> None:
    source, document = make_source(), make_document()
    first = ingest(store, [source], [document], run_id="run-a")
    second = ingest(store, [source], [document], run_id="run-b")

    assert (first.documents_inserted, first.documents_reused) == (1, 0)
    assert (second.documents_received, second.documents_inserted, second.documents_reused) == (1, 0, 1)
    assert counts(db) == {"documents": 1, "ingestion_runs": 2, "sources": 1}
    assert scalar(db, "SELECT first_ingestion_run_id FROM documents") == "run-a"
    for run_id in ("run-a", "run-b"):
        run = store.get_run(run_id)
        assert run is not None and run.status == "COMPLETED"
        assert run.documents_received == run.documents_inserted + run.documents_reused


def test_duplicate_inside_one_batch_is_reused(db: Database, store: EvidenceStore) -> None:
    document = make_document()
    result = ingest(store, [make_source(), make_source()], [document, document])
    assert (result.documents_received, result.documents_inserted, result.documents_reused) == (2, 1, 1)
    assert counts(db)["documents"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"raw_text": "Acme Corp denied the recall.", "content_sha256": None},
        {"source_url": "https://example.test/other"},
        {"published_at": None},
        {"is_synthetic": True},
        {"observed_at": T0 + timedelta(days=1)},
    ],
)
def test_conflicting_document_is_not_overwritten(
    db: Database, store: EvidenceStore, changes: dict[str, object]
) -> None:
    original = make_document()
    ingest(store, [make_source()], [original], run_id="run-1")

    changes = dict(changes)
    if "content_sha256" in changes:
        changes["content_sha256"] = compute_content_sha256(str(changes["raw_text"]))
    conflicting = make_document(**changes)
    with pytest.raises(RecordConflict) as caught:
        ingest(store, [make_source()], [conflicting], run_id="run-2")

    assert caught.value.kind == "document" and caught.value.record_id == "doc-1"
    assert caught.value.differing_fields
    assert caught.value.failure_recorded is True
    assert store.get_document("doc-1") == original
    assert counts(db) == {"documents": 1, "ingestion_runs": 2, "sources": 1}
    failed = store.get_run("run-2")
    assert failed is not None
    assert failed.status == "FAILED" and failed.failure_reason_code == "RECORD_CONFLICT"
    assert (failed.documents_inserted, failed.documents_reused) == (0, 0)


def test_conflicting_source_is_not_overwritten(db: Database, store: EvidenceStore) -> None:
    original = make_source(independence_group_id="group-A", source_tier=1)
    ingest(store, [original], [])
    with pytest.raises(RecordConflict) as caught:
        ingest(store, [make_source(independence_group_id="group-B", source_tier=1)], [])
    assert caught.value.kind == "source"
    assert caught.value.differing_fields == ("independence_group_id",)
    assert store.get_source("src-1") == original


def test_same_hash_different_documents_are_preserved(db: Database, store: EvidenceStore) -> None:
    sources = [make_source("wire"), make_source("outlet", source_family=SourceFamily.NEWS_OUTLET)]
    shared = "Identical syndicated text."
    docs = [
        make_document("doc-wire", "wire", shared),
        make_document("doc-outlet", "outlet", shared),
    ]
    result = ingest(store, sources, docs)
    assert result.documents_inserted == 2
    assert scalar(db, "SELECT count(DISTINCT content_sha256) FROM documents") == 1
    assert store.get_document("doc-wire") == docs[0]
    assert store.get_document("doc-outlet") == docs[1]


def test_run_id_reuse_is_a_conflict(db: Database, store: EvidenceStore) -> None:
    ingest(store, [make_source()], [make_document()], run_id="run-x")
    with pytest.raises(RecordConflict) as caught:
        ingest(store, [make_source()], [make_document()], run_id="run-x")
    assert caught.value.kind == "ingestion_run"
    assert caught.value.failure_recorded is False  # the existing run is not ours to mark failed
    run = store.get_run("run-x")
    assert run is not None and run.status == "COMPLETED"


def test_create_run_then_complete_adopts_running_run(store: EvidenceStore) -> None:
    created = store.create_run("run-1", RunMode.HYBRID, T0)
    assert created.status == "RUNNING" and created.finished_at is None
    assert store.create_run("run-1", RunMode.HYBRID, T0) == created  # idempotent
    with pytest.raises(RecordConflict):
        store.create_run("run-1", RunMode.LIVE, T0)

    result = store.persist_batch_and_complete(
        run_id="run-1",
        mode=RunMode.HYBRID,
        started_at=T0,
        finished_at=T0 + timedelta(seconds=1),
        sources=[make_source()],
        documents=[make_document()],
    )
    assert result.documents_inserted == 1
    run = store.get_run("run-1")
    assert run is not None and run.status == "COMPLETED"
    assert run.finished_at == T0 + timedelta(seconds=1)


# --------------------------------------------------------------------------- #
# Referential integrity and rollback
# --------------------------------------------------------------------------- #


def test_missing_source_is_rejected(db: Database, store: EvidenceStore) -> None:
    orphan = make_document("doc-orphan", "never-supplied")
    with pytest.raises(MissingReference) as caught:
        ingest(store, [make_source()], [orphan], run_id="run-orphan")

    assert caught.value.referenced_id == "never-supplied"
    assert store.get_document("doc-orphan") is None
    assert store.get_source("src-1") is None  # batch source rolled back too
    failed = store.get_run("run-orphan")
    assert failed is not None
    assert failed.status == "FAILED" and failed.failure_reason_code == "MISSING_REFERENCE"
    assert failed.documents_received == 1
    assert (failed.documents_inserted, failed.documents_reused) == (0, 0)


def test_referenced_rows_cannot_be_deleted(db: Database, store: EvidenceStore) -> None:
    ingest(store, [make_source()], [make_document()], run_id="run-1")
    for sql in (
        "DELETE FROM sources WHERE source_id = 'src-1'",
        "DELETE FROM ingestion_runs WHERE run_id = 'run-1'",
    ):
        with pytest.raises(IntegrityError), db.engine.begin() as conn:
            conn.execute(text(sql))
    assert counts(db) == {"documents": 1, "ingestion_runs": 1, "sources": 1}


def test_batch_failure_rolls_back(db: Database, store: EvidenceStore) -> None:
    existing = make_document("doc-1")
    ingest(store, [make_source("src-1")], [existing], run_id="run-1")
    before = counts(db)

    batch_docs = [
        make_document("doc-new-1", "src-2", "first new"),
        make_document("doc-new-2", "src-2", "second new"),
        make_document("doc-1", "src-1", "changed text for an existing ID"),  # fails last
    ]
    with pytest.raises(RecordConflict):
        ingest(
            store,
            [make_source("src-1"), make_source("src-2")],
            batch_docs,
            run_id="run-bad",
        )

    assert counts(db) == {**before, "ingestion_runs": before["ingestion_runs"] + 1}
    assert store.get_source("src-2") is None
    assert store.get_document("doc-new-1") is None and store.get_document("doc-new-2") is None
    assert store.get_document("doc-1") == existing
    run = store.get_run("run-bad")
    assert run is not None and run.status == "FAILED"
    assert run.documents_received == 3 and (run.documents_inserted, run.documents_reused) == (0, 0)


def test_database_error_midway_rolls_back_and_is_recorded(
    db: Database, store: EvidenceStore
) -> None:
    inserts = {"documents": 0}

    def fail_second_insert(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.startswith("INSERT INTO documents"):
            inserts["documents"] += 1
            if inserts["documents"] == 2:
                raise OperationalError(statement, parameters, Exception("disk I/O error"))

    event.listen(db.engine, "before_cursor_execute", fail_second_insert)
    with pytest.raises(PersistenceUnavailable) as caught:
        ingest(
            store,
            [make_source()],
            [make_document("doc-1"), make_document("doc-2", raw_text="two")],
            run_id="run-io",
        )
    event.remove(db.engine, "before_cursor_execute", fail_second_insert)

    assert caught.value.failure_recorded is True
    assert counts(db) == {"documents": 0, "ingestion_runs": 1, "sources": 0}
    run = store.get_run("run-io")
    assert run is not None
    assert run.status == "FAILED" and run.failure_reason_code == "PERSISTENCE_UNAVAILABLE"
    assert "disk" not in repr(run)  # no raw exception text stored


def test_unexpected_error_is_recorded_with_stable_code_and_reraised(
    db: Database, store: EvidenceStore
) -> None:
    def explode(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.startswith("INSERT INTO documents"):
            raise RuntimeError("secret token=abc123 in /home/user/path")

    event.listen(db.engine, "before_cursor_execute", explode)
    with pytest.raises(RuntimeError):
        ingest(store, [make_source()], [make_document()], run_id="run-bug")
    event.remove(db.engine, "before_cursor_execute", explode)

    run = store.get_run("run-bug")
    assert run is not None and run.failure_reason_code == "INTERNAL_ERROR"
    assert scalar(db, "SELECT count(*) FROM documents") == 0
    assert "abc123" not in str(scalar(db, "SELECT failure_reason_code FROM ingestion_runs"))


def test_failure_not_recorded_when_storage_stays_unavailable(
    db: Database, store: EvidenceStore
) -> None:
    def fail_everything_after_run_creation(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.startswith("INSERT INTO sources") or "FAILED" in tuple(parameters or ()):
            raise OperationalError(statement, parameters, Exception("database is locked"))

    event.listen(db.engine, "before_cursor_execute", fail_everything_after_run_creation)
    with pytest.raises(PersistenceUnavailable) as caught:
        ingest(store, [make_source()], [make_document()], run_id="run-down")
    event.remove(db.engine, "before_cursor_execute", fail_everything_after_run_creation)

    assert caught.value.failure_recorded is False  # honest: no FAILED audit entry exists
    run = store.get_run("run-down")
    assert run is not None and run.status == "RUNNING"
    assert counts(db)["documents"] == 0 and counts(db)["sources"] == 0


def test_completed_run_is_never_rewritten(store: EvidenceStore) -> None:
    ingest(store, [make_source()], [make_document()], run_id="run-done")
    with pytest.raises(RecordConflict):
        store.mark_run_failed("run-done", T0 + timedelta(minutes=1), "LATE_FAILURE")
    with pytest.raises(MissingReference):
        store.mark_run_failed("no-such-run", T0, "SOME_CODE")
    run = store.get_run("run-done")
    assert run is not None and run.status == "COMPLETED" and run.failure_reason_code is None


def test_mark_run_failed_for_a_running_run(store: EvidenceStore) -> None:
    store.create_run("run-1", RunMode.LIVE, T0)
    failed = store.mark_run_failed("run-1", T0 + timedelta(seconds=2), "UPSTREAM_TIMEOUT", documents_received=4)
    assert failed.status == "FAILED" and failed.failure_reason_code == "UPSTREAM_TIMEOUT"
    assert (failed.documents_received, failed.documents_inserted, failed.documents_reused) == (4, 0, 0)
    for bad_code in ("lowercase", "has space", "Trace: boom\n  File x", "", "X" * 65):
        store.create_run(f"run-{bad_code!r}", RunMode.LIVE, T0)
        with pytest.raises(ValueError):
            store.mark_run_failed(f"run-{bad_code!r}", T0, bad_code)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"run_id": "  "},
        {"started_at": datetime(2024, 3, 1, 12, 0)},  # naive
        {"finished_at": datetime(2024, 3, 1, 12, 0)},  # naive
        {"finished_at": T0 - timedelta(seconds=1)},  # before started_at
    ],
)
def test_invalid_batch_arguments_are_rejected_before_any_write(
    db: Database, store: EvidenceStore, kwargs: dict[str, object]
) -> None:
    args: dict[str, object] = {
        "run_id": "run-1",
        "mode": RunMode.REPLAY,
        "started_at": T0,
        "finished_at": T0 + timedelta(seconds=1),
        "sources": [make_source()],
        "documents": [make_document()],
    }
    args.update(kwargs)
    with pytest.raises(ValueError):
        store.persist_batch_and_complete(**args)  # type: ignore[arg-type]
    assert counts(db) == {"documents": 0, "ingestion_runs": 0, "sources": 0}


def test_run_check_constraints_reject_inconsistent_accounting(db: Database, store: EvidenceStore) -> None:
    ingest(store, [make_source()], [make_document()], run_id="run-1")
    for sql in (
        "UPDATE ingestion_runs SET documents_reused = 5",  # received != inserted + reused
        "UPDATE ingestion_runs SET documents_inserted = -1, documents_received = 0",
        "UPDATE ingestion_runs SET status = 'FAILED', failure_reason_code = 'X'",  # claims work
        "UPDATE ingestion_runs SET status = 'BOGUS'",
        "UPDATE ingestion_runs SET mode = 'BOGUS'",
    ):
        with pytest.raises(IntegrityError), db.engine.begin() as conn:
            conn.execute(text(sql))


# --------------------------------------------------------------------------- #
# Transactions and ordering
# --------------------------------------------------------------------------- #


def test_writes_begin_immediate_and_reads_do_not(db: Database, store: EvidenceStore) -> None:
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.startswith("BEGIN"):
            statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        ingest(store, [make_source()], [make_document()])
        write_begins = list(statements)
        statements.clear()
        store.get_document("doc-1")
        store.list_documents()
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)

    assert write_begins and set(write_begins) == {"BEGIN IMMEDIATE"}
    assert statements and set(statements) == {"BEGIN"}


def test_document_listing_is_stable(store: EvidenceStore) -> None:
    sources = [make_source()]
    docs = [
        make_document("doc-c", observed_at=T0),
        make_document("doc-a", observed_at=T0),  # same instant as doc-c: id breaks the tie
        make_document("doc-b", observed_at=T0 - timedelta(hours=1)),
        make_document("doc-d", observed_at=T0 + timedelta(hours=1)),
        make_document("doc-e", observed_at=T0 + timedelta(microseconds=1)),
    ]
    ingest(store, sources, docs)

    expected = ["doc-b", "doc-a", "doc-c", "doc-e", "doc-d"]
    listed = [d.document_id for d in store.list_documents()]
    assert listed == expected
    assert [d.document_id for d in store.list_documents()] == listed  # repeat: same order

    paged = [d.document_id for offset in range(0, 5, 2) for d in store.list_documents(2, offset)]
    assert paged == expected
    assert store.list_documents(limit=10, offset=5) == []
    with pytest.raises(ValueError):
        store.list_documents(limit=-1)


def test_listing_order_is_independent_of_insertion_order(store: EvidenceStore) -> None:
    docs = [make_document(f"doc-{i}", observed_at=T0 + timedelta(minutes=i % 3)) for i in range(6)]
    ingest(store, [make_source()], list(reversed(docs)))
    ids = [d.document_id for d in store.list_documents()]
    assert ids == sorted(ids, key=lambda i: (docs[int(i[-1])].observed_at, i))


# --------------------------------------------------------------------------- #
# Schema compatibility gates (never repair or relabel a damaged schema)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("table", "original", "replacement"),
    [
        ("sources", "source_family TEXT NOT NULL", "source_family TEXT"),
        ("sources", "source_tier INTEGER", "source_tier TEXT"),
        ("sources", "PRIMARY KEY (source_id),", ""),
        ("sources", "source_tier IS NULL OR source_tier > 0", "1 = 1"),
        ("documents", "ON DELETE RESTRICT", "ON DELETE CASCADE"),
        (
            "documents",
            "FOREIGN KEY(source_id) REFERENCES sources (source_id) ON DELETE RESTRICT",
            "CHECK (source_id IS NOT NULL)",
        ),
        ("documents", "CHECK (is_replay IN (0, 1))", "CHECK (is_replay IN (0, 1, 2))"),
        ("documents", "content_sha256 TEXT NOT NULL", "content_sha256 TEXT NOT NULL UNIQUE"),
        (
            "ingestion_runs",
            "documents_received = documents_inserted + documents_reused",
            "1 = 1",
        ),
    ],
)
def test_incompatible_version_one_table_is_rejected_without_changes(
    db: Database, db_settings: Settings, workspace: Path,
    table: str, original: str, replacement: str,
) -> None:
    db.engine.dispose()
    with closing(sqlite3.connect(db.path)) as raw:
        raw.execute("PRAGMA journal_mode = DELETE")
        ddl = raw.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        assert original in ddl
        # An empty isolated database; no application evidence is altered by this test.
        raw.execute(f'DROP TABLE "{table}"')
        raw.execute(ddl.replace(original, replacement, 1))
        for index in raw.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (table,)
        ).fetchall():
            assert index[0].startswith("sqlite_autoindex_")
        for index in Base.metadata.tables[table].indexes:
            columns = ", ".join(column.name for column in index.columns)
            raw.execute(f'CREATE INDEX "{index.name}" ON "{table}" ({columns})')
        raw.commit()
    before = db.path.read_bytes()

    with pytest.raises(SchemaVersionError, match="schema version"):
        initialize_database(db_settings, workspace)

    assert db.path.read_bytes() == before
    with closing(sqlite3.connect(db.path)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone() == (1,)
        assert raw.execute("PRAGMA journal_mode").fetchone() == ("delete",)


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP INDEX ix_documents_source_id",
        "DROP INDEX ix_documents_content_sha256; "
        "CREATE UNIQUE INDEX ix_documents_content_sha256 ON documents(content_sha256)",
        "DROP INDEX ix_documents_observed_at_document_id; "
        "CREATE INDEX ix_documents_observed_at_document_id ON documents(document_id, observed_at)",
        "DROP INDEX ix_sources_independence_group_id; "
        "CREATE INDEX ix_sources_independence_group_id ON sources(independence_group_id) "
        "WHERE independence_group_id IS NOT NULL",
        "CREATE TABLE unrelated (id TEXT)",
        "CREATE VIEW unrelated AS SELECT source_id FROM sources",
        "CREATE TRIGGER unrelated AFTER INSERT ON sources BEGIN DELETE FROM documents; END",
    ],
)
def test_incompatible_schema_objects_are_rejected_without_changes(
    db: Database, db_settings: Settings, workspace: Path, mutation: str,
) -> None:
    db.engine.dispose()
    with closing(sqlite3.connect(db.path)) as raw:
        raw.execute("PRAGMA journal_mode = DELETE")
        raw.executescript(mutation)
    before = db.path.read_bytes()
    with pytest.raises(SchemaVersionError):
        initialize_database(db_settings, workspace)
    assert db.path.read_bytes() == before


def test_unversioned_view_only_database_is_not_new(
    db_settings: Settings, workspace: Path,
) -> None:
    path = db_settings.resolved_db_path(workspace)
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("CREATE VIEW foreign_app AS SELECT 1 AS value")
    before = path.read_bytes()
    with pytest.raises(SchemaVersionError, match="refusing to relabel"):
        initialize_database(db_settings, workspace)
    assert path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Concurrent initialization/writes and WAL reader isolation
# --------------------------------------------------------------------------- #


def test_concurrent_initializers_accept_one_complete_schema(
    db_settings: Settings, workspace: Path,
) -> None:
    barrier = Barrier(4)

    def initialize() -> Path:
        barrier.wait(timeout=10)
        database = initialize_database(db_settings, workspace)
        try:
            assert scalar(database, "PRAGMA user_version") == 1
            assert scalar(database, "PRAGMA journal_mode") == "wal"
            assert counts(database) == {name: 0 for name in EXPECTED_TABLES}
            return database.path
        finally:
            database.engine.dispose()

    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: initialize(), range(4)))
    assert set(paths) == {db_settings.resolved_db_path(workspace)}


@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_writers_serialize_id_checks(
    db: Database, make_db: Callable[[], Database], conflicting: bool,
) -> None:
    stores = [EvidenceStore(db), EvidenceStore(make_db())]  # independent engine pools
    barrier = Barrier(2)
    documents = [make_document(), make_document(raw_text="different" if conflicting else "Acme Corp announced a recall.")]

    def write(index: int):
        barrier.wait(timeout=10)
        try:
            return ingest(stores[index], [make_source()], [documents[index]], f"writer-{index}")
        except RecordConflict as exc:
            assert exc.failure_recorded is True
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))

    assert counts(db) == {"documents": 1, "ingestion_runs": 2, "sources": 1}
    successes = [result for result in results if not isinstance(result, RecordConflict)]
    if conflicting:
        assert len(successes) == 1
        assert successes[0].documents_inserted == 1
        statuses = sorted(stores[0].get_run(f"writer-{i}").status for i in range(2))
        assert statuses == ["COMPLETED", "FAILED"]
    else:
        assert len(successes) == 2
        assert sum(result.documents_inserted for result in successes) == 1
        assert sum(result.documents_reused for result in successes) == 1
    first_run = scalar(db, "SELECT first_ingestion_run_id FROM documents")
    winner = int(str(first_run).split("-")[-1])
    assert stores[0].get_document("doc-1") == documents[winner]


def test_wal_reader_does_not_block_writer_and_keeps_snapshot(
    db: Database, make_db: Callable[[], Database],
) -> None:
    other_store = EvidenceStore(make_db())
    with db.engine.connect() as reader, reader.begin():
        assert reader.execute(text("SELECT count(*) FROM documents")).scalar() == 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(ingest, other_store, [make_source()], [make_document()], "writer").result(timeout=10)
        assert result.documents_inserted == 1
        assert reader.execute(text("SELECT count(*) FROM documents")).scalar() == 0
    assert scalar(db, "SELECT count(*) FROM documents") == 1


# --------------------------------------------------------------------------- #
# Strict UTC read-back and explicit failures for corrupted stored records
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "stored",
    [
        "2024-03-01T12:00:00Z",
        "2024-03-01T12:00:00.1Z",
        "2024-03-01T12:00:00.123456+00:00",
        "2024-03-01 12:00:00.123456Z",
        "2024-02-30T12:00:00.123456Z",
        "2024-03-01T24:00:00.123456Z",
        "2024-03-01T12:00:00.123456Z\n",
    ],
)
def test_noncanonical_or_invalid_stored_timestamps_are_rejected(stored: str) -> None:
    with pytest.raises(StoredTimestampError):
        parse_utc(stored)
    assert parse_utc(format_utc(T0)) == T0


@pytest.mark.parametrize(
    ("sql", "method", "args"),
    [
        ("UPDATE sources SET source_tier = 1.5", "get_source", ("src-1",)),
        ("UPDATE sources SET schema_version = 'private-marker'", "get_source", ("src-1",)),
        ("UPDATE documents SET raw_text = 'private-marker'", "get_document", ("doc-1",)),
        ("UPDATE documents SET raw_text = 'private-marker'", "list_documents", ()),
        ("UPDATE documents SET observed_at = '2024-99-01T12:00:00.000000Z'", "get_document", ("doc-1",)),
        ("UPDATE documents SET observed_at = '2024-99-01T12:00:00.000000Z'", "list_documents", ()),
        ("UPDATE ingestion_runs SET documents_received = 1.5, documents_inserted = 1.5", "get_run", ("original",)),
        ("UPDATE ingestion_runs SET started_at = '2024-00-01T12:00:00.000000Z'", "get_run", ("original",)),
    ],
)
def test_corrupt_records_raise_explicit_unavailable_without_payloads(
    db: Database, store: EvidenceStore, sql: str, method: str, args: tuple,
) -> None:
    ingest(store, [make_source()], [make_document()], "original")
    with db.engine.begin() as conn:
        conn.execute(text(sql))  # SQLite affinity/GLOB checks alone do not catch these cases.
    with pytest.raises(PersistenceUnavailable) as caught:
        getattr(store, method)(*args)
    assert isinstance(caught.value, StoredRecordInvalid)
    assert caught.value.failure_recorded is False
    assert "private-marker" not in str(caught.value)


def test_corrupt_document_rolls_back_new_batch_and_records_storage_failure(
    db: Database, store: EvidenceStore,
) -> None:
    original = make_document()
    ingest(store, [make_source()], [original], "original")
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE documents SET observed_at = '2024-99-01T12:00:00.000000Z'"))
    with pytest.raises(StoredRecordInvalid) as caught:
        ingest(store, [make_source("src-2")], [make_document("new", "src-2"), original], "new-run")
    assert caught.value.failure_recorded is True
    assert store.get_source("src-2") is None
    assert store.get_document("new") is None
    failed = store.get_run("new-run")
    assert failed.status == "FAILED" and failed.failure_reason_code == "PERSISTENCE_UNAVAILABLE"
    assert (failed.documents_received, failed.documents_inserted, failed.documents_reused) == (2, 0, 0)


def test_caller_private_metadata_does_not_change_domain_reuse(store: EvidenceStore) -> None:
    class CallerDocument(DocumentRecord):
        _private_note: str = PrivateAttr(default="caller-local")

    original = make_document()
    ingest(store, [make_source()], [original], "first")
    with_note = CallerDocument(**original.model_dump())
    assert with_note != original  # Pydantic object equality includes private attributes.
    result = ingest(store, [], [with_note], "second")
    assert (result.documents_inserted, result.documents_reused) == (0, 1)
    assert store.get_document("doc-1") == original


def test_failure_recording_flags_are_per_error_instance() -> None:
    first = PersistenceUnavailable("first")
    first.failure_recorded = True
    assert PersistenceUnavailable("second").failure_recorded is False


def test_database_failure_before_run_creation_does_not_claim_an_audit(
    db: Database, store: EvidenceStore,
) -> None:
    def fail_run_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO ingestion_runs"):
            raise OperationalError(statement, parameters, Exception("unavailable"))

    event.listen(db.engine, "before_cursor_execute", fail_run_insert)
    try:
        with pytest.raises(PersistenceUnavailable) as caught:
            ingest(store, [make_source()], [make_document()], "never-committed")
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_run_insert)
    assert caught.value.failure_recorded is False
    assert store.get_run("never-committed") is None
    assert counts(db) == {name: 0 for name in EXPECTED_TABLES}


@pytest.mark.parametrize("mode", list(RunMode))
def test_empty_batch_completes_with_reconciled_zero_counts(store: EvidenceStore, mode: RunMode) -> None:
    result = store.persist_batch_and_complete(
        run_id="empty", mode=mode, started_at=T0, finished_at=T0,
        sources=[], documents=[],
    )
    assert (result.documents_received, result.documents_inserted, result.documents_reused) == (0, 0, 0)
    run = store.get_run("empty")
    assert run.status == "COMPLETED" and run.mode == mode


# --------------------------------------------------------------------------- #
# Real initialization command and restart in another Python process
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli_env() -> dict[str, str]:
    return {
        **os.environ,
        "APP_ENV": "test", "API_PORT": "3001", "WEB_ORIGIN": "http://localhost:3000",
        "TRACE_DB_PATH": "nested/trace.sqlite3", "TRACE_MODE": "REPLAY",
        "GDELT_ENABLED": "false", "SEC_ENABLED": "false",
    }


def test_initialization_command_and_process_restart(
    tmp_path: Path, cli_env: dict[str, str],
) -> None:
    root = tmp_path / "workspace"
    script = Path(__file__).resolve().parents[5] / "scripts" / "init_db.py"
    command = [sys.executable, str(script), "--workspace-root", str(root)]
    initialized = subprocess.run(command, cwd=tmp_path, env=cli_env, capture_output=True, text=True)
    assert initialized.returncode == 0, initialized.stderr
    assert "schema version 1, WAL" in initialized.stdout
    cli_env["TEST_WORKSPACE_ROOT"] = str(root)
    source = make_source(quoted_source_id="external", independence_group_id=None)
    document = make_document(raw_text="東京\r\nexact text\x00", published_at=None)
    written = subprocess.run(
        [sys.executable, "-c", """
import json, os, sys
from datetime import datetime
from app.core.config import Settings
from app.domain.schemas.enums import RunMode
from app.domain.schemas.records import SourceRecord, DocumentRecord
from app.persistence import EvidenceStore, initialize_database
data = json.load(sys.stdin)
db = initialize_database(Settings(), os.environ['TEST_WORKSPACE_ROOT'])
try:
    EvidenceStore(db).persist_batch_and_complete(
        run_id='process-run', mode=RunMode.REPLAY,
        started_at=datetime.fromisoformat(data['timestamp']),
        finished_at=datetime.fromisoformat(data['timestamp']),
        sources=[SourceRecord.model_validate(data['source'])],
        documents=[DocumentRecord.model_validate(data['document'])],
    )
finally:
    db.engine.dispose()
"""],
        cwd=tmp_path, env=cli_env, capture_output=True, text=True,
        input=json.dumps({"source": source.model_dump(mode="json"), "document": document.model_dump(mode="json"), "timestamp": T0.isoformat()}),
    )
    assert written.returncode == 0, written.stderr
    # Re-running initialization must preserve the committed records.
    reopened = subprocess.run(command, cwd=tmp_path, env=cli_env, capture_output=True, text=True)
    assert reopened.returncode == 0, reopened.stderr
    read = subprocess.run(
        [sys.executable, "-c", """
import json, os
from app.core.config import Settings
from app.persistence import EvidenceStore, initialize_database
db = initialize_database(Settings(), os.environ['TEST_WORKSPACE_ROOT'])
try:
    store = EvidenceStore(db)
    print(json.dumps({'source': store.get_source('src-1').model_dump(mode='json'),
                     'document': store.get_document('doc-1').model_dump(mode='json')}))
finally:
    db.engine.dispose()
"""],
        cwd=tmp_path, env=cli_env, capture_output=True, text=True,
    )
    assert read.returncode == 0, read.stderr
    assert json.loads(read.stdout) == {
        "source": source.model_dump(mode="json"), "document": document.model_dump(mode="json"),
    }


def test_packaged_initialization_command_requires_absolute_root(
    tmp_path: Path, cli_env: dict[str, str],
) -> None:
    for args in ([], ["--workspace-root", "relative"]):
        result = subprocess.run(
            [sys.executable, "-m", "app.persistence.cli", *args],
            cwd=tmp_path, env=cli_env, capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "absolute --workspace-root" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_initialization_command_refuses_newer_database(
    tmp_path: Path, cli_env: dict[str, str],
) -> None:
    path = tmp_path / "newer.sqlite3"
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA user_version = 2")
    before = path.read_bytes()
    cli_env["TRACE_DB_PATH"] = str(path)
    result = subprocess.run(
        [sys.executable, "-m", "app.persistence.cli", "--workspace-root", str(tmp_path)],
        cwd=tmp_path, env=cli_env, capture_output=True, text=True,
    )
    assert result.returncode == 1 and "newer" in result.stderr
    assert "Traceback" not in result.stderr
    assert path.read_bytes() == before

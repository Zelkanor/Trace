"""Source/document reads and writes plus ingestion-run accounting.

`EvidenceStore` opens one short session per operation and always returns the
existing Pydantic records (never ORM entities). It never reads a clock, invents
IDs, normalizes text, fetches documents, or merges similar documents: storage
duplicate handling is exact-ID idempotency, not event clustering.

Insert policy (immutable evidence, never ``INSERT OR REPLACE``):

=================  ==============================  ==================
Existing record    Incoming record                 Result
=================  ==============================  ==================
no matching ID     valid record                    insert
same ID            identical canonical fields      reuse
same ID            different record                `RecordConflict`
different ID       same text hash                  stored separately
=================  ==============================  ==================
"""

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.schemas.enums import RunMode
from app.domain.schemas.records import DocumentRecord, SourceRecord
from app.persistence.database import (
    Database,
    PersistenceError,
    PersistenceUnavailable,
)
from app.persistence.models import DocumentRow, IngestionRunRow, SourceRow, StoredTimestampError

logger = logging.getLogger("app.persistence")

_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class FailureReason(StrEnum):
    """Stable ``failure_reason_code`` values. Never exception text or payloads."""

    RECORD_CONFLICT = "RECORD_CONFLICT"
    MISSING_REFERENCE = "MISSING_REFERENCE"
    PERSISTENCE_UNAVAILABLE = "PERSISTENCE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class IngestionRunStatus(StrEnum):
    """Local audit states; string equality and stored values stay unchanged."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RecordConflict(PersistenceError):
    """Same ID, different canonical record. The stored original is unchanged."""

    reason_code = FailureReason.RECORD_CONFLICT

    def __init__(self, kind: str, record_id: str, differing_fields: Sequence[str] = ()) -> None:
        self.kind = kind
        self.record_id = record_id
        self.differing_fields = tuple(differing_fields)
        detail = (
            f" (differing fields: {', '.join(self.differing_fields)})"
            if self.differing_fields
            else ""
        )
        # IDs are opaque caller data, so expose them as attributes, not message text.
        super().__init__(f"{kind} already exists with different content{detail}")


class MissingReference(PersistenceError):
    """A record references a source/run that is not stored."""

    reason_code = FailureReason.MISSING_REFERENCE

    def __init__(self, kind: str, record_id: str, referenced_id: str) -> None:
        self.kind = kind
        self.record_id = record_id
        self.referenced_id = referenced_id
        super().__init__(f"{kind} references a missing persisted record")


class StoredRecordInvalid(PersistenceUnavailable):
    """A stored row no longer validates as its domain record (corruption)."""


@dataclass(frozen=True)
class BatchResult:
    run_id: str
    documents_received: int
    documents_inserted: int
    documents_reused: int


@dataclass(frozen=True)
class IngestionRunRecord:
    run_id: str
    mode: RunMode
    status: IngestionRunStatus
    started_at: datetime
    finished_at: datetime | None
    documents_received: int
    documents_inserted: int
    documents_reused: int
    failure_reason_code: str | None


# --------------------------------------------------------------------------- #
# Row <-> record conversion and validation helpers
# --------------------------------------------------------------------------- #


def _source_to_row(record: SourceRecord) -> SourceRow:
    return SourceRow(
        source_id=record.source_id,
        schema_version=record.schema_version,
        source_family=record.source_family.value,
        origin_id=record.origin_id,
        quoted_source_id=record.quoted_source_id,
        independence_group_id=record.independence_group_id,
        source_tier=record.source_tier,
    )


def _source_from_row(row: SourceRow) -> SourceRecord:
    try:
        return SourceRecord(
            schema_version=row.schema_version,  # type: ignore[arg-type]
            source_id=row.source_id,
            source_family=row.source_family,  # type: ignore[arg-type]
            origin_id=row.origin_id,
            quoted_source_id=row.quoted_source_id,
            independence_group_id=row.independence_group_id,
            source_tier=row.source_tier,
        )
    except ValidationError as exc:
        raise StoredRecordInvalid("stored source record is invalid") from exc


def _document_to_row(record: DocumentRecord, first_run_id: str) -> DocumentRow:
    return DocumentRow(
        document_id=record.document_id,
        schema_version=record.schema_version,
        source_id=record.source_id,
        source_url=record.source_url,
        published_at=record.published_at,
        observed_at=record.observed_at,
        retrieved_at=record.retrieved_at,
        raw_text=record.raw_text,
        content_sha256=record.content_sha256,
        is_replay=record.is_replay,
        is_synthetic=record.is_synthetic,
        first_ingestion_run_id=first_run_id,
    )


def _document_from_row(row: DocumentRow) -> DocumentRecord:
    try:
        return DocumentRecord(
            schema_version=row.schema_version,  # type: ignore[arg-type]
            document_id=row.document_id,
            source_id=row.source_id,
            source_url=row.source_url,
            published_at=row.published_at,
            observed_at=row.observed_at,
            retrieved_at=row.retrieved_at,
            raw_text=row.raw_text,
            content_sha256=row.content_sha256,
            is_replay=row.is_replay,
            is_synthetic=row.is_synthetic,
        )
    except ValidationError as exc:
        raise StoredRecordInvalid("stored document record is invalid") from exc


def _run_from_row(row: IngestionRunRow) -> IngestionRunRecord:
    # These checks operate only on stored fields, never on caller arguments.
    # Keep ValueError handling local to the known field/enum validators.
    try:
        run_id = _require_run_id(row.run_id)
        mode = RunMode(row.mode)
        status = IngestionRunStatus(row.status)
        started = _require_utc(row.started_at, "started_at")
        finished = (
            None if row.finished_at is None else _require_utc(row.finished_at, "finished_at")
        )
        received = _require_count(row.documents_received, "documents_received")
        inserted = _require_count(row.documents_inserted, "documents_inserted")
        reused = _require_count(row.documents_reused, "documents_reused")
        code = (
            None
            if row.failure_reason_code is None
            else _require_reason_code(row.failure_reason_code)
        )
    except ValueError as exc:
        raise StoredRecordInvalid("stored ingestion run record is invalid") from exc

    coherent = (
        status == IngestionRunStatus.RUNNING
        and finished is None
        and code is None
        and received == inserted == reused == 0
    ) or (
        status == IngestionRunStatus.COMPLETED
        and finished is not None
        and code is None
        and received == inserted + reused
    ) or (
        status == IngestionRunStatus.FAILED
        and finished is not None
        and code is not None
        and inserted == reused == 0
    )
    if not coherent or (finished is not None and finished < started):
        raise StoredRecordInvalid("stored ingestion run record is invalid")

    return IngestionRunRecord(
        run_id=run_id,
        mode=mode,
        status=status,
        started_at=started,
        finished_at=finished,
        documents_received=received,
        documents_inserted=inserted,
        documents_reused=reused,
        failure_reason_code=code,
    )


def _differing_fields(stored: BaseModel, incoming: BaseModel) -> list[str]:
    # A reconstructed base record contains only frozen contract fields. Do not
    # use Pydantic object equality, which also compares private caller metadata.
    a, b = stored.model_dump(), incoming.model_dump()
    return sorted(name for name in a if a[name] != b[name])


def _require_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a nonblank string")
    return run_id


def _require_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _require_reason_code(code: object) -> str:
    text = str(code) if isinstance(code, StrEnum) else code
    if not isinstance(text, str) or not _REASON_CODE.fullmatch(text):
        raise ValueError("failure reason code must match ^[A-Z][A-Z0-9_]{0,63}$")
    return text


def _require_count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class EvidenceStore:
    """Concrete source/document repository over an initialized `Database`.

    Holds no session: each public method opens, uses and closes its own.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # -- ingestion runs ------------------------------------------------------ #

    def create_run(self, run_id: str, mode: RunMode, started_at: datetime) -> IngestionRunRecord:
        """Record (and commit) a RUNNING audit entry.

        Repeating the identical call while the run is still RUNNING returns the
        existing entry; any other existing run with that ID is a `RecordConflict`.
        """
        run_id = _require_run_id(run_id)
        mode = RunMode(mode)
        started = _require_utc(started_at, "started_at")
        try:
            with self._db.write_session_factory() as session, session.begin():
                row = self._ensure_running_run(session, run_id, mode, started)
                return _run_from_row(row)
        except StoredTimestampError as exc:
            raise StoredRecordInvalid("stored ingestion run timestamp is invalid") from exc
        except SQLAlchemyError as exc:
            raise _unavailable("create ingestion run") from exc

    def get_run(self, run_id: str) -> IngestionRunRecord | None:
        try:
            with self._db.session_factory() as session:
                row = session.get(IngestionRunRow, run_id)
                return None if row is None else _run_from_row(row)
        except StoredTimestampError as exc:
            raise StoredRecordInvalid("stored ingestion run timestamp is invalid") from exc
        except SQLAlchemyError as exc:
            raise _unavailable("read ingestion run") from exc

    def mark_run_failed(
        self,
        run_id: str,
        finished_at: datetime,
        reason_code: str,
        *,
        documents_received: int = 0,
    ) -> IngestionRunRecord:
        """Mark a RUNNING run FAILED in its own short transaction.

        Inserted/reused counts are forced to 0: a failed batch is rolled back and
        must not claim committed work. A completed/failed run is never rewritten.
        """
        run_id = _require_run_id(run_id)
        finished = _require_utc(finished_at, "finished_at")
        code = _require_reason_code(reason_code)
        received = _require_count(documents_received, "documents_received")
        try:
            with self._db.write_session_factory() as session, session.begin():
                row = session.get(IngestionRunRow, run_id)
                if row is None:
                    raise MissingReference("ingestion_run", run_id, run_id)
                stored = _run_from_row(row)
                if stored.status != IngestionRunStatus.RUNNING:
                    raise RecordConflict("ingestion_run", run_id, ["status"])
                if finished < stored.started_at:
                    raise ValueError("finished_at must not precede started_at")
                row.status = IngestionRunStatus.FAILED.value
                row.finished_at = finished
                row.documents_received = received
                row.documents_inserted = 0
                row.documents_reused = 0
                row.failure_reason_code = code
                session.flush()
                return _run_from_row(row)
        except StoredTimestampError as exc:
            raise StoredRecordInvalid("stored ingestion run timestamp is invalid") from exc
        except SQLAlchemyError as exc:
            raise _unavailable("mark ingestion run failed") from exc

    # -- batch --------------------------------------------------------------- #

    def persist_batch_and_complete(
        self,
        *,
        run_id: str,
        mode: RunMode,
        started_at: datetime,
        finished_at: datetime,
        sources: Iterable[SourceRecord],
        documents: Iterable[DocumentRecord],
    ) -> BatchResult:
        """Atomically store a source/document batch and complete its run.

        1. The RUNNING run is committed first (an existing identical RUNNING run,
           e.g. from `create_run`, is adopted).
        2. One ``BEGIN IMMEDIATE`` transaction inserts/reuses sources, verifies
           every document's source, inserts/reuses documents, and marks the run
           COMPLETED. Any failure rolls back *all* batch writes.
        3. On failure the run is marked FAILED in a separate short transaction.
           ``exc.failure_recorded`` reports honestly whether that succeeded.

        The caller supplies every timestamp; nothing here reads the clock.
        """
        run_id = _require_run_id(run_id)
        mode = RunMode(mode)
        started = _require_utc(started_at, "started_at")
        finished = _require_utc(finished_at, "finished_at")
        if finished < started:
            raise ValueError("finished_at must not precede started_at")
        source_records = tuple(sources)
        document_records = tuple(documents)
        for source in source_records:
            if not isinstance(source, SourceRecord):
                raise TypeError("sources must be SourceRecord instances")
        for document in document_records:
            if not isinstance(document, DocumentRecord):
                raise TypeError("documents must be DocumentRecord instances")

        self.create_run(run_id, mode, started)  # committed audit entry; failure propagates

        try:
            return self._write_batch(run_id, finished, source_records, document_records)
        except Exception as exc:
            error: Exception = exc
            if isinstance(exc, StoredTimestampError):
                error = StoredRecordInvalid("stored batch record timestamp is invalid")
            elif isinstance(exc, SQLAlchemyError):
                error = _unavailable("persist batch")
            reason = (
                exc.reason_code
                if isinstance(exc, RecordConflict | MissingReference)
                else FailureReason.PERSISTENCE_UNAVAILABLE
                if isinstance(error, PersistenceUnavailable)
                else FailureReason.INTERNAL_ERROR
            )
            recorded = False
            try:
                self.mark_run_failed(
                    run_id, finished, reason, documents_received=len(document_records)
                )
                recorded = True
            except Exception:  # storage may be unavailable; report honestly, do not mask
                logger.warning("could not record failure of ingestion run")
            if isinstance(error, PersistenceError):
                error.failure_recorded = recorded
            if error is exc:
                raise
            raise error from exc

    def _write_batch(
        self,
        run_id: str,
        finished_at: datetime,
        sources: tuple[SourceRecord, ...],
        documents: tuple[DocumentRecord, ...],
    ) -> BatchResult:
        with self._db.write_session_factory() as session, session.begin():
            run = session.get(IngestionRunRow, run_id)
            if run is None:
                raise RecordConflict("ingestion_run", run_id, ["status"])
            stored_run = _run_from_row(run)
            if stored_run.status != IngestionRunStatus.RUNNING:
                raise RecordConflict("ingestion_run", run_id, ["status"])
            if finished_at < stored_run.started_at:
                raise RecordConflict("ingestion_run", run_id, ["started_at"])

            for source in sources:
                self._insert_or_reuse_source(session, source)

            # Verify every reference before inserting any document.
            for document in documents:
                source = session.get(SourceRow, document.source_id)
                if source is None:
                    raise MissingReference("document", document.document_id, document.source_id)
                _source_from_row(source)

            inserted = reused = 0
            for document in documents:
                if self._insert_or_reuse_document(session, document, run_id):
                    inserted += 1
                else:
                    reused += 1

            run.status = IngestionRunStatus.COMPLETED.value
            run.finished_at = finished_at
            run.documents_received = len(documents)
            run.documents_inserted = inserted
            run.documents_reused = reused
            run.failure_reason_code = None
            session.flush()
            return BatchResult(
                run_id=run_id,
                documents_received=len(documents),
                documents_inserted=inserted,
                documents_reused=reused,
            )

    @staticmethod
    def _ensure_running_run(
        session: Session, run_id: str, mode: RunMode, started_at: datetime
    ) -> IngestionRunRow:
        row = session.get(IngestionRunRow, run_id)
        if row is None:
            row = IngestionRunRow(
                run_id=run_id,
                mode=mode.value,
                status=IngestionRunStatus.RUNNING.value,
                started_at=started_at,
                finished_at=None,
                documents_received=0,
                documents_inserted=0,
                documents_reused=0,
                failure_reason_code=None,
            )
            session.add(row)
            session.flush()
            return row
        stored = _run_from_row(row)
        differing = []
        if stored.status != IngestionRunStatus.RUNNING:
            differing.append("status")
        if stored.mode != mode:
            differing.append("mode")
        if stored.started_at != started_at:
            differing.append("started_at")
        if differing:
            raise RecordConflict("ingestion_run", run_id, differing)
        return row

    @staticmethod
    def _insert_or_reuse_source(session: Session, record: SourceRecord) -> bool:
        existing = session.get(SourceRow, record.source_id)
        if existing is None:
            session.add(_source_to_row(record))
            session.flush()  # make it visible to later lookups in the same batch
            return True
        stored = _source_from_row(existing)
        differing = _differing_fields(stored, record)
        if differing:
            raise RecordConflict("source", record.source_id, differing)
        return False

    @staticmethod
    def _insert_or_reuse_document(
        session: Session, record: DocumentRecord, first_run_id: str
    ) -> bool:
        existing = session.get(DocumentRow, record.document_id)
        if existing is None:
            session.add(_document_to_row(record, first_run_id))
            session.flush()
            return True
        stored = _document_from_row(existing)
        differing = _differing_fields(stored, record)
        if differing:
            raise RecordConflict("document", record.document_id, differing)
        return False

    # -- reads --------------------------------------------------------------- #

    def get_source(self, source_id: str) -> SourceRecord | None:
        try:
            with self._db.session_factory() as session:
                row = session.get(SourceRow, source_id)
                return None if row is None else _source_from_row(row)
        except StoredTimestampError as exc:
            raise StoredRecordInvalid("stored source record is invalid") from exc
        except SQLAlchemyError as exc:
            raise _unavailable("read source") from exc

    def get_document(self, document_id: str) -> DocumentRecord | None:
        try:
            with self._db.session_factory() as session:
                row = session.get(DocumentRow, document_id)
                return None if row is None else _document_from_row(row)
        except StoredTimestampError as exc:
            raise StoredRecordInvalid("stored document timestamp is invalid") from exc
        except SQLAlchemyError as exc:
            raise _unavailable("read document") from exc

    def list_documents(self, limit: int = 100, offset: int = 0) -> list[DocumentRecord]:
        """Documents ordered by ``observed_at`` ascending, then ``document_id`` ascending."""
        limit = _require_count(limit, "limit")
        offset = _require_count(offset, "offset")
        statement = (
            select(DocumentRow)
            .order_by(DocumentRow.observed_at.asc(), DocumentRow.document_id.asc())
            .limit(limit)
            .offset(offset)
        )
        try:
            with self._db.session_factory() as session:
                return [_document_from_row(row) for row in session.scalars(statement)]
        except StoredTimestampError as exc:
            raise StoredRecordInvalid("stored document timestamp is invalid") from exc
        except SQLAlchemyError as exc:
            raise _unavailable("list documents") from exc


def _unavailable(action: str) -> PersistenceUnavailable:
    return PersistenceUnavailable(f"database unavailable while trying to {action}")

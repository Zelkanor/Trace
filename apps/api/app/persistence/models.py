"""SQLAlchemy mappings for the initial persistence checkpoint.

Exactly three tables: ``ingestion_runs``, ``sources`` and ``documents``. Rows
are storage entities; they are never returned as domain objects (see
``repositories``). Nothing here generates IDs, reads a clock, or normalizes text.

Timestamps are stored as fixed-width UTC text, ``YYYY-MM-DDTHH:MM:SS.ffffffZ``,
so lexicographic order equals chronological order. SQLite does not enforce time
zone semantics, so the representation is enforced by `UtcText` on the way in and
by ``CHECK ... GLOB`` constraints at the database level.
"""

import re
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.schemas.enums import RunMode, SourceFamily

# Digit classes spelled out because SQLite GLOB has no quantifiers.
_UTC_TEXT_GLOB = (
    "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
    "T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'"
)
RUN_STATUSES = ("RUNNING", "COMPLETED", "FAILED")
_UTC_TEXT_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"
)


class StoredTimestampError(ValueError):
    """A stored timestamp is not fixed-width UTC text or has an invalid date/time."""


def format_utc(value: datetime) -> str:
    """Render an aware datetime as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC)."""
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """Inverse of `format_utc`; always returns an aware UTC datetime."""
    if not isinstance(value, str) or _UTC_TEXT_PATTERN.fullmatch(value) is None:
        raise StoredTimestampError("stored timestamp must be YYYY-MM-DDTHH:MM:SS.ffffffZ")
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError as exc:
        raise StoredTimestampError("stored timestamp has an invalid calendar date or time") from exc


class UtcText(TypeDecorator[datetime]):
    """Aware ``datetime`` <-> fixed-width UTC text. Naive datetimes are rejected."""

    impl = String(27)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        return None if value is None else format_utc(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        return None if value is None else parse_utc(value)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


class IngestionRunRow(Base):
    __tablename__ = "ingestion_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcText, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcText, nullable=True)
    documents_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    documents_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    documents_reused: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Stable machine code only (e.g. RECORD_CONFLICT). Never traces, secrets or payloads.
    failure_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("length(trim(run_id)) > 0", name="ck_ingestion_runs_run_id_nonblank"),
        CheckConstraint(
            _in_list("mode", tuple(m.value for m in RunMode)), name="ck_ingestion_runs_mode"
        ),
        CheckConstraint(_in_list("status", RUN_STATUSES), name="ck_ingestion_runs_status"),
        CheckConstraint(f"started_at GLOB {_UTC_TEXT_GLOB}", name="ck_ingestion_runs_started_at"),
        CheckConstraint(
            f"finished_at IS NULL OR finished_at GLOB {_UTC_TEXT_GLOB}",
            name="ck_ingestion_runs_finished_at",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_ingestion_runs_finished_after_started",
        ),
        CheckConstraint(
            "documents_received >= 0 AND documents_inserted >= 0 AND documents_reused >= 0",
            name="ck_ingestion_runs_counts_nonnegative",
        ),
        CheckConstraint(
            "failure_reason_code IS NULL OR ("
            "length(failure_reason_code) BETWEEN 1 AND 64 "
            "AND failure_reason_code NOT GLOB '*[^A-Z0-9_]*')",
            name="ck_ingestion_runs_failure_reason_code",
        ),
        # Status/field coherence. A failed (rolled-back) run never claims committed work;
        # a completed run reconciles received = inserted + reused.
        CheckConstraint(
            "(status = 'RUNNING' AND finished_at IS NULL AND failure_reason_code IS NULL"
            " AND documents_received = 0 AND documents_inserted = 0 AND documents_reused = 0)"
            " OR (status = 'COMPLETED' AND finished_at IS NOT NULL"
            " AND failure_reason_code IS NULL"
            " AND documents_received = documents_inserted + documents_reused)"
            " OR (status = 'FAILED' AND finished_at IS NOT NULL"
            " AND failure_reason_code IS NOT NULL"
            " AND documents_inserted = 0 AND documents_reused = 0)",
            name="ck_ingestion_runs_status_coherent",
        ),
    )


class SourceRow(Base):
    __tablename__ = "sources"

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_family: Mapped[str] = mapped_column(Text, nullable=False)
    origin_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Deliberately not a foreign key: quoted provenance may reference a source not yet ingested.
    quoted_source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SQL NULL means "unknown independence", never "independent".
    independence_group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("length(trim(source_id)) > 0", name="ck_sources_source_id_nonblank"),
        CheckConstraint("length(schema_version) > 0", name="ck_sources_schema_version"),
        CheckConstraint(
            _in_list("source_family", tuple(f.value for f in SourceFamily)),
            name="ck_sources_source_family",
        ),
        CheckConstraint("source_tier IS NULL OR source_tier > 0", name="ck_sources_source_tier"),
        Index("ix_sources_independence_group_id", "independence_group_id"),
    )


class DocumentRow(Base):
    __tablename__ = "documents"

    document_id: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sources.source_id", ondelete="RESTRICT"), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UtcText, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(UtcText, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(UtcText, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Not unique: different sources may publish identical text, and that is provenance.
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    is_replay: Mapped[bool] = mapped_column(
        Boolean(create_constraint=True, name="ck_documents_is_replay_bool"), nullable=False
    )
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean(create_constraint=True, name="ck_documents_is_synthetic_bool"), nullable=False
    )
    # Storage metadata only; not part of the frozen document JSON contract.
    first_ingestion_run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ingestion_runs.run_id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        CheckConstraint("length(trim(document_id)) > 0", name="ck_documents_document_id_nonblank"),
        CheckConstraint("length(schema_version) > 0", name="ck_documents_schema_version"),
        CheckConstraint(
            "length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_documents_content_sha256",
        ),
        CheckConstraint(
            f"published_at IS NULL OR published_at GLOB {_UTC_TEXT_GLOB}",
            name="ck_documents_published_at",
        ),
        CheckConstraint(f"observed_at GLOB {_UTC_TEXT_GLOB}", name="ck_documents_observed_at"),
        CheckConstraint(f"retrieved_at GLOB {_UTC_TEXT_GLOB}", name="ck_documents_retrieved_at"),
        Index("ix_documents_source_id", "source_id"),
        Index("ix_documents_content_sha256", "content_sha256"),
        Index("ix_documents_observed_at_document_id", "observed_at", "document_id"),
    )


TABLE_NAMES = frozenset(Base.metadata.tables)

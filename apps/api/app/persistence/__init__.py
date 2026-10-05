"""SQLite persistence for sources, documents and ingestion-run accounting.

Importing this package never creates an engine, file or directory.
"""

from app.persistence.database import (
    DB_SCHEMA_VERSION,
    Database,
    PersistenceError,
    PersistenceUnavailable,
    SchemaVersionError,
    initialize_database,
)
from app.persistence.repositories import (
    BatchResult,
    EvidenceStore,
    FailureReason,
    IngestionRunRecord,
    IngestionRunStatus,
    MissingReference,
    RecordConflict,
    StoredRecordInvalid,
)

__all__ = [
    "DB_SCHEMA_VERSION",
    "BatchResult",
    "Database",
    "EvidenceStore",
    "FailureReason",
    "IngestionRunRecord",
    "IngestionRunStatus",
    "MissingReference",
    "PersistenceError",
    "PersistenceUnavailable",
    "RecordConflict",
    "SchemaVersionError",
    "StoredRecordInvalid",
    "initialize_database",
]

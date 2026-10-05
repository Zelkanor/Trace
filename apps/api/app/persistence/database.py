"""Explicit SQLite engine/session setup and schema-version checking.

Nothing here runs at import time: no engine, file or directory exists until
`initialize_database` is called by application setup.

Connection policy (applied to every pooled connection):

* ``foreign_keys=ON`` (SQLite enforces foreign keys per connection, default OFF);
* ``busy_timeout=5000`` ms;
* the driver's implicit transaction handling is disabled so SQLAlchemy controls
  ``BEGIN`` explicitly: deferred for readers, ``BEGIN IMMEDIATE`` for the write
  session factory (the writer takes the lock *before* it checks existing rows, so
  check-then-insert cannot race another writer).

WAL is persistent in the database file. It is switched on once, outside any
transaction, after the schema compatibility check has accepted the file, so an
unsupported database is never altered. SQLite's durable defaults
(``synchronous``) are left untouched.
"""

import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import CheckConstraint, Connection, Engine, Table, create_engine, event, inspect
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.persistence.models import TABLE_NAMES, Base

DB_SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

_WRITE_OPTION = "trace_begin_immediate"
_SCHEMA_OBJECTS = frozenset(
    [("table", name) for name in TABLE_NAMES]
    + [("index", index.name) for table in Base.metadata.tables.values() for index in table.indexes]
)


class PersistenceError(Exception):
    """Base class for explicit persistence outcomes (not evidence or lifecycle states).

    ``failure_recorded`` is set by batch operations: True only when the failed
    ingestion run was durably marked FAILED. It stays False when storage was too
    unavailable to record that, so callers never over-claim an audit entry.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.failure_recorded = False


class PersistenceUnavailable(PersistenceError):
    """The database could not be opened, read or written."""


class SchemaVersionError(PersistenceError):
    """The file is unversioned/incompatible or was written by a newer schema.

    The file is never reset, relabelled or migrated automatically.
    """


@dataclass(frozen=True)
class Database:
    """Engine and session factories owned by application setup.

    Sessions are cheap and unshared: create one per operation/request. Never keep
    a session in a module global.
    """

    engine: Engine
    session_factory: "sessionmaker[Session]"
    write_session_factory: "sessionmaker[Session]"
    path: Path
    schema_version: int


def _configure_connection(dbapi_connection: sqlite3.Connection, _record: Any) -> None:
    # Python 3.11's sqlite3 would otherwise open implicit transactions of its own.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys = ON")
        row = cursor.execute("PRAGMA foreign_keys").fetchone()
        if row is None or row[0] != 1:
            raise PersistenceUnavailable("SQLite foreign key enforcement could not be enabled")
    finally:
        cursor.close()


def _begin(conn: Any) -> None:
    immediate = conn.get_execution_options().get(_WRITE_OPTION, False)
    conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")


def _build_engine(path: Path) -> Engine:
    url = URL.create("sqlite", database=str(path))
    engine = create_engine(url, connect_args={"timeout": BUSY_TIMEOUT_MS / 1000})
    event.listen(engine, "connect", _configure_connection)
    event.listen(engine, "begin", _begin)
    return engine


@contextmanager
def _raw_cursor(engine: Engine) -> Iterator[sqlite3.Cursor]:
    """A cursor on a pooled DBAPI connection, outside any application transaction."""
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            yield cursor
        finally:
            cursor.close()
    finally:
        raw.close()


def _schema_state(
    conn: Connection,
) -> tuple[int, frozenset[str], frozenset[tuple[str, str]]]:
    version = conn.exec_driver_sql("PRAGMA user_version").scalar_one()
    # Only implicit indexes are exempt; they are checked through index_list below.
    # In particular, a view-only database is not a new database, and LIKE's '_'
    # wildcard must not accidentally hide unrelated objects named 'sqliteX...'.
    objects = frozenset(
        (kind, name)
        for kind, name in conn.exec_driver_sql(
            "SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_autoindex_*'"
        )
    )
    tables = frozenset(name for kind, name in objects if kind == "table")
    return int(version), tables, objects


def _check_version(
    version: int, tables: frozenset[str], objects: frozenset[tuple[str, str]]
) -> bool:
    """Return True when the database is brand new (empty, unversioned).

    Raises `SchemaVersionError` for anything this code must not touch.
    """
    if version == 0:
        if objects:
            raise SchemaVersionError(
                "existing database has schema objects but no TRACE schema version "
                f"(user_version=0; objects: {sorted(objects)}); refusing to relabel it"
            )
        return True
    if version > DB_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema version {version} is newer than supported version "
            f"{DB_SCHEMA_VERSION}; upgrade the application"
        )
    if version < 0:
        raise SchemaVersionError(f"invalid database schema version {version}")
    if not TABLE_NAMES <= tables:
        raise SchemaVersionError(
            f"database claims schema version {version} but is missing tables: "
            f"{sorted(TABLE_NAMES - tables)}"
        )
    if tables != TABLE_NAMES:
        raise SchemaVersionError(
            f"database claims schema version {version} but has unexpected tables: "
            f"{sorted(tables - TABLE_NAMES)}"
        )
    if objects != _SCHEMA_OBJECTS:
        raise SchemaVersionError(
            f"database does not match schema version {version} "
            f"(schema objects differ: {sorted(objects ^ _SCHEMA_OBJECTS)})"
        )
    return False


def _sqlite_affinity(declared_type: str) -> str:
    """SQLite's documented affinity rules, in their required order."""
    declared_type = declared_type.upper()
    if "INT" in declared_type:
        return "INTEGER"
    if any(part in declared_type for part in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not declared_type or "BLOB" in declared_type:
        return "BLOB"
    if any(part in declared_type for part in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _verify_columns(conn: Connection) -> None:
    for table in Base.metadata.sorted_tables:
        quoted = conn.dialect.identifier_preparer.quote(table.name)
        # xinfo includes hidden/generated columns, unlike table_info.
        columns = {
            row[1]: row for row in conn.exec_driver_sql(f"PRAGMA table_xinfo({quoted})")
        }
        actual = set(columns)
        expected = {column.name for column in table.columns}
        if actual != expected:
            raise SchemaVersionError(
                f"table {table.name!r} does not match schema version {DB_SCHEMA_VERSION} "
                f"(columns differ: {sorted(actual ^ expected)})"
            )
        primary_keys = {
            column.name: ordinal for ordinal, column in enumerate(table.primary_key.columns, 1)
        }
        for column in table.columns:
            row = columns[column.name]
            expected_affinity = _sqlite_affinity(column.type.compile(dialect=conn.dialect))
            if (
                _sqlite_affinity(row[2]) != expected_affinity
                or bool(row[3]) != (not column.nullable)
                or row[4] is not None  # Version 1 has client defaults, not server defaults.
                or row[5] != primary_keys.get(column.name, 0)
                or row[6] != 0
            ):
                raise SchemaVersionError(
                    f"column {table.name}.{column.name} does not match schema version "
                    f"{DB_SCHEMA_VERSION} (type affinity, nullability, primary key or default differs)"
                )


def _verify_foreign_keys(conn: Connection, table: Table) -> None:
    quoted = conn.dialect.identifier_preparer.quote(table.name)
    actual = Counter(
        (row[1], row[2], row[3], row[4], row[5], row[6], row[7])
        for row in conn.exec_driver_sql(f"PRAGMA foreign_key_list({quoted})")
    )
    # The initial schema has only single-column, non-deferred foreign keys.
    # Ignore SQLite's arbitrary FK IDs, but retain sequence and both actions:
    # CASCADE/SET NULL (and even NO ACTION instead of RESTRICT) are incompatible.
    expected = Counter(
        (
            0,
            fk.column.table.name,
            fk.parent.name,
            fk.column.name,
            fk.onupdate or "NO ACTION",
            fk.ondelete or "NO ACTION",
            "NONE",
        )
        for fk in table.foreign_keys
    )
    if actual != expected:
        raise SchemaVersionError(
            f"table {table.name!r} does not match schema version {DB_SCHEMA_VERSION} "
            "(foreign keys or deletion restrictions differ)"
        )


def _verify_indexes(conn: Connection, table: Table) -> None:
    quoted = conn.dialect.identifier_preparer.quote(table.name)
    indexes = conn.exec_driver_sql(f"PRAGMA index_list({quoted})").fetchall()
    expected = {index.name: index for index in table.indexes}
    explicit = {row[1] for row in indexes if row[3] == "c"}
    primary = [row for row in indexes if row[3] == "pk"]
    if (
        explicit != set(expected)
        or len(primary) != 1
        or any(row[3] not in ("c", "pk") for row in indexes)
    ):
        raise SchemaVersionError(
            f"table {table.name!r} does not match schema version {DB_SCHEMA_VERSION} "
            "(indexes or unique constraints differ)"
        )
    for row in indexes:
        name, unique, origin, partial = row[1:5]
        if origin == "pk":
            expected_columns = [column.name for column in table.primary_key.columns]
            expected_unique = True
        else:
            index = expected[name]
            expected_columns = [column.name for column in index.columns]
            expected_unique = bool(index.unique)
        quoted_index = conn.dialect.identifier_preparer.quote(name)
        # xinfo verifies order, ascending direction, BINARY collation, and that
        # none of the required indexes was replaced by a partial/expression index.
        actual_columns = [
            (item[2], item[3], item[4])
            for item in conn.exec_driver_sql(f"PRAGMA index_xinfo({quoted_index})")
            if item[5]
        ]
        if (
            bool(unique) != expected_unique
            or partial
            or actual_columns != [(column, 0, "BINARY") for column in expected_columns]
        ):
            raise SchemaVersionError(
                f"index {name!r} does not match schema version {DB_SCHEMA_VERSION} "
                "(columns, ordering, collation, uniqueness or predicate differs)"
            )


def _verify_schema(conn: Connection) -> None:
    _verify_columns(conn)
    inspector = inspect(conn)  # Fresh reflection on each check, including after a writer waits.
    for table in Base.metadata.sorted_tables:
        _verify_foreign_keys(conn, table)
        _verify_indexes(conn, table)
        table_sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table.name,)
        ).scalar_one()
        # The mapped DDL has no comments (nor comment markers in literals).
        # Refuse them rather than letting CHECK reflection mistake commented-out
        # constraint text for a constraint SQLite actually enforces.
        if "--" in table_sql or "/*" in table_sql:
            raise SchemaVersionError(
                f"table {table.name!r} does not match schema version {DB_SCHEMA_VERSION} "
                "(commented DDL is not supported)"
            )
        # These clauses are absent from the mapped DDL and aren't fully exposed
        # by SQLite's structural PRAGMAs. Conservatively reject the keywords
        # instead of trying to parse their placement (also inside literals).
        if re.search(r"\bCOLLATE\b|\bDEFERRABLE\b|\bON\s+CONFLICT\b", table_sql, re.IGNORECASE):
            raise SchemaVersionError(
                f"table {table.name!r} does not match schema version {DB_SCHEMA_VERSION} "
                "(collation overrides, conflict policies or deferred foreign keys are not supported)"
            )
        expected_checks = Counter(
            (
                constraint.name,
                str(
                    constraint.sqltext.compile(
                        dialect=conn.dialect,
                        compile_kwargs={"literal_binds": True, "include_table": False},
                    )
                ).strip(),
            )
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        )
        actual_checks = Counter(
            (constraint["name"], constraint["sqltext"].strip())
            for constraint in inspector.get_check_constraints(table.name)
        )
        # Let SQLAlchemy parse SQLite's balanced CHECK expressions. Compare
        # mapped SQL conservatively, not guessed semantic equivalence. Boolean
        # checks must render literal 0/1, without the mapped table qualifier.
        if actual_checks != expected_checks:
            raise SchemaVersionError(
                f"table {table.name!r} does not match schema version {DB_SCHEMA_VERSION} "
                "(CHECK constraints differ)"
            )


def _check_schema(conn: Connection) -> bool:
    is_new = _check_version(*_schema_state(conn))
    if not is_new:
        _verify_schema(conn)
    return is_new


def _enable_wal(cursor: Any) -> None:
    row = cursor.execute("PRAGMA journal_mode = WAL").fetchone()
    if row is None or str(row[0]).lower() != "wal":
        raise PersistenceUnavailable(f"could not enable WAL journal mode (got {row!r})")


def initialize_database(settings: Settings, workspace_root: str | Path) -> Database:
    """Explicitly create/open the SQLite database and return its engine and sessions.

    * ``workspace_root`` must be absolute (``ValueError`` otherwise).
    * The parent directory is created here and only here.
    * A new (empty) file gets the three tables and ``user_version = 1`` atomically,
      then WAL outside a transaction. An existing database must already be at
      schema version 1; newer, unversioned or incompatible databases raise `SchemaVersionError`
      and are left untouched. ``create_all`` is not a migration system.
    """
    path = settings.resolved_db_path(workspace_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PersistenceUnavailable(f"cannot create database directory {path.parent}") from exc

    engine = _build_engine(path)
    try:
        # A read transaction gives all preflight queries one coherent snapshot.
        # Finish it before changing the persistent journal mode.
        with engine.connect() as conn, conn.begin():
            is_new = _check_schema(conn)

        if is_new:
            # BEGIN IMMEDIATE: a concurrent initializer waits, then sees version 1.
            with engine.execution_options(**{_WRITE_OPTION: True}).begin() as conn:
                if _check_schema(conn):
                    Base.metadata.create_all(conn)
                    _verify_schema(conn)
                    conn.exec_driver_sql(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
        # In particular, don't change WAL before a waiting initializer has
        # rechecked the schema under its write lock and accepted it.
        with _raw_cursor(engine) as cursor:
            _enable_wal(cursor)  # outside any transaction; persistent in the file
    except SQLAlchemyError as exc:
        engine.dispose()
        raise PersistenceUnavailable(f"cannot initialize database at {path}") from exc
    except sqlite3.Error as exc:
        engine.dispose()
        raise PersistenceUnavailable(f"cannot initialize database at {path}") from exc
    except BaseException:
        engine.dispose()
        raise

    return Database(
        engine=engine,
        session_factory=sessionmaker(engine, expire_on_commit=False),
        write_session_factory=sessionmaker(
            engine.execution_options(**{_WRITE_OPTION: True}), expire_on_commit=False
        ),
        path=path,
        schema_version=DB_SCHEMA_VERSION,
    )

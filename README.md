# TRACE-Laya

**Temporal Risk Attribution & Claim Evidence**

A replay-first, evidence-aware event-to-portfolio-stress project.
The risk object is an evolving **event hypothesis**, not an individual article.

Planned flow: document → claim → event → evidence and semantic decisions →
deterministic policy → exposure → conditional stress → explanation.

## Workspace and tools

- `apps/api`: Python 3.11, FastAPI, Pydantic settings, SQLAlchemy/SQLite, Uvicorn, pytest.
- `apps/web`: Next.js App Router, React, TypeScript, native fetch.
- `packages/shared/contracts`: generated JSON Schema artifacts, not a separate service.
- `apps/api/tests/api/unit`: pure contract, settings, and health-response model tests.
- `apps/api/tests/api/integration`: HTTP health, contract export, and file-backed persistence tests.
- `docker-compose.yml`: exactly two services, `api` and `web`.

**Tooling:** Bun workspaces and the existing Turbo commands for JavaScript;
uv workspace and `uv.lock` for Python. Node 24 remains the selected Node major.

## Current milestone

Modules 1–3 provide process health, validated TRACE settings, immutable
source/document/evidence-span contracts, deterministic schema export, and
durable source/document storage with ingestion-run accounting.
Raw text is preserved verbatim; timestamps normalize to UTC; hashes cover UTF-8
text; evidence offsets count Unicode code points (not JavaScript UTF-16 units).

Persistence stores exact evidence, reuses identical IDs, rejects changed records,
and rolls back failed batches. Replay execution, claims, ML, lifecycle policy,
and financial stress are **not yet implemented**. Financial outputs will be
labelled **Conditional scenario loss if the event materializes**, not predictions.

The broader architecture is in [docs/trace-laya-architecture.md](docs/trace-laya-architecture.md);
its downstream tables/features are not part of this checkpoint.

### Database decision

Keep **SQLite + WAL** as the primary database for this hackathon. The intended
dataset is small, writes are single-user, graph traversal is in memory, and
offline replay must work with only `api` and `web`. PostgreSQL's multi-writer
benefits do not address a demonstrated requirement here; adding a database
server would increase setup/demo dependencies. Reconsider it for a future
multi-user deployment, not before proving the offline vertical slice.

## Local setup

Run commands from the repository root unless stated otherwise.
Create local environment files without overwriting existing ones:

```sh
test -f apps/api/.env || cp apps/api/.env.example apps/api/.env
test -f apps/web/.env || cp apps/web/.env.example apps/web/.env
uv sync --locked --all-packages --all-groups
bun install --frozen-lockfile
```

The examples set the API to `http://localhost:3001` and the web app to
`http://localhost:3000`. Keep `WEB_ORIGIN` in the API environment aligned with
the browser origin, and `NEXT_PUBLIC_API_HOST` in the web environment aligned
with the browser-accessible API address. Keep actual `.env` files private.

The API also accepts `APP_ENV=development|production|test`, `LOG_LEVEL`,
`API_HOST`, `API_PORT`, and `ALLOWED_HOSTS`. Process environment values override
`apps/api/.env`. The web scripts load `apps/web/.env`, including `WEB_PORT`.

### TRACE configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `TRACE_MODE` | `REPLAY` | `REPLAY`, `HYBRID`, or `LIVE`; independent of `APP_ENV` |
| `TRACE_DEVICE` | `cpu` | `cpu` or `cuda`; validation does not load a model |
| `TRACE_DB_PATH` | `data/trace.sqlite3` | Relative to an explicit workspace root |
| `GDELT_ENABLED` | `false` | Enable the future live GDELT connector |
| `SEC_ENABLED` | `false` | Enable the future live SEC connector |
| `SEC_USER_AGENT` | Unset | Required when SEC is enabled |

REPLAY rejects enabled live connectors. LIVE requires at least one connector;
HYBRID can operate with connectors disabled. Production + REPLAY is valid.
Resolving the database path creates no file or directory. Connector flags are
configuration only; they do not imply that adapters are implemented.

```sh
bun run dev
```

Or run `bun run dev:api` and `bun run dev:web` in separate terminals.
Open <http://localhost:3000> and select **Check API**. Development API docs are
at <http://localhost:3001/docs>; production disables them.

```sh
curl --fail http://localhost:3001/health
```

## SQLite persistence — Module 3

Initialization is explicit. Imports, settings validation, `/health`, and ordinary
API startup do not create a database or check its readiness. From the repository
root, with the API environment configured:

```sh
uv run --locked --package api python scripts/init_db.py
uv run --locked --package api pytest apps/api/tests/api/integration/test_persistence.py -q
```

The script resolves `TRACE_DB_PATH` against the repository root, not the current
working directory or installed package. Override the root with
`--workspace-root /absolute/path`. The packaged equivalent always requires an
absolute root:

```sh
uv run --locked --package api python -m app.persistence.cli --workspace-root "$PWD"
```

### Storage guarantees

- Exactly three application tables: `ingestion_runs`, `sources`, `documents`.
  Schema revision is SQLite `PRAGMA user_version = 1` (separate from JSON
  `schema_version=trace-core-v0.1`).
- WAL is enabled/verified outside an application transaction. Every engine
  connection enables foreign keys and a 5000 ms busy timeout; SQLite's durable
  defaults remain unchanged. WAL permits concurrent readers, not unlimited writers.
- Indexed timestamps use UTC text `YYYY-MM-DDTHH:MM:SS.ffffffZ`; reads return
  aware timestamps. Unknown publication time and independence remain `NULL`.
- Sources and documents are immutable by ID: identical domain fields reuse a
  row; changed fields raise `RecordConflict`. Hash indexes are **nonunique**:
  different IDs with identical text/hash retain their separate provenance.
- `first_ingestion_run_id` is database metadata only; it never enters the frozen
  document JSON contract. `quoted_source_id` deliberately need not exist locally.
- Listing is `observed_at` ascending, then `document_id` ascending, with limit/offset.
  Public reads return validated Pydantic records, not detached ORM objects.

`initialize_database(settings, workspace_root)` returns the engine and per-operation
session factories. The setup caller owns/disposes the engine; `EvidenceStore(database)`
holds no shared mutable session. Its concrete methods are `create_run`,
`persist_batch_and_complete`, `mark_run_failed`, `get_run`, `get_source`,
`get_document`, and `list_documents`. Callers supply IDs, aware start/finish times,
`RunMode`, and validated records. Persistence never reads a clock, generates IDs,
normalizes text, fetches articles, infers independence, or performs event clustering.

### Transactions and failures

A batch first commits a `RUNNING` audit entry, then inserts/reuses sources and
documents and completes the run in one short `BEGIN IMMEDIATE` transaction.
Completed counts reconcile: received = inserted + reused. Any batch failure
rolls back all batch writes before a separate transaction attempts `FAILED`:
inserted/reused counts are zero and the reason is a stable code, never payloads
or a traceback. Terminal runs are not rewritten; use a new run ID for a repeat batch.

`RecordConflict`, `MissingReference`, and `PersistenceUnavailable` are explicit
persistence outcomes. Corrupted stored records also fall under
`PersistenceUnavailable`. For batch failures, `error.failure_recorded` is true
only if the `FAILED` audit commit succeeded. If the database is still unavailable,
the run may remain `RUNNING` (or never have been created); no committed audit is claimed.

Existing newer, unversioned/nonempty, or incompatible databases fail without
resetting, relabelling, or repairing them. Version-1 compatibility checks include
columns, primary/foreign keys, deletion restrictions, CHECK constraints, and
indexes. Manually rewritten DDL is checked conservatively, not treated as a migration.
`create_all` is only for a new database; future revisions need an explicit migration decision.

Runtime `.sqlite3` files and their WAL/SHM/journal sidecars are ignored; fixtures
elsewhere in `data/` remain trackable. If the SQLite CLI is installed, inspect read-only:

```sh
sqlite3 "file:data/trace.sqlite3?mode=ro" \
  "PRAGMA journal_mode; PRAGMA user_version; PRAGMA integrity_check; PRAGMA foreign_key_check;"
```

Expected: `wal`, `1`, `ok`, and no foreign-key violations. Enforcement itself is
connection-specific: integration tests verify it on application-created connections.

## Docker

Create the two local environment files as above. Stop local servers using the
same ports, then run:

```sh
bun run docker:up
```

This passes both app environment files to Compose and builds/starts `api` and
`web`. No external network needs to be created manually. To stop the services:

```sh
docker compose --env-file apps/api/.env --env-file apps/web/.env down
```

`NEXT_PUBLIC_API_HOST` is embedded into browser code at web build time. Rebuild
the web image after changing it. Use a browser-accessible address such as
`http://localhost:3001`, not the container-only hostname `api`.

The current Compose port mappings publish on all host interfaces. This prototype
has no authentication: use a trusted development machine/network, not a public
deployment. Security headers and host validation do not replace authentication.

SQLite uses the API-only named volume `trace-data`, mounted at `/app/data`, with
`TRACE_DB_PATH=/app/data/trace.sqlite3`. The image owns that directory as the
existing non-root API user (UID 1001). Initialize it explicitly after starting services:

```sh
docker compose --env-file apps/api/.env --env-file apps/web/.env \
  exec api python -m app.persistence.cli --workspace-root /app
```

The volume persists across container replacements and normal `docker compose down`.
**Do not use `down -v` unless intentionally deleting stored evidence.** A manually
substituted/pre-existing mount must also be writable by UID 1001; image ownership
does not fix permissions on an arbitrary host bind mount. No database service is added.

## Shared contracts

Pydantic definitions in `apps/api/app/domain/schemas/` are the source of truth.
Shared records use `schema_version=trace-core-v0.1`. The exporter never loads
local settings or includes machine-specific/private values.

```sh
# Run from the repository root after changing a contract.
uv run --locked --package api python scripts/export_contracts.py

# Check for drift without rewriting artifacts.
uv run --locked --package api python scripts/export_contracts.py --check
```

JSON Schema describes record shape. Server-side validation additionally checks
document hashes and span/document consistency; schema validation alone is not
evidence verification.

## Checks

```sh
# Bypass Turbo task caching when checking current behavior.
bun run test --force
bun run lint --force

# Direct pytest entrypoint; uses the same root test configuration.
uv run --locked --package api pytest -q

# Focus a test category.
uv run --locked --package api pytest apps/api/tests/api/unit -q
uv run --locked --package api pytest apps/api/tests/api/integration -q

# Production web build, including Next.js TypeScript validation.
# Set the public URL explicitly; the API does not need to be running.
(cd apps/web && NEXT_PUBLIC_API_HOST=http://localhost:3001 bun run build)

# Verify the two Compose service definitions without starting containers.
docker compose --env-file apps/api/.env --env-file apps/web/.env config --services
```

The root `pyproject.toml` owns pytest discovery. API tests live only under
`apps/api/tests/api`, outside the installed `app` package. Shared fixtures
isolate process settings, local dotenv values, and the settings cache. Turbo
test caching is disabled so changes to shared contracts and API tests are
always exercised.

### Module 2 audit — 2026-10-05

- `bun run test --force`: **122 passed**.
- Direct root pytest invocation: **122 passed**.
- Exported-schema drift check: **passed**.
- Next.js production build and TypeScript validation: **passed**.
- API syntax compilation: **passed**; this is not a Python lint/typecheck suite.
- Web lint: **still fails** because installed `typescript-eslint` rejects
  TypeScript 7.0. This is a non-blocking tooling follow-up, not a passed check.

The test run also reports an `httpx`/Starlette deprecation warning; current
HTTP integration tests pass. Container image builds and clean installation
were not rechecked in this audit.

### Module 3 storage audit — 2026-10-05

- File-backed persistence integration tests: **91 passed**.
- `bun run test --force`: **213 passed** (all 122 existing tests plus 91 persistence tests).
- Contract drift, lockfile consistency, API syntax compilation, and
  `git diff --check`: **passed**. Syntax compilation is not a Python lint/typecheck suite.
- Compose configuration: **exactly `api` and `web`**, with the declared persistent volume.
- `docker build --file apps/api/Dockerfile --tag trace-api:module3 .`: **passed**.
- Two fresh/replacement API containers sharing a disposable named volume:
  **UID 1001 write access, exact Unicode/newline/NUL text and null publication
  read-back, idempotent reuse, failed-batch rollback, WAL, foreign keys, busy
  timeout, integrity and retained first-run metadata verified**. Both ran with
  networking disabled; the check volume was removed afterwards. No production
  evidence was changed and no live connectors/models were used.
- `bun run lint --force`: API syntax check passed; web lint still fails on the
  pre-existing TypeScript 7 / `typescript-eslint` incompatibility.
- The existing Starlette/httpx deprecation warning remains; HTTP tests pass.

**Stop point:** exact source/document batches survive process/container restart,
rerun safely, and roll back without partial evidence. Replay and downstream event
or financial processing are still outside this module.

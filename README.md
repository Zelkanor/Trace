# TRACE-Laya

**Temporal Risk Attribution & Claim Evidence**

A replay-first, evidence-aware event-to-portfolio-stress project.
The risk object is an evolving **event hypothesis**, not an individual article.

Planned flow: document → claim → event → evidence and semantic decisions →
deterministic policy → exposure → conditional stress → explanation.

## Workspace and tools

- `apps/api`: Python 3.11, FastAPI, Pydantic settings, Uvicorn, pytest.
- `apps/web`: Next.js App Router, React, TypeScript, native fetch.
- `packages/shared/contracts`: generated JSON Schema artifacts, not a separate service.
- `apps/api/tests/api/unit`: pure contract, settings, and health-response model tests.
- `apps/api/tests/api/integration`: HTTP health and contract-export integration tests.
- `docker-compose.yml`: exactly two services, `api` and `web`.

**Tooling:** Bun workspaces and the existing Turbo commands for JavaScript;
uv workspace and `uv.lock` for Python. Node 24 remains the selected Node major.

## Current milestone

Modules 1–2 provide process health, validated TRACE settings, immutable
source/document/evidence-span contracts, and deterministic schema export.
Raw text is preserved verbatim; timestamps normalize to UTC; hashes cover UTF-8
text; evidence offsets count Unicode code points (not JavaScript UTF-16 units).

SQLite persistence, replay execution, ML, and financial stress are **not yet
implemented**. Financial outputs will be labelled **Conditional scenario loss
if the event materializes**, not predictions.

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

**Next module:** SQLite persistence. Build replay and the smallest offline
event-to-loss path next; do not add live-service or model dependencies first.

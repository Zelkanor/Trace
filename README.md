# TRACE-Laya

**Temporal Risk Attribution & Claim Evidence**

A replay-first, evidence-aware event-to-portfolio-stress project.
The risk object is an evolving **event hypothesis**, not an individual article.

Planned flow: document → claim → event → evidence and semantic decisions →
deterministic policy → exposure → conditional stress → explanation.

## Workspace and tools

- `apps/api`: Python 3.11, FastAPI, Pydantic settings, Uvicorn, pytest.
- `apps/web`: Next.js App Router, React, TypeScript, native fetch.
- `docker-compose.yml`: exactly two services, `api` and `web`.

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

## Checks

```sh
# Bypass Turbo task caching when checking current behavior.
bun run test --force
bun run lint --force

# Production web build, including Next.js TypeScript validation.
# Set the public URL explicitly; the API does not need to be running.
(cd apps/web && NEXT_PUBLIC_API_HOST=http://localhost:3001 bun run build)

# Verify the two Compose service definitions without starting containers.
docker compose --env-file apps/api/.env --env-file apps/web/.env config --services
```

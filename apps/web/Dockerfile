# syntax=docker/dockerfile:1
# Build context: repository root (the bun workspace lives there).

FROM node:24-alpine AS base
COPY --from=oven/bun:1-alpine /usr/local/bin/bun /usr/local/bin/bun
WORKDIR /app

FROM base AS builder
ENV NEXT_TELEMETRY_DISABLED=1
# NEXT_PUBLIC_* values are inlined at build time, so they are build args.
ARG NEXT_PUBLIC_API_HOST
ENV NEXT_PUBLIC_API_HOST=$NEXT_PUBLIC_API_HOST

# Dependencies first: cached until a manifest or the lockfile changes.
COPY package.json bun.lock ./
COPY apps/api/package.json apps/api/package.json
COPY apps/web/package.json apps/web/package.json
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile --filter web

COPY apps/web apps/web
WORKDIR /app/apps/web
RUN bun run build

FROM node:24-alpine AS runner
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
USER node
# Standalone output contains only the traced runtime dependencies.
COPY --from=builder --chown=node:node /app/apps/web/.next/standalone ./
COPY --from=builder --chown=node:node /app/apps/web/.next/static apps/web/.next/static
COPY --from=builder --chown=node:node /app/apps/web/public apps/web/public
CMD ["node", "apps/web/server.js"]

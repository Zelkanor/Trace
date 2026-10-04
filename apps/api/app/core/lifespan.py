"""Application startup and shutdown hooks."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app.state.settings
    logger.info("starting trace-api env=%s web_origin=%s", settings.app_env, settings.web_origin)
    try:
        yield
    finally:
        logger.info("trace-api stopped")

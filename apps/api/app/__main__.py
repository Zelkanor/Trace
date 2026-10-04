"""Entrypoint: `python -m app` (reload in development, plain server otherwise)."""

import uvicorn

from app.core.config import API_ROOT, get_settings
from app.core.logger import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.is_production)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.reload,
        reload_dirs=[str(API_ROOT / "app")] if settings.reload else None,
        log_config=None,  # logging is configured by app.core.logger
        server_header=False,
    )


if __name__ == "__main__":
    main()

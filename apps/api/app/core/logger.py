"""Logging setup: human-readable in development, JSON lines elsewhere."""

import json
import logging
import logging.config

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str, *, json_logs: bool) -> None:
    formatter = (
        {"()": JsonFormatter}
        if json_logs
        else {"format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s"}
    )
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": formatter},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                }
            },
            "root": {"level": level, "handlers": ["console"]},
            "loggers": {
                name: {"handlers": [], "propagate": True, "level": level}
                for name in UVICORN_LOGGERS
            },
        }
    )

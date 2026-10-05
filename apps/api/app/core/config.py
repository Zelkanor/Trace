"""Typed, validated application settings loaded from the environment."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.domain.schemas.enums import RunMode

API_ROOT = Path(__file__).resolve().parents[2]

Environment = Literal["development", "production", "test"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
TraceDevice = Literal["cpu", "cuda"]


def resolve_data_path(
    configured: str | os.PathLike[str], workspace_root: str | os.PathLike[str]
) -> Path:
    root = Path(workspace_root)
    if not root.is_absolute():
        raise ValueError("workspace_root must be an absolute path")
    if not str(configured).strip():
        raise ValueError("configured path must not be blank")
    return Path(os.path.normpath(root / configured))


class Settings(BaseSettings):
    """Process environment wins over `apps/api/.env`; unknown variables are ignored."""

    model_config = SettingsConfigDict(
        env_file=API_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app_env: Environment = "development"
    log_level: LogLevel = "INFO"

    api_host: str = "127.0.0.1"
    api_port: int = Field(ge=1, le=65535)

    web_origin: str
    allowed_hosts: Annotated[list[str], NoDecode] = ["localhost", "127.0.0.1"]

    # TRACE data-source mode. Independent of `app_env`: production + REPLAY is valid.
    trace_mode: RunMode = RunMode.REPLAY
    trace_device: TraceDevice = "cpu"
    trace_db_path: Path = Path("data/trace.sqlite3")
    gdelt_enabled: bool = False
    sec_enabled: bool = False
    sec_user_agent: str | None = None  # blank is treated as unset

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def reload(self) -> bool:
        return self.app_env == "development"

    @field_validator("web_origin")
    @classmethod
    def _normalize_origin(cls, value: str) -> str:
        value = value.strip()
        if any(char.isspace() for char in value):
            raise ValueError("must be an origin such as http://localhost:3000")
        try:
            parts = urlsplit(value)
            hostname = parts.hostname
            _ = parts.port
        except ValueError as exc:
            raise ValueError("must be an origin such as http://localhost:3000") from exc
        if (
            parts.scheme not in {"http", "https"}
            or not hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError("must be an origin such as http://localhost:3000")
        return f"{parts.scheme}://{parts.netloc}"

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip() for host in value.split(",") if host.strip()]
        return value

    @field_validator("trace_db_path", mode="before")
    @classmethod
    def _require_db_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("sec_user_agent", mode="before")
    @classmethod
    def _blank_user_agent_is_unset(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            if any(not char.isprintable() for char in value):
                raise ValueError("must not contain control characters")
        return value

    @model_validator(mode="after")
    def _validate_trace_mode(self) -> "Settings":
        connectors = self.gdelt_enabled or self.sec_enabled
        if self.trace_mode is RunMode.REPLAY and connectors:
            raise ValueError("TRACE_MODE=REPLAY forbids enabling GDELT_ENABLED or SEC_ENABLED")
        if self.trace_mode is RunMode.LIVE and not connectors:
            raise ValueError("TRACE_MODE=LIVE requires GDELT_ENABLED or SEC_ENABLED")
        if self.sec_enabled and not self.sec_user_agent:
            raise ValueError("SEC_ENABLED requires a nonblank SEC_USER_AGENT")
        return self

    def resolved_db_path(self, workspace_root: str | os.PathLike[str]) -> Path:
        """Absolute database path for `workspace_root`. Creates nothing."""
        return resolve_data_path(self.trace_db_path, workspace_root)


@lru_cache
def get_settings() -> Settings:
    return Settings()

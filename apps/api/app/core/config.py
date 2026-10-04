"""Typed, validated application settings loaded from the environment."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

API_ROOT = Path(__file__).resolve().parents[2]

Environment = Literal["development", "production", "test"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


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

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def reload(self) -> bool:
        return self.app_env == "development"

    @field_validator("web_origin")
    @classmethod
    def _normalize_origin(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
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


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment

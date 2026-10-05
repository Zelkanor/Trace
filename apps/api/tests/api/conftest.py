import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app

WEB_ORIGIN = "http://localhost:3000"


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep developer environment, dotenv files, and cached settings out of tests."""
    field_env_names = {name.upper() for name in Settings.model_fields}
    for name in tuple(os.environ):
        if name.upper() in field_env_names:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, app_env="test", api_port=3001, web_origin=WEB_ORIGIN)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost") as test_client:
        yield test_client

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

WEB_ORIGIN = "http://localhost:3000"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, app_env="test", api_port=3001, web_origin=WEB_ORIGIN)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost") as test_client:
        yield test_client

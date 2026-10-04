import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.health import BootstrapHealthResponse
from app.core.config import Settings
from tests.conftest import WEB_ORIGIN

EXPECTED = {"service": "trace-api", "api_status": "ok", "health_scope": "process"}


def test_health_returns_fixed_contract(client: TestClient) -> None:
    for _ in range(2):  # idempotent
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == EXPECTED


def test_unknown_route_is_404(client: TestClient) -> None:
    assert client.get("/nope").status_code == 404


def test_cors_allows_only_web_origin(client: TestClient) -> None:
    allowed = client.get("/health", headers={"Origin": WEB_ORIGIN})
    assert allowed.headers["access-control-allow-origin"] == WEB_ORIGIN

    denied = client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in denied.headers


def test_security_headers_present(client: TestClient) -> None:
    headers = client.get("/health").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


def test_untrusted_host_rejected(client: TestClient) -> None:
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400


def test_response_model_rejects_extra_and_missing_fields() -> None:
    with pytest.raises(ValidationError):
        BootstrapHealthResponse(**EXPECTED, extra="x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BootstrapHealthResponse(service="trace-api", api_status="ok")  # type: ignore[call-arg]


def test_settings_normalize_origin_and_hosts() -> None:
    settings = Settings(
        _env_file=None, api_port=1, web_origin="http://localhost:3000/", allowed_hosts="a, b"
    )
    assert settings.web_origin == "http://localhost:3000"
    assert settings.allowed_hosts == ["a", "b"]


@pytest.mark.parametrize("origin", ["*", "localhost:3000", "http://x/path"])
def test_settings_reject_bad_origin(origin: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_port=1, web_origin=origin)

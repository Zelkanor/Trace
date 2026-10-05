from fastapi.testclient import TestClient

from app.core.config import Settings

EXPECTED = {"service": "trace-api", "api_status": "ok", "health_scope": "process"}


def test_health_returns_fixed_contract(client: TestClient) -> None:
    for _ in range(2):  # idempotent
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == EXPECTED


def test_unknown_route_is_404(client: TestClient) -> None:
    assert client.get("/nope").status_code == 404


def test_cors_allows_only_web_origin(client: TestClient, settings: Settings) -> None:
    allowed = client.get("/health", headers={"Origin": settings.web_origin})
    assert allowed.headers["access-control-allow-origin"] == settings.web_origin

    denied = client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in denied.headers


def test_security_headers_present(client: TestClient) -> None:
    headers = client.get("/health").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


def test_untrusted_host_rejected(client: TestClient) -> None:
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400

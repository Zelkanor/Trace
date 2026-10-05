import pytest
from pydantic import ValidationError

from app.api.health import BootstrapHealthResponse

EXPECTED = {"service": "trace-api", "api_status": "ok", "health_scope": "process"}


def test_response_model_rejects_extra_and_missing_fields() -> None:
    with pytest.raises(ValidationError):
        BootstrapHealthResponse(**EXPECTED, extra="x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BootstrapHealthResponse(service="trace-api", api_status="ok")  # type: ignore[call-arg]

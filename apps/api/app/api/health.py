from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

router = APIRouter(tags=["health"])


class BootstrapHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: Literal["trace-api"]
    api_status: Literal["ok"]
    health_scope: Literal["process"]


@router.get("/health", response_model=BootstrapHealthResponse)
async def health() -> BootstrapHealthResponse:
    return BootstrapHealthResponse(
        service="trace-api", api_status="ok", health_scope="process"
    )

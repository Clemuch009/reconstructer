# api/routes/health.py

from fastapi import APIRouter
from typing_extensions import TypedDict

router = APIRouter()


class HealthResponse(TypedDict):
    status:  str
    version: str


@router.get("/health", response_model=dict)
async def health() -> HealthResponse:
    """
    Liveness check.
    Returns 200 if service is running.
    """
    from api.coc import COC_VERSION
    return HealthResponse(
        status="ok",
        version=COC_VERSION,
    )

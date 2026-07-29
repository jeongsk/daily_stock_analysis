"""Optional World Monitor integration status endpoint."""

from fastapi import APIRouter

from src.services.worldmonitor_service import WorldMonitorService

router = APIRouter()


@router.get("/status", summary="Get World Monitor integration status")
def get_worldmonitor_status() -> dict:
    # Additive: the self-hosting phase keys (status/base_url/detail) are
    # unchanged; `events` is a new read-only summary of the market-review event
    # sync (design 2026-07-29 spec §12).
    return WorldMonitorService().get_status_payload()

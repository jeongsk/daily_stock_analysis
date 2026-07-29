"""Optional World Monitor integration status endpoint."""

from fastapi import APIRouter

from src.services.worldmonitor_service import WorldMonitorService

router = APIRouter()


@router.get("/status", summary="Get World Monitor integration status")
def get_worldmonitor_status() -> dict:
    return WorldMonitorService().get_status().to_dict()

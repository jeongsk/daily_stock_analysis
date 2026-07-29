from unittest.mock import Mock, patch

import httpx

from src.config import Config
from src.services.worldmonitor_service import WorldMonitorService


def _config(**updates):
    values = {
        "worldmonitor_enabled": True,
        "worldmonitor_base_url": "http://worldmonitor:8080",
        "worldmonitor_connect_timeout_seconds": 1.0,
        "worldmonitor_read_timeout_seconds": 2.0,
    }
    values.update(updates)
    return Config(**values)


def test_worldmonitor_status_disabled():
    status = WorldMonitorService(_config(worldmonitor_enabled=False)).get_status()
    assert status.status == "disabled"
    assert status.base_url is None


def test_worldmonitor_status_rejects_credentials():
    status = WorldMonitorService(
        _config(worldmonitor_base_url="http://secret@example.test")
    ).get_status()
    assert status.status == "misconfigured"
    assert "secret" not in (status.detail or "")


@patch("src.services.worldmonitor_service.httpx.get")
def test_worldmonitor_status_healthy(mock_get):
    response = Mock(status_code=200)
    response.json.return_value = {"overall": "healthy"}
    mock_get.return_value = response
    status = WorldMonitorService(_config()).get_status()
    assert status.status == "healthy"


@patch("src.services.worldmonitor_service.httpx.get")
def test_worldmonitor_status_degraded(mock_get):
    response = Mock(status_code=503)
    response.json.return_value = {"overall": "critical"}
    mock_get.return_value = response
    status = WorldMonitorService(_config()).get_status()
    assert status.status == "degraded"


@patch("src.services.worldmonitor_service.httpx.get")
def test_worldmonitor_status_unreachable_sanitizes_error(mock_get):
    mock_get.side_effect = httpx.ConnectError(
        "failed token=super-secret",
        request=httpx.Request("GET", "http://worldmonitor:8080/api/health"),
    )
    status = WorldMonitorService(_config()).get_status()
    assert status.status == "unreachable"
    assert "super-secret" not in (status.detail or "")

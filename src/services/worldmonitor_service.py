"""Read-only health boundary for the optional World Monitor integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import httpx

from src.config import Config, get_config
from src.services.run_diagnostics import sanitize_diagnostic_text

WorldMonitorStatusName = Literal[
    "disabled", "healthy", "degraded", "unreachable", "misconfigured"
]


@dataclass(frozen=True)
class WorldMonitorStatus:
    status: WorldMonitorStatusName
    base_url: Optional[str] = None
    detail: Optional[str] = None

    def to_dict(self) -> dict[str, Optional[str]]:
        return {
            "status": self.status,
            "base_url": self.base_url,
            "detail": self.detail,
        }


class WorldMonitorService:
    """Probe World Monitor without affecting the stock-analysis liveness contract."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()

    def get_status(self) -> WorldMonitorStatus:
        if not self.config.worldmonitor_enabled:
            return WorldMonitorStatus(status="disabled")

        base_url = self.config.worldmonitor_base_url.strip().rstrip("/")
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return WorldMonitorStatus(
                status="misconfigured",
                detail="WORLDMONITOR_BASE_URL must be an HTTP(S) origin without credentials",
            )

        timeout = httpx.Timeout(
            connect=self.config.worldmonitor_connect_timeout_seconds,
            read=self.config.worldmonitor_read_timeout_seconds,
            write=self.config.worldmonitor_read_timeout_seconds,
            pool=self.config.worldmonitor_connect_timeout_seconds,
        )
        try:
            response = httpx.get(
                f"{base_url}/api/health",
                params={"compact": "1"},
                timeout=timeout,
                follow_redirects=False,
            )
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            detail = sanitize_diagnostic_text(str(exc), max_length=200) or "connection failed"
            return WorldMonitorStatus(
                status="unreachable",
                base_url=base_url,
                detail=detail,
            )

        overall = str(payload.get("overall") or payload.get("status") or "").lower() if isinstance(payload, dict) else ""
        if response.status_code == 200 and overall in {"healthy", "ok"}:
            return WorldMonitorStatus(status="healthy", base_url=base_url)
        return WorldMonitorStatus(
            status="degraded",
            base_url=base_url,
            detail=f"health response status={response.status_code} overall={overall or 'unknown'}",
        )

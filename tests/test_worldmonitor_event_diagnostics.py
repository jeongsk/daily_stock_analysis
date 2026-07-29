"""Read-only diagnostics for the World Monitor event sync.

Design: docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md §12
"""

import os
from datetime import datetime, timedelta
from unittest.mock import patch

import httpx
import pytest

from src.config import Config
from src.repositories.world_event_repo import WorldEventRepository
from src.services.worldmonitor_events import (
    CATEGORY_CONFLICT,
    CATEGORY_ENERGY,
    CATEGORY_OUTAGE,
)
from src.services.worldmonitor_service import WorldMonitorService
from src.storage import DatabaseManager

NOW = datetime(2026, 7, 29, 9, 0, 0)


@pytest.fixture
def db(tmp_path):
    env_path = tmp_path / ".env"
    db_path = tmp_path / "diag.db"
    env_path.write_text(
        "STOCK_LIST=600519\nGEMINI_API_KEY=test\nADMIN_AUTH_ENABLED=false\n"
        f"DATABASE_PATH={db_path}\n",
        encoding="utf-8",
    )
    os.environ["ENV_FILE"] = str(env_path)
    os.environ["DATABASE_PATH"] = str(db_path)
    Config.reset_instance()
    DatabaseManager.reset_instance()

    yield DatabaseManager.get_instance()

    DatabaseManager.reset_instance()
    Config.reset_instance()
    os.environ.pop("ENV_FILE", None)
    os.environ.pop("DATABASE_PATH", None)


def _service(db, **updates):
    values = {
        "worldmonitor_enabled": True,
        "worldmonitor_events_enabled": True,
        "worldmonitor_base_url": "http://worldmonitor:8080",
        "worldmonitor_event_lookback_days": 30,
        "worldmonitor_event_stale_after_seconds": 7200,
    }
    values.update(updates)
    return WorldMonitorService(
        Config(**values), repo=WorldEventRepository(db_manager=db)
    )


def test_diagnostics_reports_every_category(db):
    diag = _service(db).get_events_diagnostics(now=NOW)
    assert set(diag["categories"]) == {
        CATEGORY_CONFLICT,
        CATEGORY_OUTAGE,
        CATEGORY_ENERGY,
    }


def test_diagnostics_is_disabled_when_events_are_off(db):
    diag = _service(db, worldmonitor_events_enabled=False).get_events_diagnostics(now=NOW)
    assert diag["enabled"] is False


def test_diagnostics_separates_unverified_from_unavailable(db):
    """Both render the same sentence in the prompt but have different causes:
    'reachable but produces nothing' vs 'not reachable at all'. An operator has
    to be able to tell a missing upstream API key from a dead World Monitor."""
    repo = WorldEventRepository(db_manager=db)
    # Reachable, succeeded, but never returned an event.
    repo.record_sync(
        category=CATEGORY_CONFLICT, status="ok", synced_at=NOW, event_count=0
    )
    # Never succeeded at all.
    repo.record_sync(
        category=CATEGORY_OUTAGE,
        status="error",
        synced_at=NOW,
        event_count=0,
        error="connection failed",
    )

    diag = _service(db).get_events_diagnostics(now=NOW)
    assert diag["categories"][CATEGORY_CONFLICT]["state"] == "unverified"
    assert diag["categories"][CATEGORY_OUTAGE]["state"] == "unavailable"


def test_diagnostics_exposes_both_sync_timestamps(db):
    repo = WorldEventRepository(db_manager=db)
    repo.record_sync(
        category=CATEGORY_CONFLICT, status="ok", synced_at=NOW, event_count=3
    )

    entry = _service(db).get_events_diagnostics(now=NOW)["categories"][CATEGORY_CONFLICT]
    assert entry["last_success_at"] is not None
    assert entry["last_nonempty_at"] is not None


def test_diagnostics_reports_stored_event_counts(db):
    repo = WorldEventRepository(db_manager=db)
    repo.upsert_events(
        [
            {
                "category": CATEGORY_CONFLICT,
                "external_id": "e1",
                "title": "t",
                "occurred_at": NOW - timedelta(days=1),
                "countries": ["SY"],
                "markets": [],
                "scope": "global",
                "severity_rank": 1,
                "raw_payload": {},
            }
        ],
        collected_at=NOW,
    )

    entry = _service(db).get_events_diagnostics(now=NOW)["categories"][CATEGORY_CONFLICT]
    assert entry["stored_events"] == 1


def test_diagnostics_reports_the_unmapped_backlog(db):
    """A growing unmapped count means upstream is serving pre-denorm rows whose
    origin country is unknown; those are excluded from the prompt on purpose."""
    repo = WorldEventRepository(db_manager=db)
    repo.upsert_events(
        [
            {
                "category": CATEGORY_ENERGY,
                "external_id": "legacy",
                "title": "t",
                "occurred_at": NOW - timedelta(days=1),
                "countries": [],
                "markets": [],
                "scope": "unmapped",
                "severity_rank": 0,
                "raw_payload": {},
            }
        ],
        collected_at=NOW,
    )

    assert _service(db).get_events_diagnostics(now=NOW)["unmapped_events"] == 1


def test_diagnostics_sanitizes_the_last_error(db):
    def _leaky(url, **kwargs):
        raise httpx.ConnectError(
            "failed token=super-secret", request=httpx.Request("GET", url)
        )

    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_leaky):
        service.sync_events(now=NOW)

    diag = service.get_events_diagnostics(now=NOW)
    assert "super-secret" not in str(diag)


def test_diagnostics_never_raises_when_storage_is_unavailable(db):
    service = _service(db)
    with patch.object(
        service.repo, "get_sync_state", side_effect=RuntimeError("db gone")
    ):
        diag = service.get_events_diagnostics(now=NOW)
    assert diag["enabled"] is True


def test_status_endpoint_includes_events_without_changing_existing_keys(db):
    """The self-hosting phase status contract must stay intact; the event
    summary is additive so existing clients keep working."""
    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get") as mock_get:
        mock_get.return_value = _Ok({"overall": "healthy"})
        payload = service.get_status_payload(now=NOW)

    assert payload["status"] == "healthy"
    assert "base_url" in payload and "detail" in payload
    assert payload["events"]["enabled"] is True


def test_status_payload_events_absent_of_crash_when_disabled(db):
    service = _service(db, worldmonitor_enabled=False)
    payload = service.get_status_payload(now=NOW)
    assert payload["status"] == "disabled"
    assert payload["events"]["enabled"] is False


class _Ok:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

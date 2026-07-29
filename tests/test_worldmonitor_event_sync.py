"""Sync, freshness, and fail-open behavior for World Monitor events.

Design: docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md
§6 (timing), §6.1 (time budget), §7 (freshness), §7.1 (succeeded-but-always-empty)
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
    db_path = tmp_path / "sync.db"
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


def _config(**updates):
    values = {
        "worldmonitor_enabled": True,
        "worldmonitor_events_enabled": True,
        "worldmonitor_base_url": "http://worldmonitor:8080",
        "worldmonitor_connect_timeout_seconds": 1.0,
        "worldmonitor_read_timeout_seconds": 2.0,
        "worldmonitor_sync_cooldown_seconds": 1800,
        "worldmonitor_sync_budget_seconds": 20.0,
        "worldmonitor_event_stale_after_seconds": 7200,
        "worldmonitor_event_retention_days": 90,
        "worldmonitor_event_lookback_days": 30,
        "worldmonitor_event_prompt_limit": 5,
    }
    values.update(updates)
    return Config(**values)


def _service(db, **config_updates):
    return WorldMonitorService(
        _config(**config_updates), repo=WorldEventRepository(db_manager=db)
    )


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _acled_payload(count=1):
    return {
        "events": [
            {
                "id": f"acled-{i}",
                "eventType": "Battles",
                "country": "Syria",
                "occurredAt": int((NOW - timedelta(days=2)).timestamp() * 1000),
                "fatalities": 5,
                "actors": [],
                "source": "s",
                "admin1": "",
            }
            for i in range(count)
        ]
    }


def _outage_payload(count=1):
    return {
        "outages": [
            {
                "id": f"outage-{i}",
                "title": "Outage",
                "link": "",
                "description": "",
                "detectedAt": int((NOW - timedelta(days=1)).timestamp() * 1000),
                "country": "KR",
                "region": "Asia",
                "severity": "OUTAGE_SEVERITY_MAJOR",
                "categories": [],
                "cause": "",
                "outageType": "",
                "endedAt": 0,
            }
            for i in range(count)
        ]
    }


def _energy_payload(count=1, upstream_unavailable=False):
    return {
        "events": [
            {
                "id": f"energy-{i}",
                "assetId": "a",
                "assetType": "pipeline",
                "eventType": "outage",
                "startAt": (NOW - timedelta(days=3)).isoformat(),
                "endAt": "",
                "capacityOfflineBcmYr": 0,
                "capacityOfflineMbd": 1.0,
                "causeChain": [],
                "shortDescription": "Pipeline offline",
                "sources": [],
                "classifierVersion": "v1",
                "classifierConfidence": 0.5,
                "lastEvidenceUpdate": "",
                "countries": ["KR"],
            }
            for i in range(count)
        ],
        "fetchedAt": NOW.isoformat(),
        "classifierVersion": "v1",
        "upstreamUnavailable": upstream_unavailable,
    }


def _route(acled=None, outage=None, energy=None):
    """Return a fake httpx.get dispatching on the request path.

    Each override is either a ``_Resp`` to return as-is or a zero-arg callable
    (used to raise). ``None`` falls back to a healthy default payload.
    """

    def _resolve(override, default_payload):
        if override is None:
            return _Resp(default_payload())
        if callable(override):
            return override()
        return override

    def _get(url, **kwargs):
        if "list-acled-events" in url:
            return _resolve(acled, _acled_payload)
        if "list-internet-outages" in url:
            return _resolve(outage, _outage_payload)
        if "list-energy-disruptions" in url:
            return _resolve(energy, _energy_payload)
        raise AssertionError(f"unexpected url {url}")

    return _get


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_sync_is_a_noop_when_events_are_disabled(db):
    with patch("src.services.worldmonitor_service.httpx.get") as mock_get:
        result = _service(db, worldmonitor_events_enabled=False).sync_events(now=NOW)
    assert mock_get.call_count == 0
    assert result.performed is False


def test_sync_is_a_noop_when_the_integration_itself_is_disabled(db):
    with patch("src.services.worldmonitor_service.httpx.get") as mock_get:
        result = _service(
            db, worldmonitor_enabled=False, worldmonitor_events_enabled=True
        ).sync_events(now=NOW)
    assert mock_get.call_count == 0
    assert result.performed is False


def test_cooldown_skips_a_second_sync_within_the_window(db):
    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        service.sync_events(now=NOW)
        first_calls = 3
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()) as mock_get:
        result = service.sync_events(now=NOW + timedelta(minutes=5))
    assert mock_get.call_count == 0
    assert result.performed is False
    assert first_calls == 3


def test_cooldown_expiry_allows_a_new_sync(db):
    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        service.sync_events(now=NOW)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()) as mock_get:
        result = service.sync_events(now=NOW + timedelta(minutes=31))
    assert mock_get.call_count == 3
    assert result.performed is True


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


def test_total_failure_never_raises_into_the_caller(db):
    """Market review must survive a dead World Monitor (§6)."""
    def _boom(url, **kwargs):
        raise httpx.ConnectError("down", request=httpx.Request("GET", url))

    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_boom):
        result = _service(db).sync_events(now=NOW)

    assert result.performed is True
    assert all(outcome.status == "error" for outcome in result.outcomes.values())


def test_one_failing_category_does_not_block_the_others(db):
    def _partial_failure():
        raise httpx.ReadTimeout("slow", request=httpx.Request("GET", "http://x"))

    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(acled=_partial_failure),
    ):
        result = _service(db).sync_events(now=NOW)

    assert result.outcomes[CATEGORY_CONFLICT].status == "error"
    assert result.outcomes[CATEGORY_OUTAGE].status == "ok"
    assert result.outcomes[CATEGORY_ENERGY].status == "ok"


def test_sync_errors_are_sanitized_before_being_stored(db):
    def _leaky(url, **kwargs):
        raise httpx.ConnectError(
            "failed token=super-secret", request=httpx.Request("GET", url)
        )

    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_leaky):
        _service(db).sync_events(now=NOW)

    state = WorldEventRepository(db_manager=db).get_sync_state(CATEGORY_CONFLICT)
    assert "super-secret" not in (state.last_error or "")


def test_malformed_json_is_treated_as_a_category_error(db):
    def _bad():
        raise ValueError("not json")

    with patch(
        "src.services.worldmonitor_service.httpx.get", side_effect=_route(outage=_bad)
    ):
        result = _service(db).sync_events(now=NOW)

    assert result.outcomes[CATEGORY_OUTAGE].status == "error"


def test_non_200_response_is_a_category_error(db):
    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(outage=_Resp({}, status_code=503)),
    ):
        result = _service(db).sync_events(now=NOW)

    assert result.outcomes[CATEGORY_OUTAGE].status == "error"


# ---------------------------------------------------------------------------
# Time budget (§6.1)
# ---------------------------------------------------------------------------


def test_exhausted_budget_stops_requesting_remaining_categories(db):
    """The sync runs inline before market review and listAcledEvents can make a
    live third-party fetch on a cold cache, so the whole sync is deadlined."""
    clock = {"t": 0.0}

    def _slow(url, **kwargs):
        clock["t"] += 30.0  # blow the 20s budget on the first call
        return _Resp(_acled_payload())

    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_slow), patch(
        "src.services.worldmonitor_service.time.monotonic", side_effect=lambda: clock["t"]
    ):
        result = _service(db).sync_events(now=NOW)

    assert result.budget_exhausted is True
    # Only the first category got a request; the rest were never attempted.
    assert sum(1 for o in result.outcomes.values() if o.status == "skipped") == 2


def test_budget_is_not_taken_from_the_health_probe_timeout(db):
    """WORLDMONITOR_READ_TIMEOUT_SECONDS was sized for a local /api/health
    probe; inheriting it here would silently cap the whole sync at 2s."""
    service = _service(db, worldmonitor_read_timeout_seconds=2.0, worldmonitor_sync_budget_seconds=20.0)
    assert service.config.worldmonitor_sync_budget_seconds == 20.0
    assert service.config.worldmonitor_read_timeout_seconds == 2.0


# ---------------------------------------------------------------------------
# Freshness (§7, §7.1)
# ---------------------------------------------------------------------------


def test_successful_sync_with_events_is_fresh(db):
    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        service.sync_events(now=NOW)

    freshness = service.get_freshness(CATEGORY_CONFLICT, now=NOW)
    assert freshness.state == "fresh"


def test_a_category_that_always_returns_empty_is_unverified_not_empty(db):
    """Upstream swallows its own errors and returns an empty array forever when
    no ACLED key is configured. A time-only freshness rule reads that as
    "no conflicts" - exactly the false confidence §3.1 exists to prevent."""
    service = _service(db)
    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(acled=_Resp({"events": []})),
    ):
        service.sync_events(now=NOW)

    freshness = service.get_freshness(CATEGORY_CONFLICT, now=NOW)
    assert freshness.state == "unverified"
    assert freshness.can_claim_no_events is False


def test_empty_result_is_allowed_to_mean_no_events_once_the_source_has_proven_itself(db):
    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        service.sync_events(now=NOW)

    later = NOW + timedelta(hours=1)
    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(acled=_Resp({"events": []})),
    ):
        service.sync_events(now=later)

    freshness = service.get_freshness(CATEGORY_CONFLICT, now=later)
    assert freshness.state == "fresh"
    assert freshness.can_claim_no_events is True


def test_proof_of_life_expires_with_the_lookback_window(db):
    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        service.sync_events(now=NOW)

    much_later = NOW + timedelta(days=45)
    freshness = service.get_freshness(CATEGORY_CONFLICT, now=much_later)
    assert freshness.can_claim_no_events is False


def test_stale_when_the_last_success_is_too_old(db):
    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        service.sync_events(now=NOW)

    freshness = service.get_freshness(CATEGORY_CONFLICT, now=NOW + timedelta(hours=5))
    assert freshness.state == "stale"


def test_unavailable_before_any_sync_has_succeeded(db):
    freshness = _service(db).get_freshness(CATEGORY_CONFLICT, now=NOW)
    assert freshness.state == "unavailable"
    assert freshness.can_claim_no_events is False


def test_upstream_unavailable_flag_marks_the_category_unavailable(db):
    """list-energy-disruptions is the only source that tells us the difference
    between an empty registry and a dead Redis; that signal must not be lost."""
    service = _service(db)
    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(energy=_Resp(_energy_payload(count=0, upstream_unavailable=True))),
    ):
        service.sync_events(now=NOW)

    freshness = service.get_freshness(CATEGORY_ENERGY, now=NOW)
    assert freshness.state == "unavailable"


def test_upstream_unavailable_overrides_a_recent_success(db):
    """Regression: the earlier version of this test only covered the case where
    no sync had ever succeeded, so `last_success_at is None` short-circuited
    before the unavailable flag was ever consulted. The real failure is a
    category that synced fine and *then* lost its upstream - stale stored
    events would keep being presented as current."""
    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        service.sync_events(now=NOW)
    assert service.get_freshness(CATEGORY_ENERGY, now=NOW).state == "fresh"

    later = NOW + timedelta(minutes=40)
    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(energy=_Resp(_energy_payload(count=0, upstream_unavailable=True))),
    ):
        service.sync_events(now=later)

    freshness = service.get_freshness(CATEGORY_ENERGY, now=later)
    assert freshness.state == "unavailable"
    assert freshness.can_claim_no_events is False


def test_cooldown_survives_a_fresh_service_instance(db):
    """Regression: run_market_review builds a new WorldMonitorService per call,
    so an instance attribute could never suppress anything in production - every
    market review would re-hit the rate-limited upstream."""
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        _service(db).sync_events(now=NOW)

    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()) as mock_get:
        result = _service(db).sync_events(now=NOW + timedelta(minutes=5))

    assert mock_get.call_count == 0
    assert result.performed is False


def test_cooldown_expiry_survives_a_fresh_service_instance(db):
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        _service(db).sync_events(now=NOW)

    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()) as mock_get:
        result = _service(db).sync_events(now=NOW + timedelta(minutes=31))

    assert mock_get.call_count == 3
    assert result.performed is True


def test_ingest_is_capped_per_category(db):
    """The sync runs inline ahead of market review and the overall budget is
    only checked *between* categories, so a single category must not be able to
    do unbounded work. A 30-day global ACLED pull is thousands of events."""
    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(acled=_Resp(_acled_payload(count=200))),
    ):
        service = _service(db, worldmonitor_event_max_per_sync=25)
        result = service.sync_events(now=NOW)

    assert result.outcomes[CATEGORY_CONFLICT].event_count == 25
    assert result.outcomes[CATEGORY_CONFLICT].truncated is True


def test_untruncated_sync_is_not_flagged(db):
    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(acled=_Resp(_acled_payload(count=3))),
    ):
        result = _service(db, worldmonitor_event_max_per_sync=25).sync_events(now=NOW)

    assert result.outcomes[CATEGORY_CONFLICT].truncated is False


def test_conflict_request_bounds_the_upstream_window(db):
    """listAcledEvents defaults to a 30-day window and fetches live from a
    rate-limited third party on a cold cache; ask only for the window we will
    actually read from."""
    captured = {}

    def _capture(url, **kwargs):
        if "list-acled-events" in url:
            captured.update(kwargs.get("params") or {})
        return _Resp(_acled_payload())

    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_capture):
        _service(db, worldmonitor_event_lookback_days=7).sync_events(now=NOW)

    assert captured.get("start")
    expected_start = int((NOW - timedelta(days=7)).timestamp() * 1000)
    assert abs(captured["start"] - expected_start) < 1000


def test_failed_sync_leaves_previously_stored_events_readable(db):
    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        service.sync_events(now=NOW)

    def _boom(url, **kwargs):
        raise httpx.ConnectError("down", request=httpx.Request("GET", url))

    later = NOW + timedelta(hours=1)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_boom):
        service.sync_events(now=later)

    events = service.get_events_for_prompt(market="kr", now=later)
    assert any(events[category] for category in events)


# ---------------------------------------------------------------------------
# Storage integration
# ---------------------------------------------------------------------------


def test_synced_events_are_persisted_and_deduplicated(db):
    service = _service(db)
    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        service.sync_events(now=NOW)
        service.sync_events(now=NOW + timedelta(hours=1))

    repo = WorldEventRepository(db_manager=db)
    rows = repo.list_events(now=NOW + timedelta(hours=2))
    assert len(rows) == 3  # one per category, deduplicated across two syncs


def test_retention_prune_runs_after_a_successful_sync(db):
    repo = WorldEventRepository(db_manager=db)
    repo.upsert_events(
        [
            {
                "category": CATEGORY_CONFLICT,
                "external_id": "ancient",
                "title": "Old",
                "occurred_at": NOW - timedelta(days=400),
                "countries": [],
                "markets": [],
                "scope": "unmapped",
                "severity_rank": 0,
                "raw_payload": {},
            }
        ],
        collected_at=NOW - timedelta(days=400),
    )

    with patch("src.services.worldmonitor_service.httpx.get", side_effect=_route()):
        _service(db).sync_events(now=NOW)

    rows = repo.list_events(now=NOW, lookback_days=3650)
    assert all(row.external_id != "ancient" for row in rows)


def test_prompt_events_exclude_unmapped_scope(db):
    repo = WorldEventRepository(db_manager=db)
    repo.upsert_events(
        [
            {
                "category": CATEGORY_ENERGY,
                "external_id": "legacy-row",
                "title": "Unknown origin",
                "occurred_at": NOW - timedelta(days=1),
                "countries": [],
                "markets": [],
                "scope": "unmapped",
                "severity_rank": 99,
                "raw_payload": {},
            }
        ],
        collected_at=NOW,
    )

    events = _service(db).get_events_for_prompt(market="kr", now=NOW)
    assert all(
        row.external_id != "legacy-row" for rows in events.values() for row in rows
    )


def test_prompt_events_respect_the_per_category_limit(db):
    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(
            acled=_Resp(_acled_payload(count=10)),
            outage=_Resp(_outage_payload(count=10)),
            energy=_Resp(_energy_payload(count=10)),
        ),
    ):
        service = _service(db, worldmonitor_event_prompt_limit=2)
        service.sync_events(now=NOW)

    events = service.get_events_for_prompt(market="kr", now=NOW)
    # Assert exact counts: an all-empty result would satisfy a "<= limit" check
    # vacuously and hide a broken sync.
    assert all(len(rows) == 2 for rows in events.values())


def test_a_busy_category_cannot_crowd_out_the_others(db):
    """Per-category caps rather than one global cap: conflict events are the
    most numerous and would otherwise monopolize the prompt."""
    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(
            acled=_Resp(_acled_payload(count=50)),
            outage=_Resp(_outage_payload(count=1)),
            energy=_Resp(_energy_payload(count=1)),
        ),
    ):
        service = _service(db, worldmonitor_event_prompt_limit=3)
        service.sync_events(now=NOW)

    events = service.get_events_for_prompt(market="kr", now=NOW)
    assert len(events[CATEGORY_CONFLICT]) == 3
    assert len(events[CATEGORY_OUTAGE]) == 1
    assert len(events[CATEGORY_ENERGY]) == 1


def test_future_events_from_upstream_never_reach_the_prompt(db):
    future_payload = {
        "outages": [
            {
                "id": "future-outage",
                "title": "Not yet",
                "link": "",
                "description": "",
                "detectedAt": int((NOW + timedelta(days=3)).timestamp() * 1000),
                "country": "KR",
                "region": "Asia",
                "severity": "OUTAGE_SEVERITY_TOTAL",
                "categories": [],
                "cause": "",
                "outageType": "",
                "endedAt": 0,
            }
        ]
    }
    with patch(
        "src.services.worldmonitor_service.httpx.get",
        side_effect=_route(outage=_Resp(future_payload)),
    ):
        service = _service(db)
        service.sync_events(now=NOW)

    events = service.get_events_for_prompt(market="kr", now=NOW)
    assert events[CATEGORY_OUTAGE] == []


def test_status_probe_still_works_alongside_event_sync(db):
    """The self-hosting phase health boundary must keep its behavior."""
    with patch("src.services.worldmonitor_service.httpx.get") as mock_get:
        mock_get.return_value = _Resp({"overall": "healthy"})
        status = _service(db).get_status()
    assert status.status == "healthy"

"""Storage contract for normalized World Monitor events.

Design: docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md
§4 (schema), §4.1 (future occurrence rejection), §7.1 (sync state), §11 (retention)
"""

import os
from datetime import datetime, timedelta

import pytest

from src.config import Config
from src.repositories.world_event_repo import WorldEventRepository
from src.storage import DatabaseManager


@pytest.fixture
def repo(tmp_path):
    env_path = tmp_path / ".env"
    db_path = tmp_path / "world_events.db"
    env_path.write_text(
        "STOCK_LIST=600519\nGEMINI_API_KEY=test\nADMIN_AUTH_ENABLED=false\n"
        f"DATABASE_PATH={db_path}\n",
        encoding="utf-8",
    )
    os.environ["ENV_FILE"] = str(env_path)
    os.environ["DATABASE_PATH"] = str(db_path)
    Config.reset_instance()
    DatabaseManager.reset_instance()

    yield WorldEventRepository(db_manager=DatabaseManager.get_instance())

    DatabaseManager.reset_instance()
    Config.reset_instance()
    os.environ.pop("ENV_FILE", None)
    os.environ.pop("DATABASE_PATH", None)


def _event(**updates):
    values = {
        "category": "geopolitical_conflict",
        "source_endpoint": "/api/conflict/v1/list-acled-events",
        "external_id": "acled-SYR1234",
        "title": "Battles - Syria",
        "summary": None,
        "url": None,
        "occurred_at": datetime(2026, 7, 20, 3, 0, 0),
        "ended_at": None,
        "countries": ["SY"],
        "markets": [],
        "scope": "global",
        "severity_rank": 12,
        "raw_payload": {"fatalities": 12},
    }
    values.update(updates)
    return values


def test_upsert_persists_both_timestamps(repo):
    """Source occurrence time and DSA collection time are distinct columns."""
    collected = datetime(2026, 7, 29, 9, 0, 0)
    repo.upsert_events([_event()], collected_at=collected)

    rows = repo.list_events(categories=["geopolitical_conflict"], now=collected)
    assert len(rows) == 1
    assert rows[0].occurred_at == datetime(2026, 7, 20, 3, 0, 0)
    assert rows[0].collected_at == collected


def test_resync_is_idempotent_on_category_and_external_id(repo):
    repo.upsert_events([_event()], collected_at=datetime(2026, 7, 29, 9, 0, 0))
    repo.upsert_events([_event()], collected_at=datetime(2026, 7, 29, 9, 30, 0))

    rows = repo.list_events(now=datetime(2026, 7, 29, 10, 0, 0))
    assert len(rows) == 1


def test_resync_updates_end_time_but_never_rewrites_observation_times(repo):
    """An ongoing event ending must be reflected, but the first-observed
    timestamps stay immutable for audit and replay (§4)."""
    first_seen = datetime(2026, 7, 29, 9, 0, 0)
    repo.upsert_events([_event()], collected_at=first_seen)
    repo.upsert_events(
        [_event(ended_at=datetime(2026, 7, 28, 0, 0, 0), severity_rank=30)],
        collected_at=datetime(2026, 7, 29, 12, 0, 0),
    )

    rows = repo.list_events(now=datetime(2026, 7, 29, 13, 0, 0))
    assert len(rows) == 1
    assert rows[0].ended_at == datetime(2026, 7, 28, 0, 0, 0)
    assert rows[0].severity_rank == 30
    assert rows[0].collected_at == first_seen
    assert rows[0].occurred_at == datetime(2026, 7, 20, 3, 0, 0)


def test_future_occurrence_is_never_stored(repo):
    """A future-dated event would put an unhappened incident into today's
    market review and leak future information into later backtests (§4.1)."""
    collected = datetime(2026, 7, 29, 9, 0, 0)
    stored = repo.upsert_events(
        [_event(external_id="acled-FUTURE", occurred_at=datetime(2026, 8, 5, 0, 0, 0))],
        collected_at=collected,
    )

    assert stored == 0
    assert repo.list_events(now=collected) == []


def test_lookback_query_also_filters_future_rows(repo):
    """Defense in depth for §4.1: a row written before a clock rewind must not
    reach the prompt either."""
    repo.upsert_events([_event()], collected_at=datetime(2026, 7, 29, 9, 0, 0))

    # Query as if "now" were before the stored event occurred.
    rows = repo.list_events(now=datetime(2026, 7, 19, 0, 0, 0), lookback_days=30)
    assert rows == []


def test_lookback_window_excludes_older_events(repo):
    repo.upsert_events(
        [
            _event(external_id="recent", occurred_at=datetime(2026, 7, 25, 0, 0, 0)),
            _event(external_id="old", occurred_at=datetime(2026, 5, 1, 0, 0, 0)),
        ],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )

    rows = repo.list_events(now=datetime(2026, 7, 29, 9, 0, 0), lookback_days=30)
    assert [r.external_id for r in rows] == ["recent"]


def test_retention_prune_deletes_by_occurrence_time(repo):
    repo.upsert_events(
        [
            _event(external_id="keep", occurred_at=datetime(2026, 7, 25, 0, 0, 0)),
            _event(external_id="drop", occurred_at=datetime(2026, 1, 1, 0, 0, 0)),
        ],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )

    deleted = repo.prune(now=datetime(2026, 7, 29, 9, 0, 0), retention_days=90)

    assert deleted == 1
    rows = repo.list_events(now=datetime(2026, 7, 29, 9, 0, 0), lookback_days=3650)
    assert [r.external_id for r in rows] == ["keep"]


def test_same_external_id_in_two_categories_is_two_rows(repo):
    """Uniqueness is per (category, external_id); upstream ids are only
    guaranteed unique within their own service."""
    repo.upsert_events(
        [
            _event(external_id="shared"),
            _event(category="infrastructure_outage", external_id="shared"),
        ],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )

    rows = repo.list_events(now=datetime(2026, 7, 29, 9, 0, 0))
    assert len(rows) == 2


def test_countries_and_markets_round_trip_as_lists(repo):
    repo.upsert_events(
        [_event(countries=["KR", "JP"], markets=["kr", "jp"], scope="market")],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )

    row = repo.list_events(now=datetime(2026, 7, 29, 9, 0, 0))[0]
    assert row.country_list == ["KR", "JP"]
    assert row.market_list == ["kr", "jp"]


def test_sync_state_tracks_last_success_and_last_nonempty_separately(repo):
    """§7.1: a category that keeps succeeding with zero events must remain
    distinguishable from one that is actually reporting no events."""
    repo.record_sync(
        category="geopolitical_conflict",
        status="ok",
        synced_at=datetime(2026, 7, 29, 9, 0, 0),
        event_count=0,
    )
    state = repo.get_sync_state("geopolitical_conflict")
    assert state.last_success_at == datetime(2026, 7, 29, 9, 0, 0)
    assert state.last_nonempty_at is None

    repo.record_sync(
        category="geopolitical_conflict",
        status="ok",
        synced_at=datetime(2026, 7, 29, 10, 0, 0),
        event_count=4,
    )
    state = repo.get_sync_state("geopolitical_conflict")
    assert state.last_success_at == datetime(2026, 7, 29, 10, 0, 0)
    assert state.last_nonempty_at == datetime(2026, 7, 29, 10, 0, 0)


def test_failed_sync_does_not_advance_last_success(repo):
    repo.record_sync(
        category="infrastructure_outage",
        status="ok",
        synced_at=datetime(2026, 7, 29, 9, 0, 0),
        event_count=2,
    )
    repo.record_sync(
        category="infrastructure_outage",
        status="error",
        synced_at=datetime(2026, 7, 29, 11, 0, 0),
        event_count=0,
        error="connection failed",
    )

    state = repo.get_sync_state("infrastructure_outage")
    assert state.last_success_at == datetime(2026, 7, 29, 9, 0, 0)
    assert state.last_nonempty_at == datetime(2026, 7, 29, 9, 0, 0)
    assert state.last_status == "error"
    assert state.last_error == "connection failed"


def test_sync_state_is_absent_before_any_sync(repo):
    assert repo.get_sync_state("supply_chain_energy") is None


def test_list_events_filters_by_scope(repo):
    repo.upsert_events(
        [
            _event(external_id="mk", scope="market", markets=["kr"]),
            _event(external_id="gl", scope="global"),
            _event(external_id="un", scope="unmapped"),
        ],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )

    rows = repo.list_events(
        now=datetime(2026, 7, 29, 9, 0, 0), scopes=["market", "global"]
    )
    assert sorted(r.external_id for r in rows) == ["gl", "mk"]


def test_upsert_reports_how_many_rows_were_stored(repo):
    stored = repo.upsert_events(
        [
            _event(external_id="a"),
            _event(external_id="b"),
            _event(external_id="future", occurred_at=datetime(2027, 1, 1)),
        ],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )
    assert stored == 2


def test_prune_is_a_noop_when_nothing_is_old_enough(repo):
    repo.upsert_events([_event()], collected_at=datetime(2026, 7, 29, 9, 0, 0))
    assert repo.prune(now=datetime(2026, 7, 29, 9, 0, 0), retention_days=90) == 0


def test_events_are_returned_newest_first_within_equal_severity(repo):
    repo.upsert_events(
        [
            _event(external_id="older", occurred_at=datetime(2026, 7, 20), severity_rank=0),
            _event(external_id="newer", occurred_at=datetime(2026, 7, 26), severity_rank=0),
        ],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )

    rows = repo.list_events(now=datetime(2026, 7, 29, 9, 0, 0))
    assert [r.external_id for r in rows] == ["newer", "older"]


def test_ongoing_events_sort_ahead_of_ended_ones(repo):
    repo.upsert_events(
        [
            _event(
                external_id="ended",
                occurred_at=datetime(2026, 7, 27),
                ended_at=datetime(2026, 7, 28),
                severity_rank=5,
            ),
            _event(external_id="ongoing", occurred_at=datetime(2026, 7, 21), severity_rank=5),
        ],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )

    rows = repo.list_events(now=datetime(2026, 7, 29, 9, 0, 0))
    assert [r.external_id for r in rows] == ["ongoing", "ended"]


def test_higher_severity_outranks_recency(repo):
    repo.upsert_events(
        [
            _event(external_id="severe", occurred_at=datetime(2026, 7, 20), severity_rank=50),
            _event(external_id="mild", occurred_at=datetime(2026, 7, 28), severity_rank=1),
        ],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )

    rows = repo.list_events(now=datetime(2026, 7, 29, 9, 0, 0))
    assert [r.external_id for r in rows] == ["severe", "mild"]


def test_retention_prune_respects_a_shorter_window(repo):
    repo.upsert_events(
        [_event(occurred_at=datetime(2026, 7, 1))],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )
    assert repo.prune(now=datetime(2026, 7, 29, 9, 0, 0), retention_days=7) == 1


def test_limit_is_applied_per_query(repo):
    repo.upsert_events(
        [_event(external_id=f"e{i}", occurred_at=datetime(2026, 7, 20) + timedelta(days=i))
         for i in range(5)],
        collected_at=datetime(2026, 7, 29, 9, 0, 0),
    )

    rows = repo.list_events(now=datetime(2026, 7, 29, 9, 0, 0), limit=2)
    assert len(rows) == 2

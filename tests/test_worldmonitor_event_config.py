"""Config parsing for the World Monitor event-integration settings.

Design: docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md §10
"""

import os
from unittest.mock import patch

from src.config import Config


def _from_env(**env):
    """Build a Config from a clean environment holding only ``env``.

    ``clear=True`` keeps a developer's real ``.env``-derived process
    environment from leaking into these assertions.
    """
    with patch("src.config.setup_env"), patch.object(
        Config, "_parse_litellm_yaml", return_value=[]
    ), patch.object(Config, "_parse_stock_email_groups", return_value=[]):
        with patch.dict(os.environ, {"STOCK_LIST": "600519", **env}, clear=True):
            return Config._load_from_env()


def test_event_settings_default_to_disabled_and_documented_values():
    config = Config()
    assert config.worldmonitor_events_enabled is False
    assert config.worldmonitor_sync_cooldown_seconds == 1800
    assert config.worldmonitor_sync_budget_seconds == 20.0
    assert config.worldmonitor_event_stale_after_seconds == 7200
    assert config.worldmonitor_event_retention_days == 90
    assert config.worldmonitor_event_lookback_days == 30
    assert config.worldmonitor_event_prompt_limit == 5


def test_event_settings_parse_from_env():
    config = _from_env(
        WORLDMONITOR_EVENTS_ENABLED="true",
        WORLDMONITOR_SYNC_COOLDOWN_SECONDS="600",
        WORLDMONITOR_SYNC_BUDGET_SECONDS="12.5",
        WORLDMONITOR_EVENT_STALE_AFTER_SECONDS="3600",
        WORLDMONITOR_EVENT_RETENTION_DAYS="45",
        WORLDMONITOR_EVENT_LOOKBACK_DAYS="14",
        WORLDMONITOR_EVENT_PROMPT_LIMIT="3",
    )
    assert config.worldmonitor_events_enabled is True
    assert config.worldmonitor_sync_cooldown_seconds == 600
    assert config.worldmonitor_sync_budget_seconds == 12.5
    assert config.worldmonitor_event_stale_after_seconds == 3600
    assert config.worldmonitor_event_retention_days == 45
    assert config.worldmonitor_event_lookback_days == 14
    assert config.worldmonitor_event_prompt_limit == 3


def test_event_settings_reject_out_of_range_values():
    """Zero/negative windows would silently disable retention or the lookback
    filter, so they fall back to the documented default rather than being used."""
    config = _from_env(
        WORLDMONITOR_SYNC_COOLDOWN_SECONDS="-1",
        WORLDMONITOR_EVENT_RETENTION_DAYS="0",
        WORLDMONITOR_EVENT_LOOKBACK_DAYS="0",
        WORLDMONITOR_EVENT_PROMPT_LIMIT="0",
    )
    assert config.worldmonitor_sync_cooldown_seconds >= 0
    assert config.worldmonitor_event_retention_days >= 1
    assert config.worldmonitor_event_lookback_days >= 1
    assert config.worldmonitor_event_prompt_limit >= 1


def test_sync_budget_rejects_non_finite_values():
    """NaN compares false against every bound and would defeat the deadline
    check that keeps the inline sync from stalling market review (§6.1)."""
    config = _from_env(WORLDMONITOR_SYNC_BUDGET_SECONDS="nan")
    assert config.worldmonitor_sync_budget_seconds == 20.0


def test_events_enabled_is_independent_of_the_status_probe_flag():
    """WORLDMONITOR_ENABLED only gates the health probe shipped in the
    self-hosting phase; event injection must be opted into separately so an
    upgrade alone never changes the market-review prompt (§10)."""
    config = _from_env(WORLDMONITOR_ENABLED="true")
    assert config.worldmonitor_enabled is True
    assert config.worldmonitor_events_enabled is False

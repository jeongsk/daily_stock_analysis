# -*- coding: utf-8 -*-
"""World Monitor 이벤트가 시장 리뷰 프롬프트에 붙는 경로. 오프라인.

설계: docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md §6/§9

핵심 계약:
  - 비활성 시 기존 프롬프트와 바이트 동일 (기존 사용자 무영향)
  - 활성 시 3개 언어 모두에 블록이 붙는다
  - 동기화 실패가 프롬프트 조립을 깨뜨리지 않는다 (fail-open)
"""

import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_analyzer import MarketAnalyzer, MarketOverview
from src.services.worldmonitor_events import (
    CATEGORY_CONFLICT,
    CATEGORY_ENERGY,
    CATEGORY_OUTAGE,
    CategoryFreshness,
)

NOW = datetime(2026, 7, 29, 9, 0, 0)


class _Row:
    def __init__(self, title, occurred_at):
        self.title = title
        self.occurred_at = occurred_at
        self.ended_at = None
        self.summary = None
        self.country_list = ["KR"]

    @property
    def is_ongoing(self):
        return True


def _overview():
    return MarketOverview(date="2026-07-29")


def _config(language="ko", **updates):
    values = {"report_language": language}
    values.update(updates)
    return SimpleNamespace(**values)


def _analyzer(language="ko", region="kr", **config_updates):
    return MarketAnalyzer(
        region=region, analyzer=None, config=_config(language, **config_updates)
    )


def _fresh_service():
    """A service stub reporting one conflict event and two proven-empty categories."""
    service = Mock()
    service.get_events_for_prompt.return_value = {
        CATEGORY_CONFLICT: [_Row("Battles - Syria", datetime(2026, 7, 20))],
        CATEGORY_OUTAGE: [],
        CATEGORY_ENERGY: [],
    }
    service.get_all_freshness.return_value = {
        c: CategoryFreshness(
            category=c,
            state="fresh",
            last_success_at=NOW,
            last_nonempty_at=NOW,
            can_claim_no_events=True,
        )
        for c in (CATEGORY_CONFLICT, CATEGORY_OUTAGE, CATEGORY_ENERGY)
    }
    return service


# ---------------------------------------------------------------------------
# 비활성 시 무변경
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["ko", "en", "zh"])
def test_prompt_is_unchanged_when_events_are_disabled(language):
    """기존 배포는 WORLDMONITOR_EVENTS_ENABLED 없이 돌아간다. 업그레이드만으로
    프롬프트가 달라지면 안 된다."""
    baseline = _analyzer(language)._build_review_prompt(_overview(), [])
    explicit_off = _analyzer(
        language, worldmonitor_events_enabled=False
    )._build_review_prompt(_overview(), [])
    assert explicit_off == baseline


@pytest.mark.parametrize("language", ["ko", "en", "zh"])
def test_disabled_prompt_has_no_global_risk_heading(language):
    prompt = _analyzer(language)._build_review_prompt(_overview(), [])
    for heading in ("글로벌 리스크 이벤트", "Global Risk Events", "全球风险事件"):
        assert heading not in prompt


def test_config_without_the_attribute_at_all_does_not_raise():
    """SimpleNamespace configs (and older persisted configs) lack the new field."""
    analyzer = MarketAnalyzer(
        region="kr", analyzer=None, config=SimpleNamespace(report_language="ko")
    )
    assert analyzer._build_review_prompt(_overview(), [])


# ---------------------------------------------------------------------------
# 활성 시 주입
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "language,heading",
    [
        ("ko", "글로벌 리스크 이벤트"),
        ("en", "Global Risk Events"),
        ("zh", "全球风险事件"),
    ],
)
def test_enabled_prompt_contains_the_block(language, heading):
    analyzer = _analyzer(language, worldmonitor_events_enabled=True)
    with patch.object(analyzer, "_worldmonitor_service", return_value=_fresh_service()):
        prompt = analyzer._build_review_prompt(_overview(), [])
    assert heading in prompt
    assert "Battles - Syria" in prompt


def test_enabled_block_is_scoped_to_the_analyzer_region():
    analyzer = _analyzer("ko", region="kr", worldmonitor_events_enabled=True)
    service = _fresh_service()
    with patch.object(analyzer, "_worldmonitor_service", return_value=service):
        analyzer._build_review_prompt(_overview(), [])
    assert service.get_events_for_prompt.call_args.kwargs["market"] == "kr"


# ---------------------------------------------------------------------------
# fail-open
# ---------------------------------------------------------------------------


def test_a_failing_event_lookup_does_not_break_prompt_assembly():
    """World Monitor 장애가 시장 리뷰 자체를 실패시키면 안 된다 (§6)."""
    analyzer = _analyzer("ko", worldmonitor_events_enabled=True)
    broken = Mock()
    broken.get_events_for_prompt.side_effect = RuntimeError("db gone")

    with patch.object(analyzer, "_worldmonitor_service", return_value=broken):
        prompt = analyzer._build_review_prompt(_overview(), [])

    assert prompt
    assert "글로벌 리스크 이벤트" not in prompt


def test_service_construction_failure_is_swallowed():
    analyzer = _analyzer("ko", worldmonitor_events_enabled=True)
    with patch.object(
        analyzer, "_worldmonitor_service", side_effect=RuntimeError("no config")
    ):
        assert analyzer._build_review_prompt(_overview(), [])


# ---------------------------------------------------------------------------
# run_market_review 연결
# ---------------------------------------------------------------------------


def test_market_review_syncs_before_building_the_report():
    """동기화는 분석기가 데이터를 모으기 전에 한 번 돌아야 한다 (§6)."""
    from src.core import market_review

    service = Mock()
    service.sync_events.return_value = SimpleNamespace(performed=True, outcomes={})

    with patch.object(
        market_review, "WorldMonitorService", return_value=service
    ) as factory:
        market_review._sync_worldmonitor_events(
            SimpleNamespace(worldmonitor_enabled=True, worldmonitor_events_enabled=True)
        )

    assert factory.called
    assert service.sync_events.called


def test_market_review_sync_never_raises():
    from src.core import market_review

    with patch.object(
        market_review, "WorldMonitorService", side_effect=RuntimeError("boom")
    ):
        # Must not propagate: a World Monitor problem cannot stop market review.
        market_review._sync_worldmonitor_events(
            SimpleNamespace(worldmonitor_enabled=True, worldmonitor_events_enabled=True)
        )


def test_market_review_skips_sync_when_disabled():
    from src.core import market_review

    with patch.object(market_review, "WorldMonitorService") as factory:
        market_review._sync_worldmonitor_events(
            SimpleNamespace(worldmonitor_enabled=True, worldmonitor_events_enabled=False)
        )
    assert not factory.called

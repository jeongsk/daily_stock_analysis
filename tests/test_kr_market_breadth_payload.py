# -*- coding: utf-8 -*-
"""구조화 payload의 KR 전용 kr_market_context.breadth 계약 고정. 오프라인.

계약(스펙 D3/D7/D12):
  - KR + 유효 폭 -> payload["kr_market_context"]["breadth"] (유효 시장만)
  - 무데이터/비KR -> kr_market_context 키 부재
  - 기존 평면 breadth 계약 불변(KR은 has_market_stats=False라 평면 breadth 없음)
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_analyzer import MarketAnalyzer, MarketOverview

_BREADTH = {
    "kospi": {
        "market": "kospi", "up_count": 384, "down_count": 488, "flat_count": 40,
        "as_of": "2026-07-16", "session": "close", "source": "NAVER", "stale": False,
    },
    "kosdaq": {
        "market": "kosdaq", "up_count": 501, "down_count": 1182, "flat_count": 56,
        "as_of": "2026-07-16", "session": "close", "source": "NAVER", "stale": False,
    },
}


def _overview(breadth=None):
    ov = MarketOverview(date="2026-07-16")
    ov.kr_market_breadth = breadth
    return ov


def _analyzer(region="kr"):
    return MarketAnalyzer(region=region, analyzer=None, config=SimpleNamespace(report_language="ko"))


class TestPayloadKrMarketContext:
    def test_kr_payload_has_breadth(self):
        payload = _analyzer("kr").build_market_review_payload(_overview(_BREADTH), [], "## 리뷰\n본문")
        assert "kr_market_context" in payload
        breadth = payload["kr_market_context"]["breadth"]
        assert set(breadth) == {"kospi", "kosdaq"}
        assert breadth["kospi"]["up_count"] == 384
        assert breadth["kosdaq"]["session"] == "close"

    def test_kr_payload_only_valid_market(self):
        payload = _analyzer("kr").build_market_review_payload(
            _overview({"kospi": _BREADTH["kospi"]}), [], "## 리뷰\n본문"
        )
        assert set(payload["kr_market_context"]["breadth"]) == {"kospi"}

    def test_no_key_without_data(self):
        payload = _analyzer("kr").build_market_review_payload(_overview(None), [], "## 리뷰\n본문")
        assert "kr_market_context" not in payload

    def test_invalid_record_dropped(self):
        # as_of 없는 레코드는 payload에 싣지 않는다(스펙 D9 — 서비스 측 2차 방어)
        broken = {"kospi": {"market": "kospi", "up_count": 1, "down_count": 2, "flat_count": 3}}
        payload = _analyzer("kr").build_market_review_payload(_overview(broken), [], "## 리뷰\n본문")
        assert "kr_market_context" not in payload

    def test_non_kr_has_no_key(self):
        payload = _analyzer("us").build_market_review_payload(_overview(None), [], "## Review\nbody")
        assert "kr_market_context" not in payload

    def test_flat_breadth_contract_unchanged_for_kr(self):
        # KR은 has_market_stats=False — 평면 breadth 키는 여전히 생성되지 않는다(D12)
        payload = _analyzer("kr").build_market_review_payload(_overview(_BREADTH), [], "## 리뷰\n본문")
        assert "breadth" not in payload

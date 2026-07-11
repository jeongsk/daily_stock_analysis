# -*- coding: utf-8 -*-
"""Phase 3: 구조화 마켓 리뷰 페이로드에 KR 시장 수급 원시 레코드가 실리는지 고정.

KR + 데이터 -> payload["investor_flows"], 비KR/무데이터 -> 키 부재. 오프라인.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_analyzer import MarketAnalyzer, MarketOverview

_FLOWS = {
    "kospi": {
        "market": "kospi", "unit": "KRW",
        "days": [{"date": "2026-07-10", "foreign_net": -322800000000, "institution_net": 1131400000000, "individual_net": -808600000000}],
        "summary": {"foreign_net_5d": -322800000000, "institution_net_5d": 1131400000000},
        "source": "NAVER",
    },
    "kosdaq": {
        "market": "kosdaq", "unit": "KRW",
        "days": [{"date": "2026-07-10", "foreign_net": 51200000000, "institution_net": -28700000000, "individual_net": -22500000000}],
        "summary": {"foreign_net_5d": 51200000000, "institution_net_5d": -28700000000},
        "source": "NAVER",
    },
}


def _overview(flows=None):
    ov = MarketOverview(date="2026-07-10")
    ov.investor_flows = flows
    return ov


def _analyzer(region="kr"):
    return MarketAnalyzer(region=region, analyzer=None, config=SimpleNamespace(report_language="ko"))


class TestPayloadInvestorFlows:
    def test_kr_payload_has_flows(self):
        payload = _analyzer("kr").build_market_review_payload(_overview(_FLOWS), [], "## 리뷰\n본문")
        assert "investor_flows" in payload
        assert set(payload["investor_flows"]) == {"kospi", "kosdaq"}
        assert payload["investor_flows"]["kospi"]["summary"]["institution_net_5d"] == 1131400000000

    def test_kr_payload_only_available_market(self):
        payload = _analyzer("kr").build_market_review_payload(
            _overview({"kospi": _FLOWS["kospi"]}), [], "## 리뷰\n본문"
        )
        assert set(payload["investor_flows"]) == {"kospi"}

    def test_no_flows_key_without_data(self):
        payload = _analyzer("kr").build_market_review_payload(_overview(None), [], "## 리뷰\n본문")
        assert "investor_flows" not in payload

    def test_non_kr_has_no_flows_key(self):
        # 비KR overview는 investor_flows None -> 키 부재
        payload = _analyzer("us").build_market_review_payload(_overview(None), [], "## Review\nbody")
        assert "investor_flows" not in payload

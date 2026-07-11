# -*- coding: utf-8 -*-
"""Phase 3: 마켓 리뷰 리포트 본문에 KR 시장 수급 결정적 블록이 주입되는지 고정.

_build_kr_market_flows_block(로케일) + _inject_data_into_review(시장 요약 섹션 뒤,
헤딩 없으면 fallback append). ko 블록은 순수 한글(거부 게이트 안전). 오프라인.
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


def _analyzer(language):
    return MarketAnalyzer(region="kr", analyzer=None, config=SimpleNamespace(report_language=language))


class TestBuildKrMarketFlowsBlock:
    def test_ko_block_hangul_only(self):
        block = _analyzer("ko")._build_kr_market_flows_block(_overview(_FLOWS))
        assert "시장 투자자 수급" in block
        assert "KOSPI" in block and "-3,228억" in block
        assert "KOSDAQ" in block and "+512억" in block
        assert "2026-07-10" in block
        assert not any("一" <= c <= "鿿" for c in block)

    def test_en_and_zh_block(self):
        assert "₩-322.8B" in _analyzer("en")._build_kr_market_flows_block(_overview(_FLOWS))
        assert "亿韩元" in _analyzer("zh")._build_kr_market_flows_block(_overview(_FLOWS))

    def test_empty_without_data(self):
        assert _analyzer("ko")._build_kr_market_flows_block(_overview(None)) == ""


class TestInjectFlowsIntoReview:
    def test_inject_after_market_summary_heading(self):
        an = _analyzer("ko")
        review = "## 2026-07-10 리뷰\n\n### 1. 시장 요약\n오늘은 혼조였습니다.\n\n### 2. 지수 구조\n지수 설명.\n"
        out = an._inject_data_into_review(review, _overview(_FLOWS))
        assert "시장 투자자 수급" in out
        # 시장 요약 섹션 안(지수 구조 앞)에 주입
        assert out.index("시장 투자자 수급") < out.index("### 2. 지수 구조")

    def test_fallback_append_when_heading_missing(self):
        an = _analyzer("ko")
        review = "## 2026-07-10 리뷰\n\n본문만 있고 표준 헤딩이 없습니다.\n"
        out = an._inject_data_into_review(review, _overview(_FLOWS))
        assert "시장 투자자 수급" in out
        assert "-3,228억" in out

    def test_no_injection_without_data(self):
        an = _analyzer("ko")
        review = "## 2026-07-10 리뷰\n\n### 1. 시장 요약\n내용.\n"
        out = an._inject_data_into_review(review, _overview(None))
        assert "시장 투자자 수급" not in out

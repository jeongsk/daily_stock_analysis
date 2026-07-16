# -*- coding: utf-8 -*-
"""KR 시장 폭 프롬프트/결정적 본문 계약 고정. 오프라인.

계약(스펙 D1/D8):
  - 폭 데이터 -> stats_block이 실제 폭 섹션으로 대체(수급 덮어쓰기 아님)
  - 폭+수급 동시 -> 두 독립 섹션 + 교차 해석 가이드 1줄
  - 폭 없음+수급만 -> 기존 수급 대체 동작 유지(회귀 없음)
  - 결정적 블록: 시장 요약 주입 + fallback append, ko 순수 한글
  - 비KR 프롬프트 불변
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
        "as_of": "2026-07-16", "session": "intraday", "source": "NAVER", "stale": True,
    },
}
_FLOWS = {
    "kospi": {
        "market": "kospi", "unit": "KRW",
        "days": [{"date": "2026-07-16", "foreign_net": -322800000000, "institution_net": 1131400000000, "individual_net": None}],
        "summary": {"foreign_net_5d": -322800000000, "institution_net_5d": 1131400000000},
        "source": "NAVER",
    },
}


def _overview(breadth=None, flows=None):
    ov = MarketOverview(date="2026-07-16")
    ov.kr_market_breadth = breadth
    ov.investor_flows = flows
    return ov


def _analyzer(language="ko", region="kr"):
    return MarketAnalyzer(region=region, analyzer=None, config=SimpleNamespace(report_language=language))


class TestBreadthPromptBlock:
    def test_ko_prompt_contains_breadth_lines(self):
        prompt = _analyzer("ko")._build_review_prompt(_overview(breadth=_BREADTH), [])
        assert "## 시장 폭 (KOSPI/KOSDAQ)" in prompt
        assert "- KOSPI: 상승 384 / 하락 488 / 보합 40 · 07-16 마감 · NAVER" in prompt
        assert "- KOSDAQ: 상승 501 / 하락 1182 / 보합 56 · 07-16 장중 (지연) · NAVER" in prompt
        # 폭이 있으면 "데이터 없음" 문구는 사라진다
        assert "상승/하락 종목 수 통계를 사용할 수 없습니다" not in prompt

    def test_breadth_and_flows_are_independent_sections_with_cross_guide(self):
        prompt = _analyzer("ko")._build_review_prompt(_overview(breadth=_BREADTH, flows=_FLOWS), [])
        assert "## 시장 폭 (KOSPI/KOSDAQ)" in prompt
        assert "## 시장 투자자 수급 (KOSPI/KOSDAQ)" in prompt
        assert prompt.index("## 시장 폭") < prompt.index("## 시장 투자자 수급")
        assert "일치/엇갈림만 교차 해석" in prompt

    def test_flows_only_keeps_existing_substitution(self):
        prompt = _analyzer("ko")._build_review_prompt(_overview(flows=_FLOWS), [])
        assert "## 시장 투자자 수급 (KOSPI/KOSDAQ)" in prompt
        assert "## 시장 폭 (KOSPI/KOSDAQ)" not in prompt
        assert "교차 해석" not in prompt

    def test_no_data_keeps_placeholder(self):
        prompt = _analyzer("ko")._build_review_prompt(_overview(), [])
        assert "상승/하락 종목 수 통계를 사용할 수 없습니다" in prompt

    def test_en_data_limits_drop_breadth_clause_when_present(self):
        prompt = _analyzer("en")._build_review_prompt(_overview(breadth=_BREADTH, flows=_FLOWS), [])
        assert "## Market Breadth (KOSPI/KOSDAQ)" in prompt
        assert "Limit-up/limit-down counts, aggregate turnover, and participation are not available" in prompt
        assert "Market breadth, aggregate turnover" not in prompt

    def test_non_kr_prompt_untouched(self):
        prompt = _analyzer("en", region="us")._build_review_prompt(_overview(), [])
        assert "KOSPI" not in prompt
        assert "Market breadth, aggregate turnover, participation, and fund-flow signals are not available" in prompt


class TestBreadthDeterministicBlock:
    def test_ko_block_hangul_only(self):
        block = _analyzer("ko")._build_kr_market_breadth_block(_overview(breadth=_BREADTH))
        assert "시장 폭" in block
        assert "KOSPI" in block and "KOSDAQ" in block
        assert not any("一" <= c <= "鿿" for c in block)

    def test_inject_breadth_before_flows_in_market_summary(self):
        an = _analyzer("ko")
        review = "## 2026-07-16 리뷰\n\n### 1. 시장 요약\n혼조.\n\n### 2. 지수 구조\n설명.\n"
        out = an._inject_data_into_review(review, _overview(breadth=_BREADTH, flows=_FLOWS))
        assert "시장 폭" in out and "시장 투자자 수급" in out
        assert out.index("시장 폭") < out.index("시장 투자자 수급")
        assert out.index("시장 투자자 수급") < out.index("### 2. 지수 구조")

    def test_fallback_append_when_heading_missing(self):
        an = _analyzer("ko")
        review = "## 2026-07-16 리뷰\n\n표준 헤딩 없음.\n"
        out = an._inject_data_into_review(review, _overview(breadth=_BREADTH))
        assert "상승 384" in out

    def test_no_injection_without_data(self):
        an = _analyzer("ko")
        review = "## 2026-07-16 리뷰\n\n### 1. 시장 요약\n내용.\n"
        out = an._inject_data_into_review(review, _overview())
        assert "시장 폭**(상승/하락/보합" not in out

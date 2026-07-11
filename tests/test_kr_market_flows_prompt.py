# -*- coding: utf-8 -*-
"""Phase 3: 마켓 리뷰 LLM 프롬프트에 KR 시장 수급 섹션이 로케일별로 주입되는지 고정.

KR은 has_market_stats=False라 수급이 곧 시장 폭 신호 — KR stats_block을 수급으로
대체한다. 비KR(us 등) 프롬프트는 불변(엄격 additive). 완전 오프라인.
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


def _analyzer(language, region="kr"):
    return MarketAnalyzer(region=region, analyzer=None, config=SimpleNamespace(report_language=language))


class TestKrMarketFlowsPromptBlock:
    def test_ko_block_present_and_hangul_only(self):
        an = _analyzer("ko")
        block = an._build_kr_market_flows_prompt_block(_overview(_FLOWS), "ko")
        assert "KOSPI" in block and "KOSDAQ" in block
        assert "외국인" in block and "기관" in block
        assert "-3,228억" in block and "+11,314억" in block
        assert "2026-07-10" in block
        # ko 프롬프트 블록엔 한자가 없어야 한다(거부 게이트 안전)
        assert not any("一" <= c <= "鿿" for c in block)

    def test_en_block_present(self):
        an = _analyzer("en")
        block = an._build_kr_market_flows_prompt_block(_overview(_FLOWS), "en")
        assert "₩-322.8B" in block and "Foreign" in block

    def test_zh_block_present(self):
        an = _analyzer("zh")
        block = an._build_kr_market_flows_prompt_block(_overview(_FLOWS), "zh")
        assert "亿韩元" in block and "外国人" in block

    def test_block_empty_without_data(self):
        an = _analyzer("ko")
        assert an._build_kr_market_flows_prompt_block(_overview(None), "ko") == ""
        assert an._build_kr_market_flows_prompt_block(_overview({}), "ko") == ""


class TestBuildReviewPromptIntegration:
    def test_kr_prompt_includes_flows(self):
        an = _analyzer("ko")
        prompt = an._build_review_prompt(_overview(_FLOWS), [])
        assert "시장 투자자 수급" in prompt
        assert "+11,314억" in prompt

    def test_non_kr_prompt_has_no_flows(self):
        an = _analyzer("ko", region="us")
        prompt = an._build_review_prompt(_overview(_FLOWS), [])
        # us는 KR 브랜치 미진입 -> 수급 섹션 없음
        assert "시장 투자자 수급" not in prompt


class TestDataLimitsFundFlowClause:
    """KR 수급 주입 시 en/zh 데이터 경계 문구의 '자금흐름 없음' 자기모순 제거.

    수급이 주입되면 자금흐름이 실제로 제공되므로 en/zh 데이터 경계에서 자금흐름
    문구만 빠진다(시장 폭/거래대금/참여도 미제공은 유지). 비KR·ko·수급없는 KR은 불변.
    """

    def test_kr_en_flows_drops_fund_flow_clause(self):
        prompt = _analyzer("en")._build_review_prompt(_overview(_FLOWS), [])
        assert "fund-flow signals are not available" not in prompt
        # 시장 폭/거래대금/참여도 미제공 문구는 유지
        assert "Market breadth, aggregate turnover, and participation are not available" in prompt

    def test_kr_zh_flows_drops_fund_flow_clause(self):
        prompt = _analyzer("zh")._build_review_prompt(_overview(_FLOWS), [])
        assert "资金流信号" not in prompt
        assert "该市场暂无涨跌家数、涨跌停、成交额汇总或参与度" in prompt

    def test_kr_en_without_flows_keeps_fund_flow_clause(self):
        # 수급 데이터가 없으면 자금흐름 미제공 문구를 유지(kr_flows_present False)
        prompt = _analyzer("en")._build_review_prompt(_overview(None), [])
        assert "participation, and fund-flow signals are not available" in prompt

    def test_non_kr_en_keeps_fund_flow_clause(self):
        # 비KR(us)은 수급 레코드가 있어도 region 가드로 미진입 -> 원문 유지(바이트 동일)
        prompt = _analyzer("en", region="us")._build_review_prompt(_overview(_FLOWS), [])
        assert "participation, and fund-flow signals are not available" in prompt

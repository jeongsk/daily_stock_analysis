# -*- coding: utf-8 -*-
"""KR 업종 순위 프롬프트/결정적 본문 계약 고정. 오프라인.

계약(스펙 D5/D8):
  - 업종 데이터 -> sector_block "데이터 없음"을 KR 업종 섹션으로 대체
  - 폭/업종/수급 동시 -> 결정적 본문에 폭→업종→수급 순서로 주입
  - 결정적 블록: 시장 요약 주입 + fallback append, ko 순수 한글(한자 거부 게이트)
  - en/zh data_limits: 업종 제공 시 sector 문구를 theme-only로 조정
  - 비KR 프롬프트 불변
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_analyzer import MarketAnalyzer, MarketOverview

_SECTOR = {
    "kospi": {
        "market": "kospi", "top": [{"name": "통신업", "change_pct": 3.39}],
        "bottom": [{"name": "전기,전자", "change_pct": -9.43}],
        "as_of": "2026-07-16", "session": "close", "source": "DAUM", "stale": False,
    },
    "kosdaq": {
        "market": "kosdaq", "top": [{"name": "출판·매체복제", "change_pct": 3.29}],
        "bottom": [{"name": "기계·장비", "change_pct": -6.78}],
        "as_of": "2026-07-16", "session": "intraday", "source": "DAUM", "stale": True,
    },
}
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
_FLOWS = {
    "kospi": {
        "market": "kospi", "unit": "KRW",
        "days": [{"date": "2026-07-16", "foreign_net": -322800000000, "institution_net": 1131400000000, "individual_net": None}],
        "summary": {"foreign_net_5d": -322800000000, "institution_net_5d": 1131400000000},
        "source": "NAVER",
    },
}


def _overview(sectors=None, breadth=None, flows=None):
    ov = MarketOverview(date="2026-07-16")
    ov.kr_sector_rankings = sectors
    ov.kr_market_breadth = breadth
    ov.investor_flows = flows
    return ov


def _analyzer(language="ko", region="kr"):
    return MarketAnalyzer(region=region, analyzer=None, config=SimpleNamespace(report_language=language))


class TestSectorPromptBlock:
    def test_ko_prompt_contains_sector_section(self):
        prompt = _analyzer("ko")._build_review_prompt(_overview(sectors=_SECTOR), [])
        assert "## KR 업종 순위 (KOSPI/KOSDAQ)" in prompt
        assert "- KOSPI: 상승 통신업 +3.39%" in prompt
        assert "/ 하락 기계·장비 -6.78%" in prompt
        # 업종이 있으면 "데이터 없음" 섹터 문구는 사라진다
        assert "이 시장은 섹터 등락 데이터를 사용할 수 없습니다" not in prompt

    def test_sector_and_breadth_both_sections(self):
        prompt = _analyzer("ko")._build_review_prompt(_overview(sectors=_SECTOR, breadth=_BREADTH), [])
        assert "## 시장 폭 (KOSPI/KOSDAQ)" in prompt
        assert "## KR 업종 순위 (KOSPI/KOSDAQ)" in prompt

    def test_no_data_keeps_placeholder(self):
        prompt = _analyzer("ko")._build_review_prompt(_overview(), [])
        assert "이 시장은 섹터 등락 데이터를 사용할 수 없습니다" in prompt

    def test_en_data_limits_drop_sector_clause_when_present(self):
        prompt = _analyzer("en")._build_review_prompt(_overview(sectors=_SECTOR), [])
        assert "## KR Sector Rankings (KOSPI/KOSDAQ)" in prompt
        assert "Theme/concept ranking data is not available" in prompt
        assert "Sector/theme ranking data is not available" not in prompt

    def test_zh_data_limits_drop_sector_clause_when_present(self):
        prompt = _analyzer("zh")._build_review_prompt(_overview(sectors=_SECTOR), [])
        assert "## KR 行业排名 (KOSPI/KOSDAQ)" in prompt
        assert "该市场暂无概念题材涨跌榜。" in prompt
        assert "该市场暂无行业板块/概念题材涨跌榜。" not in prompt

    def test_non_kr_prompt_untouched(self):
        prompt = _analyzer("en", region="us")._build_review_prompt(_overview(), [])
        assert "KOSPI" not in prompt
        assert "Sector/theme ranking data is not available for this market." in prompt


class TestSectorDeterministicBlock:
    def test_ko_block_hangul_only(self):
        block = _analyzer("ko")._build_kr_sector_rankings_block(_overview(sectors=_SECTOR))
        assert "KR 업종 순위" in block
        assert "KOSPI" in block and "KOSDAQ" in block
        assert not any("一" <= c <= "鿿" for c in block)  # 한자(CJK ideograph) 거부

    def test_inject_order_breadth_sector_flows(self):
        an = _analyzer("ko")
        review = "## 2026-07-16 리뷰\n\n### 1. 시장 요약\n혼조.\n\n### 2. 지수 구조\n설명.\n"
        out = an._inject_data_into_review(
            review, _overview(sectors=_SECTOR, breadth=_BREADTH, flows=_FLOWS)
        )
        assert "시장 폭" in out and "KR 업종 순위" in out and "시장 투자자 수급" in out
        # 폭 → 업종 → 수급 순서(스펙: 결정적 본문 주입 순서)
        assert out.index("시장 폭") < out.index("KR 업종 순위") < out.index("시장 투자자 수급")
        assert out.index("시장 투자자 수급") < out.index("### 2. 지수 구조")

    def test_fallback_append_when_heading_missing(self):
        an = _analyzer("ko")
        review = "## 2026-07-16 리뷰\n\n표준 헤딩 없음.\n"
        out = an._inject_data_into_review(review, _overview(sectors=_SECTOR))
        assert "통신업 +3.39%" in out

    def test_no_injection_without_data(self):
        an = _analyzer("ko")
        review = "## 2026-07-16 리뷰\n\n### 1. 시장 요약\n내용.\n"
        out = an._inject_data_into_review(review, _overview())
        assert "KR 업종 순위**(등락률" not in out

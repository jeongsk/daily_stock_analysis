# -*- coding: utf-8 -*-
"""구조화 payload의 KR 전용 kr_market_context.sector_rankings 계약 고정. 오프라인.

계약(스펙 D3/D7/D9/D12):
  - KR + 유효 업종 -> payload["kr_market_context"]["sector_rankings"] (유효 시장만)
  - breadth/sector_rankings 서브키 독립 — 한쪽만 있어도 키 생성(둘 다 optional)
  - 무데이터/비KR -> kr_market_context 키 부재
  - 기존 평면 breadth/sectors 계약 불변(KR은 has_market_stats=False라 평면 breadth 없음)
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
        "as_of": "2026-07-16", "session": "close", "source": "DAUM", "stale": False,
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


def _overview(sectors=None, breadth=None):
    ov = MarketOverview(date="2026-07-16")
    ov.kr_sector_rankings = sectors
    ov.kr_market_breadth = breadth
    return ov


def _analyzer(region="kr"):
    return MarketAnalyzer(region=region, analyzer=None, config=SimpleNamespace(report_language="ko"))


class TestPayloadKrSectorRankings:
    def test_sector_only_has_no_breadth_subkey(self):
        payload = _analyzer("kr").build_market_review_payload(_overview(sectors=_SECTOR), [], "## 리뷰\n본문")
        ctx = payload["kr_market_context"]
        assert "sector_rankings" in ctx
        assert set(ctx["sector_rankings"]) == {"kospi", "kosdaq"}
        assert ctx["sector_rankings"]["kospi"]["top"][0]["change_pct"] == 3.39
        assert "breadth" not in ctx  # 업종만 있을 때 breadth 서브키 부재(독립)

    def test_breadth_only_has_no_sector_subkey(self):
        # 회귀: 업종 도입이 기존 폭 payload를 깨지 않는다
        payload = _analyzer("kr").build_market_review_payload(_overview(breadth=_BREADTH), [], "## 리뷰\n본문")
        ctx = payload["kr_market_context"]
        assert "breadth" in ctx
        assert "sector_rankings" not in ctx

    def test_both_present_both_subkeys(self):
        payload = _analyzer("kr").build_market_review_payload(
            _overview(sectors=_SECTOR, breadth=_BREADTH), [], "## 리뷰\n본문"
        )
        ctx = payload["kr_market_context"]
        assert set(ctx) == {"breadth", "sector_rankings"}

    def test_one_market_only_sector(self):
        payload = _analyzer("kr").build_market_review_payload(
            _overview(sectors={"kospi": _SECTOR["kospi"]}), [], "## 리뷰\n본문"
        )
        assert set(payload["kr_market_context"]["sector_rankings"]) == {"kospi"}

    def test_no_key_without_data(self):
        payload = _analyzer("kr").build_market_review_payload(_overview(), [], "## 리뷰\n본문")
        assert "kr_market_context" not in payload

    def test_invalid_record_dropped(self):
        # top/as_of 결측 레코드는 payload에 싣지 않는다(스펙 D9 — 서비스 측 2차 방어)
        broken = {"kospi": {"market": "kospi", "bottom": [{"name": "x", "change_pct": -1.0}]}}
        payload = _analyzer("kr").build_market_review_payload(_overview(sectors=broken), [], "## 리뷰\n본문")
        assert "kr_market_context" not in payload

    def test_non_kr_has_no_key(self):
        payload = _analyzer("us").build_market_review_payload(_overview(), [], "## Review\nbody")
        assert "kr_market_context" not in payload

    def test_flat_contracts_unchanged_for_kr(self):
        # KR은 has_market_stats=False — 평면 breadth/sectors 키는 여전히 비어 있다(D12)
        payload = _analyzer("kr").build_market_review_payload(_overview(sectors=_SECTOR), [], "## 리뷰\n본문")
        assert "breadth" not in payload  # 평면 breadth 없음
        assert payload["sectors"] == {"top": [], "bottom": []}  # CN 평면 sectors도 빈 채로 유지

# -*- coding: utf-8 -*-
"""KR 시장 폭 수집 배선: MarketOverview.kr_market_breadth에 fail-open으로 싣는지 고정.

계약(스펙 D3/D6):
  - DataFetcherManager.get_kr_market_breadth: fetcher 성공 -> 레코드, 실패 -> None
  - MarketAnalyzer(region="kr").get_market_overview() -> kr_market_breadth =
    {"kospi": rec, "kosdaq": rec} (유효 시장만)
  - fetcher None/raise -> 해당 시장 생략, 둘 다 없으면 None
  - 비KR 리뷰 -> kr_market_breadth None AND fetcher 미호출 (엄격 additive)
  - KR profile 공통 플래그는 계속 False — CN 공통 stats/sector 경로 미호출

완전 오프라인 — `_get_main_indices`를 목킹해 네트워크를 차단한다.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_analyzer import MarketAnalyzer

_KOSPI_REC = {
    "market": "kospi", "up_count": 384, "down_count": 488, "flat_count": 40,
    "as_of": "2026-07-16", "session": "close", "source": "NAVER", "stale": False,
}
_KOSDAQ_REC = {
    "market": "kosdaq", "up_count": 501, "down_count": 1182, "flat_count": 56,
    "as_of": "2026-07-16", "session": "close", "source": "NAVER", "stale": False,
}

_CFG = SimpleNamespace(report_language="ko")


def _fake_breadth(market):
    return {"kospi": dict(_KOSPI_REC), "kosdaq": dict(_KOSDAQ_REC)}.get(market)


class TestKrMarketBreadthWiring(unittest.TestCase):
    def _analyzer(self, region):
        return MarketAnalyzer(region=region, analyzer=None, config=_CFG)

    def test_kr_overview_populated(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None), \
                patch.object(an.data_manager, "get_kr_market_breadth", side_effect=_fake_breadth) as m:
            overview = an.get_market_overview()
        self.assertIsInstance(overview.kr_market_breadth, dict)
        self.assertEqual(overview.kr_market_breadth["kospi"]["up_count"], 384)
        self.assertEqual(overview.kr_market_breadth["kosdaq"]["down_count"], 1182)
        self.assertEqual(m.call_count, 2)

    def test_kr_one_market_missing(self):
        an = self._analyzer("kr")
        def _only_kosdaq(market):
            return dict(_KOSDAQ_REC) if market == "kosdaq" else None
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None), \
                patch.object(an.data_manager, "get_kr_market_breadth", side_effect=_only_kosdaq):
            overview = an.get_market_overview()
        self.assertEqual(set(overview.kr_market_breadth), {"kosdaq"})

    def test_kr_all_fail_is_none(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None), \
                patch.object(an.data_manager, "get_kr_market_breadth", return_value=None):
            overview = an.get_market_overview()
        self.assertIsNone(overview.kr_market_breadth)

    def test_kr_fail_open_when_fetcher_raises(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None), \
                patch.object(an.data_manager, "get_kr_market_breadth", side_effect=RuntimeError("boom")):
            overview = an.get_market_overview()  # must NOT raise
        self.assertIsNone(overview.kr_market_breadth)

    def test_non_kr_untouched_and_fetcher_not_called(self):
        an = self._analyzer("us")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_breadth", side_effect=_fake_breadth) as m:
            overview = an.get_market_overview()
        self.assertIsNone(overview.kr_market_breadth)
        self.assertEqual(m.call_count, 0)

    def test_kr_profile_flags_stay_false_and_cn_paths_not_called(self):
        # 폭 도입 후에도 KR은 CN 공통 stats/sector 경로에 진입하지 않는다(스펙 D6).
        an = self._analyzer("kr")
        self.assertFalse(an.profile.has_market_stats)
        self.assertFalse(an.profile.has_sector_rankings)
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None), \
                patch.object(an.data_manager, "get_kr_market_breadth", side_effect=_fake_breadth), \
                patch.object(an, "_get_market_statistics") as stats_m, \
                patch.object(an, "_get_sector_rankings") as sector_m:
            an.get_market_overview()
        self.assertEqual(stats_m.call_count, 0)
        self.assertEqual(sector_m.call_count, 0)

    def test_data_manager_method_fail_open(self):
        # base.py 메서드 자체가 fetcher 예외를 삼키는지 (레코드/None 계약)
        from data_provider.base import DataFetcherManager
        manager = DataFetcherManager(fetchers=[])
        with patch(
            "data_provider.kr_market_context_fetcher.KrMarketContextFetcher.get_market_breadth",
            return_value=dict(_KOSPI_REC),
        ):
            self.assertEqual(manager.get_kr_market_breadth("kospi")["source"], "NAVER")
        with patch(
            "data_provider.kr_market_context_fetcher.KrMarketContextFetcher.get_market_breadth",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(manager.get_kr_market_breadth("kospi"))


if __name__ == "__main__":
    unittest.main()

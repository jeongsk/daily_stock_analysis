# -*- coding: utf-8 -*-
"""KR 업종 순위 수집 배선: MarketOverview.kr_sector_rankings에 fail-open으로 싣는지 고정.

계약(스펙 D3/D5/D6):
  - DataFetcherManager.get_kr_sector_rankings: fetcher 성공 -> 레코드, 실패 -> None
  - MarketAnalyzer(region="kr").get_market_overview() -> kr_sector_rankings =
    {"kospi": rec, "kosdaq": rec} (유효 시장만; top+bottom+as_of 필수)
  - fetcher None/raise -> 해당 시장 생략, 둘 다 없으면 None
  - 비KR 리뷰 -> kr_sector_rankings None AND fetcher 미호출 (엄격 additive)
  - KR profile 공통 플래그는 계속 False — CN 공통 stats/sector 경로 미호출(D6 오염 방지)

완전 오프라인 — `_get_main_indices`를 목킹해 네트워크를 차단한다.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_analyzer import MarketAnalyzer

_KOSPI_SECTOR = {
    "market": "kospi",
    "top": [{"name": "통신업", "change_pct": 3.39}],
    "bottom": [{"name": "전기,전자", "change_pct": -9.43}],
    "as_of": "2026-07-16", "session": "close", "source": "DAUM", "stale": False,
}
_KOSDAQ_SECTOR = {
    "market": "kosdaq",
    "top": [{"name": "출판·매체복제", "change_pct": 3.29}],
    "bottom": [{"name": "기계·장비", "change_pct": -6.78}],
    "as_of": "2026-07-16", "session": "close", "source": "DAUM", "stale": False,
}

_CFG = SimpleNamespace(report_language="ko")


def _fake_sector(market):
    return {"kospi": dict(_KOSPI_SECTOR), "kosdaq": dict(_KOSDAQ_SECTOR)}.get(market)


class TestKrSectorRankingsWiring(unittest.TestCase):
    def _analyzer(self, region):
        return MarketAnalyzer(region=region, analyzer=None, config=_CFG)

    def _kr_patches(self, sector_side_effect):
        return (
            patch.object(self._analyzer("kr"), "_get_main_indices", return_value=[]),
        )

    def test_kr_overview_populated(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None), \
                patch.object(an.data_manager, "get_kr_market_breadth", return_value=None), \
                patch.object(an.data_manager, "get_kr_sector_rankings", side_effect=_fake_sector) as m:
            overview = an.get_market_overview()
        self.assertIsInstance(overview.kr_sector_rankings, dict)
        self.assertEqual(overview.kr_sector_rankings["kospi"]["top"][0]["name"], "통신업")
        self.assertEqual(overview.kr_sector_rankings["kosdaq"]["bottom"][0]["name"], "기계·장비")
        self.assertEqual(m.call_count, 2)

    def test_kr_one_market_missing(self):
        an = self._analyzer("kr")

        def _only_kosdaq(market):
            return dict(_KOSDAQ_SECTOR) if market == "kosdaq" else None

        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None), \
                patch.object(an.data_manager, "get_kr_market_breadth", return_value=None), \
                patch.object(an.data_manager, "get_kr_sector_rankings", side_effect=_only_kosdaq):
            overview = an.get_market_overview()
        self.assertEqual(set(overview.kr_sector_rankings), {"kosdaq"})

    def test_kr_all_fail_is_none(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None), \
                patch.object(an.data_manager, "get_kr_market_breadth", return_value=None), \
                patch.object(an.data_manager, "get_kr_sector_rankings", return_value=None):
            overview = an.get_market_overview()
        self.assertIsNone(overview.kr_sector_rankings)

    def test_kr_fail_open_when_fetcher_raises(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None), \
                patch.object(an.data_manager, "get_kr_market_breadth", return_value=None), \
                patch.object(an.data_manager, "get_kr_sector_rankings", side_effect=RuntimeError("boom")):
            overview = an.get_market_overview()  # must NOT raise
        self.assertIsNone(overview.kr_sector_rankings)

    def test_non_kr_untouched_and_fetcher_not_called(self):
        an = self._analyzer("us")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_sector_rankings", side_effect=_fake_sector) as m:
            overview = an.get_market_overview()
        self.assertIsNone(overview.kr_sector_rankings)
        self.assertEqual(m.call_count, 0)

    def test_kr_profile_flags_stay_false_and_cn_paths_not_called(self):
        # 업종 도입 후에도 KR은 CN 공통 stats/sector 경로에 진입하지 않는다(스펙 D6).
        an = self._analyzer("kr")
        self.assertFalse(an.profile.has_market_stats)
        self.assertFalse(an.profile.has_sector_rankings)
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None), \
                patch.object(an.data_manager, "get_kr_market_breadth", return_value=None), \
                patch.object(an.data_manager, "get_kr_sector_rankings", side_effect=_fake_sector), \
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
            "data_provider.kr_market_context_fetcher.KrMarketContextFetcher.get_sector_rankings",
            return_value=dict(_KOSPI_SECTOR),
        ):
            self.assertEqual(manager.get_kr_sector_rankings("kospi")["source"], "DAUM")
        with patch(
            "data_provider.kr_market_context_fetcher.KrMarketContextFetcher.get_sector_rankings",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(manager.get_kr_sector_rankings("kospi"))


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Phase 3 수집 배선: KR 시장 수급을 MarketOverview.investor_flows에 fail-open으로
싣는지 고정한다.

계약:
  - DataFetcherManager.get_kr_market_investor_flows: fetcher 성공 -> 레코드, 실패 -> None
  - MarketAnalyzer(region="kr").get_market_overview() -> overview.investor_flows =
    {"kospi": rec, "kosdaq": rec} (데이터 있는 시장만)
  - fetcher가 None/raise -> 해당 시장 생략, 둘 다 없으면 investor_flows=None
  - 비KR 리뷰(us 등) -> investor_flows None AND fetcher 미호출 (엄격 additive)

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
    "market": "kospi", "unit": "KRW",
    "days": [{"date": "2026-07-10", "foreign_net": -322800000000, "institution_net": 1131400000000, "individual_net": -808600000000}],
    "summary": {"foreign_net_5d": -322800000000, "institution_net_5d": 1131400000000},
    "source": "NAVER",
}
_KOSDAQ_REC = {
    "market": "kosdaq", "unit": "KRW",
    "days": [{"date": "2026-07-10", "foreign_net": 51200000000, "institution_net": -28700000000, "individual_net": -22500000000}],
    "summary": {"foreign_net_5d": 51200000000, "institution_net_5d": -28700000000},
    "source": "NAVER",
}

_CFG = SimpleNamespace(report_language="ko")


def _fake_flows(market, days=5):
    return {"kospi": dict(_KOSPI_REC), "kosdaq": dict(_KOSDAQ_REC)}.get(market)


class TestKrMarketFlowsWiring(unittest.TestCase):
    def _analyzer(self, region):
        return MarketAnalyzer(region=region, analyzer=None, config=_CFG)

    def test_kr_overview_populated(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", side_effect=_fake_flows) as m:
            overview = an.get_market_overview()
        self.assertIsInstance(overview.investor_flows, dict)
        self.assertEqual(overview.investor_flows["kospi"]["summary"]["institution_net_5d"], 1131400000000)
        self.assertEqual(overview.investor_flows["kosdaq"]["source"], "NAVER")
        self.assertEqual(m.call_count, 2)

    def test_kr_one_market_missing(self):
        an = self._analyzer("kr")
        def _only_kospi(market, days=5):
            return dict(_KOSPI_REC) if market == "kospi" else None
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", side_effect=_only_kospi):
            overview = an.get_market_overview()
        self.assertEqual(set(overview.investor_flows), {"kospi"})

    def test_kr_all_fail_is_none(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None):
            overview = an.get_market_overview()
        self.assertIsNone(overview.investor_flows)

    def test_kr_fail_open_when_fetcher_raises(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", side_effect=RuntimeError("boom")):
            overview = an.get_market_overview()  # must NOT raise
        self.assertIsNone(overview.investor_flows)

    def test_non_kr_untouched_and_fetcher_not_called(self):
        an = self._analyzer("us")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", side_effect=_fake_flows) as m:
            overview = an.get_market_overview()
        self.assertIsNone(overview.investor_flows)
        self.assertEqual(m.call_count, 0)

    def test_data_manager_method_fail_open(self):
        # base.py 메서드 자체가 fetcher 예외를 삼키는지 (레코드/None 계약)
        from data_provider.base import DataFetcherManager
        manager = DataFetcherManager(fetchers=[])
        with patch(
            "data_provider.kr_institutional_fetcher.KrInstitutionalFetcher.get_market_investor_flows",
            return_value=dict(_KOSPI_REC),
        ):
            self.assertEqual(manager.get_kr_market_investor_flows("kospi")["source"], "NAVER")
        with patch(
            "data_provider.kr_institutional_fetcher.KrInstitutionalFetcher.get_market_investor_flows",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(manager.get_kr_market_investor_flows("kospi"))


if __name__ == "__main__":
    unittest.main()

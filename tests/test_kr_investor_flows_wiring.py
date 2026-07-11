# -*- coding: utf-8 -*-
"""Phase 2 report-wiring tests: KR 투자자별 수급(investor_flows)을 오프쇼어
fundamental_context["investor_flows"]에 fail-open으로 배선하는지 고정한다.

계약:
  - kr with data        -> fundamental_context["investor_flows"] = 정규화 레코드
  - kr fetch None/raise  -> investor_flows = None (fail-open, 메인 분석 유지)
  - us/hk/jp/tw          -> investor_flows 키 없음/None AND kr fetcher 미호출
                            (엄격 additive: 비KR 시장 불변)
  - fetch_timeout=0      -> per-fetch 비활성, kr fetcher 미호출
  - 느린 fetch           -> stage budget에서 포기(메인 분석 차단 금지)

TW wiring 테스트(tests/test_tw_institution_report_wiring.py) 패턴 미러.
완전 오프라인 — `pytest -m "not network"` 차단 게이트에 포함된다.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetcherManager

_KR_FETCHER_METHOD = (
    "data_provider.kr_institutional_fetcher.KrInstitutionalFetcher.get_investor_flows"
)

# Phase 1 정규화 레코드 shape (2026-07-10 삼성전자 005930 실캡처 5일).
_FAKE_REC = {
    "code": "005930",
    "market": "kospi",
    "unit": "shares",
    "days": [
        {"date": "2026-07-10", "foreign_net": 625985, "institution_net": 2313745, "individual_net": -2851466},
        {"date": "2026-07-09", "foreign_net": 845552, "institution_net": 1107761, "individual_net": -1739937},
        {"date": "2026-07-08", "foreign_net": -3015093, "institution_net": 971031, "individual_net": 2031705},
        {"date": "2026-07-07", "foreign_net": -6145090, "institution_net": -1852807, "individual_net": 7870568},
        {"date": "2026-07-06", "foreign_net": -2018562, "institution_net": 14823, "individual_net": 1917050},
    ],
    "summary": {"foreign_net_5d": -9707208, "institution_net_5d": 2554553},
    "source": "NAVER",
}

_OFFSHORE_CFG = SimpleNamespace(
    enable_fundamental_pipeline=True,
    fundamental_cache_ttl_seconds=0,
    fundamental_stage_timeout_seconds=1.5,
    fundamental_fetch_timeout_seconds=0.8,
    fundamental_retry_max=1,
)

_EMPTY_BUNDLE = {
    "status": "not_supported",
    "growth": {},
    "earnings": {},
    "belong_boards": [],
    "source_chain": [],
    "errors": [],
}


class TestKrInvestorFlowsWiring(unittest.TestCase):
    def _context(self, code, flows_return=None, flows_side_effect=None):
        """get_fundamental_context(code) 오프라인 실행; (ctx, kr_fetcher_mock) 반환."""
        manager = DataFetcherManager(fetchers=[])
        kwargs = {}
        if flows_side_effect is not None:
            kwargs["side_effect"] = flows_side_effect
        else:
            kwargs["return_value"] = flows_return
        with patch("src.config.get_config", return_value=_OFFSHORE_CFG), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=_EMPTY_BUNDLE,
                ), \
                patch(_KR_FETCHER_METHOD, **kwargs) as kr_mock:
            ctx = manager.get_fundamental_context(code)
        return ctx, kr_mock

    def test_kr_investor_flows_populated_when_fetcher_has_data(self):
        ctx, kr_mock = self._context("005930.KS", flows_return=dict(_FAKE_REC))
        self.assertEqual(ctx["market"], "kr")
        rec = ctx.get("investor_flows")
        self.assertIsInstance(rec, dict)
        self.assertEqual(rec["source"], "NAVER")
        self.assertEqual(rec["unit"], "shares")
        self.assertEqual(rec["days"][0]["foreign_net"], 625985)
        self.assertEqual(rec["summary"]["institution_net_5d"], 2554553)
        kr_mock.assert_called_with("005930.KS", days=5)
        # 메인 분석은 계속 — 예외가 새어나오지 않았다
        self.assertEqual(ctx["market"], "kr")

    def test_kosdaq_routed_and_populated(self):
        ctx, _ = self._context("068270.KQ", flows_return=dict(_FAKE_REC))
        self.assertEqual(ctx["market"], "kr")
        self.assertIsInstance(ctx.get("investor_flows"), dict)

    def test_kr_fail_open_when_fetcher_returns_none(self):
        ctx, _ = self._context("005930.KS", flows_return=None)
        self.assertEqual(ctx["market"], "kr")
        self.assertIsNone(ctx.get("investor_flows"))

    def test_kr_fail_open_when_fetcher_raises(self):
        # get_investor_flows는 자체 fail-open이지만, 훅이 raise도 삼키는지 고정
        ctx, _ = self._context("005930.KS", flows_side_effect=RuntimeError("boom"))
        self.assertEqual(ctx["market"], "kr")
        self.assertIsNone(ctx.get("investor_flows"))

    def test_us_unchanged_and_kr_fetcher_not_called(self):
        ctx, kr_mock = self._context("AAPL", flows_return=dict(_FAKE_REC))
        self.assertEqual(ctx["market"], "us")
        self.assertIsNone(ctx.get("investor_flows"))
        self.assertEqual(kr_mock.call_count, 0)

    def test_other_offshore_markets_unchanged(self):
        for code, market in (("0700.HK", "hk"), ("7203.T", "jp"), ("2330.TW", "tw")):
            ctx, kr_mock = self._context(code, flows_return=dict(_FAKE_REC))
            self.assertEqual(ctx["market"], market, f"{code} routed to {ctx['market']}")
            self.assertIsNone(ctx.get("investor_flows"))
            self.assertEqual(kr_mock.call_count, 0)

    def test_kr_fail_open_when_fetcher_init_raises(self):
        manager = DataFetcherManager(fetchers=[])
        with patch("src.config.get_config", return_value=_OFFSHORE_CFG), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=_EMPTY_BUNDLE,
                ), \
                patch(
                    "data_provider.kr_institutional_fetcher.KrInstitutionalFetcher",
                    side_effect=RuntimeError("init boom"),
                ):
            ctx = manager.get_fundamental_context("005930.KS")  # must NOT raise
        self.assertEqual(ctx["market"], "kr")
        self.assertIsNone(ctx.get("investor_flows"))

    def test_kr_flows_respects_stage_timeout(self):
        slow_cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=0,
            fundamental_stage_timeout_seconds=0.3,
            fundamental_fetch_timeout_seconds=0.3,
            fundamental_retry_max=1,
        )
        manager = DataFetcherManager(fetchers=[])

        def _slow(_code, days=5):
            time.sleep(2.0)
            return dict(_FAKE_REC)

        start = time.time()
        with patch("src.config.get_config", return_value=slow_cfg), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=_EMPTY_BUNDLE,
                ), \
                patch(_KR_FETCHER_METHOD, side_effect=_slow):
            ctx = manager.get_fundamental_context("005930.KS")
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.5, f"kr flows fetch ignored stage timeout ({elapsed:.2f}s)")
        self.assertIsNone(ctx.get("investor_flows"))

    def test_kr_flows_disabled_when_fetch_timeout_zero(self):
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=0,
            fundamental_stage_timeout_seconds=8.0,
            fundamental_fetch_timeout_seconds=0.0,  # disabled
            fundamental_retry_max=1,
        )
        manager = DataFetcherManager(fetchers=[])
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=_EMPTY_BUNDLE,
                ), \
                patch(_KR_FETCHER_METHOD, return_value=dict(_FAKE_REC)) as kr_mock:
            ctx = manager.get_fundamental_context("005930.KS")
        self.assertIsNone(ctx.get("investor_flows"))
        self.assertEqual(kr_mock.call_count, 0)


if __name__ == "__main__":
    unittest.main()

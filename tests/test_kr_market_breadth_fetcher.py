# -*- coding: utf-8 -*-
"""KrMarketContextFetcher 단위 계약: 파싱·엄격 검증·stale·fail-open. 오프라인.

계약(스펙 D4/D9/D13):
  - up/down/flat + as_of + session(장중|장마감)이 모두 있어야 레코드 생성
  - 개장전(PREOPEN)·라벨 드리프트·날짜 결측 -> None (0 조작 금지)
  - 빈/무효 결과는 캐시하지 않음; stale fallback은 동일 KR 거래일 캐시만
  - 공개 메서드는 어떤 실패에서도 raise하지 않음
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.kr_market_context_fetcher import KrMarketContextFetcher


def _page(up="384", flat="40", down="488", time_text="2026.07.16 장마감"):
    return f"""
    <html><body>
    <li class="lst"><span class="blind">상한종목수</span><a href="/x"><span>6</span></a></li>
    <li class="lst2"><span class="blind">상승종목수</span><a href="/x"><span>{up}</span></a></li>
    <li class="lst3"><span class="blind">보합종목수</span><a href="/x"><span>{flat}</span></a></li>
    <li class="lst4"><span class="blind">하락종목수</span><a href="/x"><span>{down}</span></a></li>
    <li class="lst5"><span class="blind">하한종목수</span><a href="/x"><span>0</span></a></li>
    <span id="time">{time_text}</span>
    </body></html>
    """


def _fetcher(**kwargs):
    return KrMarketContextFetcher(min_request_interval=0, **kwargs)


class TestParseBreadthPage(unittest.TestCase):
    def test_parse_close_session(self):
        rec = KrMarketContextFetcher._parse_breadth_page(_page(), "kospi")
        self.assertEqual(rec["market"], "kospi")
        self.assertEqual(rec["up_count"], 384)
        self.assertEqual(rec["down_count"], 488)
        self.assertEqual(rec["flat_count"], 40)
        self.assertEqual(rec["as_of"], "2026-07-16")
        self.assertEqual(rec["session"], "close")
        self.assertEqual(rec["source"], "NAVER")
        self.assertFalse(rec["stale"])

    def test_parse_intraday_session(self):
        rec = KrMarketContextFetcher._parse_breadth_page(
            _page(time_text="2026.07.16 14:32 장중"), "kosdaq"
        )
        self.assertEqual(rec["session"], "intraday")
        self.assertEqual(rec["market"], "kosdaq")

    def test_zero_counts_are_valid(self):
        rec = KrMarketContextFetcher._parse_breadth_page(_page(up="0", flat="0"), "kospi")
        self.assertEqual(rec["up_count"], 0)
        self.assertEqual(rec["flat_count"], 0)

    def test_preopen_yields_none(self):
        # 개장전은 예상지수 구간 — 레코드를 만들지 않는다
        rec = KrMarketContextFetcher._parse_breadth_page(
            _page(time_text="2026.07.16 개장전"), "kospi"
        )
        self.assertIsNone(rec)

    def test_label_drift_yields_none(self):
        html = _page().replace("상승종목수", "상승수")  # blind 라벨 rename
        self.assertIsNone(KrMarketContextFetcher._parse_breadth_page(html, "kospi"))

    def test_missing_time_yields_none(self):
        html = _page().replace('id="time"', 'id="clock"')
        self.assertIsNone(KrMarketContextFetcher._parse_breadth_page(html, "kospi"))

    def test_unknown_session_label_yields_none(self):
        rec = KrMarketContextFetcher._parse_breadth_page(
            _page(time_text="2026.07.16 점검중"), "kospi"
        )
        self.assertIsNone(rec)


class TestGetMarketBreadth(unittest.TestCase):
    def test_success_and_cache(self):
        f = _fetcher()
        with patch.object(f, "_get_html", return_value=_page()) as m:
            first = f.get_market_breadth("kospi")
            second = f.get_market_breadth("KOSPI")  # 대소문자 허용 + 캐시 적중
        self.assertEqual(first["up_count"], 384)
        self.assertEqual(second, first)
        self.assertEqual(m.call_count, 1)

    def test_markets_have_separate_cache_keys(self):
        f = _fetcher()
        pages = {"KOSPI": _page(), "KOSDAQ": _page(up="501", flat="56", down="1182")}
        with patch.object(
            f, "_get_html", side_effect=lambda url, params=None: pages[params["code"]]
        ) as m:
            kospi = f.get_market_breadth("kospi")
            kosdaq = f.get_market_breadth("kosdaq")
        self.assertEqual(kospi["up_count"], 384)
        self.assertEqual(kosdaq["up_count"], 501)
        self.assertEqual(m.call_count, 2)

    def test_invalid_market_is_none(self):
        self.assertIsNone(_fetcher().get_market_breadth("nasdaq"))
        self.assertIsNone(_fetcher().get_market_breadth(None))

    def test_http_failure_without_cache_is_none(self):
        f = _fetcher()
        with patch.object(f, "_get_html", side_effect=RuntimeError("boom")):
            self.assertIsNone(f.get_market_breadth("kospi"))

    def test_invalid_page_not_cached(self):
        f = _fetcher()
        with patch.object(f, "_get_html", return_value="<html>drifted</html>") as m:
            self.assertIsNone(f.get_market_breadth("kospi"))
            self.assertIsNone(f.get_market_breadth("kospi"))
        self.assertEqual(m.call_count, 2)  # 무효 결과는 캐시하지 않는다

    def test_stale_fallback_same_trading_day(self):
        f = _fetcher(cache_ttl_seconds=0)  # TTL 즉시 만료 -> 재호출 유도
        with patch.object(f, "_get_html", return_value=_page()):
            first = f.get_market_breadth("kospi")
        self.assertFalse(first["stale"])
        with patch.object(f, "_get_html", side_effect=RuntimeError("down")), patch(
            "data_provider.kr_market_context_fetcher._current_kr_trading_date",
            return_value="2026-07-16",
        ):
            rec = f.get_market_breadth("kospi")
        self.assertIsNotNone(rec)
        self.assertTrue(rec["stale"])
        self.assertEqual(rec["up_count"], 384)

    def test_stale_rejected_for_other_trading_day(self):
        f = _fetcher(cache_ttl_seconds=0)
        with patch.object(f, "_get_html", return_value=_page()):
            f.get_market_breadth("kospi")
        with patch.object(f, "_get_html", side_effect=RuntimeError("down")), patch(
            "data_provider.kr_market_context_fetcher._current_kr_trading_date",
            return_value="2026-07-17",
        ):
            self.assertIsNone(f.get_market_breadth("kospi"))

    def test_stale_skipped_when_calendar_unavailable(self):
        f = _fetcher(cache_ttl_seconds=0)
        with patch.object(f, "_get_html", return_value=_page()):
            f.get_market_breadth("kospi")
        with patch.object(f, "_get_html", side_effect=RuntimeError("down")), patch(
            "data_provider.kr_market_context_fetcher._current_kr_trading_date",
            return_value=None,
        ):
            self.assertIsNone(f.get_market_breadth("kospi"))

    def test_circuit_breaker_open_uses_stale_path(self):
        f = _fetcher(cache_ttl_seconds=0)
        with patch.object(f, "_get_html", return_value=_page()):
            f.get_market_breadth("kospi")
        with patch.object(f, "_get_html", side_effect=RuntimeError("down")), patch(
            "data_provider.kr_market_context_fetcher._current_kr_trading_date",
            return_value="2026-07-16",
        ):
            for _ in range(3):  # failure_threshold=3 -> 서킷 오픈
                f.get_market_breadth("kospi")
            rec = f.get_market_breadth("kospi")  # 오픈 상태에서도 stale 제공
        self.assertIsNotNone(rec)
        self.assertTrue(rec["stale"])


if __name__ == "__main__":
    unittest.main()

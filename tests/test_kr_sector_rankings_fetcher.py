# -*- coding: utf-8 -*-
"""KrMarketContextFetcher.get_sector_rankings 단위 계약. 오프라인.

계약(스펙 D5/D9/D13, 실측 §2.2):
  - changeRate(비율) -> change_pct(%) 변환, 이름 중복 제거, 최대 n
  - top+bottom+as_of 모두 있어야 레코드 생성(불완전 -> None, 빈 목록 채움 금지)
  - 다음 sectors API는 Referer 필수(누락 시 403) — fetcher가 Referer 상수 포함
  - 시장·방향별 breaker/cache 키 분리(daum_sector_{market}_{rise|fall})
  - session은 KR 거래 캘린더(market_phase) 파생; 최신 실패 시 동일 거래일 캐시만 stale
  - 공개 메서드는 어떤 실패에서도 raise하지 않음
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.kr_market_context_fetcher import (
    _DAUM_REFERER,
    KrMarketContextFetcher,
)


def _row(name, rate, date="2026-07-16", market="KOSPI"):
    return {"sectorName": name, "changeRate": rate, "date": date, "market": market}


def _daum(rows):
    return {"data": rows}


_RISE = [_row("통신업", 0.0339), _row("음식료품", 0.021), _row("운수창고", 0.015)]
_FALL = [_row("전기,전자", -0.0943), _row("철강", -0.02)]


def _json_both(url, params=None, referer=None):
    return _daum(_RISE if params["change"] == "RISE" else _FALL)


def _fetcher(**kwargs):
    return KrMarketContextFetcher(min_request_interval=0, **kwargs)


class TestParseSectorRows(unittest.TestCase):
    def test_change_rate_to_pct(self):
        rows, as_of = KrMarketContextFetcher._parse_sector_rows(_RISE, 5)
        self.assertEqual(rows[0], {"name": "통신업", "change_pct": 3.39})
        self.assertEqual(rows[1]["change_pct"], 2.1)  # 0.021 -> 2.1
        self.assertEqual(as_of, "2026-07-16")

    def test_negative_change_rate(self):
        rows, _ = KrMarketContextFetcher._parse_sector_rows(_FALL, 5)
        self.assertEqual(rows[0]["change_pct"], -9.43)

    def test_dedup_names(self):
        rows = [_row("통신업", 0.03), _row("통신업", 0.02), _row("철강", -0.01)]
        parsed, _ = KrMarketContextFetcher._parse_sector_rows(rows, 5)
        self.assertEqual([r["name"] for r in parsed], ["통신업", "철강"])

    def test_caps_at_n(self):
        parsed, _ = KrMarketContextFetcher._parse_sector_rows(_RISE, 2)
        self.assertEqual(len(parsed), 2)

    def test_missing_change_rate_skipped(self):
        rows = [{"sectorName": "X", "date": "2026-07-16"}, _row("Y", 0.01)]
        parsed, as_of = KrMarketContextFetcher._parse_sector_rows(rows, 5)
        self.assertEqual([r["name"] for r in parsed], ["Y"])
        self.assertEqual(as_of, "2026-07-16")  # 결측 row는 건너뛰고 다음 row date

    def test_missing_name_skipped(self):
        rows = [{"changeRate": 0.01, "date": "2026-07-16"}, _row("Y", 0.02)]
        parsed, _ = KrMarketContextFetcher._parse_sector_rows(rows, 5)
        self.assertEqual([r["name"] for r in parsed], ["Y"])

    def test_non_list_returns_empty(self):
        parsed, as_of = KrMarketContextFetcher._parse_sector_rows(None, 5)
        self.assertEqual(parsed, [])
        self.assertIsNone(as_of)

    def test_empty_rows(self):
        parsed, as_of = KrMarketContextFetcher._parse_sector_rows([], 5)
        self.assertEqual(parsed, [])
        self.assertIsNone(as_of)


class TestGetSectorRankings(unittest.TestCase):
    def test_success_and_cache(self):
        f = _fetcher()
        with patch.object(f, "_get_json", side_effect=_json_both) as m:
            first = f.get_sector_rankings("kospi")
            second = f.get_sector_rankings("KOSPI")  # 대소문자 허용 + 캐시 적중
        self.assertEqual(first["top"][0]["change_pct"], 3.39)
        self.assertEqual(first["bottom"][0]["change_pct"], -9.43)
        self.assertEqual(first["source"], "DAUM")
        self.assertEqual(first["market"], "kospi")
        self.assertFalse(first["stale"])
        self.assertEqual(second, first)
        self.assertEqual(m.call_count, 2)  # RISE+FALL 1회씩, 2회째는 캐시 적중

    def test_session_derived_from_market_phase(self):
        f = _fetcher()
        with patch.object(f, "_get_json", side_effect=_json_both), patch(
            "data_provider.kr_market_context_fetcher._infer_kr_session",
            return_value="intraday",
        ):
            rec = f.get_sector_rankings("kospi")
        self.assertEqual(rec["session"], "intraday")

    def test_markets_have_separate_cache(self):
        f = _fetcher()
        with patch.object(f, "_get_json", side_effect=_json_both) as m:
            kospi = f.get_sector_rankings("kospi")
            kosdaq = f.get_sector_rankings("kosdaq")
        self.assertEqual(kospi["market"], "kospi")
        self.assertEqual(kosdaq["market"], "kosdaq")
        self.assertEqual(m.call_count, 4)  # 시장별 2호출

    def test_top_missing_yields_none_not_cached(self):
        f = _fetcher()

        def _json(url, params=None, referer=None):
            return _daum([] if params["change"] == "RISE" else _FALL)

        with patch.object(f, "_get_json", side_effect=_json) as m:
            self.assertIsNone(f.get_sector_rankings("kospi"))
            self.assertIsNone(f.get_sector_rankings("kospi"))  # 무효 결과 미캐시 -> 재호출
        self.assertGreaterEqual(m.call_count, 4)

    def test_bottom_missing_yields_none(self):
        f = _fetcher()

        def _json(url, params=None, referer=None):
            return _daum(_RISE if params["change"] == "RISE" else [])

        with patch.object(f, "_get_json", side_effect=_json):
            self.assertIsNone(f.get_sector_rankings("kospi"))

    def test_as_of_missing_yields_none(self):
        f = _fetcher()
        rise = [{"sectorName": "A", "changeRate": 0.01}]  # date 결측
        fall = [{"sectorName": "B", "changeRate": -0.01}]

        def _json(url, params=None, referer=None):
            return _daum(rise if params["change"] == "RISE" else fall)

        with patch.object(f, "_get_json", side_effect=_json):
            self.assertIsNone(f.get_sector_rankings("kospi"))

    def test_invalid_market_is_none(self):
        self.assertIsNone(_fetcher().get_sector_rankings("nasdaq"))
        self.assertIsNone(_fetcher().get_sector_rankings(None))

    def test_invalid_n_is_none(self):
        self.assertIsNone(_fetcher().get_sector_rankings("kospi", n=0))

    def test_directions_have_separate_breakers(self):
        f = _fetcher()
        with patch.object(f, "_get_json", side_effect=RuntimeError("boom")):
            for _ in range(3):  # RISE breaker 오픈(failure_threshold=3)
                f._fetch_daum_direction("kospi", "RISE", 5)
        # FALL 방향은 RISE breaker 영향 없음 — 정상 수집
        with patch.object(f, "_get_json", return_value=_daum(_FALL)) as m:
            rows, as_of = f._fetch_daum_direction("kospi", "FALL", 5)
        self.assertTrue(rows)
        self.assertEqual(as_of, "2026-07-16")
        self.assertEqual(m.call_count, 1)
        # RISE 방향은 오픈된 breaker로 미호출
        with patch.object(f, "_get_json") as m:
            rows, as_of = f._fetch_daum_direction("kospi", "RISE", 5)
        self.assertEqual(rows, [])
        self.assertIsNone(as_of)
        self.assertEqual(m.call_count, 0)


class TestRefererContract(unittest.TestCase):
    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return _daum(_FALL)

    def test_referer_header_sent(self):
        f = _fetcher()
        captured = {}

        def _fake_get(url, params=None, headers=None, timeout=None):
            captured["headers"] = headers
            captured["params"] = params
            return TestRefererContract._FakeResp()

        with patch(
            "data_provider.kr_market_context_fetcher.requests.get", side_effect=_fake_get
        ):
            f._fetch_daum_direction("kospi", "FALL", 5)
        self.assertEqual(captured["headers"]["Referer"], _DAUM_REFERER)
        self.assertEqual(captured["params"]["market"], "KOSPI")
        self.assertEqual(captured["params"]["change"], "FALL")
        self.assertEqual(captured["params"]["fieldName"], "changeRate")
        self.assertEqual(captured["params"]["order"], "desc")

    def test_http_failure_returns_empty_and_trips_breaker(self):
        f = _fetcher()
        with patch.object(f, "_get_json", side_effect=RuntimeError("403 Forbidden")):
            for _ in range(3):
                rows, _ = f._fetch_daum_direction("kospi", "RISE", 5)
                self.assertEqual(rows, [])
        self.assertFalse(f._breaker.is_available("daum_sector_kospi_rise"))
        self.assertTrue(f._breaker.is_available("daum_sector_kosdaq_rise"))  # 시장 분리


class TestStaleFallback(unittest.TestCase):
    def test_stale_fallback_same_trading_day(self):
        f = _fetcher(cache_ttl_seconds=0)  # TTL 즉시 만료 -> 재호출 유도
        with patch.object(f, "_get_json", side_effect=_json_both):
            first = f.get_sector_rankings("kospi")
        self.assertFalse(first["stale"])
        with patch.object(f, "_get_json", side_effect=RuntimeError("down")), patch(
            "data_provider.kr_market_context_fetcher._current_kr_trading_date",
            return_value="2026-07-16",
        ):
            rec = f.get_sector_rankings("kospi")
        self.assertIsNotNone(rec)
        self.assertTrue(rec["stale"])
        self.assertEqual(rec["top"][0]["name"], "통신업")

    def test_stale_rejected_for_other_trading_day(self):
        f = _fetcher(cache_ttl_seconds=0)
        with patch.object(f, "_get_json", side_effect=_json_both):
            f.get_sector_rankings("kospi")
        with patch.object(f, "_get_json", side_effect=RuntimeError("down")), patch(
            "data_provider.kr_market_context_fetcher._current_kr_trading_date",
            return_value="2026-07-17",
        ):
            self.assertIsNone(f.get_sector_rankings("kospi"))

    def test_stale_skipped_when_calendar_unavailable(self):
        f = _fetcher(cache_ttl_seconds=0)
        with patch.object(f, "_get_json", side_effect=_json_both):
            f.get_sector_rankings("kospi")
        with patch.object(f, "_get_json", side_effect=RuntimeError("down")), patch(
            "data_provider.kr_market_context_fetcher._current_kr_trading_date",
            return_value=None,
        ):
            self.assertIsNone(f.get_sector_rankings("kospi"))


class TestFailOpen(unittest.TestCase):
    def test_get_sector_rankings_fail_open_on_exception(self):
        f = _fetcher()
        with patch.object(f, "_fetch_daum_direction", side_effect=RuntimeError("boom")):
            self.assertIsNone(f.get_sector_rankings("kospi"))  # must NOT raise


if __name__ == "__main__":
    unittest.main()

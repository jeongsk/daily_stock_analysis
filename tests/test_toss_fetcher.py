# -*- coding: utf-8 -*-
"""TossFetcher offline unit tests + KR routing regression tests.

Covers: OAuth2 token issuance/caching/expiry-refresh/401-single-reissue, 429
Retry-After backoff, 403 process-wide visibility guard, symbol conversion,
get_realtime_quote price+candle supplementation (today's bar published / not
yet published / candle fetch failure fail-open), get_daily_data pagination +
normalization, and DataFetcherManager KR routing (yfinance-first daily with
Toss fallback — Toss daily candles are KRX+NXT-consolidated, not the KRX
official close; Toss-then-yfinance realtime). All mock-based and offline.

A single `-m network` smoke test is included for local runs with real
credentials + an allow-listed IP; it is not executed by CI or by
`pytest -m "not network"`.
"""

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from data_provider import toss_fetcher
from data_provider.base import DataFetchError, DataFetcherManager
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from data_provider.toss_fetcher import TossFetcher


def _make_resp(json_data=None, status_code=200, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_data if json_data is not None else {}
    if 200 <= status_code < 300:
        resp.raise_for_status.return_value = None
    else:
        import requests

        resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}")
    return resp


def _make_fetcher(client_id="test-client-id", client_secret="test-client-secret"):
    """Construct a TossFetcher with deterministic test credentials, independent of
    whatever TOSS_CLIENT_ID/TOSS_CLIENT_SECRET happen to be set in the local .env."""
    with patch.object(TossFetcher, "_load_credentials", return_value=(client_id, client_secret)):
        return TossFetcher()


def _make_daily_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-05-01",
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 1000,
                "amount": 101000.0,
                "pct_chg": 1.0,
            }
        ]
    )


def _make_quote(code: str, source=RealtimeSource.TOSS) -> UnifiedRealtimeQuote:
    return UnifiedRealtimeQuote(code=code, source=source, price=100.0)


@pytest.fixture(autouse=True)
def _block_unmocked_network(request):
    """Design spec §6: a global (whole-module) guard against any *unexpected*
    real network call in this offline suite — dry-run/order-write safety
    depends on every test here actually being offline, not merely on every
    test author remembering to mock ``requests``. Tests that legitimately
    mock the network patch ``requests.get``/``requests.post`` themselves,
    which simply replaces this guard for the duration of that patch; any
    unpatched call reaching a real socket connect() fails immediately and
    loudly instead of hanging or reaching the real Toss API. Skipped for the
    deliberately-network ``TestTossNetworkSmoke`` class below."""
    if request.node.get_closest_marker("network"):
        yield
        return

    import socket

    original_connect = socket.socket.connect

    def _guarded_connect(self, address, *a, **kw):
        raise AssertionError(
            f"Unexpected real network connection attempt to {address!r} in tests/test_toss_fetcher.py "
            f"— this suite must run fully offline (design spec "
            f"docs/superpowers/specs/2026-07-17-toss-order-phase3-design.md §6)"
        )

    socket.socket.connect = _guarded_connect
    try:
        yield
    finally:
        socket.socket.connect = original_connect


class TossFetcherTestCase(unittest.TestCase):
    """Base class: resets the process-wide 403 guard around every test."""

    def setUp(self):
        toss_fetcher.reset_toss_forbidden_guard_for_tests()

    def tearDown(self):
        toss_fetcher.reset_toss_forbidden_guard_for_tests()


class TestTokenManagement(TossFetcherTestCase):
    @patch("data_provider.toss_fetcher.requests.post")
    def test_token_issued_once_and_cached(self, mock_post):
        mock_post.return_value = _make_resp({"access_token": "tok-1", "expires_in": 3600})
        fetcher = _make_fetcher()

        t1 = fetcher._get_access_token()
        t2 = fetcher._get_access_token()

        self.assertEqual(t1, "tok-1")
        self.assertEqual(t2, "tok-1")
        mock_post.assert_called_once()

    @patch("data_provider.toss_fetcher.requests.post")
    def test_token_refreshed_after_expiry(self, mock_post):
        mock_post.side_effect = [
            _make_resp({"access_token": "tok-1", "expires_in": 3600}),
            _make_resp({"access_token": "tok-2", "expires_in": 3600}),
        ]
        fetcher = _make_fetcher()

        t1 = fetcher._get_access_token()
        fetcher._token_expires_at = time.time() - 1  # force expiry
        t2 = fetcher._get_access_token()

        self.assertEqual(t1, "tok-1")
        self.assertEqual(t2, "tok-2")
        self.assertEqual(mock_post.call_count, 2)

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_401_triggers_single_token_refresh_and_retry(self, mock_post, mock_get):
        mock_post.side_effect = [
            _make_resp({"access_token": "tok-1", "expires_in": 3600}),
            _make_resp({"access_token": "tok-2", "expires_in": 3600}),
        ]
        mock_get.side_effect = [
            _make_resp({}, status_code=401),
            _make_resp({"result": []}, status_code=200),
        ]
        fetcher = _make_fetcher()

        result = fetcher._request(toss_fetcher._PRICES_URL, params={"symbols": "005930"})

        self.assertEqual(result, {"result": []})
        self.assertEqual(mock_post.call_count, 2)  # initial issue + forced refresh on 401
        self.assertEqual(mock_get.call_count, 2)


class TestRetryAndForbiddenVisibility(TossFetcherTestCase):
    @patch("data_provider.toss_fetcher.time.sleep", return_value=None)
    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_429_backs_off_using_retry_after_then_succeeds(self, mock_post, mock_get, mock_sleep):
        mock_post.return_value = _make_resp({"access_token": "tok", "expires_in": 3600})
        mock_get.side_effect = [
            _make_resp({}, status_code=429, headers={"Retry-After": "1"}),
            _make_resp({"result": []}, status_code=200),
        ]
        fetcher = _make_fetcher()

        result = fetcher._request(toss_fetcher._PRICES_URL)

        self.assertEqual(result, {"result": []})
        mock_sleep.assert_called_once()
        self.assertAlmostEqual(mock_sleep.call_args[0][0], 1.0, delta=0.5)  # Retry-After + jitter

    @patch("data_provider.toss_fetcher.time.sleep", return_value=None)
    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_429_exceeding_retry_limit_raises(self, mock_post, mock_get, mock_sleep):
        mock_post.return_value = _make_resp({"access_token": "tok", "expires_in": 3600})
        # _RETRY_LIMIT=2: attempts 1 and 2 back off, attempt 3 exceeds the cap and raises.
        mock_get.side_effect = [_make_resp({}, status_code=429) for _ in range(3)]
        fetcher = _make_fetcher()

        with self.assertRaises(DataFetchError):
            fetcher._request(toss_fetcher._PRICES_URL)
        self.assertEqual(mock_get.call_count, 3)

    @patch("data_provider.toss_fetcher._lookup_public_ip", return_value="203.0.113.1")
    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_403_warns_once_then_skips_subsequent_calls(self, mock_post, mock_get, mock_ip):
        mock_post.return_value = _make_resp({"access_token": "tok", "expires_in": 3600})
        mock_get.return_value = _make_resp({}, status_code=403)
        fetcher = _make_fetcher()

        with self.assertLogs("data_provider.toss_fetcher", level="WARNING") as log:
            with self.assertRaises(DataFetchError):
                fetcher._request(toss_fetcher._PRICES_URL)
        self.assertTrue(any("허용되지 않은 IP" in line for line in log.output))
        self.assertTrue(toss_fetcher.is_toss_forbidden())

        mock_get.reset_mock()
        with self.assertRaises(DataFetchError):
            fetcher._request(toss_fetcher._PRICES_URL)
        mock_get.assert_not_called()  # skipped entirely, no further HTTP call


class TestSymbolConversion(unittest.TestCase):
    def test_kr_ks_suffix_strips_to_bare_code(self):
        self.assertEqual(toss_fetcher._to_toss_symbol("005930.KS"), "005930")

    def test_kr_kq_suffix_strips_to_bare_code(self):
        self.assertEqual(toss_fetcher._to_toss_symbol("035720.KQ"), "035720")

    def test_us_ticker_passes_through_unchanged(self):
        self.assertEqual(toss_fetcher._to_toss_symbol("AAPL"), "AAPL")

    def test_unsupported_markets_return_none(self):
        self.assertIsNone(toss_fetcher._to_toss_symbol("0700.HK"))
        self.assertIsNone(toss_fetcher._to_toss_symbol("600519"))  # CN A-share, no suffix

    def test_detect_market(self):
        self.assertEqual(toss_fetcher._detect_market("005930.KS"), "kr")
        self.assertEqual(toss_fetcher._detect_market("035720.KQ"), "kr")
        self.assertEqual(toss_fetcher._detect_market("AAPL"), "us")
        self.assertIsNone(toss_fetcher._detect_market("0700.HK"))


class TestRealtimeQuoteCandleSupplement(TossFetcherTestCase):
    """get_realtime_quote() price+candle merge — the (b) requirement."""

    def _prices_payload(self, timestamp: str, last_price: str = "253500"):
        return {
            "result": [
                {"symbol": "005930", "lastPrice": last_price, "currency": "KRW", "timestamp": timestamp}
            ]
        }

    def test_todays_bar_published_fills_ohlc_volume_and_pre_close(self):
        fetcher = _make_fetcher()
        candles_payload = {
            "result": {
                "candles": [
                    {
                        "timestamp": "2026-07-16T00:00:00.000+09:00",
                        "openPrice": "269000",
                        "highPrice": "269000",
                        "lowPrice": "251000",
                        "closePrice": "253500",
                        "volume": "44712225",
                        "currency": "KRW",
                    },
                    {
                        "timestamp": "2026-07-15T00:00:00.000+09:00",
                        "openPrice": "270000",
                        "highPrice": "275000",
                        "lowPrice": "268000",
                        "closePrice": "273500",
                        "volume": "3000000",
                        "currency": "KRW",
                    },
                ],
                "nextBefore": "2026-07-15T00:00:00.000+09:00",
            }
        }

        def fake_request(url, params=None):
            if url == toss_fetcher._PRICES_URL:
                return self._prices_payload("2026-07-16T19:59:59.000+09:00")
            if url == toss_fetcher._CANDLES_URL:
                return candles_payload
            raise AssertionError(f"unexpected url: {url}")

        with patch.object(fetcher, "_request", side_effect=fake_request):
            quote = fetcher.get_realtime_quote("005930.KS")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.data_quality, "ok")
        self.assertIsNone(quote.missing_fields)
        self.assertEqual(quote.open_price, 269000.0)
        self.assertEqual(quote.high, 269000.0)
        self.assertEqual(quote.low, 251000.0)
        self.assertEqual(quote.volume, 44712225)
        self.assertEqual(quote.pre_close, 273500.0)
        self.assertEqual(quote.change_amount, -20000.0)
        self.assertAlmostEqual(quote.change_pct, -7.31, places=2)
        self.assertIsNotNone(quote.amplitude)

    def test_todays_bar_not_yet_published_only_fills_pre_close(self):
        fetcher = _make_fetcher()
        candles_payload = {
            "result": {
                "candles": [
                    {
                        "timestamp": "2026-07-15T00:00:00.000+09:00",
                        "openPrice": "270000",
                        "highPrice": "275000",
                        "lowPrice": "268000",
                        "closePrice": "273500",
                        "volume": "3000000",
                        "currency": "KRW",
                    },
                ],
                "nextBefore": None,
            }
        }

        def fake_request(url, params=None):
            if url == toss_fetcher._PRICES_URL:
                return self._prices_payload("2026-07-16T09:01:00.000+09:00", last_price="274000")
            if url == toss_fetcher._CANDLES_URL:
                return candles_payload
            raise AssertionError(f"unexpected url: {url}")

        with patch.object(fetcher, "_request", side_effect=fake_request):
            quote = fetcher.get_realtime_quote("005930.KS")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.data_quality, "partial")
        self.assertIsNone(quote.open_price)
        self.assertIsNone(quote.high)
        self.assertIsNone(quote.low)
        self.assertIsNone(quote.volume)
        self.assertEqual(quote.pre_close, 273500.0)
        self.assertEqual(quote.change_amount, 500.0)
        self.assertIn("open_price", quote.missing_fields)
        self.assertIn("volume", quote.missing_fields)
        self.assertNotIn("pre_close", quote.missing_fields)

    def test_candle_fetch_failure_fails_open_to_price_only_quote(self):
        fetcher = _make_fetcher()

        def fake_request(url, params=None):
            if url == toss_fetcher._PRICES_URL:
                return self._prices_payload("2026-07-16T19:59:59.000+09:00")
            if url == toss_fetcher._CANDLES_URL:
                raise DataFetchError("[Toss] simulated candle failure")
            raise AssertionError(f"unexpected url: {url}")

        with patch.object(fetcher, "_request", side_effect=fake_request):
            quote = fetcher.get_realtime_quote("005930.KS")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.price, 253500.0)
        self.assertEqual(quote.data_quality, "partial")
        self.assertIsNone(quote.pre_close)
        self.assertIsNone(quote.change_amount)
        self.assertIsNone(quote.change_pct)


class TestDailyDataPaginationAndNormalization(TossFetcherTestCase):
    def test_fetch_raw_data_paginates_until_start_date_covered(self):
        fetcher = _make_fetcher()
        page1 = {
            "result": {
                "candles": [
                    {"timestamp": "2026-01-10T00:00:00+09:00", "openPrice": "10", "highPrice": "11", "lowPrice": "9", "closePrice": "10.5", "volume": "100"},
                    {"timestamp": "2026-01-09T00:00:00+09:00", "openPrice": "9", "highPrice": "10", "lowPrice": "8", "closePrice": "9.5", "volume": "90"},
                    {"timestamp": "2026-01-05T00:00:00+09:00", "openPrice": "8", "highPrice": "9", "lowPrice": "7", "closePrice": "8.5", "volume": "80"},
                ],
                "nextBefore": "2026-01-05T00:00:00+09:00",
            }
        }
        # 'before' pagination is inclusive of its boundary, so 01-05 legitimately
        # reappears as the first row of page 2 — normalize() must de-dup it.
        page2 = {
            "result": {
                "candles": [
                    {"timestamp": "2026-01-05T00:00:00+09:00", "openPrice": "8", "highPrice": "9", "lowPrice": "7", "closePrice": "8.5", "volume": "80"},
                    {"timestamp": "2026-01-01T00:00:00+09:00", "openPrice": "7", "highPrice": "8", "lowPrice": "6", "closePrice": "7.5", "volume": "70"},
                ],
                "nextBefore": None,
            }
        }

        mock_request = MagicMock(side_effect=[page1, page2])
        with patch.object(fetcher, "_request", mock_request):
            raw = fetcher._fetch_raw_data("005930.KS", "2026-01-01", "2026-01-10")

        self.assertEqual(mock_request.call_count, 2)
        second_call_params = mock_request.call_args_list[1].kwargs.get("params") or mock_request.call_args_list[1][0][1]
        self.assertEqual(second_call_params.get("before"), "2026-01-05T00:00:00+09:00")
        self.assertEqual(len(raw), 5)  # 3 + 2, duplicate 01-05 row not yet deduped (normalize's job)

        normalized = fetcher._normalize_data(raw, "005930.KS")

        self.assertEqual(list(normalized.columns)[1:], list(normalized.columns)[1:])  # sanity: non-empty
        for col in ("date", "open", "high", "low", "close", "volume", "amount", "pct_chg"):
            self.assertIn(col, normalized.columns)
        self.assertEqual(len(normalized), 4)  # deduped: 01-01, 01-05, 01-09, 01-10
        self.assertTrue((normalized["code"] == "005930.KS").all())
        self.assertEqual(list(normalized["close"]), [7.5, 8.5, 9.5, 10.5])

    def test_normalize_empty_dataframe_returns_standard_columns(self):
        fetcher = _make_fetcher()
        from data_provider.base import STANDARD_COLUMNS

        result = fetcher._normalize_data(pd.DataFrame(), "005930.KS")

        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), STANDARD_COLUMNS)


class TestGetClosedOrdersEnvelope(TossFetcherTestCase):
    """Strict envelope validation for GET /api/v1/orders (design spec §3
    "envelope 엄격 검증", Codex major 1). The confirmed real shape is
    ``{"result": {"orders": [...], "nextCursor": str | None, "hasNext": bool}}``
    — any deviation (missing key, wrong type, hasNext=true with an empty
    nextCursor, or exhausting the page-count safety cap while hasNext is
    still true) must raise DataFetchError instead of silently returning an
    empty/partial order list, which would look identical to "the sync found
    zero new orders" to a caller and silently drop real fills."""

    @staticmethod
    def _token_resp():
        return _make_resp({"access_token": "tok", "expires_in": 3600})

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_multi_page_real_envelope_paginates_to_completion(self, mock_post, mock_get):
        mock_post.return_value = self._token_resp()
        page1 = _make_resp(
            {
                "result": {
                    "orders": [{"orderId": "1"}, {"orderId": "2"}],
                    "nextCursor": "cursor-2",
                    "hasNext": True,
                }
            }
        )
        page2 = _make_resp(
            {
                "result": {
                    "orders": [{"orderId": "3"}],
                    "nextCursor": None,
                    "hasNext": False,
                }
            }
        )
        mock_get.side_effect = [page1, page2]
        fetcher = _make_fetcher()

        orders = fetcher.get_closed_orders(555, from_date=None)

        self.assertEqual([o["orderId"] for o in orders], ["1", "2", "3"])
        self.assertEqual(mock_get.call_count, 2)
        second_call_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertEqual(second_call_params.get("cursor"), "cursor-2")

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_has_next_true_without_cursor_raises(self, mock_post, mock_get):
        mock_post.return_value = self._token_resp()
        mock_get.return_value = _make_resp(
            {"result": {"orders": [], "nextCursor": None, "hasNext": True}}
        )
        fetcher = _make_fetcher()
        with self.assertRaises(DataFetchError):
            fetcher.get_closed_orders(555)

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_missing_orders_key_raises(self, mock_post, mock_get):
        mock_post.return_value = self._token_resp()
        mock_get.return_value = _make_resp({"result": {"hasNext": False, "nextCursor": None}})
        fetcher = _make_fetcher()
        with self.assertRaises(DataFetchError):
            fetcher.get_closed_orders(555)

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_missing_has_next_key_raises(self, mock_post, mock_get):
        mock_post.return_value = self._token_resp()
        mock_get.return_value = _make_resp({"result": {"orders": [], "nextCursor": None}})
        fetcher = _make_fetcher()
        with self.assertRaises(DataFetchError):
            fetcher.get_closed_orders(555)

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_missing_next_cursor_key_raises(self, mock_post, mock_get):
        mock_post.return_value = self._token_resp()
        mock_get.return_value = _make_resp({"result": {"orders": [], "hasNext": False}})
        fetcher = _make_fetcher()
        with self.assertRaises(DataFetchError):
            fetcher.get_closed_orders(555)

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_missing_result_object_raises(self, mock_post, mock_get):
        mock_post.return_value = self._token_resp()
        mock_get.return_value = _make_resp({"orders": []})
        fetcher = _make_fetcher()
        with self.assertRaises(DataFetchError):
            fetcher.get_closed_orders(555)

    @patch("data_provider.toss_fetcher._ORDERS_MAX_PAGES", 2)
    @patch("data_provider.toss_fetcher.requests.post")
    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.time.sleep", return_value=None)
    def test_page_cap_reached_with_has_next_true_raises_not_partial(self, mock_sleep, mock_get, mock_post):
        mock_post.return_value = self._token_resp()
        mock_get.return_value = _make_resp(
            {"result": {"orders": [{"orderId": "x"}], "nextCursor": "next", "hasNext": True}}
        )
        fetcher = _make_fetcher()
        with self.assertRaises(DataFetchError):
            fetcher.get_closed_orders(555)
        # Capped at _ORDERS_MAX_PAGES calls, not returned as a partial result.
        self.assertEqual(mock_get.call_count, 2)


class TestCredentialGatedRegistration(unittest.TestCase):
    def test_has_configured_credentials_false_when_blank(self):
        # _load_credentials falls back to os.getenv() when the config value is blank
        # (by design, mirrors other optional fetchers) — patch the env too so a real
        # local .env with TOSS_CLIENT_ID/SECRET can't leak into this assertion.
        config = SimpleNamespace(toss_client_id="", toss_client_secret="")
        with patch.dict("os.environ", {"TOSS_CLIENT_ID": "", "TOSS_CLIENT_SECRET": ""}):
            self.assertFalse(TossFetcher.has_configured_credentials(config))

    def test_has_configured_credentials_true_when_both_set(self):
        config = SimpleNamespace(toss_client_id="id", toss_client_secret="secret")
        self.assertTrue(TossFetcher.has_configured_credentials(config))

    def test_unconfigured_fetcher_makes_no_network_call(self):
        fetcher = _make_fetcher(client_id=None, client_secret=None)
        with patch("data_provider.toss_fetcher.requests.get") as mock_get, patch(
            "data_provider.toss_fetcher.requests.post"
        ) as mock_post:
            quote = fetcher.get_realtime_quote("005930.KS")
        self.assertIsNone(quote)
        mock_get.assert_not_called()
        mock_post.assert_not_called()


class TestManagerTossRouting(unittest.TestCase):
    """DataFetcherManager KR routing: yfinance-first daily (official KRX close) with
    Toss as fallback (Toss daily candles are KRX+NXT-consolidated, not the KRX
    official close — see data_provider/toss_fetcher.py module docstring and
    docs/adr/0003-toss-openapi-credential-gated-source.md), plus Toss-then-yfinance
    realtime (NXT-consolidated latest trade is the desired realtime behavior)."""

    @patch("src.config.get_config")
    def test_kr_daily_route_prefers_yfinance_over_toss(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace()
        toss = MagicMock()
        toss.name = "TossFetcher"
        toss.priority = 6
        toss.get_daily_data.return_value = _make_daily_df()

        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_daily_data.return_value = _make_daily_df()

        # Registration order deliberately mismatches priority order to prove the
        # generic priority-sorted loop (not a KR-specific reorder) decides ordering:
        # Yfinance's priority 4 sorts ahead of Toss's 6 regardless of registration order.
        manager = DataFetcherManager(fetchers=[toss, yfinance])

        df, source = manager.get_daily_data("005930.KS", start_date="2026-05-01", end_date="2026-05-08")

        self.assertFalse(df.empty)
        self.assertEqual(source, "YfinanceFetcher")
        yfinance.get_daily_data.assert_called_once()
        toss.get_daily_data.assert_not_called()

    @patch("src.config.get_config")
    def test_kr_daily_route_falls_back_to_toss_when_yfinance_fails(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace()
        toss = MagicMock()
        toss.name = "TossFetcher"
        toss.priority = 6
        toss.get_daily_data.return_value = _make_daily_df()

        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_daily_data.side_effect = RuntimeError("yfinance unavailable")

        manager = DataFetcherManager(fetchers=[yfinance, toss])

        df, source = manager.get_daily_data("005930.KS", start_date="2026-05-01", end_date="2026-05-08")

        self.assertFalse(df.empty)
        self.assertEqual(source, "TossFetcher")
        yfinance.get_daily_data.assert_called_once()
        toss.get_daily_data.assert_called_once()

    @patch("src.config.get_config")
    def test_kr_realtime_route_uses_toss_when_available(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=600)

        toss_quote = _make_quote("005930.KS", source=RealtimeSource.TOSS)
        toss = MagicMock()
        toss.name = "TossFetcher"
        toss.priority = 6
        toss.get_realtime_quote.return_value = toss_quote

        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_realtime_quote.return_value = _make_quote("005930.KS", source=RealtimeSource.FALLBACK)

        manager = DataFetcherManager(fetchers=[yfinance, toss])

        quote = manager.get_realtime_quote("005930.KS")

        self.assertIs(quote, toss_quote)
        toss.get_realtime_quote.assert_called_once_with("005930.KS")
        yfinance.get_realtime_quote.assert_not_called()

    @patch("src.config.get_config")
    def test_kr_realtime_route_demotes_to_yfinance_when_toss_fails(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=600)

        toss = MagicMock()
        toss.name = "TossFetcher"
        toss.priority = 6
        toss.get_realtime_quote.return_value = None

        yfinance_quote = _make_quote("005930.KS", source=RealtimeSource.FALLBACK)
        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_realtime_quote.return_value = yfinance_quote

        manager = DataFetcherManager(fetchers=[toss, yfinance])

        quote = manager.get_realtime_quote("005930.KS")

        self.assertIs(quote, yfinance_quote)
        self.assertEqual(quote.fallback_from, "toss")
        toss.get_realtime_quote.assert_called_once_with("005930.KS")
        yfinance.get_realtime_quote.assert_called_once_with("005930.KS")


class TestOrderLiveFlag(TossFetcherTestCase):
    """``TossFetcher.is_order_live_enabled`` — the fetcher-level half of the
    Phase 3 dual live-order gate (design spec v2 §3). ``TOSS_ORDER_LIVE`` uses
    *strict* parsing (``src.config.parse_strict_true_env_bool``) — only the
    exact value ``"true"`` is live; every other non-empty value is an
    ERROR-logged misconfiguration that stays dry-run (Codex blocker 2)."""

    def test_defaults_false_without_config_or_env(self):
        with patch("src.config.get_config", side_effect=Exception("no config")):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TOSS_ORDER_LIVE", None)
                self.assertFalse(TossFetcher.is_order_live_enabled())

    def test_reads_env_when_config_unavailable(self):
        with patch("src.config.get_config", side_effect=Exception("no config")):
            with patch.dict(os.environ, {"TOSS_ORDER_LIVE": "true"}):
                self.assertTrue(TossFetcher.is_order_live_enabled())

    def test_reads_explicit_config_object(self):
        self.assertTrue(TossFetcher.is_order_live_enabled(SimpleNamespace(toss_order_live=True)))
        self.assertFalse(TossFetcher.is_order_live_enabled(SimpleNamespace(toss_order_live=False)))

    def test_strict_parsing_table_only_exact_true_is_live(self):
        """Design spec v2 §6 strict-parsing table: "true"/"TRUE "/"1"/"yes"/
        "flase"/blank/unset."""
        from src.config import parse_strict_true_env_bool

        cases = [
            ("true", True),
            ("TRUE ", True),
            ("  true  ", True),
            ("1", False),
            ("yes", False),
            ("flase", False),
            ("", False),
            ("   ", False),
            (None, False),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(
                    parse_strict_true_env_bool(raw, field_name="TOSS_ORDER_LIVE"), expected
                )

    def test_strict_parsing_logs_error_only_for_wrong_nonempty_values(self):
        from src.config import parse_strict_true_env_bool

        with patch("src.config.logger") as mock_logger:
            parse_strict_true_env_bool("yes", field_name="TOSS_ORDER_LIVE")
            mock_logger.error.assert_called_once()

        with patch("src.config.logger") as mock_logger:
            parse_strict_true_env_bool("", field_name="TOSS_ORDER_LIVE")
            mock_logger.error.assert_not_called()

        with patch("src.config.logger") as mock_logger:
            parse_strict_true_env_bool(None, field_name="TOSS_ORDER_LIVE")
            mock_logger.error.assert_not_called()

        with patch("src.config.logger") as mock_logger:
            parse_strict_true_env_bool("true", field_name="TOSS_ORDER_LIVE")
            mock_logger.error.assert_not_called()


class TestOrderAmountCapParsing(unittest.TestCase):
    """``src.config.parse_env_float_finite_positive`` — NaN/Infinity/
    non-positive amount caps must never be clamped (a clamp compares against
    the value, and NaN compares false against everything); they must be
    replaced wholesale with the safe default (Codex blocker 5)."""

    def test_nan_infinity_and_nonpositive_force_default(self):
        from src.config import parse_env_float_finite_positive

        for raw in ("nan", "NaN", "inf", "-inf", "Infinity", "0", "-5", "not-a-number"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    parse_env_float_finite_positive(raw, 1_000_000.0, field_name="TOSS_ORDER_MAX_AMOUNT_KRW"),
                    1_000_000.0,
                )

    def test_valid_positive_value_is_used_as_is(self):
        from src.config import parse_env_float_finite_positive

        self.assertEqual(
            parse_env_float_finite_positive("2500000", 1_000_000.0, field_name="TOSS_ORDER_MAX_AMOUNT_KRW"),
            2_500_000.0,
        )

    def test_unset_uses_default_without_error_log(self):
        from src.config import parse_env_float_finite_positive

        with patch("src.config.logger") as mock_logger:
            value = parse_env_float_finite_positive(None, 1_000_000.0, field_name="TOSS_ORDER_MAX_AMOUNT_KRW")
        self.assertEqual(value, 1_000_000.0)
        mock_logger.error.assert_not_called()


class TestOrderWrites(TossFetcherTestCase):
    """``place_order``/``cancel_order`` — POST /orders, POST /orders/{id}/cancel.

    Design spec §6 mock-level assertion: without the live flag, no HTTP call
    is ever made by ``place_order``/``cancel_order`` (the fetcher-level half
    of the dual gate — the service-level half is covered in
    tests/test_portfolio_order_service.py). v2: cancel_order now shares the
    exact same live gate as place_order (Codex major 5), and ``_request_write``
    itself refuses any orders-family URL when live is disabled even if called
    directly, bypassing both public methods (Codex blocker 3).
    """

    @patch("data_provider.toss_fetcher.requests.post")
    def test_place_order_refuses_without_live_flag_and_makes_no_http_call(self, mock_post):
        fetcher = _make_fetcher()
        with patch.object(TossFetcher, "is_order_live_enabled", return_value=False):
            with self.assertRaises(toss_fetcher.TossOrderNotLiveError):
                fetcher.place_order(
                    "555",
                    symbol="005930",
                    side="BUY",
                    order_type="LIMIT",
                    quantity="1",
                    price="70000",
                    client_order_id="dsa-x",
                )
        mock_post.assert_not_called()

    def test_place_order_requires_client_order_id_argument(self):
        """``client_order_id`` is a required keyword-only argument now (no
        default) — a call site that forgets it fails immediately with a
        TypeError, not by silently sending a request without one."""
        fetcher = _make_fetcher()
        with self.assertRaises(TypeError):
            fetcher.place_order(  # noqa: missing client_order_id on purpose
                "555", symbol="005930", side="BUY", order_type="LIMIT", quantity="1", price="70000"
            )

    def test_place_order_signature_has_no_confirm_high_value_order_parameter(self):
        """``confirm_high_value_order`` must not exist at all — not merely
        default to False — so no call site can ever construct a request that
        auto-confirms a high-value order."""
        import inspect

        params = inspect.signature(TossFetcher.place_order).parameters
        self.assertNotIn("confirm_high_value_order", params)

    @patch("data_provider.toss_fetcher.requests.post")
    def test_place_order_live_success_sends_expected_body_and_headers(self, mock_post):
        mock_post.side_effect = [
            _make_resp({"access_token": "tok-1", "expires_in": 3600}),
            _make_resp({"result": {"orderId": "abc123", "clientOrderId": "dsa-x"}}, status_code=200),
        ]
        fetcher = _make_fetcher()
        with patch.object(TossFetcher, "is_order_live_enabled", return_value=True):
            result = fetcher.place_order(
                "555",
                symbol="005930",
                side="BUY",
                order_type="LIMIT",
                quantity="1",
                price="70000",
                client_order_id="dsa-x",
            )
        self.assertEqual(result["orderId"], "abc123")
        order_call = mock_post.call_args_list[1]
        self.assertEqual(
            order_call.kwargs["json"],
            {"symbol": "005930", "side": "BUY", "orderType": "LIMIT", "quantity": "1", "price": "70000", "clientOrderId": "dsa-x"},
        )
        self.assertEqual(order_call.kwargs["headers"]["X-Tossinvest-Account"], "555")
        self.assertNotIn("confirmHighValueOrder", order_call.kwargs["json"])

    @patch("data_provider.toss_fetcher.requests.post")
    def test_place_order_rejected_business_error_preserves_code_and_data(self, mock_post):
        mock_post.side_effect = [
            _make_resp({"access_token": "tok-1", "expires_in": 3600}),
            _make_resp(
                {
                    "error": {
                        "code": "insufficient-buying-power",
                        "message": "주문 가능 금액이 부족합니다.",
                        "requestId": "req-1",
                    }
                },
                status_code=422,
            ),
        ]
        fetcher = _make_fetcher()
        with patch.object(TossFetcher, "is_order_live_enabled", return_value=True):
            with self.assertRaises(toss_fetcher.TossOrderRejectedError) as ctx:
                fetcher.place_order(
                    "555",
                    symbol="005930",
                    side="BUY",
                    order_type="LIMIT",
                    quantity="1",
                    price="70000",
                    client_order_id="dsa-x",
                )
        self.assertEqual(ctx.exception.code, "insufficient-buying-power")
        self.assertEqual(ctx.exception.status_code, 422)

    @patch("data_provider.toss_fetcher.time.sleep", return_value=None)
    @patch("data_provider.toss_fetcher.requests.post")
    def test_place_order_429_backs_off_then_succeeds(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _make_resp({"access_token": "tok-1", "expires_in": 3600}),
            _make_resp({}, status_code=429),
            _make_resp({"result": {"orderId": "abc123"}}, status_code=200),
        ]
        fetcher = _make_fetcher()
        with patch.object(TossFetcher, "is_order_live_enabled", return_value=True):
            result = fetcher.place_order(
                "555",
                symbol="005930",
                side="BUY",
                order_type="LIMIT",
                quantity="1",
                price="70000",
                client_order_id="dsa-x",
            )
        self.assertEqual(result["orderId"], "abc123")
        self.assertEqual(mock_post.call_count, 3)

    @patch("data_provider.toss_fetcher.requests.post")
    def test_cancel_order_refuses_without_live_flag_and_makes_no_http_call(self, mock_post):
        """v2 reversal (Codex major 5): cancel_order now shares place_order's
        live gate — a dry-run process must not be able to cancel a real order
        either."""
        fetcher = _make_fetcher()
        with patch.object(TossFetcher, "is_order_live_enabled", return_value=False):
            with self.assertRaises(toss_fetcher.TossOrderNotLiveError):
                fetcher.cancel_order("555", "abc123")
        mock_post.assert_not_called()

    @patch("data_provider.toss_fetcher.requests.post")
    def test_cancel_order_live_success(self, mock_post):
        mock_post.side_effect = [
            _make_resp({"access_token": "tok-1", "expires_in": 3600}),
            _make_resp({"result": {"orderId": "cancel-op-1"}}, status_code=200),
        ]
        fetcher = _make_fetcher()
        with patch.object(TossFetcher, "is_order_live_enabled", return_value=True):
            result = fetcher.cancel_order("555", "abc123")
        self.assertEqual(result["orderId"], "cancel-op-1")

    @patch("data_provider.toss_fetcher.requests.post")
    def test_cancel_order_conflict_error(self, mock_post):
        mock_post.side_effect = [
            _make_resp({"access_token": "tok-1", "expires_in": 3600}),
            _make_resp({"error": {"code": "already-canceled", "message": "이미 취소된 주문입니다."}}, status_code=409),
        ]
        fetcher = _make_fetcher()
        with patch.object(TossFetcher, "is_order_live_enabled", return_value=True):
            with self.assertRaises(toss_fetcher.TossOrderRejectedError) as ctx:
                fetcher.cancel_order("555", "abc123")
        self.assertEqual(ctx.exception.code, "already-canceled")

    @patch("data_provider.toss_fetcher.requests.post")
    def test_request_write_gates_orders_url_even_when_called_directly(self, mock_post):
        """Codex blocker 3: the fetcher-level gate must be enforced inside
        ``_request_write`` itself for any orders-family URL, so a caller that
        bypasses both ``place_order`` and ``cancel_order`` (e.g. a future or
        buggy internal call site) still cannot reach a real Toss order POST."""
        fetcher = _make_fetcher()
        with patch.object(TossFetcher, "is_order_live_enabled", return_value=False):
            with self.assertRaises(toss_fetcher.TossOrderNotLiveError):
                fetcher._request_write(
                    toss_fetcher._ORDERS_URL, json_body={"symbol": "005930"}, account_seq="555"
                )
            with self.assertRaises(toss_fetcher.TossOrderNotLiveError):
                fetcher._request_write(
                    f"{toss_fetcher._ORDERS_URL}/abc123/cancel", json_body={}, account_seq="555"
                )
        mock_post.assert_not_called()

    @patch("data_provider.toss_fetcher.requests.post")
    def test_request_write_gates_orders_url_with_query_string(self, mock_post):
        """Reviewer re-review major 3: the gate must match on the URL's
        *path*, not do a raw string prefix/equality check — a query string
        appended to an orders-family URL (e.g. ``.../api/v1/orders?foo=bar``)
        must still be caught. A plain
        ``url == _ORDERS_URL or url.startswith(f"{_ORDERS_URL}/")`` check
        misses this: the character right after "orders" is "?", not "/", so
        neither branch matches."""
        fetcher = _make_fetcher()
        with patch.object(TossFetcher, "is_order_live_enabled", return_value=False):
            with self.assertRaises(toss_fetcher.TossOrderNotLiveError):
                fetcher._request_write(
                    f"{toss_fetcher._ORDERS_URL}?foo=bar", json_body={"symbol": "005930"}, account_seq="555"
                )
            with self.assertRaises(toss_fetcher.TossOrderNotLiveError):
                fetcher._request_write(
                    f"{toss_fetcher._ORDERS_URL}/abc123/cancel?foo=bar", json_body={}, account_seq="555"
                )
        mock_post.assert_not_called()

    @patch("data_provider.toss_fetcher.requests.post")
    def test_request_write_does_not_gate_non_orders_urls(self, mock_post):
        """The orders-family gate must not leak onto unrelated write-style
        URLs (none exist today, but the check is a URL-prefix match, not a
        blanket "any POST" gate)."""
        mock_post.side_effect = [
            _make_resp({"access_token": "tok-1", "expires_in": 3600}),
            _make_resp({"result": {"ok": True}}, status_code=200),
        ]
        fetcher = _make_fetcher()
        with patch.object(TossFetcher, "is_order_live_enabled", return_value=False):
            result = fetcher._request_write(
                f"{toss_fetcher._TOSS_BASE_URL}/api/v1/not-an-order-endpoint",
                json_body={},
                account_seq="555",
            )
        self.assertEqual(result, {"ok": True})


class TestOrderInfoReads(TossFetcherTestCase):
    """Read-only order-info endpoints: buying-power, sellable-quantity,
    commissions, order status."""

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_get_buying_power(self, mock_post, mock_get):
        mock_post.return_value = _make_resp({"access_token": "tok-1", "expires_in": 3600})
        mock_get.return_value = _make_resp({"result": {"currency": "KRW", "cashBuyingPower": "5000000"}})
        fetcher = _make_fetcher()
        value = fetcher.get_buying_power("555", "KRW")
        self.assertEqual(value, 5000000.0)
        self.assertEqual(mock_get.call_args.kwargs["params"], {"currency": "KRW"})

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_get_buying_power_missing_field_raises(self, mock_post, mock_get):
        mock_post.return_value = _make_resp({"access_token": "tok-1", "expires_in": 3600})
        mock_get.return_value = _make_resp({"result": {"currency": "KRW"}})
        fetcher = _make_fetcher()
        with self.assertRaises(DataFetchError):
            fetcher.get_buying_power("555", "KRW")

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_get_sellable_quantity(self, mock_post, mock_get):
        mock_post.return_value = _make_resp({"access_token": "tok-1", "expires_in": 3600})
        mock_get.return_value = _make_resp({"result": {"sellableQuantity": "100"}})
        fetcher = _make_fetcher()
        value = fetcher.get_sellable_quantity("555", "005930")
        self.assertEqual(value, 100.0)

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_get_commissions(self, mock_post, mock_get):
        mock_post.return_value = _make_resp({"access_token": "tok-1", "expires_in": 3600})
        mock_get.return_value = _make_resp({"result": [{"marketCountry": "KR", "commissionRate": "0.015"}]})
        fetcher = _make_fetcher()
        result = fetcher.get_commissions("555")
        self.assertEqual(result[0]["marketCountry"], "KR")

    @patch("data_provider.toss_fetcher.requests.get")
    @patch("data_provider.toss_fetcher.requests.post")
    def test_get_order(self, mock_post, mock_get):
        mock_post.return_value = _make_resp({"access_token": "tok-1", "expires_in": 3600})
        mock_get.return_value = _make_resp({"result": {"orderId": "abc123", "status": "FILLED"}})
        fetcher = _make_fetcher()
        result = fetcher.get_order("555", "abc123")
        self.assertEqual(result["status"], "FILLED")


class TestTossNetworkSmoke(unittest.TestCase):
    """Live smoke against the real Toss OpenAPI. Requires TOSS_CLIENT_ID/
    TOSS_CLIENT_SECRET plus an IP already allow-listed in Toss WTS (ADR 0003) —
    skipped otherwise. Not run by CI or by `pytest -m "not network"`."""

    @pytest.mark.network
    def test_live_realtime_quote(self):
        if not TossFetcher.has_configured_credentials():
            pytest.skip(
                "Toss 실측 스모크 스킵: TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 자격증명 + 허용 IP 필요"
            )
        fetcher = TossFetcher()
        quote = fetcher.get_realtime_quote("005930.KS")
        self.assertIsNotNone(quote)
        self.assertIsNotNone(quote.price)
        self.assertGreater(quote.price, 0)


if __name__ == "__main__":
    unittest.main()

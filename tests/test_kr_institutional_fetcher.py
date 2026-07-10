# -*- coding: utf-8 -*-
"""KrInstitutionalFetcher 오프라인 테스트.

픽스처는 전부 2026-07-10 실제 응답 캡처다(삼성전자 005930 / KOSPI).
네트워크 접근 없음 — `pytest -m "not network"` 게이트에 포함된다.
"""

import threading
from unittest.mock import MagicMock, patch

import requests

from data_provider.kr_institutional_fetcher import (
    KrInstitutionalFetcher,
    _clamp_days,
    _date_from_daum,
    _date_from_dot_yy,
    _date_from_yyyymmdd,
    _to_int,
)

_GET = "data_provider.kr_institutional_fetcher.requests.get"


class TestPureHelpers:
    def test_to_int_parses_signed_comma_strings(self):
        # 네이버 integration 값 형식: 부호 접두 + 콤마
        assert _to_int("+625,985") == 625985
        assert _to_int("-2,851,466") == -2851466
        assert _to_int("11,314") == 11314
        assert _to_int("0") == 0
        # 다음 JSON은 네이티브 숫자
        assert _to_int(635576) == 635576
        assert _to_int(-6170033) == -6170033
        assert _to_int(285000.0) == 285000

    def test_to_int_rejects_blanks_and_garbage(self):
        assert _to_int(None) is None
        assert _to_int("") is None
        assert _to_int("-") is None
        assert _to_int("--") is None
        assert _to_int("—") is None
        assert _to_int("46.58%") is None
        assert _to_int("날짜") is None

    def test_clamp_days(self):
        assert _clamp_days(5) == 5
        assert _clamp_days(1) == 1
        assert _clamp_days(0) == 1
        assert _clamp_days(-3) == 1
        assert _clamp_days(999) == 30
        assert _clamp_days("7") == 7
        assert _clamp_days("abc") == 5
        assert _clamp_days(None) == 5

    def test_date_normalizers(self):
        assert _date_from_yyyymmdd("20260710") == "2026-07-10"
        assert _date_from_yyyymmdd("2026-07-10") is None
        assert _date_from_yyyymmdd("") is None
        assert _date_from_yyyymmdd(None) is None

        assert _date_from_daum("2026-07-10 00:00:00") == "2026-07-10"
        assert _date_from_daum("2026-07-10") == "2026-07-10"
        assert _date_from_daum("20260710") is None
        assert _date_from_daum(None) is None

        assert _date_from_dot_yy("26.07.10") == "2026-07-10"
        assert _date_from_dot_yy("날짜") is None
        assert _date_from_dot_yy("") is None
        assert _date_from_dot_yy(None) is None


# ---------------------------------------------------------------------------
# 실캡처 픽스처 (2026-07-10, 삼성전자 005930)
# ---------------------------------------------------------------------------

# GET https://m.stock.naver.com/api/stock/005930/integration 의 dealTrendInfos.
# 첫 행만 실제 응답의 부가 키를 보존했다 — 파서는 4개 핵심 키만 읽는다.
NAVER_INTEGRATION_FIXTURE = {
    "itemCode": "005930",
    "stockName": "삼성전자",
    "dealTrendInfos": [
        {
            "itemCode": "005930",
            "bizdate": "20260710",
            "closePrice": "285,000",
            "compareToPreviousClosePrice": "7,000",
            "compareToPreviousPrice": {"code": "2", "text": "상승", "name": "RISING"},
            "accumulatedTradingVolume": "19,919,725",
            "foreignerPureBuyQuant": "+625,985",
            "foreignerHoldRatio": "46.58%",
            "organPureBuyQuant": "+2,313,745",
            "individualPureBuyQuant": "-2,851,466",
        },
        {
            "bizdate": "20260709",
            "foreignerPureBuyQuant": "+845,552",
            "organPureBuyQuant": "+1,107,761",
            "individualPureBuyQuant": "-1,739,937",
        },
        {
            "bizdate": "20260708",
            "foreignerPureBuyQuant": "-3,015,093",
            "organPureBuyQuant": "+971,031",
            "individualPureBuyQuant": "+2,031,705",
        },
        {
            "bizdate": "20260707",
            "foreignerPureBuyQuant": "-6,145,090",
            "organPureBuyQuant": "-1,852,807",
            "individualPureBuyQuant": "+7,870,568",
        },
        {
            "bizdate": "20260706",
            "foreignerPureBuyQuant": "-2,018,562",
            "organPureBuyQuant": "+14,823",
            "individualPureBuyQuant": "+1,917,050",
        },
    ],
}

# 5일 누적 (위 픽스처 기준): 외국인 -9,707,208 / 기관 +2,554,553
NAVER_FOREIGN_5D = -9707208
NAVER_INSTITUTION_5D = 2554553

# GET https://finance.daum.net/api/investor/days?symbolCode=A005930&... 응답.
# 개인 순매수 필드가 없고, 숫자는 네이티브 int다. 외국인 수치는 네이버와
# 집계 기준 차이로 소폭 다르다(예: 07-10 635,576 vs 625,985) — 실제 값이다.
DAUM_INVESTOR_FIXTURE = {
    "code": 200,
    "message": "OK",
    "data": [
        {
            "date": "2026-07-10 00:00:00",
            "foreignStraightPurchaseVolume": 635576,
            "institutionStraightPurchaseVolume": 2313745,
            "tradePrice": 285000.0,
            "change": "RISE",
            "accTradeVolume": 19919725,
        },
        {
            "date": "2026-07-09 00:00:00",
            "foreignStraightPurchaseVolume": 858869,
            "institutionStraightPurchaseVolume": 1107761,
        },
        {
            "date": "2026-07-08 00:00:00",
            "foreignStraightPurchaseVolume": -3025867,
            "institutionStraightPurchaseVolume": 971031,
        },
        {
            "date": "2026-07-07 00:00:00",
            "foreignStraightPurchaseVolume": -6170033,
            "institutionStraightPurchaseVolume": -1852807,
        },
        {
            "date": "2026-07-06 00:00:00",
            "foreignStraightPurchaseVolume": -2053941,
            "institutionStraightPurchaseVolume": 14823,
        },
    ],
}

# 5일 누적 (위 픽스처 기준): 외국인 -9,755,396 / 기관 +2,554,553
DAUM_FOREIGN_5D = -9755396
DAUM_INSTITUTION_5D = 2554553


def _resp(json_data=None, *, content=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    if json_data is not None:
        resp.json.return_value = json_data
    if content is not None:
        resp.content = content
    return resp


def _fetcher():
    return KrInstitutionalFetcher(min_request_interval=0)


class TestRowParsingAndRecord:
    def test_naver_row_three_categories(self):
        row = KrInstitutionalFetcher._parse_naver_stock_row(
            NAVER_INTEGRATION_FIXTURE["dealTrendInfos"][0]
        )
        assert row == {
            "date": "2026-07-10",
            "foreign_net": 625985,
            "institution_net": 2313745,
            "individual_net": -2851466,
        }

    def test_naver_row_missing_core_field_dropped(self):
        base = dict(NAVER_INTEGRATION_FIXTURE["dealTrendInfos"][1])
        # 필수 구성요소 결측 -> 행 폐기 (0으로 조작하지 않는다)
        broken = {k: v for k, v in base.items() if k != "foreignerPureBuyQuant"}
        assert KrInstitutionalFetcher._parse_naver_stock_row(broken) is None
        blank = dict(base, organPureBuyQuant="")
        assert KrInstitutionalFetcher._parse_naver_stock_row(blank) is None
        bad_date = dict(base, bizdate="어제")
        assert KrInstitutionalFetcher._parse_naver_stock_row(bad_date) is None
        assert KrInstitutionalFetcher._parse_naver_stock_row("not-a-dict") is None

    def test_naver_row_keeps_genuine_zero_and_null_individual(self):
        row = KrInstitutionalFetcher._parse_naver_stock_row(
            {"bizdate": "20260710", "foreignerPureBuyQuant": "0", "organPureBuyQuant": "-5"}
        )
        assert row["foreign_net"] == 0
        assert row["institution_net"] == -5
        assert row["individual_net"] is None  # 개인만 nullable

    def test_daum_row_has_no_individual(self):
        row = KrInstitutionalFetcher._parse_daum_row(DAUM_INVESTOR_FIXTURE["data"][0])
        assert row == {
            "date": "2026-07-10",
            "foreign_net": 635576,
            "institution_net": 2313745,
            "individual_net": None,
        }

    def test_daum_row_missing_core_field_dropped(self):
        base = dict(DAUM_INVESTOR_FIXTURE["data"][1])
        broken = {k: v for k, v in base.items() if k != "institutionStraightPurchaseVolume"}
        assert KrInstitutionalFetcher._parse_daum_row(broken) is None

    def test_market_of_and_base_code(self):
        assert KrInstitutionalFetcher._market_of("005930.KS") == "kospi"
        assert KrInstitutionalFetcher._market_of("068270.KQ") == "kosdaq"
        assert KrInstitutionalFetcher._market_of("005930.ks") == "kospi"
        assert KrInstitutionalFetcher._market_of("AAPL") is None
        assert KrInstitutionalFetcher._market_of("600519") is None
        assert KrInstitutionalFetcher._market_of("0700.HK") is None
        assert KrInstitutionalFetcher._market_of("7203.T") is None
        assert KrInstitutionalFetcher._market_of("") is None
        assert KrInstitutionalFetcher._market_of(None) is None
        assert KrInstitutionalFetcher._base_code("005930.KS") == "005930"

    def test_build_flows_stock_record_shape(self):
        day_rows = [
            KrInstitutionalFetcher._parse_naver_stock_row(r)
            for r in NAVER_INTEGRATION_FIXTURE["dealTrendInfos"]
        ]
        record = KrInstitutionalFetcher._build_flows(
            "kospi", day_rows, "NAVER", unit="shares", code="005930"
        )
        assert record["code"] == "005930"
        assert record["market"] == "kospi"
        assert record["unit"] == "shares"
        assert record["source"] == "NAVER"
        assert len(record["days"]) == 5
        assert record["summary"] == {
            "foreign_net_5d": NAVER_FOREIGN_5D,
            "institution_net_5d": NAVER_INSTITUTION_5D,
        }

    def test_build_flows_summary_window_is_available_rows(self):
        day_rows = [
            KrInstitutionalFetcher._parse_naver_stock_row(r)
            for r in NAVER_INTEGRATION_FIXTURE["dealTrendInfos"][:2]
        ]
        record = KrInstitutionalFetcher._build_flows(
            "kospi", day_rows, "NAVER", unit="shares", code="005930"
        )
        # summary는 전달된 행(최대 5개) 누적 — 결측일을 0으로 채우지 않는다
        assert record["summary"]["foreign_net_5d"] == 625985 + 845552
        assert record["summary"]["institution_net_5d"] == 2313745 + 1107761

    def test_build_flows_market_record_has_no_code(self):
        day_rows = [
            {"date": "2026-07-10", "foreign_net": -322800000000,
             "institution_net": 1131400000000, "individual_net": -780500000000},
        ]
        record = KrInstitutionalFetcher._build_flows("kospi", day_rows, "NAVER", unit="KRW")
        assert "code" not in record
        assert record["unit"] == "KRW"
        assert record["summary"]["foreign_net_5d"] == -322800000000


class TestGetInvestorFlowsNaver:
    def test_full_record_from_naver_fixture(self):
        f = _fetcher()
        with patch(_GET, return_value=_resp(NAVER_INTEGRATION_FIXTURE)):
            record = f.get_investor_flows("005930.KS")
        assert record is not None
        assert record["code"] == "005930"
        assert record["market"] == "kospi"
        assert record["unit"] == "shares"
        assert record["source"] == "NAVER"
        assert [r["date"] for r in record["days"]] == [
            "2026-07-10", "2026-07-09", "2026-07-08", "2026-07-07", "2026-07-06",
        ]
        assert record["days"][0] == {
            "date": "2026-07-10",
            "foreign_net": 625985,
            "institution_net": 2313745,
            "individual_net": -2851466,
        }
        assert record["summary"] == {
            "foreign_net_5d": NAVER_FOREIGN_5D,
            "institution_net_5d": NAVER_INSTITUTION_5D,
        }

    def test_days_slice_and_summary_window(self):
        f = _fetcher()
        with patch(_GET, return_value=_resp(NAVER_INTEGRATION_FIXTURE)):
            record = f.get_investor_flows("005930.KS", days=2)
        assert len(record["days"]) == 2
        assert record["summary"]["foreign_net_5d"] == 625985 + 845552
        assert record["summary"]["institution_net_5d"] == 2313745 + 1107761

    def test_non_kr_codes_fail_open_without_fetch(self):
        f = _fetcher()
        with patch(_GET) as mock_get:
            for code in ("AAPL", "600519", "0700.HK", "7203.T", "", None):
                assert f.get_investor_flows(code) is None
        mock_get.assert_not_called()

    def test_kosdaq_market_label(self):
        f = _fetcher()
        with patch(_GET, return_value=_resp(NAVER_INTEGRATION_FIXTURE)):
            record = f.get_investor_flows("068270.KQ")
        assert record["market"] == "kosdaq"

    def test_all_sources_transport_error_fails_open(self):
        f = _fetcher()
        with patch(_GET, side_effect=requests.exceptions.ConnectionError("down")):
            assert f.get_investor_flows("005930.KS") is None

    def test_same_stock_cached_single_fetch(self):
        f = _fetcher()
        with patch(_GET, return_value=_resp(NAVER_INTEGRATION_FIXTURE)) as mock_get:
            first = f.get_investor_flows("005930.KS")
            second = f.get_investor_flows("005930.KS", days=3)
            assert mock_get.call_count == 1  # days가 달라도 캐시된 행을 슬라이스
        assert first["days"][0] == second["days"][0]
        assert len(second["days"]) == 3

    def test_empty_payload_not_cached(self):
        f = _fetcher()
        with patch(_GET, return_value=_resp({"dealTrendInfos": []})) as mock_get:
            assert f.get_investor_flows("005930.KS") is None
            first_count = mock_get.call_count
            assert f.get_investor_flows("005930.KS") is None
            # 빈 결과는 캐시하지 않는다 — 다음 호출에서 재시도
            assert mock_get.call_count > first_count

    def test_renamed_core_key_fails_open(self):
        drifted = {
            "dealTrendInfos": [
                {"bizdate": "20260710", "frgnPureBuyQuant": "+625,985",
                 "organPureBuyQuant": "+2,313,745"},
            ]
        }
        f = _fetcher()
        with patch(_GET, return_value=_resp(drifted)):
            assert f.get_investor_flows("005930.KS") is None

    def test_concurrent_same_stock_single_fetch(self):
        f = _fetcher()
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait()
            results.append(f.get_investor_flows("005930.KS"))

        with patch(_GET, return_value=_resp(NAVER_INTEGRATION_FIXTURE)) as mock_get:
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert mock_get.call_count == 1  # in-flight 락으로 코얼레싱
        assert len(results) == 8
        assert all(r is not None for r in results)

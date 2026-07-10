# -*- coding: utf-8 -*-
"""KR 수급 소스 드리프트 스모크 — 실제 네트워크 필요 (`pytest -m network`).

규약: 전송 실패/비200 -> skip(일시 장애), 200 + 형식 상이 -> FAIL(드리프트).
"""

import pytest
import requests

from data_provider.kr_institutional_fetcher import (
    _MARKET_HEAD,
    _UA,
    KrInstitutionalFetcher,
)

pytestmark = pytest.mark.network

LIQUID_STOCK = "005930"  # 삼성전자 — 유동성 최상위, 수급 행 상시 존재


def _get_or_skip(url, **kwargs):
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=20, **kwargs)
    except requests.RequestException as exc:
        pytest.skip(f"전송 불가(일시 장애/차단 추정): {exc}")
    if resp.status_code != 200:
        pytest.skip(f"HTTP {resp.status_code} — 서버측 상태로 간주, 드리프트 판정 보류")
    return resp


def _fetcher():
    return KrInstitutionalFetcher(min_request_interval=0)


class TestNaverStockDrift:
    def test_integration_feed_shape_and_fetcher_crosscheck(self):
        resp = _get_or_skip(
            f"https://m.stock.naver.com/api/stock/{LIQUID_STOCK}/integration"
        )
        payload = resp.json()  # 200인데 JSON 아님 -> 여기서 FAIL (드리프트)
        infos = payload.get("dealTrendInfos")
        assert isinstance(infos, list) and infos, "dealTrendInfos 드리프트"
        first = infos[0]
        for key in (
            "bizdate",
            "foreignerPureBuyQuant",
            "organPureBuyQuant",
            "individualPureBuyQuant",
        ):
            assert key in first, f"네이버 핵심 키 드리프트: {key}"
        record = _fetcher().get_investor_flows(f"{LIQUID_STOCK}.KS")
        assert record is not None, "피드에 행이 있는데 fetcher가 None — 파싱 드리프트"
        assert record["unit"] == "shares"
        assert record["days"], "정규화 결과 행 없음"
        assert record["days"][0]["foreign_net"] is not None


class TestDaumStockDrift:
    def test_investor_days_shape_via_internal_fetch(self):
        resp = _get_or_skip(
            "https://finance.daum.net/api/investor/days",
            params={"symbolCode": f"A{LIQUID_STOCK}", "page": 1, "perPage": 5,
                    "pagination": "true"},
            headers={"Referer": f"https://finance.daum.net/quotes/A{LIQUID_STOCK}"},
        )
        payload = resp.json()
        data = payload.get("data")
        assert isinstance(data, list) and data, "다음 data 래퍼 드리프트"
        first = data[0]
        for key in ("date", "foreignStraightPurchaseVolume",
                    "institutionStraightPurchaseVolume"):
            assert key in first, f"다음 핵심 키 드리프트: {key}"
        # fallback 경로는 네이버 성공 시 공개 API로 도달 불가 — 내부 fetch로 감시
        rows = _fetcher()._fetch_daum_stock(LIQUID_STOCK)
        assert rows, "다음 피드에 행이 있는데 파싱 결과 없음 — 파싱 드리프트"
        assert rows[0]["individual_net"] is None


class TestNaverMarketDrift:
    def test_market_table_shape_and_fetcher_crosscheck(self):
        resp = _get_or_skip(
            "https://finance.naver.com/sise/investorDealTrendDay.naver",
            params={"sosok": "01", "page": 1},
        )
        html_text = resp.content.decode("euc-kr", errors="replace")
        for head in _MARKET_HEAD:
            assert head in html_text, f"시장 표 헤더 드리프트: {head}"
        record = _fetcher().get_market_investor_flows("kospi")
        assert record is not None, "시장 페이지 도달 가능한데 fetcher가 None — 파싱 드리프트"
        assert record["unit"] == "KRW"
        assert record["days"], "정규화 결과 행 없음"

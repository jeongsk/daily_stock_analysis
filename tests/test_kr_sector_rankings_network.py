# -*- coding: utf-8 -*-
"""KR 업종 순위 소스 드리프트 스모크 — 실제 네트워크 필요 (`pytest -m network`).

규약: 전송 실패/비200 -> skip(일시 장애), 200 + 형식 상이 -> FAIL(드리프트).
test_kr_market_breadth_network.py와 동일 규약.
"""

import pytest
import requests

from data_provider.kr_market_context_fetcher import (
    _DAUM_REFERER,
    _DAUM_SECTORS_URL,
    _UA,
    KrMarketContextFetcher,
)

pytestmark = pytest.mark.network


def _get_or_skip(url, *, params=None, headers=None):
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
    except requests.RequestException as exc:
        pytest.skip(f"전송 불가(일시 장애/차단 추정): {exc}")
    if resp.status_code != 200:
        pytest.skip(f"HTTP {resp.status_code} — 서버측 상태로 간주, 드리프트 판정 보류")
    return resp


class TestDaumSectorDrift:
    @pytest.mark.parametrize("code,market", [("KOSPI", "kospi"), ("KOSDAQ", "kosdaq")])
    def test_sectors_shape_and_fetcher_crosscheck(self, code, market):
        headers = {"User-Agent": _UA, "Referer": _DAUM_REFERER}
        params = {
            "market": code,
            "change": "RISE",
            "page": 1,
            "perPage": 10,
            "fieldName": "changeRate",
            "order": "desc",
            "pagination": "true",
        }
        resp = _get_or_skip(_DAUM_SECTORS_URL, params=params, headers=headers)
        data = resp.json()
        rows = data.get("data") if isinstance(data, dict) else None
        assert isinstance(rows, list) and rows, "data[] 비어있음 — 응답 형식 드리프트 의심"
        first = rows[0]
        assert isinstance(first, dict)
        for key in ("sectorName", "changeRate", "date"):
            assert key in first, f"sectors 키 드리프트: {key}"

        fetcher = KrMarketContextFetcher(min_request_interval=0)
        record = fetcher.get_sector_rankings(market)
        assert record is not None, "fetcher가 None — 두 방향(RISE/FALL) 합성 실패 또는 파싱 드리프트"
        assert record["market"] == market
        assert record["top"] and record["bottom"]
        assert record["as_of"]
        assert record["session"] in ("intraday", "close")
        assert record["source"] == "DAUM"
        for entry in record["top"] + record["bottom"]:
            assert "name" in entry and "change_pct" in entry

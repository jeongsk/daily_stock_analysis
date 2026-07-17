# -*- coding: utf-8 -*-
"""KR 시장 폭 소스 드리프트 스모크 — 실제 네트워크 필요 (`pytest -m network`).

규약: 전송 실패/비200 -> skip(일시 장애), 200 + 형식 상이 -> FAIL(드리프트).
test_kr_institutional_network.py와 동일 규약.
"""

import pytest
import requests

from data_provider.kr_market_context_fetcher import (
    _COUNT_RE,
    _TIME_RE,
    _UA,
    KrMarketContextFetcher,
)

pytestmark = pytest.mark.network


def _get_or_skip(url, *, params=None):
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=20)
    except requests.RequestException as exc:
        pytest.skip(f"전송 불가(일시 장애/차단 추정): {exc}")
    if resp.status_code != 200:
        pytest.skip(f"HTTP {resp.status_code} — 서버측 상태로 간주, 드리프트 판정 보류")
    return resp


class TestNaverBreadthDrift:
    @pytest.mark.parametrize("code,market", [("KOSPI", "kospi"), ("KOSDAQ", "kosdaq")])
    def test_index_page_shape_and_fetcher_crosscheck(self, code, market):
        resp = _get_or_skip(
            "https://finance.naver.com/sise/sise_index.naver", params={"code": code}
        )
        html_text = resp.content.decode("euc-kr", errors="replace")
        for field, pattern in _COUNT_RE.items():
            assert pattern.search(html_text), f"폭 blind 라벨 드리프트: {field}"
        assert _TIME_RE.search(html_text), "id=time 세션 요소 드리프트"

        fetcher = KrMarketContextFetcher(min_request_interval=0)
        record = fetcher.get_market_breadth(market)
        if record is None:
            # 페이지 도달 + 앵커 존재인데 None이면 개장전(PREOPEN)만 정상 사유다
            time_text = _TIME_RE.search(html_text).group(1)
            assert "개장전" in time_text, "페이지 형식 정상인데 fetcher가 None — 파싱 드리프트"
            return
        assert record["market"] == market
        assert record["session"] in ("intraday", "close")
        assert record["as_of"]
        for key in ("up_count", "down_count", "flat_count"):
            assert isinstance(record[key], int) and record[key] >= 0

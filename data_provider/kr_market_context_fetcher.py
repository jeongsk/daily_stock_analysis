# -*- coding: utf-8 -*-
"""KR 시장 컨텍스트 fetcher — KOSPI/KOSDAQ 시장 폭(상승·하락·보합 종목 수)·업종 순위.

데이터 계층 전용(로드맵 B Phase 1·2). 마켓 리뷰 연결은 상위 계층이 담당한다.
설계 스펙: docs/superpowers/specs/2026-07-16-kr-market-breadth-sector-design.md
소스 실측: docs/superpowers/research/2026-07-16-kr-breadth-sector-probe.md

소스 (무인증 공개 엔드포인트만 — ADR 0001 선례, 2026-07-16 실측):
  - 시장 폭: 네이버 PC 지수 페이지 sise_index.naver?code={KOSPI|KOSDAQ}
    (EUC-KR HTML). blind 라벨 `상승종목수/보합종목수/하락종목수` 앵커와
    `id="time"`의 `YYYY.MM.DD 장중|장마감|개장전`으로 as_of/session 판별.
  - 업종 순위: 다음 finance.daum.net/api/sectors?market={KOSPI|KOSDAQ}
    &change={RISE|FALL}&... (JSON, **Referer 필수** — 누락 시 403).
    `sectorName`/`changeRate`(비율→%)/`date` 앵커. session은 API에 없어
    저장소 KR 거래 캘린더(market_phase, XKRX)로 파생한다.

계약:
  - 폭 = up/down/flat + as_of + session만. 상·하한가 수·거래대금은 수집하지
    않는다(스펙 D4). 시장 폭과 투자자 수급은 별개 신호다(CONTEXT.md).
  - 업종 = top/bottom(각 최대 n, 이름 중복 제거) + as_of + session.
    A주 테마/개념(概念板块)과는 별개 — KR은 테마 순위를 제공하지 않는다(D5).
  - `개장전`(PREOPEN)은 예상지수 구간이므로 레코드를 생성하지 않는다.
  - 최신 호출 실패 시 **동일 KR 거래일** 캐시만 stale=True로 제공한다(D13).
    거래일 판정이 불가능하면 stale 제공을 보수적으로 생략한다.

Fail-open 계약: 공개 메서드는 어떤 실패에서도 예외를 던지지 않고 None을
반환한다. 필수값(폭 3카운트 / 업종 top+bottom+as_of)이 결측인 응답은
레코드를 폐기하며 0으로 조작하거나 빈 목록을 채우지 않는다.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from data_provider.realtime_types import CircuitBreaker

logger = logging.getLogger(__name__)

_NAVER_INDEX_URL = "https://finance.naver.com/sise/sise_index.naver"

# 다음 업종 API — Referer 필수(누락 시 403 Forbidden, 실측 §2.2)
_DAUM_SECTORS_URL = "https://finance.daum.net/api/sectors"
_DAUM_REFERER = "https://finance.daum.net/domestic/sectors"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_KST = timezone(timedelta(hours=9))

# 네이버 지수 페이지 code 파라미터 — 시장별 완전 분리(실측 §1.1)
_MARKET_CODE = {"kospi": "KOSPI", "kosdaq": "KOSDAQ"}

# blind 라벨 앵커 — rename/구조 변경 시 매칭 실패로 레코드 폐기(드리프트 방어)
_COUNT_RE = {
    "up_count": re.compile(
        r'<span class="blind">상승종목수</span><a[^>]*><span>(\d+)</span>'
    ),
    "flat_count": re.compile(
        r'<span class="blind">보합종목수</span><a[^>]*><span>(\d+)</span>'
    ),
    "down_count": re.compile(
        r'<span class="blind">하락종목수</span><a[^>]*><span>(\d+)</span>'
    ),
}

# id="time" 요소: "2026.07.16 장마감" | "2026.07.16 15:12 장중" | "2026.07.16 개장전"
_TIME_RE = re.compile(r'id="time"[^>]*>\s*([^<]+?)\s*<')
_TIME_DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")


def _parse_session(time_text: str) -> Optional[str]:
    """세션 라벨 -> 'intraday'|'close'|None(개장전/미상 — 레코드 미생성)."""
    if "장마감" in time_text:
        return "close"
    if "장중" in time_text:
        return "intraday"
    # "개장전"(예상지수 구간) 및 알 수 없는 라벨은 레코드를 만들지 않는다
    return None


def _parse_as_of(time_text: str) -> Optional[str]:
    match = _TIME_DATE_RE.search(time_text)
    if not match:
        return None
    yyyy, mm, dd = match.groups()
    return f"{yyyy}-{mm}-{dd}"


def _current_kr_trading_date() -> Optional[str]:
    """현재 KR 유효 거래일(ISO). 판정 불가 시 None — stale 제공을 생략한다."""
    try:
        from src.core.trading_calendar import get_effective_trading_date

        return get_effective_trading_date("kr").isoformat()
    except Exception as exc:
        logger.info("[kr-breadth] KR 거래일 판정 불가 — stale 생략: %s", exc)
        return None


def _to_change_pct(value: Any) -> Optional[float]:
    """다음 changeRate(소수 비율) -> change_pct(%). 변환 불가/NaN 시 None."""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate != rate:  # NaN 가드
        return None
    return round(rate * 100, 2)


def _infer_kr_session() -> str:
    """KR 장 상태 -> 'intraday'|'close'. 판정 불가 시 'close' 보수적(스펙 §6).

    다음 sectors API는 session을 회신하지 않으므로 저장소 KR 거래 캘린더
    (market_phase, XKRX)로 파생한다. 장중(INTRADAY/점심/종근 경매)='intraday',
    그 외(마감후/개장전/비거래/판정불가)='close'.
    """
    try:
        from src.core.trading_calendar import MarketPhase, infer_market_phase

        phase = infer_market_phase("kr")
        if phase in (
            MarketPhase.INTRADAY,
            MarketPhase.LUNCH_BREAK,
            MarketPhase.CLOSING_AUCTION,
        ):
            return "intraday"
        return "close"
    except Exception as exc:
        logger.info("[kr-sector] market_phase 판정 불가 — close 보수적: %s", exc)
        return "close"


class KrMarketContextFetcher:
    """KR 시장 폭 데이터 계층 — 무설정·fail-open (모듈 docstring 참조)."""

    name = "KrMarketContextFetcher"

    def __init__(
        self,
        *,
        cache_ttl_seconds: int = 300,
        min_request_interval: float = 1.0,
        timeout: int = 12,
    ):
        # 장중 폭은 수급(일별 확정치, TTL 900s)보다 빨리 변하므로 TTL을 짧게 둔다
        self._cache_ttl = max(0, int(cache_ttl_seconds))
        self._min_interval = max(0.0, float(min_request_interval))
        self._timeout = timeout
        self._cache: Dict[Any, Any] = {}
        self._cache_at: Dict[Any, float] = {}
        # stale fallback 전용 — TTL과 무관하게 마지막 유효 레코드를 보존한다
        self._last_good: Dict[Any, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._inflight: Dict[Any, threading.Lock] = {}
        self._throttle_lock = threading.Lock()
        self._last_request_at = 0.0
        # 기능·시장별 브레이커 키: naver_breadth_kospi / naver_breadth_kosdaq
        self._breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=300.0)

    # ------------------------------------------------------------------
    # 캐시 / 스로틀 / HTTP (KrInstitutionalFetcher 패턴 미러)
    # ------------------------------------------------------------------

    def _read_cache(self, key: Any) -> Any:
        with self._lock:
            at = self._cache_at.get(key)
            if at is None or (time.time() - at) > self._cache_ttl:
                return None
            return self._cache.get(key)

    def _store_cache(self, key: Any, value: Any) -> None:
        with self._lock:
            self._cache[key] = value
            self._cache_at[key] = time.time()

    def _key_lock(self, key: Any) -> threading.Lock:
        with self._lock:
            lock = self._inflight.get(key)
            if lock is None:
                lock = threading.Lock()
                self._inflight[key] = lock
            return lock

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        with self._throttle_lock:
            wait = self._min_interval - (time.time() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.time()

    def _get_html(self, url: str, *, params: Optional[Dict[str, Any]] = None) -> str:
        self._throttle()
        resp = requests.get(
            url, params=params, headers={"User-Agent": _UA}, timeout=self._timeout
        )
        resp.raise_for_status()
        # 네이버 PC 페이지는 EUC-KR — requests의 인코딩 추정에 의존하지 않는다
        return resp.content.decode("euc-kr", errors="replace")

    def _get_json(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        referer: Optional[str] = None,
    ) -> Any:
        """JSON GET — 다음 sectors API용(Referer 필수)."""
        self._throttle()
        headers = {"User-Agent": _UA}
        if referer:
            headers["Referer"] = referer
        resp = requests.get(url, params=params, headers=headers, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # 시장 폭 (네이버 지수 페이지)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_breadth_page(html_text: str, market: str) -> Optional[Dict[str, Any]]:
        """지수 페이지 HTML -> 폭 레코드. 필수값 결측/개장전이면 None."""
        counts: Dict[str, int] = {}
        for field, pattern in _COUNT_RE.items():
            match = pattern.search(html_text)
            if match is None:
                return None  # blind 라벨 드리프트 — 레코드 폐기, 0 조작 금지
            counts[field] = int(match.group(1))

        time_match = _TIME_RE.search(html_text)
        if time_match is None:
            return None
        time_text = time_match.group(1)
        as_of = _parse_as_of(time_text)
        session = _parse_session(time_text)
        if as_of is None or session is None:
            return None  # 날짜 결측 또는 개장전(예상지수) — 레코드 미생성

        return {
            "market": market,
            "up_count": counts["up_count"],
            "down_count": counts["down_count"],
            "flat_count": counts["flat_count"],
            "as_of": as_of,
            "session": session,
            "source": "NAVER",
            "stale": False,
        }

    def _fetch_naver_breadth(self, market: str) -> Optional[Dict[str, Any]]:
        html_text = self._get_html(
            _NAVER_INDEX_URL, params={"code": _MARKET_CODE[market]}
        )
        return self._parse_breadth_page(html_text, market)

    def _stale_fallback(self, key: Any) -> Optional[Dict[str, Any]]:
        """최신 호출 실패 시 동일 KR 거래일 레코드만 stale=True로 제공(D13)."""
        with self._lock:
            last_good = self._last_good.get(key)
        if last_good is None:
            return None
        trading_date = _current_kr_trading_date()
        if trading_date is None or last_good.get("as_of") != trading_date:
            return None
        return {**last_good, "stale": True}

    def get_market_breadth(self, market: str) -> Optional[Dict[str, Any]]:
        """KOSPI/KOSDAQ 시장 폭 — up/down/flat 종목 수 + as_of/session.

        market: "kospi" | "kosdaq" (대소문자/공백 허용). 실패 시 None (fail-open).
        """
        try:
            norm = str(market or "").strip().lower()
            if norm not in _MARKET_CODE:
                return None
            key = ("breadth", norm)
            cached = self._read_cache(key)
            if cached is not None:
                return cached
            with self._key_lock(key):
                cached = self._read_cache(key)
                if cached is not None:
                    return cached
                breaker_key = f"naver_breadth_{norm}"
                if not self._breaker.is_available(breaker_key):
                    logger.info("[kr-breadth] %s 서킷 오픈 — stale 확인", norm)
                    return self._stale_fallback(key)
                try:
                    record = self._fetch_naver_breadth(norm)
                except Exception as exc:
                    self._breaker.record_failure(breaker_key, str(exc))
                    logger.info("[kr-breadth] %s 수집 실패: %s", norm, exc)
                    return self._stale_fallback(key)
                # 도달성 추적: 파싱 실패(None)도 서버 도달은 성공으로 기록
                self._breaker.record_success(breaker_key)
                if record is None:
                    return self._stale_fallback(key)  # 빈/무효 결과는 캐시하지 않는다
                self._store_cache(key, record)
                with self._lock:
                    self._last_good[key] = record
                return record
        except Exception as exc:
            logger.warning("[kr-breadth] get_market_breadth(%s) fail-open: %s", market, exc)
            return None

    # ------------------------------------------------------------------
    # 업종 순위 (다음 sectors API)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sector_rows(
        rows: Any, n: int
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """다음 sectors `data` 리스트 -> (순위 리스트, as_of). 결측/빈 허용.

        각 항목: {"name": sectorName, "change_pct": changeRate*100}. 이름 중복
        제거하며 최대 n개. as_of는 첫 유효 row의 `date`에서 취한다.
        """
        if not isinstance(rows, list):
            return [], None
        result: List[Dict[str, Any]] = []
        seen = set()
        as_of: Optional[str] = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("sectorName") or "").strip()
            if not name or name in seen:
                continue
            change_pct = _to_change_pct(row.get("changeRate"))
            if change_pct is None:
                continue
            seen.add(name)
            result.append({"name": name, "change_pct": change_pct})
            if as_of is None:
                as_of = str(row.get("date") or "").strip() or None
            if len(result) >= n:
                break
        return result, as_of

    def _fetch_daum_direction(
        self, market: str, change: str, n: int
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """다음 sectors 단일 방향(RISE|FALL) 수집 -> (순위 리스트, as_of).

        방향별 breaker 키 분리(daum_sector_{market}_{rise|fall}). 서킷 오픈/실패
        시 ([], None) — caller가 stale fallback을 결정한다.
        """
        breaker_key = f"daum_sector_{market}_{change.lower()}"
        if not self._breaker.is_available(breaker_key):
            logger.info("[kr-sector] %s %s 서킷 오픈 — 생략", market, change)
            return [], None
        try:
            params = {
                "market": _MARKET_CODE[market],
                "change": change,
                "page": 1,
                "perPage": 10,
                "fieldName": "changeRate",
                "order": "desc",
                "pagination": "true",
            }
            data = self._get_json(_DAUM_SECTORS_URL, params=params, referer=_DAUM_REFERER)
        except Exception as exc:
            self._breaker.record_failure(breaker_key, str(exc))
            logger.info("[kr-sector] %s %s 수집 실패: %s", market, change, exc)
            return [], None
        # 도달성 추적: 파싱 실패도 서버 도달은 성공으로 기록
        self._breaker.record_success(breaker_key)
        rows = data.get("data") if isinstance(data, dict) else None
        return self._parse_sector_rows(rows, n)

    def get_sector_rankings(
        self, market: str, n: int = 5
    ) -> Optional[Dict[str, Any]]:
        """KOSPI/KOSDAQ 업종 순위 — top/bottom(등락률 상·하위) + as_of/session.

        market: "kospi" | "kosdaq" (대소문자/공백 허용). RISE(top)/FALL(bottom)을
        각각 수집해 한 레코드로 합친다. top·bottom·as_of 모두 있어야 유효(D9) —
        아니면 None(또는 동일 거래일 stale). 실패 시 None (fail-open).
        """
        try:
            norm = str(market or "").strip().lower()
            if norm not in _MARKET_CODE or n <= 0:
                return None
            key = ("sector", norm)
            cached = self._read_cache(key)
            if cached is not None:
                return cached
            with self._key_lock(key):
                cached = self._read_cache(key)
                if cached is not None:
                    return cached
                top, top_as_of = self._fetch_daum_direction(norm, "RISE", n)
                bottom, bottom_as_of = self._fetch_daum_direction(norm, "FALL", n)
                as_of = top_as_of or bottom_as_of
                if not top or not bottom or not as_of:
                    return self._stale_fallback(key)  # 불완전 레코드는 캐시하지 않는다
                record = {
                    "market": norm,
                    "top": top,
                    "bottom": bottom,
                    "as_of": as_of,
                    "session": _infer_kr_session(),
                    "source": "DAUM",
                    "stale": False,
                }
                self._store_cache(key, record)
                with self._lock:
                    self._last_good[key] = record
                return record
        except Exception as exc:
            logger.warning("[kr-sector] get_sector_rankings(%s) fail-open: %s", market, exc)
            return None

# -*- coding: utf-8 -*-
"""KR 투자자별 매매동향(수급) fetcher — 종목·시장 일별 순매수.

데이터 계층 전용(Phase 1). 이 모듈을 소비하는 리포트/마켓 리뷰 연결은
Phase 2/3에서 별도 PR로 추가된다. 설계 스펙:
docs/superpowers/specs/2026-07-10-kr-investor-flows-design.md

소스 체인 (무인증 공개 엔드포인트만 — docs/adr/0001, 2026-07-10 라이브 실측):
  - 종목(기본):  네이버 모바일 integration JSON — 3주체(외국인/기관/개인),
                 최근 5거래일, 주수, "+625,985" 형태 부호·콤마 문자열
  - 종목(대체):  다음 금융 investor/days JSON — 외국인/기관만(개인 없음),
                 주수 정수, Referer 헤더 필수
  - 시장(단일):  네이버 PC investorDealTrendDay — EUC-KR HTML 표,
                 억원 단위를 KRW 원으로 정규화(×1e8)
  - KRX 정보데이터시스템은 로그인 게이트 전환으로 사용 불가(ADR 0001)

단위 계약: 종목 = 주수(unit="shares"), 시장 = KRW 원(unit="KRW").
음수 = 순매도. 주수×종가 금액 추정 환산은 금지(ADR 0001).

Fail-open 계약: 공개 메서드는 어떤 실패에서도 예외를 던지지 않고 None을
반환한다. 필수 구성요소(foreign_net/institution_net)가 결측인 날짜 행은
폐기하며 0으로 조작하지 않는다. individual_net만 None 허용.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from data_provider.realtime_types import CircuitBreaker
from src.services.market_symbol_utils import is_kr_suffix_symbol

logger = logging.getLogger(__name__)

_NAVER_STOCK_URL = "https://m.stock.naver.com/api/stock/{code}/integration"
_DAUM_STOCK_URL = "https://finance.daum.net/api/investor/days"
_NAVER_MARKET_URL = "https://finance.naver.com/sise/investorDealTrendDay.naver"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_KST = timezone(timedelta(hours=9))

# 네이버 integration dealTrendInfos 핵심 키 — rename 시 행이 폐기되어 fail-open
_NAVER_KEY_DATE = "bizdate"
_NAVER_KEY_FOREIGN = "foreignerPureBuyQuant"
_NAVER_KEY_INSTITUTION = "organPureBuyQuant"
_NAVER_KEY_INDIVIDUAL = "individualPureBuyQuant"

# 다음 investor/days 핵심 키
_DAUM_KEY_DATE = "date"
_DAUM_KEY_FOREIGN = "foreignStraightPurchaseVolume"
_DAUM_KEY_INSTITUTION = "institutionStraightPurchaseVolume"

# 네이버 PC 시장 페이지: sosok 01=KOSPI, 02=KOSDAQ
_MARKET_SOSOK = {"kospi": "01", "kosdaq": "02"}
# 시장 표 헤더 선두 4열 — 이름 검증에 실패한 표는 신뢰하지 않는다(드리프트 방어)
_MARKET_HEAD = ("날짜", "개인", "외국인", "기관계")
_EOK_KRW = 100_000_000  # 억원 -> 원

_DAUM_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DOT_YY_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{2})")


def _to_int(value: Any) -> Optional[int]:
    """피드 숫자 파싱 — 부호/콤마 허용, 공백·플레이스홀더·비수치는 None."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "--", "—"):
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def _clamp_days(days: Any) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return 5
    return max(1, min(value, 30))


def _date_from_yyyymmdd(value: Any) -> Optional[str]:
    """네이버 bizdate: "20260710" -> "2026-07-10"."""
    text = str(value or "").strip()
    if not (text.isdigit() and len(text) == 8):
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def _date_from_daum(value: Any) -> Optional[str]:
    """다음 date: "2026-07-10 00:00:00" -> "2026-07-10"."""
    text = str(value or "").strip()[:10]
    return text if _DAUM_DATE_RE.fullmatch(text) else None


def _date_from_dot_yy(value: Any) -> Optional[str]:
    """네이버 PC 표 날짜: "26.07.10" -> "2026-07-10".

    페이지 lookback은 최근 일자만 다루므로 세기는 20xx로 고정한다.
    """
    text = str(value or "").strip()
    match = _DOT_YY_DATE_RE.fullmatch(text)
    if not match:
        return None
    yy, mm, dd = match.groups()
    return f"20{yy}-{mm}-{dd}"


class KrInstitutionalFetcher:
    """KR 수급 데이터 계층 — 무설정·fail-open (모듈 docstring 참조)."""

    name = "KrInstitutionalFetcher"

    def __init__(
        self,
        *,
        cache_ttl_seconds: int = 900,
        min_request_interval: float = 1.0,
        timeout: int = 15,
    ):
        self._cache_ttl = max(0, int(cache_ttl_seconds))
        self._min_interval = max(0.0, float(min_request_interval))
        self._timeout = timeout
        self._cache: Dict[Any, Any] = {}
        self._cache_at: Dict[Any, float] = {}
        self._lock = threading.Lock()
        self._inflight: Dict[Any, threading.Lock] = {}
        self._throttle_lock = threading.Lock()
        self._last_request_at = 0.0
        # 소스별 브레이커: naver_stock / daum_stock / naver_market
        self._breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=300.0)

    # ------------------------------------------------------------------
    # 캐시 / 스로틀 / HTTP (TW fetcher 패턴 미러)
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

    def _get_json(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        self._throttle()
        merged = {"User-Agent": _UA, "Accept": "application/json"}
        if headers:
            merged.update(headers)
        resp = requests.get(url, params=params, headers=merged, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # 소스 시도 / 종목 수급
    # ------------------------------------------------------------------

    def _try_source(
        self, breaker_key: str, label: str, fetch: Callable[[], List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        """단일 소스 시도. 실패는 삼켜 빈 리스트를 반환한다 — TW와 달리
        재-raise하지 않는 이유는 종목 경로가 다음 소스로 체인해야 하기 때문.
        브레이커는 도달성(reachability)을 추적한다: 빈 응답도 성공으로 기록.
        """
        if not self._breaker.is_available(breaker_key):
            logger.info("[kr-flows] %s 서킷 오픈 — 건너뜀", label)
            return []
        try:
            rows = fetch()
        except Exception as exc:
            self._breaker.record_failure(breaker_key, str(exc))
            logger.info("[kr-flows] %s 수집 실패: %s", label, exc)
            return []
        self._breaker.record_success(breaker_key)
        return rows

    # ------------------------------------------------------------------
    # 시장 수급 (네이버 PC, EUC-KR HTML, 억원 -> KRW 원)
    # ------------------------------------------------------------------

    def _get_html(self, url: str, *, params: Optional[Dict[str, Any]] = None) -> str:
        self._throttle()
        resp = requests.get(
            url, params=params, headers={"User-Agent": _UA}, timeout=self._timeout
        )
        resp.raise_for_status()
        # 네이버 PC 페이지는 EUC-KR — requests의 인코딩 추정에 의존하지 않는다
        return resp.content.decode("euc-kr", errors="replace")

    @staticmethod
    def _parse_market_table(html_text: str) -> List[Dict[str, Any]]:
        try:
            # 기존 설치 의존성(lxml) — 지연 임포트로 미설치 환경은 fail-open
            import lxml.html as lxml_html
        except ImportError:
            logger.info("[kr-flows] lxml 미설치 — 시장 수급 fail-open")
            return []
        try:
            doc = lxml_html.fromstring(html_text)
        except Exception:
            return []
        for table in doc.xpath("//table"):
            header = [th.text_content().strip() for th in table.xpath(".//tr[1]/th")]
            if tuple(header[:4]) != _MARKET_HEAD:
                continue  # 헤더 이름 검증 — rename/reorder된 표는 신뢰하지 않는다
            rows: List[Dict[str, Any]] = []
            for tr in table.xpath(".//tr[td]"):
                cells = [td.text_content().strip() for td in tr.xpath("./td")]
                if len(cells) < 4:
                    continue
                date = _date_from_dot_yy(cells[0])
                if date is None:
                    continue  # 구분선/공백 행
                foreign = _to_int(cells[2])
                institution = _to_int(cells[3])
                if foreign is None or institution is None:
                    continue  # 필수 결측 -> 행 폐기, 0 조작 금지
                individual = _to_int(cells[1])
                rows.append(
                    {
                        "date": date,
                        "foreign_net": foreign * _EOK_KRW,
                        "institution_net": institution * _EOK_KRW,
                        "individual_net": (
                            individual * _EOK_KRW if individual is not None else None
                        ),
                    }
                )
            rows.sort(key=lambda r: r["date"], reverse=True)
            return rows
        return []

    def _fetch_naver_market(self, market: str) -> List[Dict[str, Any]]:
        html_text = self._get_html(
            _NAVER_MARKET_URL,
            params={
                # bizdate가 미래/휴일이어도 최신 확정일부터 내림차순으로 응답한다
                "bizdate": datetime.now(_KST).strftime("%Y%m%d"),
                "sosok": _MARKET_SOSOK[market],
                "page": 1,
            },
        )
        return self._parse_market_table(html_text)

    def _market_rows(self, market: str) -> Optional[List[Dict[str, Any]]]:
        key = ("market", market)
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        with self._key_lock(key):
            cached = self._read_cache(key)
            if cached is not None:
                return cached
            rows = self._try_source(
                "naver_market", f"naver market {market}", lambda: self._fetch_naver_market(market)
            )
            if not rows:
                return None  # 빈 결과는 캐시하지 않는다
            self._store_cache(key, rows)
            return rows

    def get_market_investor_flows(self, market: str, days: int = 5) -> Optional[Dict[str, Any]]:
        """KOSPI/KOSDAQ 시장 전체 일별 투자자 수급 — KRW 원 단위(unit="KRW").

        market: "kospi" | "kosdaq" (대소문자/공백 허용). 실패 시 None (fail-open).
        """
        try:
            norm = str(market or "").strip().lower()
            if norm not in _MARKET_SOSOK:
                return None
            rows = self._market_rows(norm)
            if rows is None:
                return None
            return self._build_flows(norm, rows[: _clamp_days(days)], "NAVER", unit="KRW")
        except Exception as exc:
            logger.warning(
                "[kr-flows] get_market_investor_flows(%s) fail-open: %s", market, exc
            )
            return None

    def _fetch_naver_stock(self, base: str) -> List[Dict[str, Any]]:
        payload = self._get_json(_NAVER_STOCK_URL.format(code=base))
        infos = payload.get("dealTrendInfos") if isinstance(payload, dict) else None
        if not isinstance(infos, list):
            return []
        rows = [
            parsed
            for parsed in (self._parse_naver_stock_row(item) for item in infos)
            if parsed is not None
        ]
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows

    def _fetch_daum_stock(self, base: str) -> List[Dict[str, Any]]:
        payload = self._get_json(
            _DAUM_STOCK_URL,
            params={"symbolCode": f"A{base}", "page": 1, "perPage": 10, "pagination": "true"},
            # Referer가 없으면 다음이 요청을 거부한다 (2026-07-10 실측)
            headers={"Referer": f"https://finance.daum.net/quotes/A{base}"},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        rows = [
            parsed
            for parsed in (self._parse_daum_row(item) for item in data)
            if parsed is not None
        ]
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows

    def _stock_rows(self, base: str) -> Optional[Tuple[List[Dict[str, Any]], str]]:
        """종목 날짜 행 조회 — (행 리스트 내림차순, 소스명) 또는 None.

        캐시 키는 종목 단위: days 변화는 호출측 슬라이스로 처리된다.
        """
        key = ("stock", base)
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        with self._key_lock(key):
            cached = self._read_cache(key)
            if cached is not None:
                return cached
            source = "NAVER"
            rows = self._try_source(
                "naver_stock", f"naver stock {base}", lambda: self._fetch_naver_stock(base)
            )
            if not rows:
                source = "DAUM"
                rows = self._try_source(
                    "daum_stock", f"daum stock {base}", lambda: self._fetch_daum_stock(base)
                )
            if not rows:
                return None  # 빈 결과는 캐시하지 않는다
            value = (rows, source)
            self._store_cache(key, value)
            return value

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def get_investor_flows(self, stock_code: str, days: int = 5) -> Optional[Dict[str, Any]]:
        """KR 종목(.KS/.KQ)의 일별 투자자 수급 — 주수 단위(unit="shares").

        비대상 종목/전 소스 실패 시 None (fail-open — 절대 raise하지 않음).
        """
        try:
            market = self._market_of(stock_code)
            if market is None:
                return None
            base = self._base_code(stock_code)
            fetched = self._stock_rows(base)
            if fetched is None:
                return None
            rows, source = fetched
            return self._build_flows(
                market, rows[: _clamp_days(days)], source, unit="shares", code=base
            )
        except Exception as exc:
            logger.warning("[kr-flows] get_investor_flows(%s) fail-open: %s", stock_code, exc)
            return None

    @staticmethod
    def _market_of(stock_code: Any) -> Optional[str]:
        """'005930.KS' -> 'kospi', '068270.KQ' -> 'kosdaq', 그 외 None.

        KR 여부는 중앙화된 suffix 규칙(market_symbol_utils: KS/KQ + 6자리)을
        재사용하고, 시장 구분만 suffix로 나눈다.
        """
        code = str(stock_code or "").strip()
        if not is_kr_suffix_symbol(code):
            return None
        return "kosdaq" if code.upper().endswith(".KQ") else "kospi"

    @staticmethod
    def _base_code(stock_code: Any) -> str:
        return str(stock_code or "").strip().split(".", 1)[0]

    @staticmethod
    def _parse_naver_stock_row(raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        date = _date_from_yyyymmdd(raw.get(_NAVER_KEY_DATE))
        if date is None:
            return None
        foreign = _to_int(raw.get(_NAVER_KEY_FOREIGN))
        institution = _to_int(raw.get(_NAVER_KEY_INSTITUTION))
        if foreign is None or institution is None:
            return None  # 필수 결측 -> 행 폐기, 0 조작 금지
        return {
            "date": date,
            "foreign_net": foreign,
            "institution_net": institution,
            "individual_net": _to_int(raw.get(_NAVER_KEY_INDIVIDUAL)),
        }

    @staticmethod
    def _parse_daum_row(raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        date = _date_from_daum(raw.get(_DAUM_KEY_DATE))
        if date is None:
            return None
        foreign = _to_int(raw.get(_DAUM_KEY_FOREIGN))
        institution = _to_int(raw.get(_DAUM_KEY_INSTITUTION))
        if foreign is None or institution is None:
            return None
        return {
            "date": date,
            "foreign_net": foreign,
            "institution_net": institution,
            "individual_net": None,  # 다음 소스는 개인 순매수를 제공하지 않는다
        }

    @staticmethod
    def _build_flows(
        market: str,
        day_rows: List[Dict[str, Any]],
        source: str,
        *,
        unit: str,
        code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """정규화 레코드(스펙 §2). summary는 전달된 행 중 최근 최대 5행 누적."""
        summary_rows = day_rows[:5]
        record: Dict[str, Any] = {
            "market": market,
            "unit": unit,
            "days": day_rows,
            "summary": {
                "foreign_net_5d": sum(r["foreign_net"] for r in summary_rows),
                "institution_net_5d": sum(r["institution_net"] for r in summary_rows),
            },
            "source": source,
        }
        if code is not None:
            record["code"] = code
        return record

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
from datetime import timedelta, timezone
from typing import Any, Optional

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

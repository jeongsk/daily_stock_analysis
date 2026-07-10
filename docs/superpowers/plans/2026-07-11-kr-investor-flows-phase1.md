# KR 투자자별 매매동향(수급) Phase 1 — KrInstitutionalFetcher 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 무인증 공개 소스(네이버·다음)에서 KR 종목(주수)·시장(KRW) 일별 투자자 수급을 fail-open으로 수집하는 `KrInstitutionalFetcher` 데이터 계층을 추가한다 — 소비자 연결(리포트/마켓 리뷰)은 Phase 2/3 별도 PR.

**Architecture:** `data_provider/tw_institutional_fetcher.py`(TW 三大法人)를 미러링한 독립 모듈. 종목은 네이버 모바일 integration JSON(기본) → 다음 금융 JSON(fallback) 체인, 시장은 네이버 PC EUC-KR HTML 단일 소스. TTL 캐시(비어있지 않은 응답만) + 키별 in-flight 락(요청 코얼레싱) + 소스별 `CircuitBreaker` + 요청 스로틀 + 전면 fail-open(`None`).

**Tech Stack:** Python 3 / `requests`(기존) / `lxml`(기존 설치, 시장 HTML 파싱에 지연 임포트) / `pytest`(오프라인 `-m "not network"` 차단 게이트, `-m network` 관측 스모크).

**스펙:** `docs/superpowers/specs/2026-07-10-kr-investor-flows-design.md` §2·§5·§7 (Phase 1 범위)
**결정 기록:** `docs/adr/0001-kr-investor-flows-no-auth-sources.md`(무인증 소스·단위 이원화), `CONTEXT.md`(용어: 수급/투자주체/순매수/확정 거래일)

## Global Constraints

- **신규 의존성 0, 신규 설정 0** — `.env.example`·`pyproject.toml`·`requirements.txt` 변경 금지. 시장 HTML 파싱은 이미 설치된 lxml을 **지연 임포트**로 사용(미설치 환경은 fail-open).
- **무인증 공개 엔드포인트만** — API 키·로그인·쿠키 금지 (ADR 0001). KRX는 로그인 게이트로 폐기됨.
- **전면 fail-open** — 공개 메서드 2개는 어떤 입력·실패에서도 예외를 던지지 않고 `None` 반환.
- **단위 이원화** — 종목 레코드 `unit: "shares"`(주수), 시장 레코드 `unit: "KRW"`(원, 네이버 억원 ×100,000,000). **주수×종가 금액 추정 환산 금지.**
- **필수 구성요소(`foreign_net`, `institution_net`) 결측 행은 폐기** — 0으로 조작 금지. `individual_net`만 `None` 허용(다음 소스는 개인 미제공).
- **3주체 고정** — 외국인/기관계/개인. 기타법인·세부 기관 분류는 주체로 승격하지 않음(합≠0).
- **기존 파일 수정 없음** — 신규 파일 3개 + `docs/CHANGELOG.md` 1줄만. 순수 additive.
- 오프라인 게이트 통과 필수: `uv run pytest -m "not network"` + `uv run ./scripts/ci_gate.sh`.
- 커밋 메시지 **영어**, `Co-Authored-By` 금지, 커밋 제목에 `#patch`/`#minor`/`#major` 금지(자동 태그 opt-in 방지). 태스크별 커밋은 사용자의 계획 실행 승인으로 갈음하되, **`git push`/PR 생성은 별도 사용자 확인 필요**.
- 테스트 픽스처는 **2026-07-10 실캡처 데이터**(아래 각 태스크에 포함) — 임의 값으로 바꾸지 말 것.

## 파일 구조

| 파일 | 역할 |
| --- | --- |
| Create: `data_provider/kr_institutional_fetcher.py` | fetcher 전체 (상수·헬퍼·클래스) |
| Create: `tests/test_kr_institutional_fetcher.py` | 오프라인 테스트 (실캡처 픽스처, 네트워크 없음) |
| Create: `tests/test_kr_institutional_network.py` | `-m network` 드리프트 스모크 (관측용, 비차단) |
| Modify: `docs/CHANGELOG.md` | `[Unreleased]`에 플랫 1줄 |

스펙 §6의 `docs/market-support.md` 갱신은 **Phase 2로 이월** — Phase 1은 소비자가 없는 데이터 계층이라 사용자 가시 지원 범위가 아직 변하지 않는다(선례: TW fetcher도 데이터 계층 단계에서는 market-support 미기재).

참고: 캐시 키는 (레벨, 코드/시장) 단위이고 `days`는 캐시된 행의 슬라이스로 처리한다 — 스펙의 "(엔드포인트, 종목/시장, 기간) 단위 TTL 캐시"보다 강한 보장(기간이 달라도 재요청 없음)이다.

---

### Task 1: 모듈 뼈대 — 상수와 순수 헬퍼

**Files:**
- Create: `data_provider/kr_institutional_fetcher.py`
- Create: `tests/test_kr_institutional_fetcher.py`

**Interfaces:**
- Consumes: 없음 (stdlib만)
- Produces (이후 전 태스크가 사용):
  - `_to_int(value) -> Optional[int]` — `"+625,985"`/`"-2,851,466"`/`635576`/`285000.0` → int, `""`/`"-"`/`"--"`/`"—"`/비수치 → `None`
  - `_clamp_days(days) -> int` — int 변환 실패 시 5, 그 외 1..30 클램프
  - `_date_from_yyyymmdd(value) -> Optional[str]` — `"20260710"` → `"2026-07-10"`
  - `_date_from_daum(value) -> Optional[str]` — `"2026-07-10 00:00:00"` → `"2026-07-10"`
  - `_date_from_dot_yy(value) -> Optional[str]` — `"26.07.10"` → `"2026-07-10"`
  - 상수: `_NAVER_STOCK_URL`, `_DAUM_STOCK_URL`, `_NAVER_MARKET_URL`, `_UA`, `_KST`, `_NAVER_KEY_DATE/_FOREIGN/_INSTITUTION/_INDIVIDUAL`, `_DAUM_KEY_DATE/_FOREIGN/_INSTITUTION`, `_MARKET_SOSOK`, `_MARKET_HEAD`, `_EOK_KRW`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_institutional_fetcher.py` 생성:

```python
# -*- coding: utf-8 -*-
"""KrInstitutionalFetcher 오프라인 테스트.

픽스처는 전부 2026-07-10 실제 응답 캡처다(삼성전자 005930 / KOSPI).
네트워크 접근 없음 — `pytest -m "not network"` 게이트에 포함된다.
"""

from data_provider.kr_institutional_fetcher import (
    _clamp_days,
    _date_from_daum,
    _date_from_dot_yy,
    _date_from_yyyymmdd,
    _to_int,
)


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
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_institutional_fetcher.py -v`
Expected: 수집 단계 FAIL — `ModuleNotFoundError: No module named 'data_provider.kr_institutional_fetcher'`

- [ ] **Step 3: 최소 구현** — `data_provider/kr_institutional_fetcher.py` 생성:

```python
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
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_institutional_fetcher.py -v`
Expected: PASS (4 passed)

Run: `uv run python -m py_compile data_provider/kr_institutional_fetcher.py`
Expected: 출력 없음(성공)

- [ ] **Step 5: 커밋**

```bash
git add data_provider/kr_institutional_fetcher.py tests/test_kr_institutional_fetcher.py
git commit -m "feat: add KR investor flows module skeleton and parsing helpers"
```

---

### Task 2: 행 파서와 레코드 빌더 (순수 정적 메서드)

**Files:**
- Modify: `data_provider/kr_institutional_fetcher.py` (클래스 + 정적 메서드 추가)
- Modify: `tests/test_kr_institutional_fetcher.py` (픽스처 + 테스트 추가)

**Interfaces:**
- Consumes: Task 1의 헬퍼·상수 전부
- Produces (Task 3~5가 사용):
  - `class KrInstitutionalFetcher` (아직 `__init__` 없음 — 정적 메서드만)
  - `KrInstitutionalFetcher._market_of(stock_code) -> Optional[str]` — `.KS`→`"kospi"`, `.KQ`→`"kosdaq"`, 그 외 `None`
  - `KrInstitutionalFetcher._base_code(stock_code) -> str` — `"005930.KS"`→`"005930"`
  - `KrInstitutionalFetcher._parse_naver_stock_row(raw) -> Optional[dict]` / `._parse_daum_row(raw) -> Optional[dict]` — 날짜 행: `{"date": str, "foreign_net": int, "institution_net": int, "individual_net": Optional[int]}`
  - `KrInstitutionalFetcher._build_flows(market: str, day_rows: list, source: str, *, unit: str, code: Optional[str] = None) -> dict` — 스펙 §2 레코드 계약
- 테스트 모듈 상수(Task 3~4 재사용): `NAVER_INTEGRATION_FIXTURE`, `DAUM_INVESTOR_FIXTURE`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_institutional_fetcher.py`의 import 블록을 다음으로 교체하고, 파일 끝에 픽스처와 테스트 클래스를 추가:

```python
from data_provider.kr_institutional_fetcher import (
    KrInstitutionalFetcher,
    _clamp_days,
    _date_from_daum,
    _date_from_dot_yy,
    _date_from_yyyymmdd,
    _to_int,
)
```

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_institutional_fetcher.py -v`
Expected: 수집 단계 FAIL — `ImportError: cannot import name 'KrInstitutionalFetcher'`

- [ ] **Step 3: 최소 구현** — `data_provider/kr_institutional_fetcher.py`에 추가.

import 블록에 두 줄 추가/교체:

```python
from typing import Any, Dict, List, Optional

from src.services.market_symbol_utils import is_kr_suffix_symbol
```

파일 끝에 클래스 추가:

```python
class KrInstitutionalFetcher:
    """KR 수급 데이터 계층 — 무설정·fail-open (모듈 docstring 참조)."""

    name = "KrInstitutionalFetcher"

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
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_institutional_fetcher.py -v`
Expected: PASS (13 passed)

- [ ] **Step 5: 커밋**

```bash
git add data_provider/kr_institutional_fetcher.py tests/test_kr_institutional_fetcher.py
git commit -m "feat: add KR investor flows row parsers and record builder"
```

---

### Task 3: `get_investor_flows` — 네이버 기본 경로 + 캐시/스로틀/코얼레싱/fail-open

**Files:**
- Modify: `data_provider/kr_institutional_fetcher.py`
- Modify: `tests/test_kr_institutional_fetcher.py`

**Interfaces:**
- Consumes: Task 2의 정적 메서드, Task 1의 헬퍼·상수, `data_provider.realtime_types.CircuitBreaker` (`is_available(source)` / `record_failure(source, error=None)` / `record_success(source)` / `get_status() -> Dict[str, str]` / 상수 `OPEN`·`CLOSED`)
- Produces:
  - `KrInstitutionalFetcher.__init__(*, cache_ttl_seconds: int = 900, min_request_interval: float = 1.0, timeout: int = 15)`
  - `get_investor_flows(stock_code: str, days: int = 5) -> Optional[dict]` (공개 API — 스펙 §2)
  - 내부: `_read_cache(key)`, `_store_cache(key, value)`, `_key_lock(key) -> threading.Lock`, `_throttle()`, `_get_json(url, *, params=None, headers=None)`, `_try_source(breaker_key: str, label: str, fetch: Callable[[], List[dict]]) -> List[dict]`, `_fetch_naver_stock(base: str) -> List[dict]`, `_stock_rows(base: str) -> Optional[Tuple[List[dict], str]]`
  - 브레이커 키: 종목 네이버 = `"naver_stock"` (Task 4: `"daum_stock"`, Task 5: `"naver_market"`)
- 테스트 헬퍼(Task 4~5 재사용): `_resp(json_data=None, *, content=None)`, `_fetcher()`, patch 대상 문자열 `"data_provider.kr_institutional_fetcher.requests.get"`

**주의:** 이 태스크의 테스트는 Task 4(다음 fallback 추가) 이후에도 수정 없이 통과하도록 설계되어 있다 — 전송 오류/빈 응답 시나리오는 "모든 소스 실패"로 기술한다(단일 `side_effect`/`return_value`가 네이버·다음 양쪽에 동일하게 적용됨).

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_institutional_fetcher.py` import 블록을 다음으로 교체:

```python
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
```

픽스처 아래에 헬퍼 2개, 파일 끝에 테스트 클래스 추가:

```python
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
```

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_institutional_fetcher.py -v -k TestGetInvestorFlowsNaver`
Expected: FAIL — `TypeError: KrInstitutionalFetcher() takes no arguments` 또는 `AttributeError: ... has no attribute 'get_investor_flows'`

- [ ] **Step 3: 최소 구현** — `data_provider/kr_institutional_fetcher.py` import 블록을 다음 최종 형태로 교체:

```python
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from data_provider.realtime_types import CircuitBreaker
from src.services.market_symbol_utils import is_kr_suffix_symbol
```

(`datetime` 심볼은 이 시점에 아직 미사용이므로 import하지 않는다 — flake8 F401 방지. Task 5에서 시장 bizdate 계산에 쓰일 때 추가한다.)

클래스에 다음을 추가 (`name = "KrInstitutionalFetcher"` 아래):

```python
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
            rows = self._try_source(
                "naver_stock", f"naver stock {base}", lambda: self._fetch_naver_stock(base)
            )
            if not rows:
                return None  # 빈 결과는 캐시하지 않는다
            value = (rows, "NAVER")
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
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_institutional_fetcher.py -v`
Expected: PASS (22 passed)

- [ ] **Step 5: 커밋**

```bash
git add data_provider/kr_institutional_fetcher.py tests/test_kr_institutional_fetcher.py
git commit -m "feat: fetch per-stock KR investor flows from Naver integration API"
```

---

### Task 4: 다음(DAUM) fallback 체인 + 소스별 서킷브레이커

**Files:**
- Modify: `data_provider/kr_institutional_fetcher.py`
- Modify: `tests/test_kr_institutional_fetcher.py`

**Interfaces:**
- Consumes: Task 3의 `_try_source`/`_get_json`/`_stock_rows`, Task 2의 `_parse_daum_row`, 테스트 픽스처 `DAUM_INVESTOR_FIXTURE`·헬퍼 `_resp`/`_fetcher`/`_GET`
- Produces:
  - `_fetch_daum_stock(base: str) -> List[dict]` — `Referer: https://finance.daum.net/quotes/A{base}` 헤더 필수(없으면 다음이 거부)
  - `_stock_rows` v2 — 네이버 실패/빈 응답 시 다음 시도, 성공 소스명(`"NAVER"`/`"DAUM"`)을 튜플로 반환
- 기존 Task 3 테스트는 **수정 없이** 계속 통과해야 한다.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_institutional_fetcher.py` import 블록에 추가:

```python
from data_provider.realtime_types import CircuitBreaker
```

파일 끝에 라우터 헬퍼와 테스트 클래스 추가:

```python
def _route_get(naver, daum):
    """URL 호스트별로 다른 응답/예외를 돌려주는 requests.get side_effect."""

    def _route(url, **kwargs):
        if "m.stock.naver.com" in url:
            result = naver
        elif "finance.daum.net" in url:
            result = daum
        else:
            raise AssertionError(f"unexpected url: {url}")
        if isinstance(result, Exception):
            raise result
        return result

    return _route


class TestDaumFallback:
    def test_naver_http_error_falls_back_to_daum(self):
        f = _fetcher()
        with patch(_GET, side_effect=_route_get(
            requests.exceptions.HTTPError("500"), _resp(DAUM_INVESTOR_FIXTURE)
        )):
            record = f.get_investor_flows("005930.KS")
        assert record is not None
        assert record["source"] == "DAUM"
        assert record["unit"] == "shares"
        assert record["market"] == "kospi"
        assert record["days"][0] == {
            "date": "2026-07-10",
            "foreign_net": 635576,
            "institution_net": 2313745,
            "individual_net": None,
        }
        assert record["summary"] == {
            "foreign_net_5d": DAUM_FOREIGN_5D,
            "institution_net_5d": DAUM_INSTITUTION_5D,
        }

    def test_naver_empty_falls_back_to_daum_with_referer(self):
        f = _fetcher()
        with patch(_GET, side_effect=_route_get(
            _resp({"dealTrendInfos": []}), _resp(DAUM_INVESTOR_FIXTURE)
        )) as mock_get:
            record = f.get_investor_flows("005930.KS")
        assert record["source"] == "DAUM"
        daum_calls = [c for c in mock_get.call_args_list if "finance.daum.net" in c.args[0]]
        assert daum_calls, "다음 fallback이 호출되지 않았다"
        headers = daum_calls[0].kwargs["headers"]
        assert headers["Referer"] == "https://finance.daum.net/quotes/A005930"
        params = daum_calls[0].kwargs["params"]
        assert params["symbolCode"] == "A005930"

    def test_both_sources_fail_none(self):
        f = _fetcher()
        with patch(_GET, side_effect=_route_get(
            requests.exceptions.ConnectionError("down"),
            requests.exceptions.ConnectionError("down"),
        )):
            assert f.get_investor_flows("005930.KS") is None

    def test_naver_breaker_open_goes_straight_to_daum(self):
        f = _fetcher()
        codes = ("005930.KS", "000660.KS", "035420.KS", "005380.KS")
        with patch(_GET, side_effect=_route_get(
            requests.exceptions.ConnectionError("blocked"), _resp(DAUM_INVESTOR_FIXTURE)
        )) as mock_get:
            records = [f.get_investor_flows(code) for code in codes]
            naver_calls = [
                c for c in mock_get.call_args_list if "m.stock.naver.com" in c.args[0]
            ]
        # 연속 3회 실패로 naver_stock 서킷 오픈 -> 4번째 종목은 네이버를 건너뜀
        assert len(naver_calls) == 3
        assert f._breaker.get_status().get("naver_stock") == CircuitBreaker.OPEN
        # 다음 소스는 독립 브레이커라 4건 모두 DAUM으로 성공
        assert all(r is not None and r["source"] == "DAUM" for r in records)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_institutional_fetcher.py -v -k TestDaumFallback`
Expected: 4 FAIL — fallback 미구현이라 `record is None` 계열 실패

- [ ] **Step 3: 최소 구현** — `_fetch_naver_stock` 아래에 추가:

```python
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
```

`_stock_rows`의 소스 시도 구간을 교체 — 기존:

```python
            rows = self._try_source(
                "naver_stock", f"naver stock {base}", lambda: self._fetch_naver_stock(base)
            )
            if not rows:
                return None  # 빈 결과는 캐시하지 않는다
            value = (rows, "NAVER")
```

교체 후:

```python
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
```

- [ ] **Step 4: 통과 확인 (Task 3 테스트 포함 전체)**

Run: `uv run pytest tests/test_kr_institutional_fetcher.py -v`
Expected: PASS (26 passed) — `TestGetInvestorFlowsNaver`가 수정 없이 통과하는지 반드시 확인

- [ ] **Step 5: 커밋**

```bash
git add data_provider/kr_institutional_fetcher.py tests/test_kr_institutional_fetcher.py
git commit -m "feat: add Daum fallback for per-stock KR investor flows"
```

---

### Task 5: `get_market_investor_flows` — 시장 수급 (EUC-KR HTML, 억원→KRW)

**Files:**
- Modify: `data_provider/kr_institutional_fetcher.py`
- Modify: `tests/test_kr_institutional_fetcher.py`

**Interfaces:**
- Consumes: Task 1 `_date_from_dot_yy`/`_to_int`/`_MARKET_SOSOK`/`_MARKET_HEAD`/`_EOK_KRW`/`_KST`, Task 2 `_build_flows`, Task 3 인프라(`_read_cache`/`_store_cache`/`_key_lock`/`_throttle`/`_try_source`)
- Produces:
  - `get_market_investor_flows(market: str, days: int = 5) -> Optional[dict]` (공개 API — `market`: `"kospi"`|`"kosdaq"`, 대소문자/공백 허용)
  - 내부: `_get_html(url, *, params=None) -> str` (EUC-KR 디코딩), `_fetch_naver_market(market) -> List[dict]`, `_parse_market_table(html_text) -> List[dict]` (staticmethod, lxml 지연 임포트), `_market_rows(market) -> Optional[List[dict]]`
  - 시장 레코드: `unit="KRW"`(원), `code` 키 없음, `source="NAVER"`, 브레이커 키 `"naver_market"`

- [ ] **Step 1: 실패하는 테스트 작성** — 픽스처 구역에 추가:

```python
# GET https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate=20260710&sosok=01
# 의 대상 표 마크업 (EUC-KR 원문 구조 보존, 값 단위: 억원).
# 날짜/개인/외국인/기관계/기타법인 열은 실캡처 값이고, 기관 세부 6열은
# 기관계 합과 일치하는 대표값이다(파서는 선두 4열만 읽는다).
# 선두의 헤더 불일치 표는 표 선택 로직 검증용 미끼다.
MARKET_HTML_FIXTURE = """<html><head><title>네이버페이 증권</title></head><body>
<table class="type_1" summary="다른 표">
<tr><th>지수</th><th>등락</th></tr>
<tr><td>코스피</td><td>+1.2%</td></tr>
</table>
<table summary="일자별 순매수에 관한 표 입니다." cellpadding="0" cellspacing="0" class="type_1">
<tr class="udline">
  <th rowspan="2" class="noln">날짜</th>
  <th rowspan="2">개인</th>
  <th rowspan="2">외국인</th>
  <th rowspan="2">기관계</th>
  <th colspan="6" class="eb">기관</th>
  <th rowspan="2">기타법인</th>
</tr>
<tr class="udline">
  <th class="sub">금융투자</th><th class="sub">보험</th><th class="sub">투신<br>(사모)</th>
  <th class="sub">은행</th><th class="sub">기타금융기관</th><th class="sub">연기금등</th>
</tr>
<tr>
  <td class="date2">26.07.10</td>
  <td class="rate_down3">-7,805</td>
  <td class="rate_down3">-3,228</td>
  <td class="rate_up3">11,314</td>
  <td class="rate_up3">5,377</td><td class="rate_up3">632</td><td class="rate_up3">2,269</td>
  <td class="rate_down3">-83</td><td class="rate_up3">169</td><td class="rate_up3">2,950</td>
  <td class="rate_down3">-282</td>
</tr>
<tr>
  <td class="date2">26.07.09</td>
  <td class="rate_down3">-13,278</td>
  <td class="rate_up3">1,343</td>
  <td class="rate_up3">12,884</td>
  <td class="rate_up3">9,347</td><td class="rate_up3">361</td><td class="rate_up3">1,721</td>
  <td class="rate_up3">36</td><td class="rate_up3">96</td><td class="rate_up3">1,323</td>
  <td class="rate_down3">-950</td>
</tr>
</table>
</body></html>"""
```

파일 끝에 테스트 클래스 추가:

```python
class TestMarketFlows:
    def _market_resp(self):
        return _resp(content=MARKET_HTML_FIXTURE.encode("euc-kr"))

    def test_kospi_record_from_html_fixture(self):
        f = _fetcher()
        with patch(_GET, return_value=self._market_resp()):
            record = f.get_market_investor_flows("kospi")
        assert record is not None
        assert "code" not in record
        assert record["market"] == "kospi"
        assert record["unit"] == "KRW"
        assert record["source"] == "NAVER"
        # 억원 -> 원 (×1e8)
        assert record["days"][0] == {
            "date": "2026-07-10",
            "foreign_net": -322800000000,
            "institution_net": 1131400000000,
            "individual_net": -780500000000,
        }
        assert record["days"][1]["date"] == "2026-07-09"
        assert record["summary"] == {
            "foreign_net_5d": (-3228 + 1343) * 100000000,
            "institution_net_5d": (11314 + 12884) * 100000000,
        }

    def test_market_arg_normalization_and_sosok_param(self):
        f = _fetcher()
        with patch(_GET, return_value=self._market_resp()) as mock_get:
            record = f.get_market_investor_flows(" KOSDAQ ")
        assert record["market"] == "kosdaq"
        params = mock_get.call_args.kwargs["params"]
        assert params["sosok"] == "02"
        assert len(params["bizdate"]) == 8 and params["bizdate"].isdigit()

    def test_invalid_market_none_without_fetch(self):
        f = _fetcher()
        with patch(_GET) as mock_get:
            for market in ("nasdaq", "kr", "", None):
                assert f.get_market_investor_flows(market) is None
        mock_get.assert_not_called()

    def test_header_rename_fails_open(self):
        drifted = MARKET_HTML_FIXTURE.replace(">외국인<", ">외인<")
        f = _fetcher()
        with patch(_GET, return_value=_resp(content=drifted.encode("euc-kr"))):
            assert f.get_market_investor_flows("kospi") is None

    def test_days_slice(self):
        f = _fetcher()
        with patch(_GET, return_value=self._market_resp()):
            record = f.get_market_investor_flows("kospi", days=1)
        assert len(record["days"]) == 1
        assert record["summary"]["foreign_net_5d"] == -322800000000

    def test_market_cached_per_market(self):
        f = _fetcher()
        with patch(_GET, return_value=self._market_resp()) as mock_get:
            f.get_market_investor_flows("kospi")
            f.get_market_investor_flows("kospi", days=1)
            assert mock_get.call_count == 1
            f.get_market_investor_flows("kosdaq")
            assert mock_get.call_count == 2

    def test_market_breaker_opens_after_three_failures(self):
        f = _fetcher()
        with patch(_GET, side_effect=requests.exceptions.ConnectionError("down")) as mock_get:
            for _ in range(3):
                assert f.get_market_investor_flows("kospi") is None
            assert f._breaker.get_status().get("naver_market") == CircuitBreaker.OPEN
            assert f.get_market_investor_flows("kospi") is None
            assert mock_get.call_count == 3  # 서킷 오픈 동안 요청 없음
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_institutional_fetcher.py -v -k TestMarketFlows`
Expected: 7 FAIL — `AttributeError: ... has no attribute 'get_market_investor_flows'`

- [ ] **Step 3: 최소 구현** — import 블록의 `from datetime import timedelta, timezone`을 `from datetime import datetime, timedelta, timezone`으로 교체한다(시장 bizdate 계산에 사용 — 이 시점부터 실사용이므로 F401 없음). 클래스에 추가:

```python
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
```

- [ ] **Step 4: 통과 확인 (전체)**

Run: `uv run pytest tests/test_kr_institutional_fetcher.py -v`
Expected: PASS (33 passed)

- [ ] **Step 5: 커밋**

```bash
git add data_provider/kr_institutional_fetcher.py tests/test_kr_institutional_fetcher.py
git commit -m "feat: add KOSPI/KOSDAQ market investor flows from Naver daily page"
```

---

### Task 6: network 드리프트 스모크 + CHANGELOG + 최종 게이트

**Files:**
- Create: `tests/test_kr_institutional_network.py`
- Modify: `docs/CHANGELOG.md` (`## [Unreleased]` 바로 아래에 1줄)

**Interfaces:**
- Consumes: 공개 API 2개 + `_fetch_daum_stock`(다음 경로는 네이버 성공 시 공개 API로 도달 불가 — 드리프트 감시 목적의 내부 호출), `_MARKET_HEAD`, `_UA`
- Produces: 없음 (관측용 종단 검증 + 문서)

**드리프트 스모크 규약** (TW `tests/test_tw_institutional_network.py`와 동일): 전송 실패·비200은 조용히 skip(일시 장애/IP 차단), **200인데 형식이 다르면 시끄럽게 FAIL**(파싱 드리프트). `network-smoke` 워크플로가 관측용으로 실행하며 CI 차단 아님.

- [ ] **Step 1: network 스모크 작성** — `tests/test_kr_institutional_network.py` 생성:

```python
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


def _get_or_skip(url, *, params=None, headers=None):
    merged = {"User-Agent": _UA}
    if headers:
        merged.update(headers)
    try:
        resp = requests.get(url, params=params, headers=merged, timeout=20)
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
```

- [ ] **Step 2: 오프라인 게이트에서 제외되는지 확인**

Run: `uv run pytest tests/test_kr_institutional_network.py -m "not network" -q`
Expected: `no tests ran` (deselected) — network 마커가 오프라인 게이트를 오염시키지 않음

(선택 — 실제 네트워크가 있으면) Run: `uv run pytest tests/test_kr_institutional_network.py -m network -v`
Expected: 3 passed 또는 skip(전송 장애 시). FAIL이면 소스 드리프트이므로 구현을 재점검.

- [ ] **Step 3: CHANGELOG 갱신** — `docs/CHANGELOG.md`의 `## [Unreleased]` 항목 목록에 다음 1줄 추가 (플랫 형식 — `### 类目标题` 신설 금지):

```markdown
- [新功能] KR 투자자별 매매동향(수급) 데이터 계층 추가: `KrInstitutionalFetcher`가 네이버·다음 무인증 소스로 종목(주수)·KOSPI/KOSDAQ 시장(KRW) 일별 순매수를 수집 — fail-open·캐시·서킷브레이커 포함, 리포트 연동은 후속 Phase (설계: docs/superpowers/specs/2026-07-10-kr-investor-flows-design.md)
```

- [ ] **Step 4: 최종 게이트 실행**

Run: `uv run pytest -m "not network" -q`
Expected: 기존 전체 스위트 + 신규 33개 전부 PASS, 실패 0

Run: `uv run ./scripts/ci_gate.sh`
Expected: exit 0 (lint 포함 통과)

- [ ] **Step 5: 커밋 (2건)**

```bash
git add tests/test_kr_institutional_network.py
git commit -m "test: add network drift smoke for KR investor flows sources"
git add docs/CHANGELOG.md
git commit -m "docs: add changelog entry for KR investor flows data layer"
```

---

## 완료 기준 (스펙 §2·§5 대조)

- [ ] `get_investor_flows("005930.KS")` — 주수 레코드(3주체, 내림차순, summary, source NAVER/DAUM), 비KR·실패 시 `None`
- [ ] `get_market_investor_flows("kospi"|"kosdaq")` — KRW 레코드(억원 ×1e8), `code` 키 없음, 실패 시 `None`
- [ ] 네이버→다음 fallback 체인, 소스별 서킷브레이커(3회/5분), ~1초 스로틀, 비어있지 않은 응답만 TTL 캐시, 키별 in-flight 락
- [ ] 필수 결측 행 폐기 / 진짜 0 유지 / `individual_net`만 nullable
- [ ] 오프라인 게이트(`-m "not network"` + `ci_gate.sh`) 통과, network 스모크는 관측용
- [ ] 기존 파일 무변경(순수 additive), 신규 의존성·설정 0, CHANGELOG 플랫 1줄

**롤백:** 신규 파일 3개 삭제 + CHANGELOG 1줄 제거(= 커밋 revert)로 완전 롤백 — 기존 코드 경로에 어떤 참조도 추가되지 않는다.

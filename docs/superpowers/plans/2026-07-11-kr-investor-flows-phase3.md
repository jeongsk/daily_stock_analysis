# KR 투자자별 매매동향(수급) Phase 3 — 마켓 리뷰 연결 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phase 1에서 만든 `KrInstitutionalFetcher.get_market_investor_flows`를 KR 마켓 리뷰(`MARKET_REVIEW_REGION=kr`)에 연결한다 — KOSPI/KOSDAQ 시장 전체 수급(KRW)을 fail-open으로 수집해 (1) 마켓 리뷰 LLM 프롬프트, (2) 리뷰 리포트 본문의 결정적 요약, (3) 구조화 페이로드에 반영한다.

**Architecture:** Phase 2의 "단일 위치 → 세 소비면 자동 도달" 정신을 마켓 리뷰에 미러링한다. 수급 레코드는 **단일 위치** `MarketOverview.investor_flows`(신규 optional 필드)에 저장되어 프롬프트 빌더(`_build_review_prompt`)와 결정적 렌더러(`_inject_data_into_review`) 양쪽에 자동 도달한다. 수집은 `get_market_overview()`에 `if self.region == "kr"` 브랜치 1개를 추가하는 것이 유일한 훅이며, fetcher 호출은 `DataFetcherManager.get_kr_market_investor_flows`(Phase 2 lazy 싱글턴 재사용)를 경유한다. KR은 `has_market_stats=False`라 상승/하락 통계·섹터 데이터가 없으므로, **수급이 곧 KR의 시장 폭(market breadth) 신호**다 — 프롬프트에서 KR의 "시장 폭 데이터 없음" 블록을 실제 수급으로 대체하고, 리포트 본문의 `시장 요약` 섹션에 결정적 수급 블록을 주입한다.

**Tech Stack:** Python 3 / `requests`(Phase 1 fetcher) / `pytest`(오프라인 `-m "not network"` 차단 게이트) / 소비 경로: `src/market_analyzer.py`(수집·프롬프트·렌더·페이로드), `src/report_language.py`(KRW 로케일 포맷터), `data_provider/base.py`(fetcher 배선).

**스펙:** `docs/superpowers/specs/2026-07-10-kr-investor-flows-design.md` §4 (Phase 3 범위)
**결정 기록:** `docs/adr/0001-...`(무인증 소스·단위 이원화), `docs/adr/0002-...`(전역 품질 블록 — Phase 2), `CONTEXT.md`(용어)
**선행 계획:** `docs/superpowers/plans/2026-07-11-kr-investor-flows-phase1.md`, `docs/superpowers/plans/2026-07-11-kr-investor-flows-phase2.md`

## Global Constraints

- **신규 의존성 0, 신규 설정 0** — `.env.example`·`pyproject.toml`·`requirements.txt` 변경 금지. Phase 1 fetcher는 이미 무설정 동작한다.
- **전면 fail-open** — 수급 수집/파싱/렌더링의 어떤 실패도 마켓 리뷰를 중단시키지 않는다. 실패 시 수급 섹션만 생략되고 리뷰는 정상 진행한다.
- **엄격 additive — 비KR 마켓 리뷰는 바이트 동일** — cn/us/hk/jp 리뷰의 프롬프트와 리포트는 **불변**이어야 한다. 모든 신규 코드 경로는 `self.region == "kr"` 또는 `overview.investor_flows` 존재로 가드된다(비KR은 `investor_flows`가 None이라 자동 스킵). 회귀 테스트로 고정.
- **투자 판단 신호 비연결** — `capital_flow_signal`·signal_attribution·매수/매도 스코어의 입력으로 쓰지 않는다. 수급은 LLM 참고 정보 + 표시용뿐이다(품질 점수는 마켓 리뷰에 없으므로 Phase 3 비대상).
- **중국어 혼입 거부 게이트 통과** — KR(ko) 리뷰 출력에는 한자가 있으면 안 된다(`has_disallowed_report_script`, `report_language.py:779`). 결정적 렌더 블록은 이 게이트가 검사하는 LLM 텍스트 **이후**에 주입되므로(`generate_market_review`: 게이트 826 → `_inject_data_into_review` 864), **ko 결정적 블록은 순수 한글로 작성**해 최종 리포트에 한자가 새지 않게 한다.
- **단위 = KRW 원(시장 레코드 `unit:"KRW"`)** — 억(1e8)/십억(1e9) 스케일로만 표기. 로케일: ko `억` / zh `亿韩元` / en `₩…B`(십억 원). 예: `-3,228억` = `₩-322.8B` = `-3,228亿韩元`.
- **데이터 단일 위치** — 수급 레코드는 `MarketOverview.investor_flows` 한 곳(신규 optional 필드)에만 둔다. 이 필드는 `_build_review_prompt`·`_inject_data_into_review`·`build_market_review_payload`가 모두 받는 `overview`에 실려 세 소비면에 자동 도달한다.
- **표시 결정(사용자 확정 2026-07-11):** ① KOSPI·KOSDAQ **2줄**(시장별 독립 라인). ② **리뷰 본문 + 구조화 페이로드** 양쪽 렌더.
- **시장명 = KOSPI/KOSDAQ 고정** — 고유명사이므로 zh/en/ko 모두 `KOSPI`/`KOSDAQ`로 표기(라벨 로컬라이즈 불필요, 한자 회피에도 유리).
- **필수 결측/비대상 처리** — 두 시장 모두 레코드 없으면 프롬프트·리포트·페이로드 섹션을 생략한다. 0으로 조작하지 않는다. `individual_net`(개인)은 요약 라인에서 제외(외국인·기관 역방향 중복). 한 시장만 데이터가 있으면 그 시장만 렌더한다.
- **커밋 메시지 영어**, `Co-Authored-By` 금지, 커밋 제목에 `#patch`/`#minor`/`#major` 금지(자동 태그 opt-in 방지). 태스크별 커밋은 계획 실행 승인으로 갈음하되, **`git push`/PR 생성은 별도 사용자 확인 필요**.
- 오프라인 게이트 통과 필수: `uv run pytest -m "not network"` + `uv run ./scripts/ci_gate.sh`.
- 테스트 픽스처는 **KRW 원 단위 시장 레코드**(2026-07-10 기준 값, summary가 스펙 §4 예시 `외국인 -3,228억 / 기관 +11,314억`과 일치) — 임의 값으로 바꾸지 말 것.

## 파일 구조

| 파일 | 역할 | 변경 성격 |
| --- | --- | --- |
| Modify: `src/report_language.py` | `format_net_krw_localized(value, language)` KRW 로케일 포맷터 신규(ko 억 / zh 亿韩元 / en ₩…B) | additive 함수 1개 |
| Create: `tests/test_kr_market_flows_format.py` | KRW 포맷터 단위 테스트(ko/zh/en/무효) | 신규 |
| Modify: `data_provider/base.py` | `DataFetcherManager.get_kr_market_investor_flows(market, days)` — Phase 2 lazy 싱글턴 재사용, fail-open | additive 메서드 1개 |
| Modify: `src/market_analyzer.py` | `MarketOverview.investor_flows` 필드 + `_get_kr_market_investor_flows` + `get_market_overview` KR 브랜치 | 수집 훅 |
| Create: `tests/test_kr_market_flows_wiring.py` | 수집 배선 오프라인 테스트(KR 채움 / 비KR None / fail-open) | 신규 |
| Modify: `src/market_analyzer.py` | `_kr_market_flow_lines`·`_kr_market_flows_asof`·`_build_kr_market_flows_prompt_block` + `_build_review_prompt` KR stats_block 대체 | 프롬프트 주입 |
| Create: `tests/test_kr_market_flows_prompt.py` | 프롬프트 섹션 렌더 + 비KR 불변 테스트 | 신규 |
| Modify: `src/market_analyzer.py` | `_build_kr_market_flows_block` + `_inject_data_into_review` 주입(시장 요약 뒤, fallback append) | 결정적 렌더 |
| Create: `tests/test_kr_market_flows_report.py` | 결정적 블록 zh/en/ko + 주입 + 무데이터 생략 테스트 | 신규 |
| Modify: `src/market_analyzer.py` | `build_market_review_payload`에 `investor_flows` 구조화 키 | 페이로드 |
| Create: `tests/test_kr_market_flows_payload.py` | 페이로드 키 존재(KR) / 부재(비KR) 테스트 | 신규 |
| Modify: `docs/market-support.md` | KR 마켓 리뷰 수급 지원 기재 | 문서 |
| Modify: `docs/CHANGELOG.md` | `[Unreleased]` 플랫 항목 | 문서 |

**설계 노트 (스펙 §9 확정 사항 — 탐색으로 확정):**
- **수집 훅 = `src/market_analyzer.py:get_market_overview()`** (indices/stats/sector 수집 뒤, `return overview` 직전). KR은 `self.region == "kr"`로 판별한다. fetcher 호출은 `self.data_manager.get_kr_market_investor_flows(...)`를 경유(별도 파이프라인 단계 불필요).
- **agent/multi-agent 경로 무관** — 마켓 리뷰는 종목 분석의 agent executor 경로와 분리된 `MarketAnalyzer` 단일 경로다. `overview`가 프롬프트·렌더·페이로드에 모두 흐르므로 per-agent 배선 없음.
- **KR 프로파일 = `has_market_stats=False`, `has_sector_rankings=False`** (`src/core/market_profile.py:82`). 따라서 KR 리뷰엔 `_build_stats_block`(결정적)·stats_block(프롬프트)이 원래 비어있거나 "데이터 없음" 문구다 — 수급이 그 자리를 채운다.
- **중국어 거부 게이트 순서** — `generate_market_review`에서 게이트(`has_disallowed_report_script`, 826)가 LLM `review`를 검사한 **뒤** `_inject_data_into_review`(864)가 결정적 블록을 붙인다. 결정적 블록은 게이트를 우회하므로 ko 블록은 반드시 순수 한글이어야 한다(위 Global Constraints).

---

### Task 1: KRW 로케일 포맷터 — `format_net_krw_localized` (ko 억 / zh 亿韩元 / en ₩…B)

**Files:**
- Modify: `src/report_language.py` (모듈 레벨 함수 신규, 기존 `has_disallowed_report_script` 근처)
- Create: `tests/test_kr_market_flows_format.py`

**Interfaces:**
- Consumes: 기존 `normalize_report_language(value, default="zh")`(같은 모듈), 순매수 금액 `int`(원 단위)
- Produces (Task 3·4가 사용): `format_net_krw_localized(value: Any, language: Optional[str]) -> str` — 부호 붙은 로케일 KRW 문자열. `None`/NaN/비수치 → `"N/A"`.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_market_flows_format.py` 생성:

```python
# -*- coding: utf-8 -*-
"""Phase 3: KRW 순매수 금액을 로케일 단위로 포맷하는 format_net_krw_localized 고정.

시장 수급 레코드는 KRW 원 단위(unit="KRW")이므로 억(1e8)/십억(1e9) 스케일로만
표기한다. 완전 오프라인 — `pytest -m "not network"` 차단 게이트에 포함된다.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.report_language import format_net_krw_localized


class TestFormatNetKrwLocalized:
    def test_ko_uses_eok(self):
        # -3.228e11 원 -> -3,228억 (스펙 §4 예시)
        assert format_net_krw_localized(-322800000000, "ko") == "-3,228억"
        assert format_net_krw_localized(1131400000000, "ko") == "+11,314억"
        assert format_net_krw_localized(51200000000, "ko") == "+512억"

    def test_zh_uses_yi_won(self):
        assert format_net_krw_localized(-322800000000, "zh") == "-3,228亿韩元"
        assert format_net_krw_localized(1131400000000, "zh") == "+11,314亿韩元"

    def test_en_uses_won_billions(self):
        assert format_net_krw_localized(-322800000000, "en") == "₩-322.8B"
        assert format_net_krw_localized(1131400000000, "en") == "₩+1,131.4B"

    def test_invalid_is_na(self):
        assert format_net_krw_localized(None, "ko") == "N/A"
        assert format_net_krw_localized("x", "en") == "N/A"
        assert format_net_krw_localized(float("nan"), "zh") == "N/A"

    def test_none_language_defaults_to_zh(self):
        assert format_net_krw_localized(51200000000, None) == "+512亿韩元"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_market_flows_format.py -v`
Expected: FAIL — `ImportError: cannot import name 'format_net_krw_localized'`

- [ ] **Step 3: 최소 구현** — `src/report_language.py`에 `has_disallowed_report_script`(현재 779-785) **다음**에 함수 추가:

```python
def format_net_krw_localized(value: Any, language: Optional[str]) -> str:
    """부호 붙은 순매수 금액(원)을 로케일 단위로 포맷(+ = 순매수).

    시장 수급 레코드는 원 단위 대금이 커서 억(1e8)/십억(1e9) 스케일로만 표기한다.
      - ko: `억`  (예: -3,228억)
      - zh: `亿韩元` (예: -3,228亿韩元)
      - en: `₩…B` 십억 원 (예: ₩-322.8B)
    None/NaN/비수치 -> "N/A". language None/미지원 -> zh 기본(normalize_report_language).
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if amount != amount:  # NaN
        return "N/A"
    sign = "+" if amount > 0 else ("-" if amount < 0 else "")
    a = abs(amount)
    lang = normalize_report_language(language)
    if lang == "en":
        return f"₩{sign}{a / 1e9:,.1f}B"
    if lang == "ko":
        return f"{sign}{a / 1e8:,.0f}억"
    return f"{sign}{a / 1e8:,.0f}亿韩元"
```

(주의: `Any`, `Optional`는 `report_language.py` 상단 typing import에 이미 있는지 확인 — 없으면 추가. `normalize_report_language`는 같은 모듈 함수다.)

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_market_flows_format.py -v`
Expected: PASS (5 passed)

Run: `uv run python -m py_compile src/report_language.py`
Expected: 출력 없음

- [ ] **Step 5: 커밋**

```bash
git add src/report_language.py tests/test_kr_market_flows_format.py
git commit -m "feat: add KRW locale formatter for KR market investor flows"
```

---

### Task 2: 수집 훅 — `MarketOverview.investor_flows` + fetcher 배선 (fail-open)

**Files:**
- Modify: `data_provider/base.py` (`DataFetcherManager`에 메서드 추가)
- Modify: `src/market_analyzer.py` (`MarketOverview` 필드 + `_get_kr_market_investor_flows` + `get_market_overview` 브랜치)
- Create: `tests/test_kr_market_flows_wiring.py`

**Interfaces:**
- Consumes: `KrInstitutionalFetcher.get_market_investor_flows(market, days=5) -> Optional[dict]` (Phase 1), `self.data_manager`(MarketAnalyzer), `self._kr_institutional_fetcher`(Phase 2 lazy 싱글턴, base.py:3073)
- Produces (Task 3·4·5가 사용): `MarketOverview.investor_flows: Optional[Dict[str, Any]]` = `{"kospi": rec, "kosdaq": rec}`(데이터 있는 시장만) 또는 `None`. rec shape: `{"market","unit":"KRW","days":[{date,foreign_net,institution_net,individual_net}],"summary":{"foreign_net_5d","institution_net_5d"},"source":"NAVER"}`. `DataFetcherManager.get_kr_market_investor_flows(market: str, days: int=5) -> Optional[Dict[str, Any]]`.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_market_flows_wiring.py` 생성:

```python
# -*- coding: utf-8 -*-
"""Phase 3 수집 배선: KR 시장 수급을 MarketOverview.investor_flows에 fail-open으로
싣는지 고정한다.

계약:
  - DataFetcherManager.get_kr_market_investor_flows: fetcher 성공 -> 레코드, 실패 -> None
  - MarketAnalyzer(region="kr").get_market_overview() -> overview.investor_flows =
    {"kospi": rec, "kosdaq": rec} (데이터 있는 시장만)
  - fetcher가 None/raise -> 해당 시장 생략, 둘 다 없으면 investor_flows=None
  - 비KR 리뷰(us 등) -> investor_flows None AND fetcher 미호출 (엄격 additive)

완전 오프라인 — `_get_main_indices`를 목킹해 네트워크를 차단한다.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_analyzer import MarketAnalyzer

_KOSPI_REC = {
    "market": "kospi", "unit": "KRW",
    "days": [{"date": "2026-07-10", "foreign_net": -322800000000, "institution_net": 1131400000000, "individual_net": -808600000000}],
    "summary": {"foreign_net_5d": -322800000000, "institution_net_5d": 1131400000000},
    "source": "NAVER",
}
_KOSDAQ_REC = {
    "market": "kosdaq", "unit": "KRW",
    "days": [{"date": "2026-07-10", "foreign_net": 51200000000, "institution_net": -28700000000, "individual_net": -22500000000}],
    "summary": {"foreign_net_5d": 51200000000, "institution_net_5d": -28700000000},
    "source": "NAVER",
}

_CFG = SimpleNamespace(report_language="ko")


def _fake_flows(market, days=5):
    return {"kospi": dict(_KOSPI_REC), "kosdaq": dict(_KOSDAQ_REC)}.get(market)


class TestKrMarketFlowsWiring(unittest.TestCase):
    def _analyzer(self, region):
        return MarketAnalyzer(region=region, analyzer=None, config=_CFG)

    def test_kr_overview_populated(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", side_effect=_fake_flows) as m:
            overview = an.get_market_overview()
        self.assertIsInstance(overview.investor_flows, dict)
        self.assertEqual(overview.investor_flows["kospi"]["summary"]["institution_net_5d"], 1131400000000)
        self.assertEqual(overview.investor_flows["kosdaq"]["source"], "NAVER")
        self.assertEqual(m.call_count, 2)

    def test_kr_one_market_missing(self):
        an = self._analyzer("kr")
        def _only_kospi(market, days=5):
            return dict(_KOSPI_REC) if market == "kospi" else None
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", side_effect=_only_kospi):
            overview = an.get_market_overview()
        self.assertEqual(set(overview.investor_flows), {"kospi"})

    def test_kr_all_fail_is_none(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", return_value=None):
            overview = an.get_market_overview()
        self.assertIsNone(overview.investor_flows)

    def test_kr_fail_open_when_fetcher_raises(self):
        an = self._analyzer("kr")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", side_effect=RuntimeError("boom")):
            overview = an.get_market_overview()  # must NOT raise
        self.assertIsNone(overview.investor_flows)

    def test_non_kr_untouched_and_fetcher_not_called(self):
        an = self._analyzer("us")
        with patch.object(an, "_get_main_indices", return_value=[]), \
                patch.object(an.data_manager, "get_kr_market_investor_flows", side_effect=_fake_flows) as m:
            overview = an.get_market_overview()
        self.assertIsNone(overview.investor_flows)
        self.assertEqual(m.call_count, 0)

    def test_data_manager_method_fail_open(self):
        # base.py 메서드 자체가 fetcher 예외를 삼키는지 (레코드/None 계약)
        from data_provider.base import DataFetcherManager
        manager = DataFetcherManager(fetchers=[])
        with patch(
            "data_provider.kr_institutional_fetcher.KrInstitutionalFetcher.get_market_investor_flows",
            return_value=dict(_KOSPI_REC),
        ):
            self.assertEqual(manager.get_kr_market_investor_flows("kospi")["source"], "NAVER")
        with patch(
            "data_provider.kr_institutional_fetcher.KrInstitutionalFetcher.get_market_investor_flows",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(manager.get_kr_market_investor_flows("kospi"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_market_flows_wiring.py -v`
Expected: FAIL — `AttributeError: 'DataFetcherManager' object has no attribute 'get_kr_market_investor_flows'` / `overview.investor_flows` 없음.

- [ ] **Step 3a: `DataFetcherManager` 메서드 추가** — `data_provider/base.py`의 `DataFetcherManager` 클래스 내부(예: 기존 `get_main_indices`(2490) 근처)에 추가:

```python
    def get_kr_market_investor_flows(self, market: str, days: int = 5) -> Optional[Dict[str, Any]]:
        """KOSPI/KOSDAQ 시장 전체 투자자 수급(KRW) — KR 마켓 리뷰용, fail-open.

        Phase 1 KrInstitutionalFetcher.get_market_investor_flows를 lazy 싱글턴으로
        호출한다(종목 수급 훅과 동일 인스턴스 self._kr_institutional_fetcher 재사용).
        비대상/실패/예외 시 None (절대 raise하지 않음).
        """
        try:
            fetcher = getattr(self, "_kr_institutional_fetcher", None)
            if fetcher is None:
                from data_provider.kr_institutional_fetcher import KrInstitutionalFetcher

                fetcher = KrInstitutionalFetcher()
                self._kr_institutional_fetcher = fetcher
            return fetcher.get_market_investor_flows(market, days=days)
        except Exception as exc:  # noqa: BLE001 - fail-open
            logger.warning("[kr-flows] get_kr_market_investor_flows(%s) fail-open: %s", market, exc)
            return None
```

(주의: `Optional`, `Dict`, `Any`, `logger`는 base.py에 이미 있다(Phase 2에서 사용). 없으면 추가.)

- [ ] **Step 3b: `MarketOverview` 필드 추가** — `src/market_analyzer.py`의 `MarketOverview` 데이터클래스(현재 100-116)에서 마지막 필드 `bottom_concepts` **다음 줄**에 추가:

```python
    # KR 시장 수급(외국인/기관/개인, KRW) — {"kospi": rec, "kosdaq": rec}; 비KR은 None
    investor_flows: Optional[Dict[str, Any]] = None
```

(`Optional`, `Dict`, `Any`는 market_analyzer.py:19에 이미 import됨.)

- [ ] **Step 3c: 수집 헬퍼 + `get_market_overview` 브랜치** — `src/market_analyzer.py`. 먼저 `get_market_overview`(현재 525-550)의 `return overview`(550) **직전**에 KR 브랜치 추가:

```python
        # 5. KR 시장 수급(외국인/기관, KRW) — kr 전용, fail-open
        if self.region == "kr":
            overview.investor_flows = self._get_kr_market_investor_flows()

        return overview
```

그리고 `get_market_overview` 메서드 **다음**에 헬퍼 추가:

```python
    def _get_kr_market_investor_flows(self) -> Optional[Dict[str, Any]]:
        """KOSPI/KOSDAQ 시장 수급 레코드 수집 — kr 전용, 전면 fail-open.

        {"kospi": rec, "kosdaq": rec}(데이터 있는 시장만) 반환. 둘 다 없으면 None.
        어떤 예외도 삼켜 마켓 리뷰 메인 흐름을 중단시키지 않는다.
        """
        flows: Dict[str, Any] = {}
        for market_key in ("kospi", "kosdaq"):
            try:
                rec = self.data_manager.get_kr_market_investor_flows(market_key, days=5)
            except Exception as exc:  # noqa: BLE001 - fail-open
                logger.warning(
                    "[大盘] %s action=kr_market_flows market=%s status=fail-open error=%s",
                    self._log_context(), market_key, exc,
                )
                rec = None
            if isinstance(rec, dict) and rec.get("days"):
                flows[market_key] = rec
        return flows or None
```

(`logger`는 market_analyzer.py에 이미 있다.)

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_market_flows_wiring.py -v`
Expected: PASS (6 passed)

Run: `uv run python -m py_compile data_provider/base.py src/market_analyzer.py`
Expected: 출력 없음

- [ ] **Step 5: 커밋**

```bash
git add data_provider/base.py src/market_analyzer.py tests/test_kr_market_flows_wiring.py
git commit -m "feat: collect KR market investor flows into MarketOverview (fail-open)"
```

---

### Task 3: LLM 프롬프트 주입 — KR 시장 수급 섹션 (로케일 zh/en/ko)

**Files:**
- Modify: `src/market_analyzer.py` (공유 라인 렌더러 + 프롬프트 블록 + `_build_review_prompt` KR stats_block 대체)
- Create: `tests/test_kr_market_flows_prompt.py`

**Interfaces:**
- Consumes: Task 1 `format_net_krw_localized`, Task 2 `overview.investor_flows`
- Produces (Task 4가 라인 렌더러 재사용): `_kr_market_flow_lines(overview, language) -> List[str]`, `_kr_market_flows_asof(overview) -> str`, `_build_kr_market_flows_prompt_block(overview, review_language) -> str`
- 배치: `_build_review_prompt`의 stats_block/sector_block 언어 분기(현재 1682-1746) **다음**, `data_no_indices_hint`(1748) **직전**에 KR 대체 훅 1개. KR은 `has_market_stats=False`라 stats_block이 "데이터 없음" 문구이므로 실제 수급으로 대체한다(비KR 미진입 → 바이트 동일).

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_market_flows_prompt.py` 생성:

```python
# -*- coding: utf-8 -*-
"""Phase 3: 마켓 리뷰 LLM 프롬프트에 KR 시장 수급 섹션이 로케일별로 주입되는지 고정.

KR은 has_market_stats=False라 수급이 곧 시장 폭 신호 — KR stats_block을 수급으로
대체한다. 비KR(us 등) 프롬프트는 불변(엄격 additive). 완전 오프라인.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_analyzer import MarketAnalyzer, MarketOverview

_FLOWS = {
    "kospi": {
        "market": "kospi", "unit": "KRW",
        "days": [{"date": "2026-07-10", "foreign_net": -322800000000, "institution_net": 1131400000000, "individual_net": -808600000000}],
        "summary": {"foreign_net_5d": -322800000000, "institution_net_5d": 1131400000000},
        "source": "NAVER",
    },
    "kosdaq": {
        "market": "kosdaq", "unit": "KRW",
        "days": [{"date": "2026-07-10", "foreign_net": 51200000000, "institution_net": -28700000000, "individual_net": -22500000000}],
        "summary": {"foreign_net_5d": 51200000000, "institution_net_5d": -28700000000},
        "source": "NAVER",
    },
}


def _overview(flows=None):
    ov = MarketOverview(date="2026-07-10")
    ov.investor_flows = flows
    return ov


def _analyzer(language, region="kr"):
    return MarketAnalyzer(region=region, analyzer=None, config=SimpleNamespace(report_language=language))


class TestKrMarketFlowsPromptBlock:
    def test_ko_block_present_and_hangul_only(self):
        an = _analyzer("ko")
        block = an._build_kr_market_flows_prompt_block(_overview(_FLOWS), "ko")
        assert "KOSPI" in block and "KOSDAQ" in block
        assert "외국인" in block and "기관" in block
        assert "-3,228억" in block and "+11,314억" in block
        assert "2026-07-10" in block
        # ko 프롬프트 블록엔 한자가 없어야 한다(거부 게이트 안전)
        assert not any("一" <= c <= "鿿" for c in block)

    def test_en_block_present(self):
        an = _analyzer("en")
        block = an._build_kr_market_flows_prompt_block(_overview(_FLOWS), "en")
        assert "₩-322.8B" in block and "Foreign" in block

    def test_zh_block_present(self):
        an = _analyzer("zh")
        block = an._build_kr_market_flows_prompt_block(_overview(_FLOWS), "zh")
        assert "亿韩元" in block and "外国人" in block

    def test_block_empty_without_data(self):
        an = _analyzer("ko")
        assert an._build_kr_market_flows_prompt_block(_overview(None), "ko") == ""
        assert an._build_kr_market_flows_prompt_block(_overview({}), "ko") == ""


class TestBuildReviewPromptIntegration:
    def test_kr_prompt_includes_flows(self):
        an = _analyzer("ko")
        prompt = an._build_review_prompt(_overview(_FLOWS), [])
        assert "시장 투자자 수급" in prompt
        assert "+11,314억" in prompt

    def test_non_kr_prompt_has_no_flows(self):
        an = _analyzer("ko", region="us")
        prompt = an._build_review_prompt(_overview(_FLOWS), [])
        # us는 KR 브랜치 미진입 -> 수급 섹션 없음
        assert "시장 투자자 수급" not in prompt
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_market_flows_prompt.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_build_kr_market_flows_prompt_block'`

- [ ] **Step 3a: 공유 라인 렌더러 + 프롬프트 블록** — `src/market_analyzer.py`의 `_build_stats_block`(현재 1093) **앞**(예: `_inject_data_into_review` 근처)에 메서드 3개 추가:

```python
    def _kr_market_flow_lines(self, overview: MarketOverview, language: str) -> List[str]:
        """KOSPI/KOSDAQ 시장 수급을 로케일 라인 리스트로. 데이터 없으면 [].

        각 라인: `- KOSPI: 외국인 -3,228억 / 기관 +11,314억 · NAVER`.
        개인(individual)은 제외(외국인·기관 역방향 중복). 두 시장 모두 없으면 [].
        """
        records = overview.investor_flows if isinstance(overview.investor_flows, dict) else {}
        if language == "en":
            foreign_label, inst_label = "Foreign", "Institutions"
        elif language == "ko":
            foreign_label, inst_label = "외국인", "기관"
        else:
            foreign_label, inst_label = "外国人", "机构"
        lines: List[str] = []
        for market_key, market_name in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
            rec = records.get(market_key)
            if not isinstance(rec, dict):
                continue
            days = rec.get("days")
            if not isinstance(days, list) or not days:
                continue
            summary = rec.get("summary") if isinstance(rec.get("summary"), dict) else {}
            foreign = format_net_krw_localized(summary.get("foreign_net_5d"), language)
            institution = format_net_krw_localized(summary.get("institution_net_5d"), language)
            if foreign == "N/A" and institution == "N/A":
                continue
            source = str(rec.get("source") or "").strip() or "N/A"
            lines.append(
                f"- {market_name}: {foreign_label} {foreign} / {inst_label} {institution} · {source}"
            )
        return lines

    @staticmethod
    def _kr_market_flows_asof(overview: MarketOverview) -> str:
        """수급 최신 확정 거래일(두 시장 중 최신). 없으면 ""."""
        records = overview.investor_flows if isinstance(overview.investor_flows, dict) else {}
        dates: List[str] = []
        for market_key in ("kospi", "kosdaq"):
            rec = records.get(market_key)
            if isinstance(rec, dict):
                days = rec.get("days")
                if isinstance(days, list) and days and isinstance(days[0], dict):
                    date = days[0].get("date")
                    if date:
                        dates.append(str(date))
        return max(dates) if dates else ""

    def _build_kr_market_flows_prompt_block(
        self, overview: MarketOverview, review_language: str
    ) -> str:
        """KR 시장 수급 -> LLM 프롬프트 섹션(로케일). 데이터 없으면 ""."""
        lines = self._kr_market_flow_lines(overview, review_language)
        if not lines:
            return ""
        date = self._kr_market_flows_asof(overview) or "N/A"
        if review_language == "en":
            heading = "## Market Investor Flows (KOSPI/KOSDAQ)"
            guide = (
                f"(5-day cumulative net buy in KRW, as of {date}. Investor flows are an "
                "auxiliary market-breadth signal, not a standalone trade decision.)"
            )
        elif review_language == "ko":
            heading = "## 시장 투자자 수급 (KOSPI/KOSDAQ)"
            guide = (
                f"(5일 누적 순매수, 원 단위, {date} 기준. 투자자 수급은 시장 폭을 보는 "
                "보조 신호이며 단독 매매 판단 근거가 아닙니다.)"
            )
        else:
            heading = "## 市场投资者动向 (KOSPI/KOSDAQ)"
            guide = (
                f"（5日累计净买卖，韩元单位，截至{date}。投资者动向是市场宽度的辅助信号，"
                "不作为独立交易决策依据。）"
            )
        return "\n".join([heading, guide, ""] + lines)
```

(주의: `format_net_krw_localized`를 파일 상단 `from src.report_language import (...)` 블록(24-29)에 추가한다.)

- [ ] **Step 3b: `_build_review_prompt` KR stats_block 대체** — `src/market_analyzer.py`의 stats_block/sector_block 언어 분기가 끝나는 지점(현재 1746, zh `data_limits_block` 조립 뒤) **다음**, `data_no_indices_hint =`(1748) **직전**에 삽입:

```python
        # KR: 수급이 곧 시장 폭 신호 — "데이터 없음" stats_block을 실제 수급으로 대체.
        # region 가드로 비KR은 미진입(바이트 동일). 데이터 없으면 기존 문구 유지.
        if self.region == "kr":
            kr_flows_prompt = self._build_kr_market_flows_prompt_block(overview, review_language)
            if kr_flows_prompt:
                stats_block = kr_flows_prompt

```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_market_flows_prompt.py -v`
Expected: PASS (6 passed)

Run: `uv run python -m py_compile src/market_analyzer.py`
Expected: 출력 없음

- [ ] **Step 5: 커밋**

```bash
git add src/market_analyzer.py tests/test_kr_market_flows_prompt.py
git commit -m "feat: inject KR market investor flows into market review prompt"
```

---

### Task 4: 결정적 리포트 본문 렌더 — `_inject_data_into_review` 수급 블록 (zh/en/ko)

**Files:**
- Modify: `src/market_analyzer.py` (`_build_kr_market_flows_block` + `_inject_data_into_review` 주입)
- Create: `tests/test_kr_market_flows_report.py`

**Interfaces:**
- Consumes: Task 3 `_kr_market_flow_lines`/`_kr_market_flows_asof`, `self._get_review_language()`, 기존 `_insert_after_section`, `_KOREAN/ENGLISH/CHINESE_SECTION_PATTERNS`
- Produces: `_build_kr_market_flows_block(overview) -> str` (결정적 블록, `self` 언어 사용). `_inject_data_into_review`가 `시장 요약`(market_summary) 섹션 뒤에 주입, 헤딩 미탐지 시 fallback append.
- 게이트 순서 주의: 이 주입은 `generate_market_review`의 중국어 거부 게이트 **이후**에 실행되므로 ko 블록은 순수 한글이어야 한다(Task 3의 라인 렌더러가 이미 보장 — ko는 외국인/기관/억).

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_market_flows_report.py` 생성:

```python
# -*- coding: utf-8 -*-
"""Phase 3: 마켓 리뷰 리포트 본문에 KR 시장 수급 결정적 블록이 주입되는지 고정.

_build_kr_market_flows_block(로케일) + _inject_data_into_review(시장 요약 섹션 뒤,
헤딩 없으면 fallback append). ko 블록은 순수 한글(거부 게이트 안전). 오프라인.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_analyzer import MarketAnalyzer, MarketOverview

_FLOWS = {
    "kospi": {
        "market": "kospi", "unit": "KRW",
        "days": [{"date": "2026-07-10", "foreign_net": -322800000000, "institution_net": 1131400000000, "individual_net": -808600000000}],
        "summary": {"foreign_net_5d": -322800000000, "institution_net_5d": 1131400000000},
        "source": "NAVER",
    },
    "kosdaq": {
        "market": "kosdaq", "unit": "KRW",
        "days": [{"date": "2026-07-10", "foreign_net": 51200000000, "institution_net": -28700000000, "individual_net": -22500000000}],
        "summary": {"foreign_net_5d": 51200000000, "institution_net_5d": -28700000000},
        "source": "NAVER",
    },
}


def _overview(flows=None):
    ov = MarketOverview(date="2026-07-10")
    ov.investor_flows = flows
    return ov


def _analyzer(language):
    return MarketAnalyzer(region="kr", analyzer=None, config=SimpleNamespace(report_language=language))


class TestBuildKrMarketFlowsBlock:
    def test_ko_block_hangul_only(self):
        block = _analyzer("ko")._build_kr_market_flows_block(_overview(_FLOWS))
        assert "시장 투자자 수급" in block
        assert "KOSPI" in block and "-3,228억" in block
        assert "KOSDAQ" in block and "+512억" in block
        assert "2026-07-10" in block
        assert not any("一" <= c <= "鿿" for c in block)

    def test_en_and_zh_block(self):
        assert "₩-322.8B" in _analyzer("en")._build_kr_market_flows_block(_overview(_FLOWS))
        assert "亿韩元" in _analyzer("zh")._build_kr_market_flows_block(_overview(_FLOWS))

    def test_empty_without_data(self):
        assert _analyzer("ko")._build_kr_market_flows_block(_overview(None)) == ""


class TestInjectFlowsIntoReview:
    def test_inject_after_market_summary_heading(self):
        an = _analyzer("ko")
        review = "## 2026-07-10 리뷰\n\n### 1. 시장 요약\n오늘은 혼조였습니다.\n\n### 2. 지수 구조\n지수 설명.\n"
        out = an._inject_data_into_review(review, _overview(_FLOWS))
        assert "시장 투자자 수급" in out
        # 시장 요약 섹션 안(지수 구조 앞)에 주입
        assert out.index("시장 투자자 수급") < out.index("### 2. 지수 구조")

    def test_fallback_append_when_heading_missing(self):
        an = _analyzer("ko")
        review = "## 2026-07-10 리뷰\n\n본문만 있고 표준 헤딩이 없습니다.\n"
        out = an._inject_data_into_review(review, _overview(_FLOWS))
        assert "시장 투자자 수급" in out
        assert "-3,228억" in out

    def test_no_injection_without_data(self):
        an = _analyzer("ko")
        review = "## 2026-07-10 리뷰\n\n### 1. 시장 요약\n내용.\n"
        out = an._inject_data_into_review(review, _overview(None))
        assert "시장 투자자 수급" not in out
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_market_flows_report.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_build_kr_market_flows_block'`

- [ ] **Step 3a: 결정적 블록 렌더러** — `src/market_analyzer.py`의 `_build_stats_block`(현재 1093) **앞**(Task 3에서 추가한 메서드들 근처)에 추가:

```python
    def _build_kr_market_flows_block(self, overview: MarketOverview) -> str:
        """KR 시장 수급 결정적 블록(로케일) — 리포트 본문 주입용. 데이터 없으면 "".

        `시장 요약` 섹션에 삽입되므로 별도 ### 헤딩 없이 볼드 헤더 + 시장별 라인.
        _get_review_language()를 쓰며, ko는 순수 한글(거부 게이트 이후 주입 안전).
        """
        language = self._get_review_language()
        lines = self._kr_market_flow_lines(overview, language)
        if not lines:
            return ""
        date = self._kr_market_flows_asof(overview) or "N/A"
        window = 5
        if language == "en":
            head = f"**Market Investor Flows** ({window}d · as of {date})"
        elif language == "ko":
            head = f"**시장 투자자 수급**({window}일 · {date} 기준)"
        else:
            head = f"**市场投资者动向**（{window}日 · 截至{date}）"
        return "\n".join([head, ""] + lines)
```

- [ ] **Step 3b: `_inject_data_into_review` 주입** — `src/market_analyzer.py`의 `_inject_data_into_review`(현재 1023-1072)에서 sector_block 처리 블록(1056-1070) **다음**, `return review`(1072) **직전**에 삽입:

```python
        flows_block = self._build_kr_market_flows_block(overview)
        if flows_block:
            original_review = review
            review = self._insert_after_section(
                review,
                patterns["market_summary"],
                flows_block,
            )
            if review == original_review and flows_block not in review:
                fallback_headings = {
                    "en": "### 1. Market Summary",
                    "ko": "### 1. 시장 요약",
                    "zh": "### 一、盘面总览",
                }
                fallback_heading = fallback_headings[language]
                review = f"{review.rstrip()}\n\n{fallback_heading}\n{flows_block}\n"

        return review
```

(`patterns`·`language`는 이 메서드 상단 1034-1040에서 이미 정의됨.)

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_market_flows_report.py -v`
Expected: PASS (6 passed)

Run: `uv run python -m py_compile src/market_analyzer.py`
Expected: 출력 없음

- [ ] **Step 5: 커밋**

```bash
git add src/market_analyzer.py tests/test_kr_market_flows_report.py
git commit -m "feat: render KR market investor flows line in market review body"
```

---

### Task 5: 구조화 페이로드 — `build_market_review_payload`에 `investor_flows` 키

**Files:**
- Modify: `src/market_analyzer.py` (`build_market_review_payload`)
- Create: `tests/test_kr_market_flows_payload.py`

**Interfaces:**
- Consumes: Task 2 `overview.investor_flows`
- Produces: 페이로드 `payload["investor_flows"] = {"kospi": rec, "kosdaq": rec}`(데이터 있는 시장만). 데이터 없으면 키 부재(비KR 포함). 웹/데스크톱/푸시 구조화 소비자용 원시 레코드 — 마크다운 본문(Task 4)과 별개.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_market_flows_payload.py` 생성:

```python
# -*- coding: utf-8 -*-
"""Phase 3: 구조화 마켓 리뷰 페이로드에 KR 시장 수급 원시 레코드가 실리는지 고정.

KR + 데이터 -> payload["investor_flows"], 비KR/무데이터 -> 키 부재. 오프라인.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_analyzer import MarketAnalyzer, MarketOverview

_FLOWS = {
    "kospi": {
        "market": "kospi", "unit": "KRW",
        "days": [{"date": "2026-07-10", "foreign_net": -322800000000, "institution_net": 1131400000000, "individual_net": -808600000000}],
        "summary": {"foreign_net_5d": -322800000000, "institution_net_5d": 1131400000000},
        "source": "NAVER",
    },
    "kosdaq": {
        "market": "kosdaq", "unit": "KRW",
        "days": [{"date": "2026-07-10", "foreign_net": 51200000000, "institution_net": -28700000000, "individual_net": -22500000000}],
        "summary": {"foreign_net_5d": 51200000000, "institution_net_5d": -28700000000},
        "source": "NAVER",
    },
}


def _overview(flows=None):
    ov = MarketOverview(date="2026-07-10")
    ov.investor_flows = flows
    return ov


def _analyzer(region="kr"):
    return MarketAnalyzer(region=region, analyzer=None, config=SimpleNamespace(report_language="ko"))


class TestPayloadInvestorFlows:
    def test_kr_payload_has_flows(self):
        payload = _analyzer("kr").build_market_review_payload(_overview(_FLOWS), [], "## 리뷰\n본문")
        assert "investor_flows" in payload
        assert set(payload["investor_flows"]) == {"kospi", "kosdaq"}
        assert payload["investor_flows"]["kospi"]["summary"]["institution_net_5d"] == 1131400000000

    def test_kr_payload_only_available_market(self):
        payload = _analyzer("kr").build_market_review_payload(
            _overview({"kospi": _FLOWS["kospi"]}), [], "## 리뷰\n본문"
        )
        assert set(payload["investor_flows"]) == {"kospi"}

    def test_no_flows_key_without_data(self):
        payload = _analyzer("kr").build_market_review_payload(_overview(None), [], "## 리뷰\n본문")
        assert "investor_flows" not in payload

    def test_non_kr_has_no_flows_key(self):
        # 비KR overview는 investor_flows None -> 키 부재
        payload = _analyzer("us").build_market_review_payload(_overview(None), [], "## Review\nbody")
        assert "investor_flows" not in payload
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_market_flows_payload.py -v`
Expected: FAIL — `KeyError`/`assert "investor_flows" in payload` 실패.

- [ ] **Step 3: 최소 구현** — `src/market_analyzer.py`의 `build_market_review_payload`(현재 890-962)에서 breadth 조립(951-960) **다음**, `return payload`(962) **직전**에 삽입:

```python
        flows_payload = {}
        if isinstance(overview.investor_flows, dict):
            for market_key in ("kospi", "kosdaq"):
                rec = overview.investor_flows.get(market_key)
                if isinstance(rec, dict) and rec.get("days"):
                    flows_payload[market_key] = rec
        if flows_payload:
            payload["investor_flows"] = flows_payload

        return payload
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_market_flows_payload.py -v`
Expected: PASS (4 passed)

Run: `uv run python -m py_compile src/market_analyzer.py`
Expected: 출력 없음

- [ ] **Step 5: 커밋**

```bash
git add src/market_analyzer.py tests/test_kr_market_flows_payload.py
git commit -m "feat: add KR market investor flows to market review payload"
```

---

### Task 6: 문서 — market-support.md + CHANGELOG

**Files:**
- Modify: `docs/market-support.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:** 없음 (문서 전용, 테스트 미실행)

- [ ] **Step 1: market-support.md 갱신** — **구현 전에 `docs/market-support.md`의 실제 구조를 읽고** KR 마켓 리뷰 항목에 "시장 전체 투자자별 수급(외국인/기관, KRW, KOSPI/KOSDAQ)" 지원을 기존 서술 스타일로 추가한다. 무인증 소스(네이버 단일)·fail-open·기준일(최신 확정 거래일) 규칙을 1~2줄로 명시한다. Phase 2에서 KR **종목** 수급을 이미 기재했다면, KR **마켓 리뷰** 수급을 별도로 구분해 추가한다.

- [ ] **Step 2: CHANGELOG 갱신** — `docs/CHANGELOG.md`의 `## [Unreleased]` 항목 목록에 플랫 1줄 추가(`### 类目标题` 신설 금지):

```markdown
- [新功能] KR 마켓 리뷰에 시장 전체 투자자별 수급(외국인/기관, KOSPI/KOSDAQ, KRW) 연동: LLM 프롬프트·리뷰 본문 결정적 라인·구조화 페이로드에 반영 — fail-open, 비KR 리뷰 바이트 동일, 중국어 거부 게이트 통과(ko 순수 한글).
```

- [ ] **Step 3: 문서 일관성 확인** — 추가한 파일명·소스명·동작 서술이 실제 구현(Task 1~5)과 일치하는지 대조한다. 중영 이중 문서가 있으면(예: `docs/market-support.*` 영문본) 동기화 필요 여부를 판단하고, 미동기화 시 교부 설명에 이유를 남긴다.

- [ ] **Step 4: 커밋**

```bash
git add docs/market-support.md docs/CHANGELOG.md
git commit -m "docs: document KR market review investor flows support"
```

---

### Task 7: 통합 게이트 + 회귀 검증

**Files:** 없음 (검증 전용)

- [ ] **Step 1: 오프라인 전체 게이트**

Run: `uv run pytest -m "not network" -q`
Expected: 기존 전체 스위트 + 신규 테스트(format/wiring/prompt/report/payload) 전부 PASS, 실패 0. 특히 마켓 리뷰 회귀(`tests/test_market_review.py`, `tests/test_market_analyzer_generate_text.py`)가 통과하는지 확인 — 비KR 프롬프트/리포트 불변.

- [ ] **Step 2: lint/CI 게이트**

Run: `uv run ./scripts/ci_gate.sh`
Expected: exit 0 (flake8 포함 통과). 미사용 import(F401)·라인 길이 위반 없는지 확인.

- [ ] **Step 3: 변경 파일 컴파일 확인**

Run: `uv run python -m py_compile src/report_language.py data_provider/base.py src/market_analyzer.py`
Expected: 출력 없음

- [ ] **Step 4: (선택, 네트워크 있으면) 실 KR 마켓 리뷰 스모크** — `MARKET_REVIEW_REGION=kr uv run python main.py --market-review --dry-run` 계열로 KR 마켓 리뷰가 예외 없이 완료되고 리포트에 시장 수급 라인이 나오는지 육안 확인. PR 설명에 리포트 발췌/스크린샷을 첨부한다(AGENTS.md: 리포트 렌더링 변경은 스크린샷 필수). 네트워크 미가용 시 이유를 명시하고 오프라인 렌더 테스트(Task 4)로 갈음한다.

---

## 완료 기준 (스펙 §4 대조)

- [ ] KR 마켓 리뷰 시 `get_market_investor_flows("kospi"/"kosdaq", 5)`를 호출해 `MarketOverview.investor_flows`에 fail-open으로 채운다(비KR·실패 시 None). (Task 2)
- [ ] LLM 프롬프트에 시장 수급 요약 섹션(외국인/기관 5일 누적 + 기준일, 로케일 zh/en/ko, "보조 신호" 가이드) 주입, 데이터 없으면 생략. (Task 3)
- [ ] 리뷰 리포트 본문에 결정적 요약 라인(KOSPI/KOSDAQ 2줄, 외국인/기관 5일 누적, 최신 확정일·출처, 로케일 KRW 억/亿韩元/₩B, 개인 제외) zh/en/ko. (Task 4)
- [ ] 구조화 페이로드에 `investor_flows` 원시 레코드 키. (Task 5)
- [ ] fail-open: 수급 데이터 없으면 섹션 생략, 리뷰 정상 진행. (Task 2·3·4·5 전부)
- [ ] 중국어 혼입 거부 게이트 통과 — ko 결정적 블록은 순수 한글. (Task 3·4)
- [ ] `docs/market-support.md` + `docs/CHANGELOG.md` 갱신. (Task 6)
- [ ] 오프라인 게이트(`-m "not network"` + `ci_gate.sh`) 통과, 신규 의존성·설정 0, 비KR 마켓 리뷰 바이트 동일. (Task 7)

**롤백:** 전부 additive(비KR는 `region!="kr"`·`investor_flows=None`으로 미진입) → PR revert만으로 완전 롤백. 하위 호환 파손 없음(`MarketOverview.investor_flows`는 기본값 None optional 필드, 페이로드는 optional 키).

## 스펙 대비 확정/편차 기록

- **수집 훅(§9 확정)** = `MarketAnalyzer.get_market_overview()`의 `if self.region == "kr"` 브랜치 + `MarketOverview.investor_flows` 신규 optional 필드. fetcher 호출은 `DataFetcherManager.get_kr_market_investor_flows`가 Phase 2 lazy 싱글턴(`_kr_institutional_fetcher`)을 재사용한다. 별도 파이프라인 수집 단계·생성자 변경 없음.
- **프롬프트 언어 = 로케일(zh/en/ko)** — Phase 2 종목 프롬프트는 zh 고정이었으나, 마켓 리뷰 프롬프트는 이미 `review_language`별로 로컬라이즈되며 KR(ko) 출력엔 중국어 거부 게이트가 있어 zh 주입이 위험하다. 따라서 수급 프롬프트 섹션도 `review_language`를 따른다(스펙 §4 "zh/en/ko 3언어" 충족).
- **프롬프트 주입 지점 = KR stats_block 대체** — KR은 `has_market_stats=False`라 프롬프트 stats_block이 "시장 폭 데이터 없음" 문구다. 새 템플릿 슬롯을 추가하는 대신(비KR 공백 라인 삽입 위험) `if self.region == "kr"` 단일 지점에서 stats_block을 수급으로 대체한다 → 기존 `{stats_block}` 슬롯 재사용, 비KR 바이트 동일.
- **KRW 포맷터 홈 = `src/report_language.py`** — Phase 2 주수 포맷터(`_format_net_shares_localized`)는 `notification.py`(NotificationService static)에 있으나, 마켓 리뷰 렌더는 `market_analyzer.py`에서 일어나며 이 파일은 `notification`이 아닌 `report_language`에 의존한다. 로케일 포맷 책임의 의미상 홈이자 재사용·단위테스트에 유리한 `report_language.py`에 둔다.
- **결정적 블록 주입 = 시장 요약 섹션 뒤 + fallback append** — KR엔 up/down 통계가 없어 수급이 시장 폭 신호이므로 `시장 요약`(market_summary) 섹션에 삽입한다. LLM이 표준 헤딩을 내지 않으면 sector_block 선례처럼 fallback 헤딩으로 append. 이 주입은 중국어 거부 게이트 **이후**이므로 ko 블록은 순수 한글로 작성한다.
- **표시 결정(사용자 확정 2026-07-11)** = ① KOSPI·KOSDAQ 2줄, ② 리뷰 본문 + 구조화 페이로드 양쪽. 시장명은 KOSPI/KOSDAQ 고유명사로 전 언어 공통 표기.
- **품질 점수 = 비대상** — 마켓 리뷰엔 종목 분석의 데이터 품질 점수(ADR 0002, Phase 2)가 없다. 스펙 §4는 품질 반영을 요구하지 않으므로 Phase 3 범위 밖.

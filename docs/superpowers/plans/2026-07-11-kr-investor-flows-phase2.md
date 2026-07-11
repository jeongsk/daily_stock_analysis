# KR 투자자별 매매동향(수급) Phase 2 — 개별 종목 리포트 연결 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phase 1에서 만든 `KrInstitutionalFetcher.get_investor_flows`를 개별 KR 종목 분석에 연결한다 — 수급 데이터를 fail-open으로 수집해 (1) 데이터 품질 점수의 전역 블록, (2) LLM 프롬프트, (3) 리포트/알림 결정적 요약 라인에 반영한다. 시장 수급(`get_market_investor_flows`) → 마켓 리뷰 연결은 Phase 3 별도 PR.

**Architecture:** TW 三大法人 선례를 end-to-end 미러링한다(그릴링 후 사용자 선택 = "설계 B"). 수급 레코드는 **단일 위치** `fundamental_context["investor_flows"]`에 저장되어 세 소비면(품질 블록·프롬프트·리포트)에 자동 도달한다. base.py 오프쇼어 fundamental 수집에 `if market == "kr"` 브랜치 1개를 추가하는 것이 유일한 수집 훅이며, `PipelineAnalysisArtifacts`와 파이프라인 생성자는 **변경하지 않는다**. 데이터 품질 점수는 ADR 0002대로 investor_flows 전역 가중치(5) + NOT_SUPPORTED 제외 정규화로 바꾼다(기존 시장 동작 중립).

**Tech Stack:** Python 3 / `requests`(기존) / `pytest`(오프라인 `-m "not network"` 차단 게이트) / TW 선례 파일: `data_provider/tw_institutional_fetcher.py` 소비 경로(base.py·analyzer.py·notification.py·report_language.py).

**스펙:** `docs/superpowers/specs/2026-07-10-kr-investor-flows-design.md` §3 (Phase 2 범위)
**결정 기록:** `docs/adr/0002-investor-flows-global-quality-block.md`(전역 품질 블록·NOT_SUPPORTED 제외 정규화), `docs/adr/0001-...`(무인증 소스·단위 이원화), `CONTEXT.md`(용어)
**Phase 1 계획:** `docs/superpowers/plans/2026-07-11-kr-investor-flows-phase1.md`

## Global Constraints

- **신규 의존성 0, 신규 설정 0** — `.env.example`·`pyproject.toml`·`requirements.txt` 변경 금지. Phase 1 fetcher는 이미 무설정 동작한다.
- **전면 fail-open** — 수급 수집/파싱/렌더링의 어떤 실패도 메인 분석을 중단시키지 않는다. 실패 시 수급 섹션만 생략되고 나머지 분석은 정상 진행한다.
- **엄격 additive** — 비KR 시장은 **바이트 동일**해야 한다. 수급 블록은 비KR에서 `NOT_SUPPORTED`이고, 품질 점수는 정규화로 인해 **불변**이어야 한다(회귀 테스트로 고정 — ADR 0002).
- **투자 판단 신호 비연결** — `capital_flow_signal`·signal_attribution·매수/매도 스코어의 입력으로 쓰지 않는다. 수급은 LLM 참고 정보 + 표시용 + 품질(수집 완결성) 지표뿐이다.
- **단위 이원화(종목=주수)** — 종목 레코드 `unit: "shares"`. 주수×종가 금액 추정 환산 금지. 리포트 라인은 로케일 단위(ko `만주` / zh `万股` / en `M shares`)로 표기하되 값은 그대로 렌더한다.
- **데이터 단일 위치** — 수급 레코드는 `fundamental_context["investor_flows"]` 한 곳에만 둔다. `PipelineAnalysisArtifacts`에 top-level 필드를 추가하지 않는다(TW 미러). enhanced_context는 이미 fundamental_context를 담으므로(pipeline.py:1056) 별도 배선 불필요.
- **프롬프트 주입은 zh 고정** — analyzer 프롬프트 전체가 중국어이고 출력 언어는 별도 지시문(market_context.py)이 제어한다. TW institutional 주입과 동일하게 KR 수급 프롬프트 섹션도 zh로 작성한다. (스펙 §3 item4의 "zh/en/ko 프롬프트"는 실제 코드 현실과 어긋나며, zh/en/ko 다국어는 컨텍스트 팩 상태 라인과 리포트 라인에서 충족한다 — 아래 참고.)
- **필수 결측/비대상 처리** — 수급 레코드가 없으면 프롬프트·리포트 섹션을 생략한다. 0으로 조작하지 않는다. `individual_net`(개인)은 요약 라인에서 제외(nullable + 외국인·기관 역방향 중복), 프롬프트 컨텍스트에는 포함한다.
- **커밋 메시지 영어**, `Co-Authored-By` 금지, 커밋 제목에 `#patch`/`#minor`/`#major` 금지(자동 태그 opt-in 방지). 태스크별 커밋은 계획 실행 승인으로 갈음하되, **`git push`/PR 생성은 별도 사용자 확인 필요**.
- 오프라인 게이트 통과 필수: `uv run pytest -m "not network"` + `uv run ./scripts/ci_gate.sh`.
- 테스트 픽스처는 **2026-07-10 실캡처 데이터**(삼성전자 005930, Phase 1 픽스처와 동일 수치) — 임의 값으로 바꾸지 말 것.

## 파일 구조

| 파일 | 역할 | 변경 성격 |
| --- | --- | --- |
| Modify: `data_provider/base.py` | `_build_offshore_fundamental_context`에 `if market == "kr"` 수집 브랜치 + `result_ctx["investor_flows"]` | additive 브랜치 1개 |
| Create: `tests/test_kr_investor_flows_wiring.py` | base.py 수집 배선 오프라인 테스트 (TW wiring 테스트 미러) | 신규 |
| Modify: `src/services/analysis_context_builder.py` | `_build_investor_flows_block` + 등록 + 가중치 5 + NOT_SUPPORTED 제외 정규화 + limitation 튜플 | 핵심(ADR 0002) |
| Modify: `tests/test_analysis_context_builder.py` | 수급 블록 상태 4종 + 품질 정규화 회귀(비KR 불변) 테스트 | 테스트 추가/갱신 |
| Modify: `src/analysis_context_pack_prompt.py` | `BLOCK_LABELS_{ZH,EN,KO}`에 `investor_flows` 라벨(상태 라인 zh/en/ko 자동 렌더) | additive 3줄 |
| Modify: `tests/test_analysis_context_pack_prompt.py` | 상태 라인 zh/en/ko 렌더 테스트 | 테스트 추가 |
| Modify: `src/analyzer.py` | LLM 프롬프트에 KR 수급 값 섹션 주입(zh, TW 3897 미러) | additive 블록 1개 |
| Modify: `tests/test_kr_investor_flows_prompt.py` | 프롬프트 주입 테스트(데이터 있음/없음) | 신규 |
| Modify: `src/report_language.py` | zh/en/ko 라벨 3딕셔너리에 KR 수급 라벨 추가 | additive |
| Modify: `src/notification.py` | 블록 추출 + `_append_kr_investor_flows` 결정적 라인 + 로케일 주수 포맷터 | additive |
| Modify: `tests/test_kr_investor_flows_report.py` | 리포트 라인 zh/en/ko + all-N/A 가드 + 무데이터 생략 테스트 | 신규 |
| Modify: `docs/market-support.md` | KR 종목 수급 지원 기재(Phase 1에서 이월) | 문서 |
| Modify: `docs/CHANGELOG.md` | `[Unreleased]` 플랫 항목 | 문서 |

**설계 노트 (스펙 §9 확정 사항):**
- **수집 훅 = `data_provider/base.py:3001` 부근** (`_build_offshore_fundamental_context`의 TW `if market=="tw"` 옆). KR은 이미 `market in {"us","hk","jp","kr","tw"}`로 이 함수에 라우팅된다(base.py:3162). 별도 파이프라인 수집 단계나 생성자 변경 불필요.
- **agent/multi-agent 프롬프트 동기화 = 자동.** 컨텍스트 팩 요약 문자열(`analysis_context_pack_summary`)은 표준 경로(analyzer.py:3762)와 agent 경로(executor/orchestrator/base_agent) 양쪽에서 동일 렌더러를 거친다. `blocks["investor_flows"]`가 팩에 들어가면 두 경로에 자동 전파되므로 per-agent 배선 변경은 없다.
- **KR 판별 = code suffix 재사용.** 컨텍스트 블록 빌더는 `is_kr_suffix_symbol(artifacts.code)`(중앙 규칙, Phase 1 fetcher와 동일)로 KR 여부를 판정한다 — market 문자열 값에 의존하지 않는다.

---

### Task 1: base.py 수집 훅 — `fundamental_context["investor_flows"]` (fail-open, TW 미러)

**Files:**
- Modify: `data_provider/base.py` (`_build_offshore_fundamental_context`, TW institution 블록 뒤)
- Create: `tests/test_kr_investor_flows_wiring.py`

**Interfaces:**
- Consumes: `KrInstitutionalFetcher.get_investor_flows(stock_code, days=5) -> Optional[dict]` (Phase 1), 함수 내 로컬 `market`/`stock_code`/`fetch_timeout`/`stage_timeout`/`start_ts`, `self._run_with_retry(fn, timeout, label) -> (result, err, ms)`
- Produces (Task 2·4·5가 사용): `result_ctx["investor_flows"]` = Phase 1 정규화 레코드 dict 또는 `None`. 레코드 shape: `{"code","market","unit":"shares","days":[{date,foreign_net,institution_net,individual_net}],"summary":{"foreign_net_5d","institution_net_5d"},"source":"NAVER"|"DAUM"}`
- 브레이커/스로틀/캐시는 fetcher 내부(Phase 1)가 처리 — 이 훅은 lazy 싱글턴 + 타임아웃 가드만.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_investor_flows_wiring.py` 생성 (TW wiring 테스트 미러):

```python
# -*- coding: utf-8 -*-
"""Phase 2 report-wiring tests: KR 투자자별 수급(investor_flows)을 오프쇼어
fundamental_context["investor_flows"]에 fail-open으로 배선하는지 고정한다.

계약:
  - kr with data        -> fundamental_context["investor_flows"] = 정규화 레코드
  - kr fetch None/raise  -> investor_flows = None (fail-open, 메인 분석 유지)
  - us/hk/jp/tw          -> investor_flows 키 없음/None AND kr fetcher 미호출
                            (엄격 additive: 비KR 시장 불변)
  - fetch_timeout=0      -> per-fetch 비활성, kr fetcher 미호출
  - 느린 fetch           -> stage budget에서 포기(메인 분석 차단 금지)

TW wiring 테스트(tests/test_tw_institution_report_wiring.py) 패턴 미러.
완전 오프라인 — `pytest -m "not network"` 차단 게이트에 포함된다.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetcherManager

_KR_FETCHER_METHOD = (
    "data_provider.kr_institutional_fetcher.KrInstitutionalFetcher.get_investor_flows"
)

# Phase 1 정규화 레코드 shape (2026-07-10 삼성전자 005930 실캡처 5일).
_FAKE_REC = {
    "code": "005930",
    "market": "kospi",
    "unit": "shares",
    "days": [
        {"date": "2026-07-10", "foreign_net": 625985, "institution_net": 2313745, "individual_net": -2851466},
        {"date": "2026-07-09", "foreign_net": 845552, "institution_net": 1107761, "individual_net": -1739937},
        {"date": "2026-07-08", "foreign_net": -3015093, "institution_net": 971031, "individual_net": 2031705},
        {"date": "2026-07-07", "foreign_net": -6145090, "institution_net": -1852807, "individual_net": 7870568},
        {"date": "2026-07-06", "foreign_net": -2018562, "institution_net": 14823, "individual_net": 1917050},
    ],
    "summary": {"foreign_net_5d": -9707208, "institution_net_5d": 2554553},
    "source": "NAVER",
}

_OFFSHORE_CFG = SimpleNamespace(
    enable_fundamental_pipeline=True,
    fundamental_cache_ttl_seconds=0,
    fundamental_stage_timeout_seconds=1.5,
    fundamental_fetch_timeout_seconds=0.8,
    fundamental_retry_max=1,
)

_EMPTY_BUNDLE = {
    "status": "not_supported",
    "growth": {},
    "earnings": {},
    "belong_boards": [],
    "source_chain": [],
    "errors": [],
}


class TestKrInvestorFlowsWiring(unittest.TestCase):
    def _context(self, code, flows_return=None, flows_side_effect=None):
        """get_fundamental_context(code) 오프라인 실행; (ctx, kr_fetcher_mock) 반환."""
        manager = DataFetcherManager(fetchers=[])
        kwargs = {}
        if flows_side_effect is not None:
            kwargs["side_effect"] = flows_side_effect
        else:
            kwargs["return_value"] = flows_return
        with patch("src.config.get_config", return_value=_OFFSHORE_CFG), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=_EMPTY_BUNDLE,
                ), \
                patch(_KR_FETCHER_METHOD, **kwargs) as kr_mock:
            ctx = manager.get_fundamental_context(code)
        return ctx, kr_mock

    def test_kr_investor_flows_populated_when_fetcher_has_data(self):
        ctx, kr_mock = self._context("005930.KS", flows_return=dict(_FAKE_REC))
        self.assertEqual(ctx["market"], "kr")
        rec = ctx.get("investor_flows")
        self.assertIsInstance(rec, dict)
        self.assertEqual(rec["source"], "NAVER")
        self.assertEqual(rec["unit"], "shares")
        self.assertEqual(rec["days"][0]["foreign_net"], 625985)
        self.assertEqual(rec["summary"]["institution_net_5d"], 2554553)
        kr_mock.assert_called_with("005930.KS", days=5)
        # 메인 분석은 계속 — 예외가 새어나오지 않았다
        self.assertEqual(ctx["market"], "kr")

    def test_kosdaq_routed_and_populated(self):
        ctx, _ = self._context("068270.KQ", flows_return=dict(_FAKE_REC))
        self.assertEqual(ctx["market"], "kr")
        self.assertIsInstance(ctx.get("investor_flows"), dict)

    def test_kr_fail_open_when_fetcher_returns_none(self):
        ctx, _ = self._context("005930.KS", flows_return=None)
        self.assertEqual(ctx["market"], "kr")
        self.assertIsNone(ctx.get("investor_flows"))

    def test_kr_fail_open_when_fetcher_raises(self):
        # get_investor_flows는 자체 fail-open이지만, 훅이 raise도 삼키는지 고정
        ctx, _ = self._context("005930.KS", flows_side_effect=RuntimeError("boom"))
        self.assertEqual(ctx["market"], "kr")
        self.assertIsNone(ctx.get("investor_flows"))

    def test_us_unchanged_and_kr_fetcher_not_called(self):
        ctx, kr_mock = self._context("AAPL", flows_return=dict(_FAKE_REC))
        self.assertEqual(ctx["market"], "us")
        self.assertIsNone(ctx.get("investor_flows"))
        self.assertEqual(kr_mock.call_count, 0)

    def test_other_offshore_markets_unchanged(self):
        for code, market in (("0700.HK", "hk"), ("7203.T", "jp"), ("2330.TW", "tw")):
            ctx, kr_mock = self._context(code, flows_return=dict(_FAKE_REC))
            self.assertEqual(ctx["market"], market, f"{code} routed to {ctx['market']}")
            self.assertIsNone(ctx.get("investor_flows"))
            self.assertEqual(kr_mock.call_count, 0)

    def test_kr_fail_open_when_fetcher_init_raises(self):
        manager = DataFetcherManager(fetchers=[])
        with patch("src.config.get_config", return_value=_OFFSHORE_CFG), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=_EMPTY_BUNDLE,
                ), \
                patch(
                    "data_provider.kr_institutional_fetcher.KrInstitutionalFetcher",
                    side_effect=RuntimeError("init boom"),
                ):
            ctx = manager.get_fundamental_context("005930.KS")  # must NOT raise
        self.assertEqual(ctx["market"], "kr")
        self.assertIsNone(ctx.get("investor_flows"))

    def test_kr_flows_respects_stage_timeout(self):
        slow_cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=0,
            fundamental_stage_timeout_seconds=0.3,
            fundamental_fetch_timeout_seconds=0.3,
            fundamental_retry_max=1,
        )
        manager = DataFetcherManager(fetchers=[])

        def _slow(_code, days=5):
            time.sleep(2.0)
            return dict(_FAKE_REC)

        start = time.time()
        with patch("src.config.get_config", return_value=slow_cfg), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=_EMPTY_BUNDLE,
                ), \
                patch(_KR_FETCHER_METHOD, side_effect=_slow):
            ctx = manager.get_fundamental_context("005930.KS")
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.5, f"kr flows fetch ignored stage timeout ({elapsed:.2f}s)")
        self.assertIsNone(ctx.get("investor_flows"))

    def test_kr_flows_disabled_when_fetch_timeout_zero(self):
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=0,
            fundamental_stage_timeout_seconds=8.0,
            fundamental_fetch_timeout_seconds=0.0,  # disabled
            fundamental_retry_max=1,
        )
        manager = DataFetcherManager(fetchers=[])
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=_EMPTY_BUNDLE,
                ), \
                patch(_KR_FETCHER_METHOD, return_value=dict(_FAKE_REC)) as kr_mock:
            ctx = manager.get_fundamental_context("005930.KS")
        self.assertIsNone(ctx.get("investor_flows"))
        self.assertEqual(kr_mock.call_count, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_investor_flows_wiring.py -v`
Expected: FAIL — `ctx.get("investor_flows")`가 항상 `None`(브랜치 미구현)이라 populated/kosdaq 계열 테스트 실패, `kr_mock.assert_called_with` 실패.

- [ ] **Step 3: 최소 구현** — `data_provider/base.py`의 TW institution 블록(`else: ... result_ctx["institution"] = ...` 끝, 현재 `result_ctx["belong_boards"] = belong_boards` 직전)에 KR 브랜치 추가:

```python
        # investor_flows: kr (한국) has free unauthenticated per-stock investor net-buy
        # feeds (Naver integration JSON + Daum fallback, Phase 1); every other market skips
        # this. kr-only + strictly additive + fail-open: any error / no-data -> None, stored
        # as result_ctx["investor_flows"] = None, which the context pack maps to FETCH_FAILED
        # and never interrupts the main analysis. Raw normalized record only — the quality
        # block / prompt / report consume it downstream (ADR 0002, spec §3).
        kr_record = None
        if market == "kr":
            kr_fetcher = getattr(self, "_kr_institutional_fetcher", None)
            if kr_fetcher is None:
                # Wiring (import + construct) is a one-time op; a failure here is a
                # programming / deploy bug, so log it LOUD (error). Still fail-open.
                try:
                    from data_provider.kr_institutional_fetcher import KrInstitutionalFetcher

                    kr_fetcher = KrInstitutionalFetcher()
                    self._kr_institutional_fetcher = kr_fetcher
                except Exception as exc:  # noqa: BLE001 - wiring failure: loud but fail-open
                    logger.error("[kr-flows] fetcher init failed (wiring bug?) code=%s: %s", stock_code, exc)
                    kr_fetcher = None
            # fetch_timeout == 0 disables per-fetch fundamental fetches (same semantic the
            # valuation/bundle/tw-institution paths honour); respect it for kr flows too.
            if kr_fetcher is not None and fetch_timeout > 0:
                flows_timeout = max(stage_timeout - (time.time() - start_ts), 0.0)
                if flows_timeout > 0:
                    kr_record, flows_err, _flows_ms = self._run_with_retry(
                        lambda: kr_fetcher.get_investor_flows(stock_code, days=5),
                        flows_timeout,
                        "fundamental_kr_investor_flows",
                    )
                    if flows_err:
                        logger.warning("[kr-flows] fetch failed/timeout code=%s: %s", stock_code, flows_err)
        result_ctx["investor_flows"] = kr_record if isinstance(kr_record, dict) else None
```

주의: `get_investor_flows`는 Phase 1에서 자체 fail-open(예외 없이 None)이므로 `_run_with_retry`는 주로 타임아웃 상한을 강제한다. `result_ctx["investor_flows"]`는 `coverage`/`block_statuses` 튜플에 넣지 않는다 — 별도 키로만 두어(레코드에 `errors`/`source_chain` 키가 없으므로) 3078~3080의 집계 루프를 오염시키지 않는다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_investor_flows_wiring.py -v`
Expected: PASS (9 passed)

Run: `uv run python -m py_compile data_provider/base.py`
Expected: 출력 없음(성공)

- [ ] **Step 5: 커밋**

```bash
git add data_provider/base.py tests/test_kr_investor_flows_wiring.py
git commit -m "feat: wire KR investor flows into offshore fundamental context (fail-open)"
```

---

### Task 2: 컨텍스트 팩 전역 품질 블록 + NOT_SUPPORTED 제외 정규화 (ADR 0002)

**Files:**
- Modify: `src/services/analysis_context_builder.py`
- Modify: `tests/test_analysis_context_builder.py`

**Interfaces:**
- Consumes: Task 1의 `artifacts.fundamental_context["investor_flows"]`, 기존 `ContextFieldStatus`(schema), `_STATUS_SCORES`, `AnalysisContextBlock`/`AnalysisContextItem`, `is_kr_suffix_symbol`
- Produces (Task 3이 렌더, Task 5는 무관): `blocks["investor_flows"]` (status AVAILABLE/FALLBACK/FETCH_FAILED/NOT_SUPPORTED), `_QUALITY_BLOCK_WEIGHTS["investor_flows"]=5`, 정규화된 `overall_score`
- 상태 매핑(스펙 §3 item2): 비KR → `NOT_SUPPORTED`; KR + 레코드 None → `FETCH_FAILED`; KR + `source="DAUM"` → `FALLBACK`; KR + `source="NAVER"` → `AVAILABLE`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_analysis_context_builder.py` 파일 끝에 추가:

```python
# ---------------------------------------------------------------------------
# Phase 2: KR investor_flows 전역 품질 블록 + NOT_SUPPORTED 제외 정규화
# ---------------------------------------------------------------------------

from src.schemas.analysis_context_pack import ContextFieldStatus
from src.services.analysis_context_builder import (
    AnalysisContextBuilder,
    PipelineAnalysisArtifacts,
)

_KR_FLOWS_REC = {
    "code": "005930",
    "market": "kospi",
    "unit": "shares",
    "days": [
        {"date": "2026-07-10", "foreign_net": 625985, "institution_net": 2313745, "individual_net": -2851466},
        {"date": "2026-07-09", "foreign_net": 845552, "institution_net": 1107761, "individual_net": -1739937},
    ],
    "summary": {"foreign_net_5d": 1471537, "institution_net_5d": 3421506},
    "source": "NAVER",
}


def _artifacts(code, *, fundamental_context=None):
    """블록 빌더 테스트용 최소 아티팩트 — 네트워크/피처 불필요."""
    return PipelineAnalysisArtifacts(
        code=code,
        stock_name="TEST",
        market="kr" if code.upper().endswith((".KS", ".KQ")) else "us",
        phase=None,
        base_context={},
        enhanced_context={},
        realtime_quote=None,
        trend_result=None,
        chip_data=None,
        fundamental_context=fundamental_context,
        news_context=None,
        news_result_count=None,
        metadata={},
    )


class TestInvestorFlowsBlock:
    def test_non_kr_is_not_supported(self):
        pack = AnalysisContextBuilder.build(_artifacts("AAPL"))
        block = pack.blocks["investor_flows"]
        assert block.status == ContextFieldStatus.NOT_SUPPORTED

    def test_kr_none_record_is_fetch_failed(self):
        pack = AnalysisContextBuilder.build(
            _artifacts("005930.KS", fundamental_context={"investor_flows": None})
        )
        assert pack.blocks["investor_flows"].status == ContextFieldStatus.FETCH_FAILED

    def test_kr_missing_key_is_fetch_failed(self):
        # KR인데 fundamental_context에 investor_flows 키 자체가 없는 경우도 수집 실패
        pack = AnalysisContextBuilder.build(
            _artifacts("005930.KS", fundamental_context={})
        )
        assert pack.blocks["investor_flows"].status == ContextFieldStatus.FETCH_FAILED

    def test_kr_naver_is_available(self):
        pack = AnalysisContextBuilder.build(
            _artifacts("005930.KS", fundamental_context={"investor_flows": dict(_KR_FLOWS_REC)})
        )
        block = pack.blocks["investor_flows"]
        assert block.status == ContextFieldStatus.AVAILABLE
        assert block.source == "NAVER"

    def test_kr_daum_is_fallback(self):
        rec = dict(_KR_FLOWS_REC, source="DAUM")
        pack = AnalysisContextBuilder.build(
            _artifacts("068270.KQ", fundamental_context={"investor_flows": rec})
        )
        assert pack.blocks["investor_flows"].status == ContextFieldStatus.FALLBACK


class TestQualityNormalization:
    def test_non_kr_score_is_behavior_neutral(self):
        # investor_flows(NOT_SUPPORTED)는 분자·분모에서 제외 -> 비KR 점수 불변.
        # 모든 코어/aux 블록이 MISSING인 최소 아티팩트: 정규화 분모는 여전히 100.
        pack = AnalysisContextBuilder.build(_artifacts("AAPL"))
        dq = pack.data_quality
        # investor_flows는 block_scores에는 기록되지만(진단용) overall에는 미반영
        assert dq.block_scores["investor_flows"] == 70  # NOT_SUPPORTED score
        # 비KR: investor_flows 제외 후 6블록 가중치 합 100으로 정규화 (동작 중립)
        expected = round(
            (
                dq.block_scores["quote"] * 25
                + dq.block_scores["daily_bars"] * 25
                + dq.block_scores["technical"] * 25
                + dq.block_scores["news"] * 10
                + dq.block_scores["fundamentals"] * 10
                + dq.block_scores["chip"] * 5
            )
            / 100
        )
        assert dq.overall_score == expected

    def test_kr_investor_flows_participates_in_score(self):
        # KR + AVAILABLE(100)이면 investor_flows가 분자·분모(가중치 5)에 참여.
        rec = dict(_KR_FLOWS_REC)
        pack = AnalysisContextBuilder.build(
            _artifacts("005930.KS", fundamental_context={"investor_flows": rec})
        )
        dq = pack.data_quality
        assert dq.block_scores["investor_flows"] == 100
        # 분모가 105로 늘어 investor_flows AVAILABLE이 점수에 기여
        total_weight = 25 + 25 + 25 + 10 + 10 + 5 + 5
        weighted = (
            dq.block_scores["quote"] * 25
            + dq.block_scores["daily_bars"] * 25
            + dq.block_scores["technical"] * 25
            + dq.block_scores["news"] * 10
            + dq.block_scores["fundamentals"] * 10
            + dq.block_scores["chip"] * 5
            + 100 * 5
        )
        assert dq.overall_score == round(weighted / total_weight)

    def test_kr_fetch_failed_lowers_score_and_notes_limitation(self):
        pack = AnalysisContextBuilder.build(
            _artifacts("005930.KS", fundamental_context={"investor_flows": None})
        )
        dq = pack.data_quality
        assert dq.block_scores["investor_flows"] == 25  # FETCH_FAILED
        # aux limitation: FETCH_FAILED는 표기된다
        assert any("investor_flows" in lim for lim in dq.limitations)

    def test_non_kr_not_supported_is_not_a_limitation(self):
        # NOT_SUPPORTED / MISSING은 aux limitation에 넣지 않는다(노이즈 방지)
        pack = AnalysisContextBuilder.build(_artifacts("AAPL"))
        assert not any("investor_flows" in lim for lim in pack.data_quality.limitations)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_analysis_context_builder.py -v -k "InvestorFlows or QualityNormalization"`
Expected: FAIL — `KeyError: 'investor_flows'`(블록 미등록) 계열.

- [ ] **Step 3: 최소 구현** — `src/services/analysis_context_builder.py` 수정:

(a) import 블록에 추가 (파일 상단 기존 import 근처):

```python
from src.services.market_symbol_utils import is_kr_suffix_symbol
```

(b) `_QUALITY_BLOCK_WEIGHTS`(현재 24-31)에 investor_flows 가중치 추가 — `"chip": 5,` 다음 줄:

```python
_QUALITY_BLOCK_WEIGHTS: Dict[str, int] = {
    "quote": 25,
    "daily_bars": 25,
    "technical": 25,
    "news": 10,
    "fundamentals": 10,
    "chip": 5,
    "investor_flows": 5,  # ADR 0002: 전 시장 공통 품질 블록(비KR은 NOT_SUPPORTED로 정규화 제외)
}
```

(c) `_build_data_quality`(현재 504-524)의 채점 루프를 NOT_SUPPORTED 제외 정규화로 교체 — 기존:

```python
    block_scores: Dict[str, int] = {}
    weighted_sum = 0
    for key, weight in _QUALITY_BLOCK_WEIGHTS.items():
        status = _quality_block_status(blocks, key)
        score = _STATUS_SCORES.get(status, _STATUS_SCORES[ContextFieldStatus.MISSING])
        block_scores[key] = score
        weighted_sum += score * weight

    overall_score = int(round(weighted_sum / 100))
```

교체 후:

```python
    block_scores: Dict[str, int] = {}
    weighted_sum = 0
    total_weight = 0
    for key, weight in _QUALITY_BLOCK_WEIGHTS.items():
        status = _quality_block_status(blocks, key)
        score = _STATUS_SCORES.get(status, _STATUS_SCORES[ContextFieldStatus.MISSING])
        block_scores[key] = score  # 진단용: NOT_SUPPORTED 점수도 기록
        if status == ContextFieldStatus.NOT_SUPPORTED:
            # ADR 0002: NOT_SUPPORTED 블록은 분자·분모 모두에서 제외한다.
            # 점수 의미 = "이 시장이 지원하는 블록 대비 수집 완결성".
            continue
        weighted_sum += score * weight
        total_weight += weight

    overall_score = int(round(weighted_sum / total_weight)) if total_weight else 0
```

(주의: 현재 어떤 블록도 프로덕션에서 NOT_SUPPORTED가 되지 않으므로(`chip_not_supported`는 어디에서도 설정되지 않음) 비KR `total_weight`는 100으로 유지되어 기존 점수가 **불변**이다. investor_flows만 비KR에서 NOT_SUPPORTED가 되어 제외된다 — 정규화의 유일한 실효 대상.)

(d) `_quality_limitations`(현재 553-565)의 aux 튜플(현재 `("news", "fundamentals", "chip")`)에 investor_flows 추가:

```python
    for key in ("news", "fundamentals", "chip", "investor_flows"):
        status = _quality_block_status(blocks, key)
        if status in _AUX_LIMITATION_STATUSES:
            limitations.append(f"{key}: {status.value}")
```

(`_AUX_LIMITATION_STATUSES` = {FETCH_FAILED, FALLBACK, STALE} 이므로 NOT_SUPPORTED/MISSING은 표기되지 않는다 — 비KR 노이즈 없음.)

(e) `_build_investor_flows_block` 신규 (`_build_chip_block` 아래, 예: 354 이후에 추가):

```python
def _build_investor_flows_block(artifacts: PipelineAnalysisArtifacts) -> AnalysisContextBlock:
    """KR 투자자별 수급 블록 — 전 시장 공통 품질 블록(ADR 0002).

    상태 매핑(스펙 §3 item2):
      - 비KR 종목            -> NOT_SUPPORTED (정규화에서 분자·분모 제외)
      - KR + 레코드 없음/None -> FETCH_FAILED (상장 KR은 포털에 수급이 항상 있으므로
                               None은 사실상 수집 실패)
      - KR + source="DAUM"   -> FALLBACK
      - KR + source="NAVER"  -> AVAILABLE
    """
    code = str(getattr(artifacts, "code", "") or "")
    if not is_kr_suffix_symbol(code):
        return AnalysisContextBlock(
            status=ContextFieldStatus.NOT_SUPPORTED,
            items={
                "investor_flows": AnalysisContextItem(
                    status=ContextFieldStatus.NOT_SUPPORTED,
                    missing_reason="investor_flows_not_supported",
                )
            },
        )

    context = artifacts.fundamental_context if isinstance(artifacts.fundamental_context, dict) else {}
    record = context.get("investor_flows")
    if not isinstance(record, dict) or not record.get("days"):
        return AnalysisContextBlock(
            status=ContextFieldStatus.FETCH_FAILED,
            items={
                "investor_flows": AnalysisContextItem(
                    status=ContextFieldStatus.FETCH_FAILED,
                    missing_reason="investor_flows_fetch_failed",
                )
            },
        )

    source = _source_text(record.get("source"))
    status = (
        ContextFieldStatus.FALLBACK
        if str(record.get("source") or "").upper() == "DAUM"
        else ContextFieldStatus.AVAILABLE
    )
    summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
    latest = record["days"][0] if isinstance(record["days"][0], dict) else {}
    return AnalysisContextBlock(
        status=status,
        items={
            "foreign_net_5d": AnalysisContextItem(
                status=status, value=summary.get("foreign_net_5d"), source=source
            ),
            "institution_net_5d": AnalysisContextItem(
                status=status, value=summary.get("institution_net_5d"), source=source
            ),
            "latest_date": AnalysisContextItem(
                status=status, value=latest.get("date"), source=source
            ),
        },
        source=source,
        metadata={"unit": record.get("unit"), "days": len(record["days"])},
    )
```

(f) `AnalysisContextBuilder.build`(현재 96, `blocks["news"] = _build_news_block(artifacts)` 다음)에 등록:

```python
        blocks["investor_flows"] = _build_investor_flows_block(artifacts)
```

- [ ] **Step 4: 통과 확인 (신규 + 기존 회귀)**

Run: `uv run pytest tests/test_analysis_context_builder.py -v`
Expected: PASS. **기존 `test_data_quality_scores_fixed_blocks_and_limits_auxiliary_missing`가 깨지면**(block_scores에 investor_flows 키 추가로 dict 비교 실패), 그 기대 dict에 `"investor_flows": 70`을 추가하고 `overall_score`/`level`은 그대로 유지되는지 확인해 회귀가 **동작 중립**임을 고정한다(비KR 아티팩트이므로 investor_flows=NOT_SUPPORTED=70, overall 불변).

Run: `uv run pytest tests/test_analysis_context_pack_prompt.py -v`
Expected: PASS — 기존 zh/en/ko 요약 테스트가 quality score 라벨(예: "76/100")을 assert한다면 값이 바뀌지 않았는지 확인(비KR 픽스처는 동작 중립). 바뀌면 픽스처가 KR인지/정규화 오류인지 점검.

- [ ] **Step 5: 커밋**

```bash
git add src/services/analysis_context_builder.py tests/test_analysis_context_builder.py
git commit -m "feat: add KR investor flows quality block with not-supported normalization"
```

---

### Task 3: 컨텍스트 팩 프롬프트 상태 라인 — `investor_flows` 라벨 (zh/en/ko)

**Files:**
- Modify: `src/analysis_context_pack_prompt.py`
- Modify: `tests/test_analysis_context_pack_prompt.py`

**Interfaces:**
- Consumes: Task 2의 `blocks["investor_flows"]` (status/source), `_block_lines`(자동 렌더 루프)
- Produces: 3언어 컨텍스트 팩 요약에 수급 블록 상태 라인이 자동 포함됨(값은 미표기 — 이 파일은 status/source만 렌더)
- 참고: agent/표준 경로 모두 이 요약 문자열을 공유하므로 프롬프트 동기화는 자동.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_analysis_context_pack_prompt.py`에 추가 (기존 헬퍼 `_builder_artifacts`/픽스처 스타일 재사용). 아래는 파일 끝에 추가:

```python
from src.analysis_context_pack_prompt import format_analysis_context_pack_prompt_section
from src.services.analysis_context_builder import AnalysisContextBuilder


def _kr_pack(source="NAVER"):
    from src.services.analysis_context_builder import PipelineAnalysisArtifacts

    rec = {
        "code": "005930", "market": "kospi", "unit": "shares",
        "days": [{"date": "2026-07-10", "foreign_net": 1, "institution_net": 2, "individual_net": 3}],
        "summary": {"foreign_net_5d": 1, "institution_net_5d": 2},
        "source": source,
    }
    artifacts = PipelineAnalysisArtifacts(
        code="005930.KS", stock_name="삼성전자", market="kr", phase=None,
        base_context={}, enhanced_context={}, realtime_quote=None, trend_result=None,
        chip_data=None, fundamental_context={"investor_flows": rec},
        news_context=None, news_result_count=None, metadata={},
    )
    return AnalysisContextBuilder.build(artifacts)


class TestInvestorFlowsStatusLine:
    def test_ko_renders_investor_flows_status(self):
        pack = _kr_pack("NAVER")
        text = format_analysis_context_pack_prompt_section(pack, report_language="ko")
        assert "투자자매매" in text

    def test_en_renders_investor_flows_status(self):
        pack = _kr_pack("NAVER")
        text = format_analysis_context_pack_prompt_section(pack, report_language="en")
        assert "investor flows" in text.lower()

    def test_zh_renders_investor_flows_status(self):
        pack = _kr_pack("DAUM")
        text = format_analysis_context_pack_prompt_section(pack, report_language="zh")
        assert "投资者" in text
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_analysis_context_pack_prompt.py -v -k InvestorFlowsStatusLine`
Expected: FAIL — 라벨 미등록이라 블록 키가 fallback으로 렌더되어 라벨 문자열 미포함.

- [ ] **Step 3: 최소 구현** — `src/analysis_context_pack_prompt.py`의 세 라벨 딕셔너리(현재 10-35)에 `investor_flows` 추가. **주의:** `BLOCK_LABELS_KO`에서 `chip`이 이미 `"수급"`을 쓰므로 라벨 충돌을 피해 investor_flows는 `"투자자매매"`로 구분한다.

```python
BLOCK_LABELS_ZH = {
    "quote": "行情",
    "daily_bars": "日线",
    "technical": "技术",
    "chip": "筹码",
    "fundamentals": "基本面",
    "news": "新闻",
    "investor_flows": "投资者",
}

BLOCK_LABELS_EN = {
    "quote": "quote",
    "daily_bars": "daily bars",
    "technical": "technical",
    "chip": "chip",
    "fundamentals": "fundamentals",
    "news": "news",
    "investor_flows": "investor flows",
}

BLOCK_LABELS_KO = {
    "quote": "시세",
    "daily_bars": "일봉",
    "technical": "기술",
    "chip": "수급",
    "fundamentals": "기본면",
    "news": "뉴스",
    "investor_flows": "투자자매매",
}
```

(렌더 순서는 `iter_analysis_context_pack_block_keys`가 `BLOCK_LABELS_ZH` 키 순서를 따르므로 investor_flows는 news 뒤에 나온다 — 별도 코드 변경 불필요.)

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_analysis_context_pack_prompt.py -v`
Expected: PASS (신규 3 + 기존 전부)

- [ ] **Step 5: 커밋**

```bash
git add src/analysis_context_pack_prompt.py tests/test_analysis_context_pack_prompt.py
git commit -m "feat: label KR investor flows block in context pack summary (zh/en/ko)"
```

---

### Task 4: analyzer.py — LLM 프롬프트에 KR 수급 값 주입 (zh, TW 三大法人 미러)

**Files:**
- Modify: `src/analyzer.py` (TW institution 프롬프트 블록 뒤, 현재 3929 이후)
- Create: `tests/test_kr_investor_flows_prompt.py`

**Interfaces:**
- Consumes: `context.get("fundamental_context")["investor_flows"]` (Task 1이 채운 레코드 — analyzer는 3800에서 `fundamental_context = context.get("fundamental_context")`로 읽고, enhanced_context가 이를 담는다 pipeline.py:1056)
- Produces: KR 종목 프롬프트에 수급 요약 섹션(5일 누적 + 최신일, 주수 단위, "보조 신호" 가이드). 데이터 없으면 섹션 생략.
- zh 고정(주변 프롬프트·TW 선례 일관). 개인(individual)은 컨텍스트에 포함(프롬프트에서만) — 요약 라인(Task 5)에서는 제외.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_investor_flows_prompt.py` 생성. analyzer 프롬프트 빌더의 정확한 진입 함수는 구현 시 확인하되(예: `GeminiAnalyzer._format_prompt` 계열), 아래는 계약을 고정하는 헬퍼 기반 테스트다. **Step 3 구현 전에 실제 프롬프트 함수 시그니처를 확인**하고 아래 호출부를 맞춘다(TW 테스트 `tests/test_tw_report_consumption.py`의 프롬프트 주입 테스트가 정확한 진입점 선례):

```python
# -*- coding: utf-8 -*-
"""Phase 2: analyzer LLM 프롬프트에 KR 수급 값이 주입되는지 고정(오프라인).

TW 三大法人 프롬프트 주입(analyzer.py ~3897) 미러. context.fundamental_context
["investor_flows"] 레코드가 있으면 프롬프트에 수급 표가 들어가고, 없으면 생략된다.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.analyzer import _kr_investor_flows_prompt_section  # 신규 순수 헬퍼(Step 3에서 추가)

_REC = {
    "code": "005930", "market": "kospi", "unit": "shares",
    "days": [
        {"date": "2026-07-10", "foreign_net": 625985, "institution_net": 2313745, "individual_net": -2851466},
        {"date": "2026-07-09", "foreign_net": 845552, "institution_net": 1107761, "individual_net": -1739937},
    ],
    "summary": {"foreign_net_5d": 1471537, "institution_net_5d": 3421506},
    "source": "NAVER",
}


class TestKrFlowsPromptSection:
    def test_section_present_when_record_valid(self):
        text = _kr_investor_flows_prompt_section({"investor_flows": _REC})
        assert text  # 비어있지 않음
        assert "1471537" in text or "3421506" in text  # 5일 누적 값 노출
        assert "2026-07-10" in text  # 최신 확정일
        assert "NAVER" in text

    def test_section_empty_when_no_record(self):
        assert _kr_investor_flows_prompt_section({}) == ""
        assert _kr_investor_flows_prompt_section({"investor_flows": None}) == ""
        assert _kr_investor_flows_prompt_section(None) == ""

    def test_section_empty_when_days_missing(self):
        bad = {"investor_flows": {"summary": {}, "days": []}}
        assert _kr_investor_flows_prompt_section(bad) == ""
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_investor_flows_prompt.py -v`
Expected: FAIL — `ImportError: cannot import name '_kr_investor_flows_prompt_section'`

- [ ] **Step 3: 최소 구현** — `src/analyzer.py`에 순수 헬퍼를 추가하고(모듈 함수, 예: TW 주입부 근처 또는 파일 상단 헬퍼 구역), TW institution 주입 블록(현재 3929 이후) 뒤에서 호출한다.

(a) 순수 헬퍼 (모듈 레벨 함수):

```python
def _kr_investor_flows_prompt_section(fundamental_context: Optional[Dict[str, Any]]) -> str:
    """KR 수급 레코드 -> LLM 프롬프트 섹션(zh). 레코드 없으면 "".

    TW 三大法人 주입과 동일 정신(값 표기, 파생 신호 없음). 주수 단위 명시 +
    "보조 신호" 가이드. 개인 순매수도 참고로 포함하되 과대해석 방지 문구 첨부.
    """
    if not isinstance(fundamental_context, dict):
        return ""
    record = fundamental_context.get("investor_flows")
    if not isinstance(record, dict):
        return ""
    days = record.get("days")
    if not isinstance(days, list) or not days:
        return ""
    summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
    latest = days[0] if isinstance(days[0], dict) else {}
    foreign_5d = summary.get("foreign_net_5d")
    inst_5d = summary.get("institution_net_5d")
    if foreign_5d is None or inst_5d is None:
        return ""
    date = latest.get("date", "N/A")
    source = record.get("source", "N/A")
    ind = latest.get("individual_net")
    ind_cell = "N/A" if ind is None else ind
    return f"""
### 韩股投资者供需动向（外国人/机构/个人，净买卖，单位:股，最近确定交易日 {date}）
| 主体 | 5日累计净买卖 | 最新一日({date}) | 决策含义 |
|------|------|------|----------|
| 外国人 | {foreign_5d} | {latest.get('foreign_net', 'N/A')} | 正值=净买偏支持，负值=净卖偏压制 |
| 机构 | {inst_5d} | {latest.get('institution_net', 'N/A')} | 韩股机构方向参考 |
| 个人 | (未提供累计) | {ind_cell} | 常与外资/机构反向，仅作参考 |

> 供需（投资者动向）是**辅助信号**，用于价格位置的过滤参考，不作为独立买卖决策依据。数据来源 {source}。单位为股，不要换算为金额。
"""
```

(주의: `Optional`, `Dict`, `Any`가 analyzer.py에 이미 import되어 있는지 확인 — 없으면 typing import에 추가.)

(b) TW institution 주입 블록(현재 `# 添加三大法人动向...` ~3929) 뒤에 호출부 추가:

```python
        # 韩股投资者供需（外国人/机构/个人）— kr-only；레코드 없으면 빈 문자열로 생략, 严格 additive。
        prompt += _kr_investor_flows_prompt_section(fundamental_context)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_investor_flows_prompt.py -v`
Expected: PASS (3 passed)

Run: `uv run python -m py_compile src/analyzer.py`
Expected: 출력 없음

- [ ] **Step 5: 커밋**

```bash
git add src/analyzer.py tests/test_kr_investor_flows_prompt.py
git commit -m "feat: inject KR investor flows summary into analysis prompt"
```

---

### Task 5: report_language 라벨 + notification 결정적 요약 라인 (zh/en/ko, 로케일 주수)

**Files:**
- Modify: `src/report_language.py` (zh/en/ko 라벨 3딕셔너리)
- Modify: `src/notification.py` (블록 추출 + `_append_kr_investor_flows` + 로케일 주수 포맷터)
- Create: `tests/test_kr_investor_flows_report.py`

**Interfaces:**
- Consumes: `result.fundamental_context["investor_flows"]` (Task 1), `get_report_labels(language)`, 기존 `_get_fundamental_blocks`/`_append_fundamental_blocks` 구조
- Produces: 종목 리포트에 결정적 요약 라인 1줄 — 예 ko `**투자자 수급**(5일 · 07-10 기준): 외국인 -970.72만주 / 기관 +255.46만주 · NAVER`. 데이터 없거나 값 전부 N/A면 라인 생략. 개인은 라인에서 제외.
- 신규 라벨 키: `kr_flow_label`(수급 제목), `kr_flow_institution_label`(기관). 외국인은 기존 `inst_foreign_label` 재사용.
- 신규 포맷터: `_format_net_shares_localized(value, language) -> str` (ko `만주`/`억주`, zh `万股`/`亿股`, en `M shares`).

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_kr_investor_flows_report.py` 생성:

```python
# -*- coding: utf-8 -*-
"""Phase 2: 리포트/알림에 KR 수급 결정적 요약 라인이 zh/en/ko로 렌더되는지 고정.

TW _append_institutional_flow 렌더 선례 미러. 오프라인.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.notification import NotificationService

_REC = {
    "code": "005930", "market": "kospi", "unit": "shares",
    "days": [
        {"date": "2026-07-10", "foreign_net": 625985, "institution_net": 2313745, "individual_net": -2851466},
        {"date": "2026-07-09", "foreign_net": 845552, "institution_net": 1107761, "individual_net": -1739937},
        {"date": "2026-07-08", "foreign_net": -3015093, "institution_net": 971031, "individual_net": 2031705},
        {"date": "2026-07-07", "foreign_net": -6145090, "institution_net": -1852807, "individual_net": 7870568},
        {"date": "2026-07-06", "foreign_net": -2018562, "institution_net": 14823, "individual_net": 1917050},
    ],
    "summary": {"foreign_net_5d": -9707208, "institution_net_5d": 2554553},
    "source": "NAVER",
}


class TestFormatNetSharesLocalized:
    def test_ko_uses_manju(self):
        assert NotificationService._format_net_shares_localized(625985, "ko") == "+62.60만주"
        assert NotificationService._format_net_shares_localized(-9707208, "ko") == "-970.72만주"

    def test_zh_uses_wan(self):
        assert NotificationService._format_net_shares_localized(625985, "zh") == "+62.60万股"

    def test_en_uses_millions(self):
        assert NotificationService._format_net_shares_localized(625985, "en") == "+0.63M shares"

    def test_invalid_is_na(self):
        assert NotificationService._format_net_shares_localized(None, "ko") == "N/A"
        assert NotificationService._format_net_shares_localized("x", "en") == "N/A"


def _blocks(record):
    """_append_kr_investor_flows가 소비하는 blocks dict 최소 형태."""
    return {"investor_flows": record}


class TestAppendKrInvestorFlows:
    def _render(self, record, language):
        from src.report_language import get_report_labels

        notifier = NotificationService.__new__(NotificationService)  # __init__ 부작용 회피
        lines = []
        notifier._append_kr_investor_flows(
            lines, _blocks(record), get_report_labels(language), language
        )
        return "\n".join(lines)

    def test_ko_line(self):
        text = self._render(dict(_REC), "ko")
        assert "외국인 -970.72만주" in text
        assert "기관 +255.46만주" in text
        assert "07-10" in text
        assert "NAVER" in text
        assert "개인" not in text  # 개인은 요약 라인에서 제외

    def test_en_line(self):
        text = self._render(dict(_REC), "en")
        assert "shares" in text and "NAVER" in text

    def test_zh_line(self):
        text = self._render(dict(_REC), "zh")
        assert "万股" in text and "NAVER" in text

    def test_no_record_omits_line(self):
        assert self._render(None, "ko") == ""
        assert self._render({"days": []}, "ko") == ""

    def test_all_na_omits_line(self):
        bad = {"summary": {"foreign_net_5d": None, "institution_net_5d": None},
               "days": [{"date": "2026-07-10"}], "source": "NAVER"}
        assert self._render(bad, "ko") == ""
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kr_investor_flows_report.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_format_net_shares_localized'` / `_append_kr_investor_flows`.

- [ ] **Step 3: 최소 구현**

(a) `src/report_language.py` — 세 라벨 딕셔너리(zh 373-378, en 497-502, ko 621-626)의 institutional 라벨 옆에 KR 수급 라벨 추가:

zh (`"institutional_flow_heading"` 근처):
```python
        "kr_flow_label": "投资者动向",
        "kr_flow_institution_label": "机构",
```
en:
```python
        "kr_flow_label": "Investor Flows",
        "kr_flow_institution_label": "Institutions",
```
ko:
```python
        "kr_flow_label": "투자자 수급",
        "kr_flow_institution_label": "기관",
```
(외국인 라벨은 기존 `inst_foreign_label`(外资/Foreign/외국인)을 재사용한다 — 세 언어에 이미 존재.)

(b) `src/notification.py` — 로케일 주수 포맷터를 `_format_net_shares`(현재 2200-2217) 아래에 추가:

```python
    @classmethod
    def _format_net_shares_localized(cls, value: Any, language: str) -> str:
        """부호 붙은 순매수 주수를 로케일 단위로 포맷(+ = 순매수).

        ko: 억주/만주/주, zh: 亿股/万股/股, en: M shares(백만주). None/NaN/비수치 -> N/A.
        """
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return "N/A"
        if amount != amount:  # NaN
            return "N/A"
        sign = "+" if amount > 0 else ("-" if amount < 0 else "")
        a = abs(amount)
        if language == "en":
            return f"{sign}{a / 1e6:.2f}M shares"
        if language == "ko":
            if a >= 1e8:
                return f"{sign}{a / 1e8:.2f}억주"
            if a >= 1e4:
                return f"{sign}{a / 1e4:.2f}만주"
            return f"{sign}{a:.0f}주"
        # zh (default)
        if a >= 1e8:
            return f"{sign}{a / 1e8:.2f}亿股"
        if a >= 1e4:
            return f"{sign}{a / 1e4:.2f}万股"
        return f"{sign}{a:.0f}股"
```

(c) `_append_institutional_flow`(현재 2219-2256) 아래에 KR 수급 라인 렌더러 추가:

```python
    def _append_kr_investor_flows(
        self,
        lines: List[str],
        blocks: Dict[str, Any],
        labels: Dict[str, str],
        report_language: str,
    ) -> None:
        """KR 종목 수급 결정적 요약 라인 — kr-only, 데이터 있을 때만 렌더(严格 additive).

        5일 누적 외국인/기관 순매수(주수)를 최신 확정 거래일·출처와 함께 1줄로.
        개인은 nullable이고 외국인·기관 합의 역방향이라 라인에서 제외한다.
        """
        record = blocks.get("investor_flows")
        if not isinstance(record, dict):
            return
        days = record.get("days")
        if not isinstance(days, list) or not days:
            return
        summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
        foreign = self._format_net_shares_localized(summary.get("foreign_net_5d"), report_language)
        institution = self._format_net_shares_localized(summary.get("institution_net_5d"), report_language)
        if foreign == "N/A" and institution == "N/A":
            return
        latest = days[0] if isinstance(days[0], dict) else {}
        date = self._format_text(latest.get("date"))
        source = self._format_text(record.get("source"))
        window = min(len(days), 5)
        flow_label = labels.get("kr_flow_label", "Investor Flows")
        foreign_label = labels.get("inst_foreign_label", "Foreign")
        inst_label = labels.get("kr_flow_institution_label", "Institutions")
        if report_language == "en":
            head = f"**{flow_label}** ({window}d · as of {date})"
        elif report_language == "ko":
            head = f"**{flow_label}**({window}일 · {date} 기준)"
        else:  # zh
            head = f"**{flow_label}**（{window}日 · 截至{date}）"
        lines.extend([
            f"{head}: {foreign_label} {foreign} / {inst_label} {institution} · {source}",
            "",
        ])
```

(d) `_get_fundamental_blocks`(현재 2034)의 반환 dict에 investor_flows 추출 추가 — 정상 경로(2088-2096 반환 dict) 및 빈 fallback dict(2043-2053) 양쪽에:

빈 fallback dict에:
```python
                "investor_flows": None,
```
정상 반환 dict에:
```python
            "investor_flows": ctx.get("investor_flows") if isinstance(ctx.get("investor_flows"), dict) else None,
```

(e) `_append_fundamental_blocks`(현재 2098-2112)에서 institutional 다음에 KR 수급 호출 추가:

```python
        self._append_institutional_flow(lines, blocks, labels)
        self._append_kr_investor_flows(lines, blocks, labels, report_language)
```

(`report_language`는 이 메서드에 이미 로컬로 있다 — 현재 2106 `report_language = self._get_report_language(result)`.)

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kr_investor_flows_report.py -v`
Expected: PASS

Run: `uv run python -m py_compile src/notification.py src/report_language.py`
Expected: 출력 없음

- [ ] **Step 5: 커밋**

```bash
git add src/report_language.py src/notification.py tests/test_kr_investor_flows_report.py
git commit -m "feat: render KR investor flows summary line in reports (zh/en/ko)"
```

---

### Task 6: 문서 — market-support.md + CHANGELOG (Phase 1에서 이월)

**Files:**
- Modify: `docs/market-support.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:** 없음 (문서 전용, 테스트 미실행)

- [ ] **Step 1: market-support.md 갱신** — 현재 KR 종목 지원 항목에 "투자자별 수급(외국인/기관/개인, 주수)" 지원을 추가한다. **구현 전에 `docs/market-support.md`의 실제 표/섹션 구조를 읽고** KR 행에 수급 열/항목을 기존 서술 스타일로 추가한다(Phase 1은 소비자가 없어 미기재했고, Phase 2에서 사용자 가시 지원이 생겼으므로 이제 기재한다). 무인증 소스(네이버·다음)·fail-open·기준일(최신 확정 거래일) 표기 규칙을 1~2줄로 명시한다.

- [ ] **Step 2: CHANGELOG 갱신** — `docs/CHANGELOG.md`의 `## [Unreleased]` 항목 목록에 플랫 1줄 추가(`### 类目标题` 신설 금지):

```markdown
- [新功能] KR 개별 종목 분석에 투자자별 수급(외국인/기관/개인) 연동: 데이터 품질 전역 블록·LLM 프롬프트·리포트 요약 라인(zh/en/ko)에 반영 — fail-open, 비KR 시장 점수 동작 중립(ADR 0002). 시장 리뷰 연동은 후속 Phase.
```

- [ ] **Step 3: 문서 일관성 확인** — 추가한 파일명·소스명·동작 서술이 실제 구현(Task 1~5)과 일치하는지 대조한다. 중영 이중 문서가 있으면(예: `docs/market-support.*` 영문본) 동기화 필요 여부를 판단하고, 미동기화 시 교부 설명에 이유를 남긴다.

- [ ] **Step 4: 커밋**

```bash
git add docs/market-support.md docs/CHANGELOG.md
git commit -m "docs: document KR per-stock investor flows support"
```

---

### Task 7: 통합 게이트 + 회귀 검증

**Files:** 없음 (검증 전용)

- [ ] **Step 1: 오프라인 전체 게이트**

Run: `uv run pytest -m "not network" -q`
Expected: 기존 전체 스위트 + 신규 테스트 전부 PASS, 실패 0. 특히 비KR 품질 점수 회귀(`test_analysis_context_builder.py`, `test_analysis_context_pack_prompt.py`)가 **동작 중립**으로 통과하는지 확인.

- [ ] **Step 2: lint/CI 게이트**

Run: `uv run ./scripts/ci_gate.sh`
Expected: exit 0 (flake8 포함 통과). 미사용 import(F401)·라인 길이 위반 없는지 확인.

- [ ] **Step 3: 변경 파일 컴파일 확인**

Run: `uv run python -m py_compile data_provider/base.py src/services/analysis_context_builder.py src/analysis_context_pack_prompt.py src/analyzer.py src/notification.py src/report_language.py`
Expected: 출력 없음

- [ ] **Step 4: (선택, 네트워크 있으면) 실 KR 종목 스모크** — `uv run python main.py --stocks 005930.KS --dry-run` 계열로 KR 종목 분석이 예외 없이 완료되고 리포트에 수급 라인이 나오는지 육안 확인. PR 설명에 리포트 스크린샷/발췌를 첨부한다(AGENTS.md: 리포트 렌더링 변경은 스크린샷 필수). 네트워크 미가용 시 이유를 명시하고 오프라인 렌더 테스트(Task 5)로 갈음한다.

---

## 완료 기준 (스펙 §3 대조)

- [ ] KR 종목 분석 시 `fundamental_context["investor_flows"]`에 수급 레코드가 fail-open으로 채워진다(비KR·실패 시 None). (Task 1)
- [ ] 데이터 품질 점수에 `investor_flows` 전역 블록(가중치 5) 반영 + NOT_SUPPORTED 제외 정규화, 비KR 점수 **동작 중립**(회귀 테스트로 고정). (Task 2 · ADR 0002)
- [ ] 컨텍스트 팩 요약(표준+agent 경로 공유)에 수급 블록 상태 라인 zh/en/ko. (Task 3)
- [ ] LLM 프롬프트에 수급 값 섹션 주입(5일 누적+최신일, 주수 단위, "보조 신호" 가이드), 데이터 없으면 생략. (Task 4)
- [ ] 리포트/알림에 결정적 요약 라인(외국인/기관 5일 누적, 최신 확정일·출처, 로케일 주수, 개인 제외) zh/en/ko, 데이터 없으면 생략. (Task 5)
- [ ] `docs/market-support.md` + `docs/CHANGELOG.md` 갱신. (Task 6)
- [ ] 오프라인 게이트(`-m "not network"` + `ci_gate.sh`) 통과, 신규 의존성·설정 0, 비KR 바이트 동일. (Task 7)

**롤백:** 전부 additive(비KR NOT_SUPPORTED로 동작 중립) → PR revert만으로 완전 롤백. 하위 호환 파손 없음(스키마는 동적 dict라 변경 없음, `PipelineAnalysisArtifacts` 무변경).

## 스펙 대비 확정/편차 기록

- **수집 훅(§9 확정)** = `data_provider/base.py` 오프쇼어 fundamental의 `if market=="kr"` 브랜치. 스펙 §3의 "PipelineAnalysisArtifacts 신규 필드"는 채택하지 않음 — 사용자 선택(설계 B, TW 미러). 기능 계약(전역 품질 블록·프롬프트·리포트·fail-open)은 동일 충족.
- **agent 경로 동기화(§9 확정)** = 자동. 컨텍스트 팩 요약이 표준/agent 양쪽 공유 → 블록만 추가하면 전파. per-agent 배선 변경 없음.
- **프롬프트 언어 편차** = analyzer 프롬프트는 zh 고정(주변 프롬프트·TW 선례 일관, 출력 언어는 별도 제어). 스펙 §3 item4의 "zh/en/ko 프롬프트"는 실제 코드 현실과 어긋나며, zh/en/ko 다국어는 컨텍스트 팩 상태 라인(Task 3)과 리포트 라인(Task 5)에서 충족.
- **요약 라인 값 = 5일 누적** = fetcher `summary.foreign_net_5d`/`institution_net_5d`. 스펙 §3 item5 예시(`외국인 +62.6만주`)는 07-10 단일일 수치로 보이나(5일 누적은 부호가 다름), fetcher summary 계약·"5일" 라벨·§2 5일 누적 일관성을 위해 5일 누적을 렌더한다. 검토자가 단일일 표기를 원하면 라인 소스만 `days[0]`로 교체.

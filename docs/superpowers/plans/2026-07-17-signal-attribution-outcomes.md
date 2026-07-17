# 신호 귀인 후험(Signal Attribution Outcomes) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 리포트 `dashboard.signal_attribution`(기술/뉴스/펀더멘털/시장환경 기여도)을 DecisionSignal 생성 시점에 캡처하고, 후험 평가 시 `dominant_attribution` 축으로 동결해 "어떤 신호 유형이 실제로 맞았는가"를 기존 hit/miss 통계의 새 breakdown 축으로 집계한다.

**Architecture:** 캡처는 **두 프로듀서 경로 모두**(일반 추출 `decision_signal_extractor.py` + reassess-persist `decision_signal_reassess_service.py`)가 **하나의 공유 헬퍼**로 `metadata_json.signal_attribution`에 원본 6필드를 저장한다(신호 스키마 불변). 후험은 `_snapshot_fields`가 평가 시점에 metadata에서 지배 귀인 라벨을 파생해 `decision_signal_outcomes.dominant_attribution` 신규 컬럼에 동결한다 — 기존 8개 축(`action`/`market`/…)과 동일한 시맨틱. stats `dimensions`에 축 1개를 추가하면 breakdown→API→Web이 기존 범용 구조로 도달한다.

**Tech Stack:** Python 3 / SQLAlchemy(SQLite `_ensure_*` 수동 마이그레이션 관례) / pytest 오프라인 게이트 / FastAPI 스키마 / React(dsa-web).

**스펙:** `docs/superpowers/specs/2026-07-17-signal-attribution-outcomes-design.md` (D1~D8)
**코드 실측 기준:** main `9a9f5889` (upstream reassess-persist #2014 포함)

## Global Constraints

- **귀인은 후험 리포팅 전용** — 매수/매도 점수·guardrail·Market Light·프롬프트의 입력으로 쓰지 않는다(스펙 비범위).
- **재정규화 금지** — 캡처는 이미 `normalize_report_signal_attribution`으로 정규화된 dict를 복사만 한다. 정규화 소스오브트루스는 `src/utils/data_processing.py` 1곳 유지.
- **all-or-nothing 캡처** — 4가중치(`SIGNAL_ATTRIBUTION_WEIGHT_KEYS`) 중 하나라도 유효 숫자가 아니면 metadata 키 자체를 생략한다. 부분 저장 금지.
- **parity 강제** — 두 프로듀서가 반드시 같은 헬퍼를 import한다. 평행 구현 금지(reassess 경로만 귀인이 빠지는 gap 방지).
- **후험 판정 불변** — hit/miss/neutral 로직·`BacktestEngine`·`engine_version="decision-signal-v1"`·upsert 유니크 키(`signal_id, horizon, engine_version`) 무변경. 축 추가는 스냅샷 확장일 뿐이다.
- **결측 fail-open** — 귀인 없음/무효/파싱 실패 → dominant `None` → 평가·통계·API 정상. 기존 신호(캡처 이전)도 동일.
- **마이그레이션은 기존 관례 복제** — `_ensure_decision_signal_profile_schema`(:1263) 패턴(SQLite 가드→컬럼 검사→ALTER→duplicate 무시→`CREATE INDEX IF NOT EXISTS`)만 사용. 비SQLite는 skip. 컬럼은 nullable, 롤백 시 잔존 무해.
- **dominant 라벨 값 고정**: `"technical" | "news" | "fundamental" | "market" | "mixed" | None`. 최댓값 유일→해당 라벨, 동률→`mixed`, all-zero/결측→None (스펙 D4).
- 커밋 메시지 영어, `Co-Authored-By` 금지, 커밋 제목에 `#patch`/`#minor`/`#major` 금지. 태스크별 커밋은 계획 승인으로 갈음하되 **`git push`/PR 생성은 별도 사용자 확인 필요**.
- Phase 1(Task 1–3, 캡처)과 Phase 2(Task 4–8, 후험 축)는 **독립 PR** — Phase 1 배포만으로 신규 신호부터 데이터 축적이 시작된다.
- 오프라인 게이트: `uv run ./scripts/ci_gate.sh` + (Phase 2) `cd apps/dsa-web && npm run lint && npm run build`.
- `docs/CHANGELOG.md` `[Unreleased]`는 플랫 1줄 형식(`- [类型] 描述`), `###` 소제목 금지.

---

## Phase 1 — 캡처 (스키마 변경 없음)

### Task 1: 공유 추출 헬퍼

`signal_attribution` dict에서 저장용 6필드를 뽑는 순수 함수. 기존 귀인 키 상수가 있는 `src/utils/data_processing.py`에 둔다(스펙 §10 확정: extractor 모듈이 아닌 utils — reassess 서비스가 extractor를 import하지 않고도 재사용).

**Files:**
- Modify: `src/utils/data_processing.py`
- Test: `tests/test_signal_attribution_capture.py` (신규)

**Interfaces:**
- `extract_signal_attribution_for_metadata(dashboard: Optional[Mapping]) -> Optional[Dict[str, Any]]`
  - 입력: `dashboard` dict (top-level `signal_attribution` 키 보유 가정 — 호출자가 `result.dashboard` 또는 `raw_result["dashboard"]`를 전달)
  - 출력: `{technical_indicators, news_sentiment, fundamentals, market_conditions, strongest_bullish_signal, strongest_bearish_signal}` 또는 None

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_signal_attribution_capture.py`:

```python
# -*- coding: utf-8 -*-
"""공유 귀인 캡처 헬퍼 계약: all-or-nothing, 재정규화 없음, fail-open."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.data_processing import extract_signal_attribution_for_metadata


def _dashboard(attr):
    return {"sentiment_score": 70, "signal_attribution": attr}


class TestExtractSignalAttributionForMetadata:
    def test_valid_attribution_copied_verbatim(self):
        attr = {
            "technical_indicators": 40, "news_sentiment": 25,
            "fundamentals": 20, "market_conditions": 15,
            "strongest_bullish_signal": "MA golden cross",
            "strongest_bearish_signal": None,
        }
        out = extract_signal_attribution_for_metadata(_dashboard(attr))
        assert out == attr          # 값 복사 — 재정규화/보정 없음
        assert out is not attr      # 원본 dict 비공유(사본)

    def test_all_zero_is_preserved(self):
        # all-zero("유효 신호 없음")는 저장 대상 — dominant 파생 단계에서 None 처리
        attr = {k: 0 for k in (
            "technical_indicators", "news_sentiment", "fundamentals", "market_conditions")}
        out = extract_signal_attribution_for_metadata(_dashboard(attr))
        assert out["technical_indicators"] == 0
        assert out["strongest_bullish_signal"] is None

    def test_missing_or_invalid_weight_drops_whole_key(self):
        # all-or-nothing: 하나라도 숫자가 아니면 None
        for bad in ({"technical_indicators": None}, {"news_sentiment": "N/A"},
                    {"fundamentals": "abc"}, {}):
            attr = {"technical_indicators": 40, "news_sentiment": 25,
                    "fundamentals": 20, "market_conditions": 15, **bad}
            if not bad:
                attr.pop("market_conditions")
            assert extract_signal_attribution_for_metadata(_dashboard(attr)) is None

    def test_non_dict_inputs_fail_open(self):
        assert extract_signal_attribution_for_metadata(None) is None
        assert extract_signal_attribution_for_metadata({}) is None
        assert extract_signal_attribution_for_metadata({"signal_attribution": "text"}) is None
        assert extract_signal_attribution_for_metadata(_dashboard([1, 2])) is None

    def test_numeric_strings_accepted_as_stored_numbers(self):
        # 정규화기가 놓친 문자열 숫자("40")는 float로 변환해 저장(집계 파생 안전성)
        attr = {"technical_indicators": "40", "news_sentiment": 25.5,
                "fundamentals": 20, "market_conditions": 14.5}
        out = extract_signal_attribution_for_metadata(_dashboard(attr))
        assert out["technical_indicators"] == 40.0
```

- [ ] **Step 2: 실행해 실패 확인** — `uv run pytest tests/test_signal_attribution_capture.py -q` → ImportError 예상.

- [ ] **Step 3: 헬퍼 구현** — `data_processing.py`에 `SIGNAL_ATTRIBUTION_WEIGHT_KEYS`/`SIGNAL_ATTRIBUTION_SIGNAL_KEYS`(:8–17) 바로 아래 함수 추가. 4가중치를 float 변환(실패 시 전체 None), 2텍스트는 str 또는 None passthrough. 새 dict 반환.

- [ ] **Step 4: 테스트 green + 커밋** — `feat: add shared signal-attribution metadata extraction helper`

### Task 2: 일반 추출 경로 캡처

**Files:**
- Modify: `src/services/decision_signal_extractor.py`
- Test: `tests/test_decision_signal_extractor.py` (기존 파일 확장)

- [ ] **Step 1: 실패하는 테스트 작성** — 기존 extractor 테스트 픽스처(AnalysisResult 유사 객체)에 `dashboard.signal_attribution` 추가 후:
  - 유효 귀인 → `payload["metadata"]["signal_attribution"]`에 6필드 그대로 존재
  - 귀인 없음/무효 → metadata에 키 부재
  - 기존 metadata 키들(`decision_profile`, `holding_state` 등) 불변

- [ ] **Step 2: 구현** — `build_decision_signal_payload_from_report`의 metadata 조립부(:143 `holding_state` 직후)에:

```python
    signal_attribution = extract_signal_attribution_for_metadata(dashboard)
    if signal_attribution:
        metadata["signal_attribution"] = signal_attribution
```

(`dashboard`는 :61에서 이미 확보된 지역 변수. import는 기존 `src.utils.data_processing` 사용처와 병합.)

- [ ] **Step 3: green + 커밋** — `feat: capture signal attribution into decision-signal metadata (extractor path)`

### Task 3: reassess-persist 경로 캡처 (parity)

**Files:**
- Modify: `src/services/decision_signal_reassess_service.py`
- Test: `tests/test_decision_signal_api.py` (기존 reassess-persist 시나리오 확장)

- [ ] **Step 1: 실패하는 테스트 작성** — 기존 reassess persist 테스트의 raw_result 픽스처에 `dashboard.signal_attribution`을 넣고, persist된 신호의 `metadata_json`에 `signal_attribution` 6필드가 존재함을 단언. 귀인 없는 픽스처는 키 부재. **preview-only(persist=false) 응답 metadata에도 포함되는지 계약을 함께 고정**(포함 권장 — preview/persist 대칭).

- [ ] **Step 2: 구현** — metadata 조립부(:91–100, `guardrail_result` 직후)에 Task 2와 동일 3줄. dashboard는 `_build_candidate`가 파싱한 것을 재사용하기 어려우면 호출부의 `raw_result.get("dashboard")`로 동일 소스 접근(:215와 같은 `_as_mapping` 경유). **Task 1 헬퍼 import — 평행 구현 금지.**

- [ ] **Step 3: parity 고정 테스트** — 두 경로가 같은 헬퍼를 쓰는지 계약으로 고정:

```python
def test_both_producers_share_capture_helper():
    import src.services.decision_signal_extractor as ex
    import src.services.decision_signal_reassess_service as rs
    from src.utils import data_processing as dp
    assert ex.extract_signal_attribution_for_metadata is dp.extract_signal_attribution_for_metadata
    assert rs.extract_signal_attribution_for_metadata is dp.extract_signal_attribution_for_metadata
```

- [ ] **Step 4: green + Phase 1 게이트** — `uv run ./scripts/ci_gate.sh` 전체 green. 커밋 — `feat: capture signal attribution in reassess-persist path (producer parity)`

### Task 3b: (선택, D6) metadata 백필

기존 신호에 연결 리포트 `raw_result`→dashboard→귀인을 소급 저장. `_backfill_decision_signal_profile_from_metadata`(:1337 인근) 패턴 복제, 부팅 시 idempotent 실행(스펙 §10 권장). **이 태스크는 Phase 1 PR에 포함하되, 리뷰 부담이 크면 별도 후속 PR로 분리 가능** — 분리 시 PR body에 명시.

- [ ] 백필 함수 + 테스트(있음/없음/깨진 raw_result/이미 백필됨-idempotent) + 커밋 — `feat: backfill signal attribution metadata from analysis history`

---

## Phase 2 — 후험 축 + 소비면 (컬럼 1 + 인덱스 1)

### Task 4: outcome 스키마·마이그레이션

**Files:**
- Modify: `src/storage.py`
- Test: `tests/test_storage_decision_signal_outcome_schema.py` (신규)

- [ ] **Step 1: 실패하는 테스트 작성**
  - 신규 DB(`create_all`): `decision_signal_outcomes`에 `dominant_attribution` 컬럼 + `ix_decision_signal_outcome_stats_attribution` 인덱스 존재
  - 기존 DB 시뮬레이션: 컬럼 없는 테이블 생성 후 `DatabaseManager` 부팅 → ALTER로 컬럼·인덱스 생김
  - 마이그레이션 재실행(idempotent): duplicate column 무시
- [ ] **Step 2: 구현**
  - `DecisionSignalOutcomeRecord`(:1078)에 `dominant_attribution = Column(String(16), nullable=True)` (스냅샷 컬럼 블록 `holding_state` 옆), `__table_args__`에 `Index('ix_decision_signal_outcome_stats_attribution', 'engine_version', 'dominant_attribution', 'horizon')` 추가(:1114 기존 2개와 동일 모양)
  - `_ensure_decision_signal_outcome_attribution_schema()` — `_ensure_decision_signal_profile_schema`(:1263) 복제(SQLite 가드/`has_table`/`get_columns`/`ALTER TABLE ... ADD COLUMN dominant_attribution VARCHAR(16)`/duplicate 무시/`CREATE INDEX IF NOT EXISTS`)
  - `DatabaseManager.__init__`의 `_ensure_*` 목록(:1214–1215)에 호출 추가
- [ ] **Step 3: green + 커밋** — `feat: add dominant_attribution column to decision signal outcomes`

### Task 5: 후험 스냅샷·통계 축

**Files:**
- Modify: `src/services/decision_signal_outcome_service.py`
- Test: `tests/test_decision_signal_outcome_service.py` (기존 확장)

- [ ] **Step 1: 실패하는 테스트 작성**
  - `_dominant_attribution` 단위: 유일 최댓값(4라벨 각각) / 동률→`mixed` / all-zero→None / metadata 키 없음→None / metadata_json 깨짐→None / 문자열 숫자 가중치
  - 통합: 귀인 있는 신호 평가 → outcome row `dominant_attribution` 동결; **unable 경로**(예: unsupported_horizon)에서도 축 스냅샷; 귀인 없는 신호 → None; 기존 8축 값 불변
  - stats: `get_stats().breakdowns["dominant_attribution"]` 버킷 등장, None 값 처리, 기존 축 breakdown 불변
  - 직렬화: `_serialize_outcome` 출력에 필드 포함
- [ ] **Step 2: 구현**
  - `_dominant_attribution(self, signal) -> Optional[str]` — `_holding_state`(:518)와 같은 스타일로 `signal.metadata_json` 파싱 → `signal_attribution` → 4가중치 float 변환 → D4 규칙. 라벨 매핑: `technical_indicators→technical`, `news_sentiment→news`, `fundamentals→fundamental`, `market_conditions→market`
  - `_snapshot_fields`(:451) dict에 `"dominant_attribution": self._dominant_attribution(signal)` 추가 (evaluate/unable 경로는 `**base` 전파로 자동)
  - `get_stats` `dimensions`(:318)에 `"dominant_attribution"` 추가; `_serialize_outcome`(:631)에 필드 추가
- [ ] **Step 3: green + 커밋** — `feat: snapshot dominant attribution axis in outcome evaluation and stats`

### Task 6: API 스키마

**Files:**
- Modify: `api/v1/schemas/decision_signals.py`
- Test: `tests/test_decision_signal_outcome_api.py`, `tests/test_api_schema_pydantic.py`, `tests/test_decision_signal_docs.py` (기존 확장)

- [ ] `DecisionSignalOutcomeItem`(:130–154)에 `dominant_attribution: Optional[str] = None` 추가. stats bucket은 범용(dimension/value)이라 불변.
- [ ] outcomes API 응답에 필드 노출 + stats breakdown에 새 dimension 포함을 API 레벨 테스트로 고정. `docs/architecture/api_spec.json` 정합(문서 테스트 통과) 확인.
- [ ] 커밋 — `feat: expose dominant attribution in decision-signal outcome API`

### Task 7: Web 소비

**Files:**
- Modify: `apps/dsa-web/src/types/decisionSignals.ts`, `apps/dsa-web/src/api/decisionSignals.ts`(필요 시), `apps/dsa-web/src/pages/DecisionSignalsPage.tsx`, `apps/dsa-web/src/i18n/uiText.ts`
- Test: `apps/dsa-web/src/api/__tests__/decisionSignals.test.ts`, 페이지 테스트 (기존 확장)

- [ ] outcome item 타입에 `dominantAttribution?: string | null` 추가(camelCase 변환은 범용 통과 — 테스트로 고정).
- [ ] 성과 카드 영역에 `dominant_attribution` breakdown 표시 — **기존에 노출 중인 breakdown 카드 스타일 재사용**(신규 컴포넌트 금지). 라벨 i18n(zh/en/ko): `technical=技术面/Technical/기술`, `news=消息面/News/뉴스`, `fundamental=基本面/Fundamentals/펀더멘털`, `market=市场环境/Market/시장환경`, `mixed=混合/Mixed/혼합`, None 버킷=`未归因/Unattributed/귀인 없음`.
- [ ] `cd apps/dsa-web && npm run lint && npm run build` + vitest green. 커밋 — `feat: show dominant attribution breakdown on decision signals page`
- [ ] **PR 증거**: Web UI 변경이므로 PR body에 화면 스크린샷(불가 시 사유 + stats API 응답 코드블록 대체 증거).

### Task 8: 문서·CHANGELOG·최종 게이트

**Files:**
- Modify: `docs/decision-signals.md`, `docs/CHANGELOG.md`

- [ ] `docs/decision-signals.md` — 후험 "평가 시점 동결 통계 차원" 목록에 `dominant_attribution` 추가, 캡처 계약(두 프로듀서·all-or-nothing·라벨 규칙·None 시맨틱), 마이그레이션 절에 신규 `_ensure_*` 항목·비파괴 롤백 서술.
- [ ] `docs/CHANGELOG.md` `[Unreleased]` 플랫 1줄 × 2 (Phase 1/2 각각): `- [新功能] 신호 귀인 후험 ...` — 캡처(두 경로), dominant 축, stats breakdown, API/Web 노출, 점수·판정 불변 명시.
- [ ] 최종 게이트: `uv run ./scripts/ci_gate.sh` + web lint/build. 커밋 — `docs: document signal attribution outcome axis and capture contract`

---

## 검증 매트릭스 (교차 확인용)

| 계층 | 수용 기준 |
|---|---|
| 캡처 | 두 프로듀서 payload metadata에 동일 계약; 무효 입력 시 키 부재; 같은 헬퍼 공유(parity 테스트) |
| 신호 스키마 | `decision_signals` 테이블 무변경 |
| 후험 | outcome `dominant_attribution` 동결(evaluate+unable); 판정·8축·유니크 키·engine_version 불변 |
| stats | breakdown에 새 축; None 버킷; 기존 축 불변 |
| API | item 필드 additive; api_spec/docs 테스트 green |
| Web | 타입+breakdown 카드; lint/build/vitest green; 스크린샷 또는 대체 증거 |
| 마이그레이션 | 신규/기존 DB 양쪽 컬럼·인덱스; idempotent; 비SQLite skip |

## 롤백

- Phase 1: 커밋 revert — metadata 키는 additive 잔존 무해(소비자 없음 상태로 복귀).
- Phase 2: 커밋 revert — nullable 컬럼·인덱스 잔존 무해(기존 `decision_profile` 비파괴 원칙). stats 축/API 필드는 코드 revert로 즉시 제거.
- outcome 재계산 불요(판정 불변). 백필 롤백 불요(원본 raw_result 불변).

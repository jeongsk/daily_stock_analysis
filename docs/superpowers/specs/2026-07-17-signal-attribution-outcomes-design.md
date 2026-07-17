# 신호 귀인 후험(Signal Attribution Outcomes) — 설계 스펙 초안

- 작성일: 2026-07-17
- 상태: **초안 (권장안 기반 — 사용자 확정 대기)**
- 관련 영역: DecisionSignal 캡처·후험(outcome) 스냅샷·통계 집계·API/Web
- 선행 컨텍스트: 로드맵 D. 코드 실측 2026-07-17 (rebased main `9a9f5889`, upstream reassess-persist #2014 포함)
- 관련 문서: `docs/decision-signals.md`(후험·사이드카·마이그레이션 계약), `src/schemas/report_schema.py:129`(SignalAttribution)

## 1. 개요와 목표

리포트의 `dashboard.signal_attribution`(기술지표/뉴스/펀더멘털/시장환경 기여도 0~100, 합 100 정규화 + 최강 강세/약세 신호 텍스트)은 현재 **표시 전용**이다 — `AnalysisHistory.raw_result` JSON blob에만 저장되고, `DecisionSignalRecord` → `DecisionSignalOutcomeRecord`(hit/miss/neutral 후험) 파이프라인에는 배선되어 있지 않다(grep 교차 0건, 실측 확인).

이번 작업은 귀인을 **신호 생성 시점에 캡처**하고, **후험 평가 시점에 지배 귀인(dominant attribution)을 스냅샷**해서, "어떤 신호 유형이 실제로 맞았는가"를 기존 후험 통계의 새 축으로 집계 가능하게 만든다.

### 성공 기준

- 신규 생성·재평가(persist)되는 DecisionSignal이 생성 시점의 정규화된 `signal_attribution`을 보존한다 — **두 프로듀서 경로 모두**(일반 추출 + reassess-persist), parity gap 없음.
- 후험 평가 시 outcome 레코드에 `dominant_attribution` 축이 동결되고, `GET /outcomes/stats`의 breakdown에 이 축이 추가된다.
- 귀인이 없거나 무효인 신호도 기존과 동일하게 평가된다(축 값만 None) — **후험 파이프라인 무회귀**.
- 기존 8개 통계 축, hit/miss/neutral 판정, engine_version, 멱등 upsert 계약 불변.

### 명시적 비범위 (YAGNI)

- 귀인을 **매수/매도 점수·guardrail·Market Light의 입력으로 쓰지 않는다** — 후험 리포팅 전용(KR 수급 비범위 결정과 동일 원칙).
- LLM 프롬프트/스키마 변경 없음 — 기존 `SignalAttribution` 산출물을 소비만 한다.
- 4개 가중치의 회귀분석·가중 hit-rate 등 고급 분석 없음 — 1차는 지배 귀인 라벨 축 1개.
- 리포트 본문/알림 렌더 변경 없음 — 소비면은 outcomes stats API + Web 통계 카드만.
- 자동 배치 트리거 신설 없음 — 후험 실행은 기존 `POST /outcomes/run` 수동/명시 호출 유지.
- 비SQLite 엔진용 마이그레이션 도구 도입 없음(기존 `_ensure_*` SQLite 관례 유지).

## 2. 확정할 설계 결정 (권장안)

| # | 결정 | 권장값 | 근거 |
|---|---|---|---|
| D1 | 귀인 저장 형태 | **원본 4가중치+최강신호는 신호 metadata에, 집계 축은 파생 `dominant_attribution` 라벨** | 원본 보존(향후 고급 분석 여지) + 집계는 기존 8축과 동일한 단일 문자열 컬럼 패턴 |
| D2 | 신호 측 저장 위치 | **`DecisionSignalRecord.metadata_json`의 `signal_attribution` 키** (컬럼 신설 없음) | `holding_state`/`data_quality_level`이 이미 metadata JSON→평가시점 파생으로 동작하는 선례; decision_signals 주 테이블 불변(P5 원칙) |
| D3 | outcome 측 저장 위치 | **`decision_signal_outcomes`에 `dominant_attribution` 컬럼 1개 + stats 인덱스** | 기존 8축 스냅샷과 동일 구조; SQL 집계 직접 가능 |
| D4 | dominant 판정 규칙 | 정규화된 4가중치 중 **최댓값 라벨**(`technical`/`news`/`fundamental`/`market`). 동률 → `mixed`, 전부 0·결측·무효 → None | 결정적·설명 가능; None은 기존 축들의 결측 처리와 동일 |
| D5 | 프로듀서 커버리지 | **두 경로 모두 필수**: `decision_signal_extractor.py`(일반) + `decision_signal_reassess_service.py`(reassess-persist) | 한쪽만 하면 reassess 신호만 귀인 누락되는 parity gap(실측 확인된 리스크) |
| D6 | 기존 신호 백필 | **metadata 백필 함수 제공(기존 `_backfill_decision_signal_profile_*` 패턴), 기존 outcome 재평가는 자동 안 함** — 재평가는 기존 `force` 재실행에 위임 | 백필은 `AnalysisHistory.raw_result`에서 복원 가능; outcome 강제 재계산은 사용자 통제 유지 |
| D7 | engine_version | **`decision-signal-v1` 유지** (버전 bump 없음) | 판정 로직 불변 — 축 추가는 스냅샷 확장이지 평가 의미 변경이 아님; bump 시 유니크 키 분리로 중복 행 발생 |
| D8 | 소비면 | stats `dimensions`에 `dominant_attribution` 추가 + outcome item 직렬화/API 스키마/Web 타입·breakdown 노출 + `docs/decision-signals.md` | breakdown 스키마는 범용(dimension/value)이라 additive |

## 3. 아키텍처와 데이터 흐름

```
[리포트 생성]  dashboard.signal_attribution (이미 정규화됨 — normalize_report_signal_attribution)
      │
      ├─ (a) 일반 경로: pipeline → decision_signal_extractor.build_decision_signal_payload_from_report
      │       dashboard 접근 지점(:61)에서 metadata["signal_attribution"] = {t,n,f,m,strongest_bullish,strongest_bearish}
      │
      └─ (b) reassess-persist 경로: decision_signal_reassess_service._build_persist_payload
              _build_candidate가 이미 dashboard를 파싱(:215) → 동일 키를 metadata에 포함
      │
DecisionSignalRecord.metadata_json  (스키마 불변, D2)
      │
[후험 평가]  DecisionSignalOutcomeService._snapshot_fields (:449)
      │        + _dominant_attribution(signal) 헬퍼 — metadata_json 파싱, D4 규칙
      ▼
DecisionSignalOutcomeRecord.dominant_attribution  (신규 컬럼, D3)
      │
get_stats() dimensions += "dominant_attribution"  →  API breakdown  →  Web 통계 카드
```

핵심 원칙:

- **캡처는 생성 시점, 축 동결은 평가 시점** — 기존 8축과 동일한 시맨틱(평가 후 신호 metadata가 바뀌어도 outcome 스냅샷 불변).
- 귀인 값은 이미 정규화된 dict를 **그대로 복사**(재정규화 없음). 정규화 소스오브트루스는 기존 `normalize_report_signal_attribution` 1곳 유지.
- 모든 신규 로직은 결측 허용(fail-open): 귀인 없음 → metadata 키 생략 → dominant None → 평가·통계 정상.

## 4. 컴포넌트 (파일별 책임)

### 수정 — Phase 1 (캡처)

- **`src/services/decision_signal_extractor.py`** — payload metadata(:105–143 인근)에 `signal_attribution` 추가. dashboard에서 6개 필드만 추출하는 `_extract_signal_attribution(dashboard) -> Optional[dict]` 헬퍼(4가중치 숫자 검증, 아니면 None).
- **`src/services/decision_signal_reassess_service.py`** — `_build_persist_payload`(:162–198)의 metadata에 동일 헬퍼 적용(extractor의 헬퍼 import 재사용, 평행 구현 금지).
- **`src/storage.py`** — 스키마 불변. (선택) `_backfill_decision_signal_attribution_from_history()` 백필 함수: 신호의 연결 리포트 `raw_result`→dashboard→metadata 갱신, 기존 `_backfill_decision_signal_profile_*`(:1337 인근) 패턴 복제. D6.

### 수정 — Phase 2 (후험 축·소비)

- **`src/storage.py`** — `DecisionSignalOutcomeRecord`(:1078–1116)에 `dominant_attribution = Column(String, nullable=True)` + `ix_decision_signal_outcome_stats_attribution (engine_version, dominant_attribution, horizon)` 인덱스. `_ensure_decision_signal_outcome_attribution_schema()` 마이그레이션(기존 `_ensure_decision_signal_profile_schema` :1259 패턴: SQLite 가드→컬럼 검사→ALTER→duplicate 무시→인덱스), `DatabaseManager.__init__`의 `_ensure_*` 목록에 추가.
- **`src/services/decision_signal_outcome_service.py`** — `_snapshot_fields`(:449)에 `dominant_attribution` 추가(D4 규칙 헬퍼 — `_holding_state`/`_data_quality_level`와 같은 metadata 파싱 스타일). `get_stats` `dimensions`(:291–300)에 축 추가. `_serialize_outcome`(:622)에 필드 추가. evaluate/unable 경로는 `**base` 전파로 자동 커버.
- **`api/v1/schemas/decision_signals.py`** — `DecisionSignalOutcomeItem`(:130–154)에 `dominant_attribution: Optional[str]` 추가. stats bucket 스키마는 범용이라 불변.
- **`apps/dsa-web/src/types/decisionSignals.ts` + `api/decisionSignals.ts`** — item 타입 필드 추가(camelCase 변환은 범용 통과).
- **`apps/dsa-web/src/pages/DecisionSignalsPage.tsx`** — 기존 성과 카드 영역에 `dominant_attribution` breakdown(라벨 4+mixed, zh/en/ko i18n) 추가. 기존 카드 레이아웃·다른 축 노출 불변.

### 불변 (검증만)

- `BacktestEngine`·hit/miss/neutral 판정·`RETRYABLE_UNABLE_REASONS`·feedback — 무변경.
- 기존 8개 스냅샷 축과 인덱스, `upsert_outcome` 유니크 키(`signal_id, horizon, engine_version`) — 무변경.
- `SignalAttribution` Pydantic 스키마·`normalize_*` 유틸·LLM 프롬프트 — 무변경.

## 5. 데이터 계약

### 신호 metadata (`metadata_json.signal_attribution`)

```python
{
  "technical_indicators": 40,   # int/float 0~100, 합계 100 (기존 정규화 결과 그대로)
  "news_sentiment": 25,
  "fundamentals": 20,
  "market_conditions": 15,
  "strongest_bullish_signal": "...",   # Optional[str]
  "strongest_bearish_signal": "...",   # Optional[str]
}
```

- 4가중치 중 하나라도 숫자가 아니면 **키 자체를 생략**(부분 저장 금지 — all-or-nothing).
- 전부 0("유효 신호 없음" 시맨틱)은 **저장은 하되** dominant 파생 시 None.

### outcome 축 (`dominant_attribution`)

`"technical" | "news" | "fundamental" | "market" | "mixed" | None`

- 최댓값 유일 → 해당 라벨. 최댓값 동률(2개 이상) → `"mixed"`. metadata 키 없음·전부 0·파싱 실패 → `None`.
- stats breakdown에서 None은 기존 축들의 결측 처리와 동일하게 취급.

## 6. 오류 처리 & 엣지 케이스

| 상황 | 처리 |
|---|---|
| dashboard에 signal_attribution 없음/무효 | metadata 키 생략 → dominant None → 평가·통계 정상 (fail-open) |
| all-zero 귀인(유효 신호 없음) | 원본은 저장, dominant None (D4) |
| 가중치 동률 | `mixed` — 임의 tie-break로 잘못된 축 귀속 방지 |
| 기존 신호(캡처 이전 생성) | dominant None; D6 백필 실행 시 소급 가능, outcome은 force 재실행 시에만 갱신 |
| reassess-persist로 refresh된 신호 | (b) 경로가 최신 리포트 dashboard 기준으로 metadata 갱신 — 이후 평가는 갱신값 스냅샷 |
| 이미 completed된 outcome | 자동 재계산 없음(D6/D7); `force=true` 재실행 시 upsert로 축 채워짐 |
| metadata_json 파싱 실패 | 기존 `_holding_state` 등과 동일하게 삼키고 None |
| 비SQLite 엔진 | 마이그레이션 skip(기존 `_ensure_*` 가드와 동일) — 문서에 명시 |

## 7. 테스트 전략 (오프라인·결정적)

- **캡처**: extractor payload에 귀인 포함/생략(무효 입력) 케이스; reassess-persist payload 동일 계약(parity 고정 테스트 — 두 경로가 같은 헬퍼를 쓰는지); 백필 함수(있음/없음/깨진 raw_result).
- **후험**: `_dominant_attribution` 단위(유일 최댓값/동률→mixed/all-zero→None/키 없음→None/문자열 숫자); `_snapshot_fields` 통합; unable 경로에서도 축 스냅샷; 기존 8축 회귀.
- **stats/API**: breakdown에 새 축 등장, None 버킷 처리, 기존 축 불변; outcome item 직렬화; `tests/test_decision_signal_outcome_service.py`/`..._api.py` 확장 + `test_api_schema_pydantic.py`/`test_decision_signal_docs.py` 정합.
- **마이그레이션**: 신규 DB(create_all)와 기존 DB(ALTER 경로) 양쪽에서 컬럼·인덱스 존재; duplicate column 무시; 비SQLite skip.
- **Web**: item 타입/breakdown 렌더 스냅샷, i18n 3언어.
- **게이트**: `./scripts/ci_gate.sh` + `cd apps/dsa-web && npm run lint && npm run build`.

## 8. 단계 분리 (각각 독립 PR)

| Phase | 내용 | 스키마 변경 |
|---|---|---|
| 1 | 캡처: 두 프로듀서 metadata 배선 + (선택) 백필 함수 + 테스트 | 없음 |
| 2 | 후험 축: outcome 컬럼+마이그레이션+스냅샷+stats+API/Web+docs | `decision_signal_outcomes` 컬럼 1 + 인덱스 1 |

Phase 1 단독으로도 데이터 축적이 시작되므로(신규 신호부터), Phase 2 배포 시점에 이미 귀인 있는 신호가 존재하게 되는 순서 이점이 있다.

## 9. 롤백

- Phase 1: 커밋 revert — metadata 키는 additive라 잔존해도 무해(소비자 없음).
- Phase 2: 커밋 revert — SQLite ALTER 컬럼은 잔존하지만 nullable·미소비라 무해(기존 `decision_profile` 마이그레이션과 동일한 비파괴 원칙). stats 축·API 필드는 코드 revert로 즉시 사라짐.
- 데이터 정리 불필요 — 판정·유니크 키 불변이므로 outcome 재계산 불요.

## 10. 구현 계획에서 확정할 항목

- `_extract_signal_attribution` 헬퍼의 배치 위치(extractor 모듈 vs `src/utils/data_processing.py` — 후자는 이미 귀인 키 상수 보유).
- 백필 함수의 실행 방식(부팅 시 자동 1회 vs 수동 스크립트) — 권장: `decision_profile` 백필 선례를 따라 부팅 시 idempotent 실행.
- Web breakdown UI의 구체 형태(기존 카드 스타일 재사용 범위)와 i18n 라벨 문구.
- `mixed`/None 라벨의 Web 표기(예: ko "혼합"/"귀인 없음").
- `docs/decision-signals.md` 갱신 위치(통계 차원 목록 + 마이그레이션 절).

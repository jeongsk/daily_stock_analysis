# 한국어 보고서 생성 단절(Gap) 수정 계획

## 배경 / 진단

한국어 UI를 선택해도 분석 보고서(市場情绪 / 操作建议 / 觀望 / 板块联动 / 策略点位 / 狙击点位 / 理想买入 / 止损价位 / 止盈目标 등)가 중국어로 나오는 현상을 조사한 결과, **한국어 번역 자원은 이미 대부분 구비**되어 있으나 **활성화 경로가 2곳 단절**되어 `report_language=ko`로 분석이 생성되지 않는 것이 원인.

이미 정상인 한국어 자원(수정 불필요):
- 프론트 라벨: `apps/dsa-web/src/utils/reportLanguage.ts` ko 사전
- 백엔드 enum/라벨: `src/report_language.py`, `src/schemas/decision_action.py` ko 컬럼
- LLM 한국어 프롬프트: `src/core/pipeline.py:1244`

## 핵심 결정사항

1. **UI 언어 → 분석 요청 `report_language` 강제 연동**: 분석 트리거 시 항상 `reportLanguage = uiLanguage`를 전송(zh→zh, en→en, ko→ko). 이것이 한국어(및 영어) 보고서 생성을 켜는 단일 스위치.
2. **API 요청 스키마에 `ko` 허용**: 현재 `Literal["zh","en"]`이 `ko`를 422로 거부하므로 `Literal["zh","en","ko"]`로 확장.
3. **값 prefix 파서는 검증 후 조건부 확장**: 한국어 LLM 출력이 파서 정제/수치 추출에 영향을 주는지 end-to-end로 확인한 뒤, 필요시 한국어 prefix만 추가.

## 작업 목록

### Gap 1 — 프론트 UI↔보고서 언어 연동
- `apps/dsa-web/src/pages/HomePage.tsx`
  - `submitAnalysis(...)` 호출 2곳(`handleSubmitAnalysis` ~L413, 재분석 ~L454)에 `reportLanguage: uiLanguage` 옵션 추가. (`uiLanguage`는 이미 `useUiLanguage()`로 보유, L49)
  - `stockPoolStore.ts:794`는 이미 `options.reportLanguage`를 `analyzeAsync`에 전달하므로 스토어 자체는 수정 불필요(전달 누락이 원인).
- `apps/dsa-web/src/pages/__tests__/HomePage.test.tsx`
  - L692, L1123 의 `not.toHaveProperty('reportLanguage')` 단언을 "전송됨(= uiLanguage)" 기대로 수정.
  - ko-UI 시 요청에 `reportLanguage: 'ko'` 포함 케이스 추가.

### Gap 2 — API 스키마 ko 허용
- `api/v1/schemas/analysis.py`: L83, L119 `Literal["zh","en"]` → `Literal["zh","en","ko"]`
- `api/v1/schemas/decision_signals.py:56`: 동일 확장
- `api/v1/schemas/history.py`: `report_language` 필드의 Literal/description/example을 zh/en/ko로 갱신 (L135, L280 확인)
- 스키마 테스트 추가: `report_language="ko"` 가 요청 검증을 통과하는지 단언.

### Gap 3(검증 항목) — 값 prefix 파서 한국어 대응
- 한국어 보고서 1건 end-to-end 생성 후 아래 확인:
  - `src/services/report_renderer.py:52` `_strip_value_prefix`
  - `src/notification.py:1015`
  - `bot/commands/ask.py:493`
- 전부 zh/en prefix만 매칭(`理想买入点：/止损位：/Ideal Entry:` 등). LLM이 한국어 prefix(예: "이상적 진입:")로 값을 내보내면:
  - 표시 값에 prefix가 그대로 남거나, `analysis_history` 수치열(狙击点位)이 비게 될 수 있음.
  - 영향 확인 후 한국어 prefix를 3개 위치에 추가. (미발생 시 본 항목은 no-op)

## 검증

- 백엔드: `./scripts/ci_gate.sh` + 신규 스키마 테스트 + `python -m pytest tests/test_report_language.py -m "not network"`
- 프론트: `cd apps/dsa-web && npm run lint && npm run build`; HomePage ko-UI → `report_language=ko` 전송 테스트 통과
- E2E 스모크: 한국어 보고서 1건 생성(오프라인 가능 경로) → 라벨/enum/LLM 본문이 한국어, 전략 포인트 값 정상 표시 확인

## 리스크

1. **en-UI 사용자 동작 변화(낮음)**: 연동으로 en-UI 분석이 zh→en으로 변경. 기존 zh 기본 사용자(zh-UI)는 영향 없음(zh→zh). 의도된 효과이나 배포 노트에 명시.
2. **LLM 한국어 JSON 품질(중간)**: 한국어 프롬프트 출력이 스키마를 준수하는지 E2E로 확인.
3. **ko 값 파서 누락(낮음)**: Gap 3 검증으로 방어.

## 회滚

- Gap 1·2 변경을 revert하면 즉시 기존(zh 기본) 동작으로 복귀. 보고서 생성 자체에는 영향 없음.
- 테스트 단언도 함께 revert.

## Out of Scope

- 별도 평가서(`plans/korean-i18n-hardcoded-fixes.md`)의 UI 하드코딩 중국어 수정(ChatPage/StockScreeningPage/PortfolioPage 등) — 본 계획은 보고서 생성 언어 경로만 다룸.
- 데스크톱 리소스 번들 분리, 추가 언어(일본어 등).

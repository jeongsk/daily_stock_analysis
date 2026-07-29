# World Monitor 이벤트 → 시장 리뷰 연동 구현 계획

**Spec:** `docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md`

TDD 적용 지점을 각 단계에 명시한다. 정규화, 신선도 판정, fail-open, 미래정보
차단 경로는 테스트를 먼저 작성한다.

## Phase 1: 설정

1. `src/config.py`에 신규 6개 설정과 파싱·검증을 추가한다(§10).
2. 양수·범위 검증을 붙이고 잘못된 값은 기본값으로 강등한다.
3. `.env.example`, `.env.example.ko`에 동일 항목을 반영한다.
4. 설정 파싱 테스트를 추가한다.

## Phase 2: 저장 모델

1. `src/storage.py`에 `WorldEvent`와 `WorldEventSyncState`를 추가한다(§4, §7.1).
2. `(category, external_id)` unique 제약과 조회 인덱스를 건다.
3. `src/repositories/world_event_repo.py`를 `IntelligenceRepository` 관례에 맞춰
   작성한다: upsert, 조회 창 질의, 보관 정리, sync state 읽기/쓰기.
4. **테스트 우선**: 멱등 upsert, `occurred_at`/`collected_at` 불변,
   진행 중 → 종료 전이, 미래 발생 시각 거부(§4.1), 보관 정리.

## Phase 3: 정규화

1. 세 소스별 projector를 작성한다(§3 필드 매핑표).
2. `severity_rank` 정수 매핑을 §9 표 그대로 구현한다.
3. `scope` 판정(`market`/`global`/`unmapped`)을 §8.1 그대로 구현한다.
4. **테스트 우선**: 소스별 매핑, 발생 시각 보존, 결측 필드 강등, 빈 `countries`가
   `unmapped`가 되는 경로, `severity_rank` 동률 tiebreak.

## Phase 4: 동기화 서비스

1. 기존 `src/services/worldmonitor_service.py`를 확장한다. 새 서비스 모듈을
   만들지 않는다(§5).
2. 세 엔드포인트 GET 클라이언트를 추가하고 전체 시간 예산을 적용한다(§6.1).
3. 쿨다운, 카테고리별 부분 실패 격리, 전체 fail-open을 구현한다(§6).
4. 신선도 판정을 `last_success_at` + `last_nonempty_at` 조합으로 구현한다(§7, §7.1).
5. 동기화 성공 직후 보관 정리를 수행한다(§11).
6. **테스트 우선**: fail-open(예외 미전파), 시간 예산 소진, `upstreamUnavailable`,
   항상 빈 응답 → `unverified`, 0건이지만 `last_nonempty_at` 유효 → "해당 없음" 허용.

## Phase 5: 프롬프트 주입

1. `MarketAnalyzer`에 `_build_worldmonitor_prompt_block`을 기존
   `_build_kr_*_prompt_block` 관례대로 추가하고 en/ko/zh 3개 언어를 제공한다(§9).
2. `_build_review_prompt`에 조건부로 삽입한다. 비활성이면 기존 프롬프트와
   바이트 동일해야 한다.
3. `run_market_review`가 분석기 생성 전에 동기화를 1회 호출하도록 연결한다(§6).
4. **테스트 우선**: 카테고리별 상한과 독점 방지, 신선도 문구, `unmapped` 제외,
   미래 이벤트 제외, 비활성 시 프롬프트 무변경.

## Phase 6: 진단

1. 기존 진단 표면에 카테고리별 요약을 추가한다(§12).
2. `unverified`/`unavailable` 구분과 `unmapped` 건수를 노출한다.
3. 오류 문자열은 `sanitize_diagnostic_text`를 거친다.
4. secret redaction 테스트를 추가한다.

## Phase 7: 문서와 검증

1. `docs/intelligence-sources.md` 경계 갱신, `docs/CHANGELOG.md` `[Unreleased]`
   扁平 1줄 추가.
2. `uv run pytest -m "not network"` 전체 실행.
3. `./scripts/ci_gate.sh` 실행.
4. 시장 리뷰 프롬프트 변경이므로 영향받는 리포트 산출물 증거를 확보한다.
5. 온라인 검증(실제 스택 대상 동기화, seeder 중단 시 `unavailable` 실증)은
   가능하면 수행하고, 불가하면 미검증 항목으로 명시한다.

## 범위 밖

- 개별 종목 분석·점수·매매 판단 변경
- `intelligence_items` 병합
- 신규 공개 API 엔드포인트
- 산업·섹터 노출도 매핑
- 백테스트 실행

## 원격 상태 변경

사용자의 명시적 승인 없이 push, tag, PR 생성을 하지 않는다.

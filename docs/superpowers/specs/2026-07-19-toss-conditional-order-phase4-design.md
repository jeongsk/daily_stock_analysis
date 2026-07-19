# 토스증권 서버측 조건주문 (Phase 4) — 설계 스펙

- 작성일: 2026-07-19
- 상태: 설계 확정 (그릴링 결정 8건 반영: Toss 서버측 감시 / 등록=승인 / SINGLE-STOP만 / 자동 제안 비범위 / 등록 시 한도 즉시 산입 / expireDate 최대 7일 / 정정 비범위 / API-only)
- 선행: `2026-07-17-toss-order-phase3-design.md`(상태기계·게이트·한도·감사 인프라 전제), `2026-07-17-toss-portfolio-sync-design.md`(연동 계좌·체결 반영 전제), ADR 0003
- 사전 조사: `.claude/reviews/2026-07-19-toss-phase4-probe.md` (OpenAPI v1.2.4 실측)
- 관련 영역: `data_provider/toss_fetcher.py`, `src/services/portfolio_order_service.py`, `src/repositories/portfolio_repo.py`, `src/storage.py`, `api/v1/endpoints/portfolio.py`

## 1. 개요와 목표

연동 계좌(Phase 2) + 수동 승인 주문(Phase 3) 위에 **Toss 서버측 조건주문**을
추가한다. 흐름은 Phase 3와 같은 2단계 — 조건주문 제안 생성 → 사용자 승인 —
이되, **승인의 의미가 다르다: 승인 = Toss에 조건주문을 등록하는 것이며, 등록
후 조건이 충족되면 Toss가 사용자 개입 없이 자동으로 실주문을 낸다.** 이는
Phase 3의 "자동 매매는 없다" 대비 자동화 수위가 한 단계 올라가는 것을
명시적으로 수용한 결정이다(그릴링 결정 2). 트리거 순간의 재승인은 Toss API
구조상 불가능하므로, 승인 시점에 "이 조건이 충족되면 자동 체결되는 것에
동의"가 계약이 된다.

감시가 Toss 서버에서 이루어지므로 로컬 프로세스 생존·허용 IP 등록 여부와
무관하게 감시가 지속된다(ADR 0003과의 정합 — 등록/조회/취소 API 호출만
허용 IP에서 가능).

### 성공 기준

- dry-run 기본 유지: `TOSS_ORDER_LIVE` strict `"true"`가 아니면 어떤 경로로도
  Toss 조건주문 write(`POST/DELETE /api/v1/conditional-orders*`)가 발생하지
  않음을 테스트로 보장. **fetcher 레벨 URL 게이트가 `/conditional-orders`
  경로를 반드시 포함** — 현행 `_ORDERS_URL_PATH` 매칭은 이 경로를 잡지
  못하므로(probe B-1 실측) 게이트 확장 없이는 dry-run이 뚫리는 회귀가 된다.
  이 확장이 Phase 4의 첫 번째 구현 태스크이자 회귀 테스트 대상.
- Phase 3 안전장치 전부 상속: 1회 100만 / 일일 500만 KRW 한도(조건주문은
  등록 시 즉시 산입), 인증 필수(`ADMIN_AUTH_ENABLED` + 세션), FX
  fail-closed(비 KRW), `confirmHighValueOrder` 절대 미전송(1억 이상 무조건
  거부), append-only 감사로그, `clientOrderId` 멱등(우리 쪽 필수 관례 유지).
- 등록 응답 유실 시에도 "기록 없으면 등록 없음" 불변식 유지 —
  `registration_unknown` 상태 + reconcile 경로로 수렴.

### 명시적 비범위

- OCO/OTO(후속 — SINGLE만), `PROFIT_RATE` 트리거(문서 내부 상충으로 실사용
  미확인 — probe A표), 트레일링(Toss 미지원), 조건주문·일반주문 정정(취소 후
  새 제안으로 재등록만), 자동 주문 제안 생성(Phase 5 후보), Web/봇 UI
  표면(API-only), 다중 사용자 인증, GTC/예약주문(Toss에 개념 없음).

## 2. API 제약 (스펙 v1.2.4 실측 — probe A표 요약)

| 항목 | 사실 | 귀결 |
|---|---|---|
| 생성 | `POST /api/v1/conditional-orders` — `type: SINGLE`, condition `type: STOP`(고정 트리거가), leg는 **LIMIT만**, KR·US 지원 | MARKET leg 불가 — Phase 3의 `TOSS_ORDER_ALLOW_MARKET` 개념은 조건주문에 해당 없음 |
| 라이프사이클 | `WATCHING → ORDERING → ORDERED → COMPLETED`, 그 외 `PAUSED`, `EXPIRED` | 등록 즉시 감시 시작 — dry-run에서 등록하면 실주문 위험. dry-run은 등록 자체를 하지 않는다 |
| 만료 | `expireDate` 필수, 미충족 시 자동 `EXPIRED` | 우리 쪽 상한 7일 강제(그릴링 결정 6) |
| 정정 | `POST .../{id}/modify`는 내부적으로 취소+재생성 — **ID가 바뀜** | 감사 체인 단절 위험 → 정정 비범위(결정 7). 취소는 `DELETE .../{id}`(204) |
| 취소 | `DELETE /api/v1/conditional-orders/{conditionalOrderId}` | write 게이트 대상 |
| 조회 | `GET .../{id}`(단건), `GET /conditional-orders?status=OPEN\|CLOSED&symbol=`(목록, 커서) | reconcile·상태 동기화 원천. 허용 IP에서만 호출 가능 |
| 멱등 | `clientOrderId` 선택(최대 36자) | 우리는 **필수 관례 유지**: `dsa-cond-{proposal_uuid}` — 등록 응답 유실 시 목록 조회로 매칭 수렴 |
| 고액 확인 | `confirmHighValueOrder` 필드 존재 | Phase 3와 동일 — **절대 미전송**, 1억 이상 hard reject |
| Rate limit | `CONDITIONAL_ORDER` / `CONDITIONAL_ORDER_HISTORY` 그룹 존재, TPS 수치는 문서상 미확인 | 429 처리 필요. 수치는 구현 시 실측 |
| 체결 반영 | 트리거로 생성된 실주문의 체결은 일반 주문과 동일 | 원장 반영은 기존 Phase 2 sync가 담당(신규 경로 없음) |

## 3. 확정된 설계 결정

| 결정 | 값 | 근거 |
|---|---|---|
| 감시 위치 | **Toss 서버측 조건주문만 사용.** 로컬 폴링 감시 루프는 만들지 않는다 | 로컬 프로세스 생존·IP 등록과 무관한 감시 지속. 트레일링 등 임의 조건은 포기(비범위) — 그릴링 결정 1 |
| 승인 계약 | 2단계 유지: `POST .../conditional-orders/proposals`(제안) → `POST .../proposals/{uuid}/approve`(body `confirm: true` 필수) = **Toss 등록**. 승인 문서·감사로그에 "조건 충족 시 자동 체결" 문구 명시 | 트리거 시점 승인은 구조적으로 불가 — 등록을 승인 대상으로 재정의(그릴링 결정 2). 엔드포인트 이름을 Phase 3의 `execute`와 다르게(`approve`) 두어 의미 차이를 API 표면에 드러냄 |
| 타입 스코프 | `SINGLE` + condition `STOP`만. 방향은 매도(손절/익절)·매수 모두 Toss가 허용하는 범위 그대로, leg는 LIMIT만(API 제약) | 최소 스코프로 라이프사이클 계약 먼저 안착(그릴링 결정 3) |
| dry-run 의미론 | dry-run 모드에서는 **Toss 등록 POST 자체를 하지 않는다**: 전체 검증 + 감사로그 + `dry_run_approved` 종결 상태. "dry-run인데 등록만 해보기"는 존재하지 않음 — 등록 즉시 Toss가 감시·자동 체결하므로 등록=실거래 위임 | 등록이 곧 실행 위임인 조건주문 특성상 Phase 3의 "dry-run에서 POST 없음" 불변식을 등록 시점으로 앞당겨 적용 |
| write 게이트 확장 | `_request_write`의 주문 URL 매칭을 `/api/v1/orders*` + `/api/v1/conditional-orders*` (path 기준, 쿼리 문자열 무관) 둘 다로 확장. 신규 fetcher 메서드(`place_conditional_order`/`cancel_conditional_order`)와 내부 헬퍼 직접 호출 모두 게이트 통과 필수 | probe에서 실측된 회귀 위험 차단 — 게이트 확장 누락 시 dry-run이 조건주문 POST를 통과시킴 |
| 한도 산입 | 등록(승인) 시 예상 금액(트리거가 아닌 **leg 지정가 × 수량** 기준, 비 KRW는 FX 환산) 을 **즉시 전액 산입**. 산입 유지: `WATCHING`/`PAUSED`/`ORDERING`/`registration_unknown` — 날짜 무관 미확정 전액(Phase 3 v3 원칙). 해제: `EXPIRED`/취소 확인 시. 확정 전환: `COMPLETED`(체결 확정분으로 전환). 1회 한도·1억 hard reject는 등록 시점 검사 | 한도 우회용 다중 조건주문 레이스 원천 차단(그릴링 결정 5). 보수적 이중 산입 허용 — 한도는 상한이지 목표가 아니다 |
| expireDate 상한 | 제안 생성·승인 양쪽에서 `expireDate ≤ 오늘(KST)+7일` 검증, 위반 시 422. 만료 후 재개는 새 제안(승인 재수행) | 한도 장기 점유 완화 + 주기적 재확인 강제(그릴링 결정 6) |
| 정정 | 비범위. 변경은 취소(`DELETE`) → 새 제안 → 재승인만 | Toss 정정이 ID를 교체해 감사 체인이 끊김(그릴링 결정 7) |
| UX 표면 | API-only(Phase 3와 동일). 목록/단건 조회 엔드포인트로 가시성 제공 | 단일 사용자 시스템 — UI는 수요 확인 후 별도(그릴링 결정 8) |
| 저장 모델 | 신규 테이블 `PortfolioConditionalOrderProposal`(additive). Phase 3 `PortfolioOrderProposal`·상태기계는 **무변경** — 라이프사이클이 달라(원격 감시 상태 보유) 기존 테이블에 타입 컬럼으로 얹지 않는다. 감사는 기존 `PortfolioOrderAudit` 재사용 + `conditional_*` 이벤트 추가(append-only 트리거 그대로) | 기존 계약 불변 + 신규는 additive. 감사 인프라는 검증된 것 재사용 |
| 로컬 상태기계 | `pending → approving(원자적 claim: Phase 3 execute와 동일 패턴 — 단일 write 트랜잭션에서 재확인+전이+한도 검증+reservation) → approved(conditionalOrderId 보유) / registration_failed / registration_unknown`, `pending → canceled / expired(TTL) / dry_run_approved`, `approved → triggered_completed / toss_expired / toss_canceled / paused(비종결)`. `registration_unknown → approved / registration_failed`(reconcile). terminal에서의 전이 거부 | Phase 3 상태기계 원칙(원자적 claim, "기록 없으면 등록 없음", outcome_unknown 계열) 상속 |
| reconcile | 등록 응답 유실·timeout·`conditionalOrderId` 누락 시 `registration_unknown`. `POST .../proposals/{uuid}/reconcile`: `GET /conditional-orders` 목록에서 `clientOrderId` 매칭으로 실제 등록 여부를 확인해 수렴(발견 → approved + ID 기록, 미발견 + 재시도 아님 → registration_failed). 미확정 동안 한도 산입 유지 | 조건주문 생성도 분산 트랜잭션 — Phase 3 blocker 7 교훈 상속. 매칭 원천이 재POST가 아닌 목록 조회인 점만 다름(재POST는 이중 등록 위험) |
| 상태 동기화 | `approved` 상태 제안들의 Toss 상태를 (a) 관련 API 조회 시 lazy refresh, (b) `POST /portfolio/links/{account_id}/conditional-orders/sync`(수동 일괄) 로 갱신. 자동 백그라운드 폴링은 두지 않음(허용 IP 제약 + API-only 운용) | 조회는 IP 등록된 로컬에서만 가능 — 감시는 Toss가 하므로 동기화 지연이 안전성을 해치지 않음(상태 표시가 늦을 뿐) |
| 체결 원장 반영 | 트리거된 실주문의 체결은 **기존 Phase 2 sync가 반영**. Phase 4는 원장에 직접 쓰지 않음 | Phase 3와 동일 원칙 — 체결 반영 경로 단일화 |
| 인증·FX·한도값 검증 | Phase 3 계약 그대로: 쓰기 전부 `ADMIN_AUTH_ENABLED=true`+세션 필수(403 `order-auth-required`), 비 KRW는 FX 미존재·stale(24h 벽시계 포함)·fallback 시 거부, `TOSS_ORDER_*` 환경변수 파싱 규칙 동일. **신규 환경변수 없음** | 검증된 계약 재사용, 설정 표면 불증가 |

## 4. 데이터 흐름

```
[제안]  POST /portfolio/links/{account_id}/conditional-orders/proposals
        {symbol, side, trigger_price, limit_price, quantity, expire_date}
        → 검증(심볼 해석·expireDate≤7일·1회 한도·1억 거부·FX·sellable/buying-power)
        → PortfolioConditionalOrderProposal(pending, TTL 10분) + audit(conditional_proposed)

[승인]  POST .../conditional-orders/proposals/{uuid}/approve  {confirm: true}
        → 원자적 claim(pending 재확인 + 일일 한도 재검증·reservation + approving 전이)
        → dry-run: 등록 없이 dry_run_approved 종결 + audit(mode=dry_run)
        → live: Toss POST /conditional-orders (clientOrderId=dsa-cond-{uuid})
           ├─ 성공 → approved + conditionalOrderId 기록 + audit(conditional_approved)
           ├─ 명시적 4xx → registration_failed + reservation 해제 + audit
           └─ 유실/timeout/ID누락 → registration_unknown(산입 유지) + audit

[수렴]  POST .../proposals/{uuid}/reconcile
        → GET /conditional-orders 목록에서 clientOrderId 매칭 → approved | registration_failed

[관찰]  GET .../conditional-orders (목록: 로컬 상태 + lazy Toss 상태 refresh)
        GET .../conditional-orders/proposals/{uuid} (단건)
        POST .../conditional-orders/sync (수동 일괄 상태 동기화)

[취소]  DELETE .../conditional-orders/proposals/{uuid}
        → pending이면 로컬 취소만; approved면 Toss DELETE 후 toss_canceled + reservation 해제

[체결]  Toss가 트리거 → 실주문 → 체결. 다음 Phase 2 sync가 trade로 원장 반영.
        상태 동기화 시 COMPLETED 관측 → triggered_completed + 한도 확정 전환 + audit
```

## 5. 엣지 케이스 계약

- **등록 성공 후 로컬 DB 쓰기 실패**: audit의 approving reservation이 이미
  커밋되어 있으므로 "기록 없는 등록"은 발생하지 않는다. 제안은
  `registration_unknown`으로 남고 reconcile로 수렴.
- **PAUSED**(예수금 부족 등 Toss 측 일시정지): 비종결 상태로 노출만 하고
  자동 조치 없음(한도 산입 유지). 해소/취소는 사용자 판단.
- **트리거됐지만 leg 주문이 미체결(ORDERED, LIMIT 미도달)**: Toss 상태
  그대로 노출. 한도는 미확정 산입 유지 — 체결(COMPLETED) 시 확정 전환.
- **자정 경계**: Phase 3 v3와 동일 — 미확정 reservation은 날짜 무관 전액
  산입이므로 경계 경합 없음.
- **동일 종목 반대 pending 주문**(`opposite-pending-order-exists` 등 Toss
  에러): 코드별 4xx로 명확히 전달, 뭉개기 금지(Phase 3 에러 모델 상속).
- **429**(CONDITIONAL_ORDER 그룹): 승인 경로에서는 재시도하지 않고 명시적
  실패(registration_failed 아님 — claim 해제 후 pending 유지가 아니라, POST
  전 429 감지 시 approving 진입 전 거부; POST 응답이 429면 유실과 동일하게
  registration_unknown 처리 후 reconcile). 조회 경로는 지수 백오프 재시도.
- **`PROFIT_RATE`·OCO/OTO 요청 유입**: 스키마 레벨에서 422 거부(enum 미포함).

## 6. 검증 계획

- 게이트 회귀(최우선): dry-run 상태에서 `place_conditional_order`·
  `cancel_conditional_order`·`_request_write` 직접 호출(쿼리 문자열 포함
  URL 변형 포함) 전부가 POST/DELETE를 발생시키지 않음을 단위 테스트로 실증.
- 상태기계: 원자적 claim 경합(병렬 approve 한쪽만 성공), terminal 전이 거부,
  registration_unknown → reconcile 양방향 수렴, dry_run_approved 불변식.
- 한도: 등록 시 산입·해제·확정 전환 각 경로, WATCHING 다건 합산이 일일
  한도를 차단하는지, expireDate 7일 초과 422.
- 감사: conditional_* 이벤트가 append-only 트리거 하에 기록되는지.
- 실계정 스모크(격리, live 게이트 on, 사용자 입회): 소액 SINGLE-STOP 등록 →
  목록 조회 → 취소 왕복. 트리거 실발동은 스모크 비범위(시장 조건 의존).
- 기존 게이트: `./scripts/ci_gate.sh` 전체 green + Phase 3 테스트 무회귀.

## 7. 리스크와 롤백

- **최대 리스크**: 승인 후 자동 체결이라는 계약 자체. 완화 — dry-run 기본,
  등록 시 한도 전액 산입, 7일 만료 상한, 승인 body `confirm: true` 필수,
  감사로그. 잔여 리스크는 사용자가 결정 2에서 명시적으로 수용.
- **게이트 확장 누락 회귀**: 성공 기준에 명시 + 회귀 테스트 최우선 —
  구현 리뷰에서 이 항목을 blocker 기준으로 본다.
- **롤백**: 신규 테이블·엔드포인트·fetcher 메서드는 전부 additive — 코드
  리버트로 즉시 제거 가능. 이미 Toss에 등록된 조건주문은 리버트와 무관하게
  Toss 앱/API에서 직접 취소 가능(만료 상한 7일이 자연 소멸 상한).

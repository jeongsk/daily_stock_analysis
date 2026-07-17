# 토스증권 수동 승인 반자동 주문 (Phase 3) — 설계 스펙

- 작성일: 2026-07-17 (v2: 같은 날 Codex BLOCK 리뷰 8건 반영 재설계 — reservation 상태 기계, 인증 필수화, FX fail-closed, strict live 플래그)
- 상태: 설계 확정 (그릴링 결정 3건 반영: 수동 승인 반자동 / 보수 안전장치 기본 / REST API만)
- 선행: `2026-07-17-toss-openapi-design.md` §6, `2026-07-17-toss-portfolio-sync-design.md` (연동 계좌 전제), ADR 0003
- 관련 영역: `data_provider/toss_fetcher.py`, `src/services/`, `src/storage.py`, `api/v1/`

## 1. 개요와 목표

연동 계좌(Phase 2)에 **2단계 수동 승인 주문**을 추가한다: 주문 제안(proposal)
생성 → 사용자 확인 → 실행. 자동 매매는 없다 — 모든 실행은 사용자의 명시적
API 호출 2회를 요구한다. 기본 모드는 **dry-run**(실주문 불가)이며, 실주문은
`TOSS_ORDER_LIVE=true` 환경변수 + 요청 본문 `confirm: true`가 모두 있어야 한다.

### 성공 기준

- dry-run 기본: `TOSS_ORDER_LIVE` 미설정 시 어떤 경로로도 Toss 주문 POST가
  발생하지 않음을 테스트로 보장.
- 안전장치가 전부 기본 on: 1회 금액 상한(기본 100만 KRW), 일일 누적 상한
  (기본 500만 KRW), 지정가만 허용(시장가는 `TOSS_ORDER_ALLOW_MARKET=true`
  opt-in), 전 이벤트 append-only 감사로그, `clientOrderId` 멱등성.
- 실행 전 이중 검증: 제안 시점과 실행 시점 모두에서 한도·매수가능금액·매도
  가능수량을 재확인.
- 주문 취소(placed order cancel)는 리스크 축소 수단으로 포함. 주문 정정
  (modify)은 비범위.

### 명시적 비범위

- 분석 결과 → 주문 제안 자동 생성(후속), 조건주문(SINGLE/OCO/OTO — 후속),
  주문 정정, 봇/Web UI 표면, 스케줄 실행, 미연동 계좌 주문.

## 2. API 제약 (스펙·실측 확인)

| 항목 | 사실 | 귀결 |
|---|---|---|
| 주문 생성 | `POST /api/v1/orders` (LIMIT/MARKET, KR·US, `clientOrderId` 멱등) | ORDER 그룹 6 TPS(09:00-09:10 KST는 3), 429 처리 필요 |
| 고액 확인 | 1억원 이상은 `confirmHighValueOrder=true` 필요 (`confirm-high-value-required`) | **자동 세팅 금지 — 1억 이상은 무조건 거부** (한도 상향 설정과 무관하게 hard reject) |
| 멱등성 | 동일 `clientOrderId` 재요청: 처리 중 409 `request-in-progress`, 내용 다르면 422 `idempotency-key-conflict` | `clientOrderId = "dsa-{proposal_uuid}"` — 재시도 안전 |
| 에러 모델 | `insufficient-buying-power`, `order-hours-closed`, `price-out-of-range`, `invalid-tick-size`, `opposite-pending-order-exists` 등 | 코드별로 명확한 4xx로 전달 — 뭉개기 금지 |
| 거래 가능 정보 | `GET /buying-power`(KRW·USD), `GET /sellable-quantity`, `GET /commissions` — 전부 read-only | 검증 단계에서 사용 (실주문 없이 검증 가능) |
| 주문 조회 | `GET /orders/{orderId}` 전 상태 조회 | 실행 후 상태 추적, Phase 2 sync가 체결을 원장에 반영 |

## 3. 확정된 설계 결정

| 결정 | 값 | 근거 |
|---|---|---|
| 2단계 흐름 | `POST /portfolio/links/{account_id}/orders/proposals`(제안 생성·검증) → `POST .../orders/proposals/{proposal_id}/execute`(body `confirm: true` 필수) | 수동 승인 계약. 제안과 실행이 다른 HTTP 호출이어야 실수 클릭 한 번으로 주문 불가 |
| 실행 모드 | `TOSS_ORDER_LIVE`는 **strict 파싱**: trim·소문자화 후 정확히 `"true"`만 live. 그 외 비어 있지 않은 값(오타·"1"·"yes" 포함)은 **ERROR 로그 + live 비활성(fail-closed)**. 미설정/false → dry-run: 전체 검증 + 감사로그 + `mode="dry_run"`, Toss POST 없음 | parse_env_bool의 관용 파싱이 안전 플래그에 부적합 (Codex blocker 2). 일반 bool 파서와 별도의 전용 파서 사용 |
| 이중 게이트 | live 검사를 서비스와 fetcher 양쪽에서 수행하되, **fetcher 레벨 게이트는 `_request_write`가 주문 계열 URL(orders 경로 전체)을 받을 때 무조건 강제** — `place_order`/`cancel_order` 뿐 아니라 내부 헬퍼 직접 호출도 차단. `place_order`는 `client_order_id` **필수 인자**, `confirm_high_value_order` 인자는 **제공하지 않음**(항상 미전송 — 1억 이상은 어떤 경로로도 불가). `cancel_order`도 live 게이트 적용(dry-run 모드에서는 실주문이 존재할 수 없으므로 취소도 불필요) | fetcher 직접 호출 우회 차단 (Codex blocker 3, major 5) |
| 인증 (필수, v3 명확화) | 주문 관련 쓰기 엔드포인트 전부(제안 생성·실행·reconcile·취소, dry-run 포함)는 **`ADMIN_AUTH_ENABLED=true` + 인증 통과가 전제조건** — 인증 비활성 상태에서는 403 `order-auth-required`. **인증 계약: 단일 공유 관리자 인증을 통과한 세션은 모든 계좌의 관리 권한을 가진다(단일 사용자 시스템). 자기주장 요청 헤더 기반 owner 대조는 제공하지 않는다** — 검증 없는 헤더 대조는 가짜 보안이므로 금지. 다중 사용자 신원 인증은 명시적 비범위 | 인증 꺼진 기본값에서 서버 접근자 전원이 주문 가능한 구멍 차단 (Codex blocker 1) + 자기주장 헤더의 허구적 owner 대조 제거 (재리뷰 major 2) |
| FX (fail-closed) | USD 주문의 KRW 평가에서 환율이 **미존재·stale(24시간 초과)·fallback(1:1 등)이면 주문 거부** — 한도 계산에 추정·폴백 환율 사용 금지 | 1 USD = 1 KRW fail-open으로 $5,000이 5,000 KRW로 평가되던 결함 차단 (Codex blocker 4) |
| 한도 값 검증 | `TOSS_ORDER_*_AMOUNT_KRW` 파싱 시 `math.isfinite()` + 양수 검증 — NaN/Infinity/비양수는 ERROR 로그 + **기본값으로 강제**(사용자 설정 무시) | NaN 비교가 모든 한도 검사를 무력화하는 결함 차단 (Codex blocker 5) |
| 주문 유형 | LIMIT만 기본 허용. MARKET은 `TOSS_ORDER_ALLOW_MARKET=true` opt-in. 금액주문(orderAmount)·timeInForce 비기본값은 비범위 | 보수 기본값 결정 |
| 금액 한도 | 1회: `TOSS_ORDER_MAX_AMOUNT_KRW`(기본 1,000,000), 일일 누적(감사로그 기준, KST 날짜, dry-run 제외): `TOSS_ORDER_DAILY_MAX_AMOUNT_KRW`(기본 5,000,000). USD 주문은 기존 FX 환산으로 KRW 평가해 한도 적용. 1억원 이상은 설정 무관 거부 | 보수 기본값 결정. 미설정시에도 동작(기본값) 가드레일 |
| 상태 기계 (v2) | `pending → executing → executed / failed / outcome_unknown`, `outcome_unknown → executed / failed` (reconcile 경로), `pending → canceled / expired / dry_run_executed`. **executing 진입은 원자적 claim**(단일 write 트랜잭션에서 pending 확인 + 전이 + 금액 reservation 기록) — 같은 proposal의 병렬 execute는 한쪽만 claim 성공. canceled/expired는 pending에서만 가능(executing 중 취소 불가 — 복구 절차 후에만). terminal 상태에서의 어떤 전이도 거부 | 원격 실행 중 상태 부재로 경합을 표현 못 하던 결함 해소 (Codex blocker 8, 스펙 결함 2) |
| 분산 트랜잭션 계약 (v2) | "로그 없으면 주문 없음"의 정확한 의미: **Toss POST 전에 durable reservation 로그(event=executing, 금액 포함)를 먼저 커밋**하고, 이 커밋이 실패하면 POST 자체를 하지 않는다. POST 후 (a) 성공 → executed 전이 + toss_order_id 기록, (b) 명시적 4xx 거부 → failed, (c) 응답 유실·timeout·orderId 누락·POST 후 DB 실패·409 request-in-progress → **outcome_unknown**(terminal 아님). outcome_unknown 복구: 동일 `clientOrderId`로 재POST(Toss 멱등 — 기존 주문이면 그 주문 반환, 처리 중이면 409 대기, `idempotency-key-conflict`면 결함 표면화)해 실제 결과로 수렴시키는 `POST .../proposals/{uuid}/reconcile` 엔드포인트 제공. **outcome_unknown·executing의 reservation 금액은 복구 확정 전까지 일일 한도에 계속 산입** | 원격 POST 이후 audit 실패 시 기록 없는 주문이 남고 한도에서도 빠지던 결함 해소 (Codex blocker 7, 스펙 결함 1) |
| 일일 한도 원자성 (v3) | 한도 검사와 reservation 기록을 **동일 write 트랜잭션**(SQLite BEGIN IMMEDIATE)에서 수행. 합산 규칙: `sum(executed 금액, KST 당일 확정분) + sum(executing/outcome_unknown 금액, **날짜 무관 미확정 전액**) + 이번 금액 ≤ cap`. 미확정 reservation을 날짜와 무관하게 전액 산입하는 이유: 자정 직전 reservation이 자정 직후 claim의 당일 합산에서 빠지는 경합을 구조적으로 차단(보수적 이중 산입은 허용 — 한도는 상한이지 목표가 아니다) | TOCTOU 해소 (Codex blocker 6) + 자정 경계 cap 초과 경합 차단 (재리뷰 blocker 1) |
| 제안 수명 | proposal은 생성 후 `10분` TTL — 만료 후 execute는 409. **pending 10건 상한 검사도 count+insert를 동일 write 트랜잭션으로** 수행 | 오래된 가격 실행 방지 + 상한 TOCTOU 해소 (Codex major 3) |
| 검증 시점 | 제안 생성 시: 심볼 해석, 한도, buying-power(매수)/sellable-quantity(매도) 확인. 실행 시: TTL + 한도 + buying-power/sellable **재확인** 후 POST | 제안~실행 사이 상태 변화 방어 |
| 감사로그 | 신규 테이블 `PortfolioOrderAudit` (append-only): proposal_uuid, account_id, symbol, side, order_type, price, quantity, est_amount_krw, mode(dry_run/live), event(proposed/executing/executed/rejected/canceled/expired/failed/outcome_unknown/reconciled), toss_order_id, error_code, detail(JSON), created_at. append-only는 API 부재가 아니라 **SQLite 트리거로 UPDATE/DELETE를 DB 레벨에서 거부**해 강제. 실행일 확정: POST 성공 직후 KST 시각 기준이며, reservation 시각과 확정 시각의 날짜가 다르면(자정 경계) **양쪽 날짜 모두에 보수적으로 산입** | 전 이벤트 추적 + 한도 원천의 무결성 (Codex major 2·4) |
| 계좌 자격 검증 | 실행 시 단일 검증으로 묶음: 계좌 `is_active` + 활성 링크 `provider='toss'` + (owner_id 설정 시) 인증 주체 대조. 어느 하나라도 실패면 403/404 | 비활성·타 provider 계좌 주문 차단 (Codex major 1) |
| 멱등성 | `clientOrderId = "dsa-{proposal_uuid}"` (fetcher 인자 필수). execute 재시도: executed/dry_run_executed면 캐시 반환, executing/outcome_unknown이면 409 + reconcile 안내. 409 `request-in-progress`는 **failed가 아니라 outcome_unknown**으로 기록(다른 시도가 성공할 수 있음), 422 `idempotency-key-conflict`는 결함 표면화 | 이중 주문 방지 + 경합의 오판정 방지 (Codex blocker 8) |
| 주문 취소 | `POST .../orders/{toss_order_id}/cancel` — 인증 필수 + 연동 계좌의 자사 발행(감사로그 존재) 주문만. **fetcher cancel_order에도 live 게이트 적용**, 감사로그 기록. executing/outcome_unknown 상태의 proposal 취소는 불가(reconcile 우선) | 취소도 쓰기 행위 — 인증·게이트·경합 계약 필요 (Codex major 5, 스펙 결함 8) |
| TossFetcher 확장 | `get_buying_power`, `get_sellable_quantity`, `get_commissions`, `get_order(order_id)` (read-only) + `place_order`, `cancel_order` (계좌 헤더, ORDER 그룹 429 처리). place_order는 env 게이트 내장 | Phase 1/2 인프라 재사용 |
| 스토리지 | `PortfolioOrderAudit`만 신규 (additive). proposal은 audit 테이블의 이벤트로 표현하지 않고 **별도 테이블 없이 audit + 최신 상태 조회로 구성하지 않는다 — proposal 전용 테이블 `PortfolioOrderProposal`을 둔다** (status 전이가 1급 개념이므로) | 상태 기계를 audit 로그 재구성으로 흉내내면 취약 |

## 4. 데이터 흐름

```
[제안]  POST /portfolio/links/{account_id}/orders/proposals
  {symbol, side, order_type=LIMIT, price, quantity}
  → 활성 링크 확인 → 심볼 해석(KR 6자리/US 티커 ↔ 저장소 표기)
  → 금액 산출(KRW 환산) → 1회·일일 한도 검사 → 1억 이상 즉시 거부
  → 매수: buying-power 확인 / 매도: sellable-quantity 확인
  → proposal 저장(pending, TTL 10분) + audit(proposed)
  → 응답: proposal_uuid, 검증 요약, expires_at, mode 예고(dry_run|live)

[실행]  POST .../orders/proposals/{uuid}/execute  {confirm: true}
  → confirm 필수 → TTL·상태 확인 → 한도·거래가능 재확인
  → TOSS_ORDER_LIVE 미설정: audit(executed, mode=dry_run) → dry_run_executed
  → 설정: place_order(clientOrderId="dsa-{uuid}") → toss_order_id 저장
      · 429 → Retry-After 백오프 (Phase 1 계약)
      · Toss 4xx → audit(rejected, error_code) + 명시적 에러 응답
  → audit(executed, mode=live) → 응답: {status, toss_order_id?, mode}

[취소]  POST .../orders/{toss_order_id}/cancel → 자사 발행 확인 → cancel_order
[조회]  GET  .../orders/proposals?status=... / GET .../orders/{toss_order_id}
```

## 5. 엣지 케이스 계약

- **미설정 자격증명 / 403**: Phase 2와 동일 계약 (4xx 명시 / 502 전달).
- **confirm 누락**: 400 — "confirm: true 필수" 명시. dry-run에서도 요구
  (실모드 전환 시 동작 차이가 없도록).
- **장 마감 시간**: Toss `order-hours-closed`를 그대로 4xx로 전달. 사전
  차단하지 않는다 (장 캘린더 API 소비는 비범위 — Toss가 진실원).
- **잔여 pending 제안**: 계좌당 pending 제안 수 상한 10건 — 초과 시 409.
- **동일 종목 반대 방향 pending 주문**: Toss `opposite-pending-order-exists`
  전달. 사전 차단하지 않음.
- **실행-체결 반영**: 체결 결과는 Phase 2 sync가 원장에 반영한다 (주문
  실행 자체는 원장에 기록하지 않음 — 체결 전 주문은 보유 변화가 아니다).

## 6. 검증 계획

- 오프라인(전부 mock): dry-run 기본 보장(**TOSS_ORDER_LIVE 미설정 시
  place_order가 fetcher·서비스 어느 경로로도 HTTP POST를 만들지 않음을
  mock 레벨에서 단언** + `_request_write` 직접 호출도 차단됨을 단언),
  **전 테스트에 전역 네트워크 금지 fixture**(예상 외 `requests.post` 즉시
  실패), strict live 플래그 파싱 테이블("true"/"TRUE "/"1"/"yes"/"flase"/
  공백/미설정), NaN·Infinity·음수 한도 강제 기본값, FX 미존재·stale·
  fallback 시 주문 거부, 한도(1회/일일/1억 거부), LIMIT-only, TTL 만료,
  confirm 누락, **barrier 기반 동시성**(동일 proposal 병렬 execute 1회만
  claim, 서로 다른 proposal 병렬 execute의 일일 한도 직렬 판정, pending
  상한 병렬 생성), **fault-injection**(POST 후 응답 유실 → outcome_unknown
  → reconcile 수렴, POST 후 DB 실패, orderId 누락, request-in-progress),
  멱등 재시도, 429 백오프, Toss 4xx 매핑, 감사로그 append-only 트리거
  (UPDATE/DELETE 거부 확인), 인증 비활성 시 403, 취소 흐름·경합.
- 온라인(-m network, 감독자 전용): **read-only만** — buying-power/
  commissions/sellable-quantity 실조회 + dry-run 제안→실행 왕복.
  **실주문 POST는 어떤 검증에서도 실행하지 않는다.**
- 문서: CHANGELOG, full-guide KR/EN, `.env.example`(+ko) 신규 키 3종.

## 7. 리스크와 롤백

- **리스크**: dry-run과 live의 코드 경로 차이가 커지면 dry-run 통과가 live
  안전을 보장하지 못함 → 분기를 place_order 호출 직전 1점으로 최소화.
- **리스크**: 일일 한도가 audit 로그 기준이므로 로그 유실 시 한도 우회
  가능 → **POST 전 reservation 커밋 실패 시 POST 자체를 하지 않는다**
  (분산 트랜잭션 계약 — §3). POST 후의 불확실성은 outcome_unknown +
  reconcile로 수렴시키며, 확정 전까지 한도에 산입 유지.
- **롤백**: `TOSS_ORDER_LIVE` 제거만으로 실주문 전면 차단. 테이블·엔드포인트
  additive. 자격증명 제거로 전체 비활성.

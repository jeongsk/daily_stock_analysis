# 토스증권 서버측 조건주문 (Phase 4) — 설계 스펙

- 작성일: 2026-07-19
- 상태: 설계 확정 (그릴링 결정 8건 반영: Toss 서버측 감시 / 등록=승인 / SINGLE-STOP만 / 자동 제안 비범위 / 등록 시 한도 즉시 산입 / expireDate 최대 7일 / 정정 비범위 / API-only)
- **v2 (2026-07-19, 구현 후): Codex 독립 리뷰 BLOCK 판정(blocker 2건·major 3건·minor 1건) 반영 — 조정자(아키텍처 감독) 확정 수렴 계약으로 §3/§4/§5/§6를 갱신.** 원 설계의 reconcile·429·인증·가용성 계약 4곳이 구현 검증 과정에서 실측 API 계약 및 동시성 분석과 충돌해 재확정됨(아래 각 절의 "v2" 표기 참고). 그릴링 결정 자체(감시 위치·타입 스코프·한도 산입 원칙 등)는 변경 없음 — 구현 세부 계약만 보강.
- **v3 (2026-07-19, 2차 리뷰 후): Codex 2차 독립 리뷰 BLOCK 판정(blocker 2건·minor 1건 — v2 blocker 2건 중 1건은 부분수렴으로 재분류, 1건은 신규) 반영 — 조정자 확정 수렴 계약으로 §3/§5/§6/§7 갱신.** 두 blocker는 표면상 다른 증상(오매칭 vs 지연 POST 유실)이지만 근본 원인이 동일 — **"원격 conditionalOrderId의 로컬 소유권을 확정할 수 없다."** (1) 유일 매칭 후보라도 그 ID를 실제로 이 proposal이 소유하는지(타 proposal이 먼저 등록했거나 동시에 경합 중인지)는 속성 매칭만으로 증명할 수 없었고, (2) reconcile의 stale-claim 인수가 "진짜 살아있는 POST"를 결코 앞지르지 못한다는 보장이 시간 상수 관계로 명시적으로 강제되지 않아 지연 POST 결과가 조용히 유실될 수 있었다. §3의 "reconcile 매칭 계약"에 소유권 배타성 검증(DB 유니크 인덱스 + 소유권 조회 + 로컬 경쟁자 검사)을 추가하고, 신규 상수 관계(`_RECONCILE_STALE_APPROVING_AFTER ≥ 10 × 조건주문 write 타임아웃 최악값`)를 도입해 경합 창을 구조적으로 제거했다 — 잔여는 감사망(`conditional_registration_conflict`)으로 커버.
- **v4 (2026-07-19, 3차 리뷰 후): Codex 3차 독립 리뷰 BLOCK 판정(blocker 2건·minor 1건 — 전부 v3 R1의 하위 두 갈래) 반영 — 조정자 확정 수렴 계약으로 §3/§5 갱신.** v3의 소유권 배타 검증(로컬 경쟁자 조회·소유권 조회)은 일반 read session에서, 채택(상태 전이+ID 기록)은 별도 `BEGIN IMMEDIATE` write 트랜잭션에서 수행돼 **검사~채택 사이에 TOCTOU 창**이 남아 있었다(R1c) — 예: B가 "경쟁자 0건"을 확인한 직후, 채택 트랜잭션이 열리기 전에 동일 속성 A가 approving에 진입해도 DB 유니크 인덱스만으로는 이 경우를 막지 못한다(A는 아직 ID를 갖지 않으므로). 또한 approved 저장이 유니크 위반으로 실패했을 때의 fallback이 **같은 conditionalOrderId를 그대로 재전달**해 동일 인덱스에서 두 번째 IntegrityError를 유발, `OrderAuditPersistFailedError`(500)로 끝나 proposal이 `approving`에 방치되는 결함이 있었다(R1d). v4는 (1) 경쟁자 재검사+원격 후보 유일성 재확인+채택을 **단일 write 트랜잭션 안**에서 수행하는 `PortfolioRepository.adopt_reconciled_order_if_uncontended`를 신설해 TOCTOU 창을 SQLite `BEGIN IMMEDIATE`의 직렬화 보장으로 구조적으로 제거하고(네트워크 I/O는 트랜잭션 밖에서 선행), (2) `_resolve_registration_outcome`의 unique 위반 fallback을 **ID 없이(NULL)** registration_unknown으로 전이하도록 고쳐 이중 IntegrityError를 원천 차단했다(그 ID는 "타 proposal 소유"이므로 재기록하지 않음). 정상 수렴 경로에도 DEBUG 로그 1줄을 추가해 운영 추적성을 보강했다(R2b, minor).
- 선행: `2026-07-17-toss-order-phase3-design.md`(상태기계·게이트·한도·감사 인프라 전제), `2026-07-17-toss-portfolio-sync-design.md`(연동 계좌·체결 반영 전제), ADR 0003
- 사전 조사: `.claude/reviews/2026-07-19-toss-phase4-probe.md` (OpenAPI v1.2.4 실측)
- **구현 시 추가 실측(v2)**: `openapi.tossinvest.com/openapi-docs/latest/openapi.json`(info.version "1.2.4") 원문 스키마를 직접 확인 — probe A표의 산문 요약과 다른 지점 2곳 발견(아래 §3 clientOrderId/reconcile 행 참고).
- 관련 영역: `data_provider/toss_fetcher.py`, `src/services/portfolio_order_service.py`, `src/services/portfolio_conditional_order_service.py`, `src/repositories/portfolio_repo.py`, `src/storage.py`, `api/v1/endpoints/portfolio.py`

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
  등록 시 즉시 산입), 인증 필수(`ADMIN_AUTH_ENABLED` + 세션 — **v2: 조회
  엔드포인트 포함 전부**, 아래 §3 인증 행 참고), FX
  fail-closed(비 KRW), `confirmHighValueOrder` 절대 미전송(1억 이상 무조건
  거부), append-only 감사로그, `clientOrderId` 멱등(우리 쪽 필수 관례 유지).
- 등록 응답 유실 시에도 "기록 없으면 등록 없음" 불변식 유지 —
  `registration_unknown` 상태 + reconcile 경로로 수렴. **v2: `reconcile`
  자체가 진행 중인 승인 POST와 경합하지 않고, 오매칭으로 다른 주문을 자기
  주문으로 오인하지 않는 것도 이 불변식의 일부(§3/§4 v2 행 참고).**

### 명시적 비범위

- OCO/OTO(후속 — SINGLE만), `PROFIT_RATE` 트리거(문서 내부 상충으로 실사용
  미확인 — probe A표), 트레일링(Toss 미지원), 조건주문·일반주문 정정(취소 후
  새 제안으로 재등록만), 자동 주문 제안 생성(Phase 5 후보), Web/봇 UI
  표면(API-only), 다중 사용자 인증, GTC/예약주문(Toss에 개념 없음).

## 2. API 제약 (스펙 v1.2.4 실측 — probe A표 요약, v2에서 원문 스키마로 재검증)

| 항목 | 사실 | 귀결 |
|---|---|---|
| 생성 | `POST /api/v1/conditional-orders` — `type: SINGLE`, condition `type: STOP`(고정 트리거가), leg는 **LIMIT만**, KR·US 지원. **v2 실측**: 요청 바디는 평탄한 구조 — 최상위 `symbol`/`type`/`quantity`/`orderType`/`expireDate`/`clientOrderId` + `first: {orderSide, triggerPrice, orderPrice}`(SINGLE은 `second` 생략) — probe의 `condition: {...}` 중첩 요약과 실제 필드명이 다름(`orderSide`/`orderPrice`, `condition` 래퍼 없음) | MARKET leg 불가 — Phase 3의 `TOSS_ORDER_ALLOW_MARKET` 개념은 조건주문에 해당 없음. 구현은 원문 스키마를 그대로 따름 |
| 라이프사이클 | `WATCHING → ORDERING → ORDERED → COMPLETED`, 그 외 `PAUSED`, `EXPIRED` | 등록 즉시 감시 시작 — dry-run에서 등록하면 실주문 위험. dry-run은 등록 자체를 하지 않는다 |
| 만료 | `expireDate` 필수, 미충족 시 자동 `EXPIRED` | 우리 쪽 상한 7일 강제(그릴링 결정 6) |
| 정정 | `POST .../{id}/modify`는 내부적으로 취소+재생성 — **ID가 바뀜** | 감사 체인 단절 위험 → 정정 비범위(결정 7). 취소는 `DELETE .../{id}`(204) |
| 취소 | `DELETE /api/v1/conditional-orders/{conditionalOrderId}` | write 게이트 대상 |
| 조회 | `GET .../{id}`(단건), `GET /conditional-orders?status=OPEN\|CLOSED&symbol=`(목록, 커서). **v2 실측**: `status`는 **필수** 쿼리 파라미터(선택 아님) | reconcile·상태 동기화 원천. 허용 IP에서만 호출 가능. `status` 생략 불가 — reconcile은 `OPEN`/`CLOSED` 양쪽을 명시적으로 순회 |
| 멱등 | `clientOrderId` 선택(최대 36자, 패턴 `^[a-zA-Z0-9\-_]+$`) | **v2**: 우리 쪽 관례가 `dsa-cond-{proposal_uuid}`(45자)로는 이 36자 상한을 넘어 전송 자체가 불가능함이 실측으로 확인됨 — `dc-{uuid_hex}`(35자)로 수정. 등록 응답 유실 시 clientOrderId 매칭이 아닌 **속성 매칭**으로 수렴(아래 §3/§4 reconcile 행 v2) |
| 고액 확인 | `confirmHighValueOrder` 필드 존재 | Phase 3와 동일 — **절대 미전송**, 1억 이상 hard reject |
| Rate limit | `CONDITIONAL_ORDER` / `CONDITIONAL_ORDER_HISTORY` 그룹 존재, TPS 수치는 문서상 미확인 | 429 처리 필요. 수치는 구현 시 실측. **v2**: 조건주문 write(등록 POST·취소 DELETE)는 429여도 재시도하지 않음 — 아래 §5 "429" 행 v2 참고 |
| 체결 반영 | 트리거로 생성된 실주문의 체결은 일반 주문과 동일 | 원장 반영은 기존 Phase 2 sync가 담당(신규 경로 없음) |
| 조회 응답에 clientOrderId 없음 | **v2 실측**: `ConditionalOrderDetailResponse`(목록/단건 응답 공용 스키마)에 `clientOrderId` 필드가 존재하지 않음 — 오직 생성 응답(`ConditionalOrderCreateResponse`)에만 1회성으로 존재 | reconcile을 clientOrderId 매칭으로 구현할 수 없음(원 설계 §3/§4의 전제가 실제 API와 맞지 않음) — 속성 매칭 + 유일성 + 시간창 계약으로 대체(아래 §3/§4 v2) |

## 3. 확정된 설계 결정

| 결정 | 값 | 근거 |
|---|---|---|
| 감시 위치 | **Toss 서버측 조건주문만 사용.** 로컬 폴링 감시 루프는 만들지 않는다 | 로컬 프로세스 생존·IP 등록과 무관한 감시 지속. 트레일링 등 임의 조건은 포기(비범위) — 그릴링 결정 1 |
| 승인 계약 | 2단계 유지: `POST .../conditional-orders/proposals`(제안) → `POST .../proposals/{uuid}/approve`(body `confirm: true` 필수) = **Toss 등록**. 승인 문서·감사로그에 "조건 충족 시 자동 체결" 문구 명시 | 트리거 시점 승인은 구조적으로 불가 — 등록을 승인 대상으로 재정의(그릴링 결정 2). 엔드포인트 이름을 Phase 3의 `execute`와 다르게(`approve`) 두어 의미 차이를 API 표면에 드러냄 |
| 타입 스코프 | `SINGLE` + condition `STOP`만. 방향은 매도(손절/익절)·매수 모두 Toss가 허용하는 범위 그대로, leg는 LIMIT만(API 제약) | 최소 스코프로 라이프사이클 계약 먼저 안착(그릴링 결정 3) |
| dry-run 의미론 | dry-run 모드에서는 **Toss 등록 POST 자체를 하지 않는다**: 전체 검증 + 감사로그 + `dry_run_approved` 종결 상태. "dry-run인데 등록만 해보기"는 존재하지 않음 — 등록 즉시 Toss가 감시·자동 체결하므로 등록=실거래 위임 | 등록이 곧 실행 위임인 조건주문 특성상 Phase 3의 "dry-run에서 POST 없음" 불변식을 등록 시점으로 앞당겨 적용 |
| write 게이트 확장 | `_request_write`의 주문 URL 매칭을 `/api/v1/orders*` + `/api/v1/conditional-orders*` (path 기준, 쿼리 문자열 무관) 둘 다로 확장. 신규 fetcher 메서드(`place_conditional_order`/`cancel_conditional_order`)와 내부 헬퍼 직접 호출 모두 게이트 통과 필수. **v2: 취소는 `DELETE` verb이므로 신규 `_request_delete` 헬퍼가 필요하며, 동일 게이트 술어(`_is_order_write_gated_path`)를 공유** | probe에서 실측된 회귀 위험 차단 — 게이트 확장 누락 시 dry-run이 조건주문 POST를 통과시킴 |
| **v2: 429 처리** | `place_conditional_order`(POST)와 `cancel_conditional_order`(DELETE)는 **429에서 재시도하지 않는다** — 응답 유실과 동일하게 즉시 표면화해 서비스가 `registration_unknown`(등록) 또는 명시적 에러(취소, 호출자 재시도)로 처리한다. Toss가 429 요청을 실제로 처리했는지 알 수 없는 상태에서 맹목적 재시도는 중복 등록 위험을 만든다. **Phase 3의 `place_order`(일반 주문) POST 429 재시도-백오프 동작은 이번 결정과 무관하게 무변경** — 스코프는 조건주문 write 경로로 한정 | Codex BLOCK 리뷰 major 1: 첫 요청 처리 여부가 불명확한 상태에서의 자동 재전송은 중복 등록 가능성을 열어둠. 조회(get/list)는 기존 지수 백오프 재시도 유지(멱등 read이므로 무해) |
| 한도 산입 | 등록(승인) 시 예상 금액(트리거가 아닌 **leg 지정가 × 수량** 기준, 비 KRW는 FX 환산) 을 **즉시 전액 산입**. 산입 유지: `WATCHING`/`PAUSED`/`ORDERING`/`registration_unknown` — 날짜 무관 미확정 전액(Phase 3 v3 원칙). 해제: `EXPIRED`/취소 확인 시. 확정 전환: `COMPLETED`(체결 확정분으로 전환). 1회 한도·1억 hard reject는 등록 시점 검사 | 한도 우회용 다중 조건주문 레이스 원천 차단(그릴링 결정 5). 보수적 이중 산입 허용 — 한도는 상한이지 목표가 아니다 |
| expireDate 상한 | 제안 생성·승인 양쪽에서 `expireDate ≤ 오늘(KST)+7일` 검증, 위반 시 422. 만료 후 재개는 새 제안(승인 재수행) | 한도 장기 점유 완화 + 주기적 재확인 강제(그릴링 결정 6) |
| 정정 | 비범위. 변경은 취소(`DELETE`) → 새 제안 → 재승인만 | Toss 정정이 ID를 교체해 감사 체인이 끊김(그릴링 결정 7) |
| UX 표면 | API-only(Phase 3와 동일). 목록/단건 조회 엔드포인트로 가시성 제공 | 단일 사용자 시스템 — UI는 수요 확인 후 별도(그릴링 결정 8) |
| 저장 모델 | 신규 테이블 `PortfolioConditionalOrderProposal`(additive). Phase 3 `PortfolioOrderProposal`·상태기계는 **무변경** — 라이프사이클이 달라(원격 감시 상태 보유) 기존 테이블에 타입 컬럼으로 얹지 않는다. 감사는 기존 `PortfolioOrderAudit` 재사용 + `cond_*` 이벤트 추가(append-only 트리거 그대로). **v2: 이벤트 접두사는 스펙 초안의 `conditional_*`가 아닌 `cond_*`** — 기존 `event = String(24)` 컬럼(Phase 3에서 이미 확정된, 변경 불가 스키마) 폭에 `conditional_registration_unknown`(33자) 등 여러 이름이 들어가지 않아 축약함 | 기존 계약 불변 + 신규는 additive. 감사 인프라는 검증된 것 재사용 |
| 로컬 상태기계 | `pending → approving(원자적 claim: Phase 3 execute와 동일 패턴 — 단일 write 트랜잭션에서 재확인+전이+한도 검증+reservation) → approved(conditionalOrderId 보유) / registration_failed / registration_unknown`, `pending → canceled / expired(TTL) / dry_run_approved`, `approved → triggered_completed / toss_expired / toss_canceled / paused(비종결)`. `registration_unknown → approved / registration_failed`(reconcile). terminal에서의 전이 거부 | Phase 3 상태기계 원칙(원자적 claim, "기록 없으면 등록 없음", outcome_unknown 계열) 상속 |
| **v2: reconcile 대상 상태 게이팅** | reconcile은 원칙적으로 `registration_unknown`만 대상. `approving`은 **409 `approval-in-progress`**로 거부한다 — 실행 중인 approve POST가 아직 응답을 기다리고 있을 가능성이 있으므로 reconcile이 그 claim을 선점하면 안 된다. 예외(프로세스 사망 복구): claim 시각(`reserved_at`)이 **10분 초과** 경과한 `approving`만 reconcile이 원자적 age-check 전이(`approving → registration_unknown`, `PortfolioRepository.reconcile_claim_stale_approving`)로 인수 후 진행. **POST 결과가 authoritative**: `approve`의 성공 경로(`approved`, ID 기록)와 명시적 4xx 경로(`registration_failed`) 모두 `from_statuses={"approving","registration_unknown"}`으로 전이를 허용해, reconcile이 먼저 상태를 가져간 인터리빙에서도 실제 POST 결과가 항상 최종 기록으로 수렴하고 `conditionalOrderId`가 유실되지 않는다. terminal 상태에서의 전이는 여전히 거부 | Codex BLOCK 리뷰 blocker 2: reconcile이 진행 중인 approve POST와 경합해 `approving`을 선점하면, 이후 실제 POST가 성공해도 그 결과(및 ID)가 기록되지 않고 유실될 수 있음. 원 설계는 이 경합을 명시하지 않았음 |
| **v3: reconcile 매칭 계약(소유권 배타성 추가)** | Toss 조건주문 조회 응답에는 `clientOrderId`가 없으므로(§2 실측), reconcile은 `GET /conditional-orders`의 `OPEN`+`CLOSED` 목록에서 **속성 매칭**으로 수렴한다. 매칭 조건 **전부** 필수: (a) `symbol` 일치(목록 API의 `symbol` 필터 파라미터로 서버측 좁히기 + 응답 항목의 `symbol` 필드로 클라이언트측 재확인), (b) side/트리거가/지정가/수량/`expireDate` 일치, (c) 후보의 Toss `createdAt`이 `[해당 proposal의 approving claim 시각(reserved_at) − 5분, 현재]` 범위 내, (d) **`conditionalOrderId` 기준 dedupe**(같은 주문이 `OPEN`+`CLOSED` 양쪽에 걸치면 1개로 계상, `CLOSED`가 나중 조회이므로 상태는 `CLOSED` 쪽 우선 — v3 minor), (e) dedupe 후 `OPEN`+`CLOSED` 통틀어 매칭 후보가 **정확히 1개**. **v3 추가 소유권 검증**: 후보가 (e)를 만족해도 곧바로 채택하지 않고 — (f) 그 `conditionalOrderId`가 이미 **다른** 로컬 proposal(상태 무관)에 기록돼 있으면 후보에서 제외(`PortfolioRepository.find_conditional_order_ids_owned_by_others`), (g) 같은 계좌에서 동일 속성(symbol/side/trigger/limit/qty/expireDate)이고 아직 미해결(`approving`/`registration_unknown`)인 **다른** proposal이 존재하면(`local_contender_count > 0`) 원격 후보가 유일해도 채택하지 않는다 — 어느 쪽 것인지 속성만으로 식별 불가능하기 때문. (f)/(g) 어느 쪽이든 걸리면 `registration_unknown` 유지. 후보 0개·2개 이상·`local_contender_count > 0` 은 모두 **동일하게** `registration_unknown` 유지 — 절대 `registration_failed`로 강등하지 않고, 절대 임의 선택하지 않는다. `candidate_count`/`local_contender_count`/`owned_by_other_proposal_count` 모두 audit `detail`에 기록. **DB 레벨 백스톱**: `portfolio_conditional_order_proposals.toss_conditional_order_id`에 **partial unique index**(`WHERE toss_conditional_order_id IS NOT NULL`, `DatabaseManager._ensure_conditional_order_toss_id_unique_index`, 비파괴 `CREATE UNIQUE INDEX IF NOT EXISTS` 마이그레이션) 신설 — (f)/(g) 검사와 실제 전이 write 사이의 경합 창이 남아있으므로, 그 경합이 실제로 발생하면 `IntegrityError`로 실패해 채택을 막고 `registration_unknown`으로 귀결(`error_code=unique-conflict-on-adopt`). 유일 매칭된 후보의 Toss 상태가 `CLOSED` 그룹(`COMPLETED`/`EXPIRED`)이면 그 매핑(`triggered_completed`/`toss_expired`)도 위 전 조건을 만족할 때만 적용 | Codex BLOCK 리뷰 blocker 1(v2) — "속성만으로 매칭하면 Toss 앱이나 다른 proposal이 우연히 동일 속성을 가진 조건주문을 등록했을 때 그 주문의 ID를 자기 것으로 오인할 수 있음" — 을 v2는 시간창+유일성으로 완화했으나, Codex 2차 리뷰는 **유일 후보라도 소유권이 증명되지 않는다**(동일 속성 proposal A/B 중 A만 실제 등록됐는데 B의 reconcile이 A의 주문을 유일 후보로 채택 가능)는 부분수렴 blocker를 제기 — v3에서 소유권 배타 검증(f)(g)와 DB 유니크 인덱스로 닫음. 원 설계의 "GET 목록에서 clientOrderId 매칭"은 API가 그 필드를 반환하지 않아 애초에 구현 불가능했음(§2) — 재POST(Phase 3 방식)는 이중 등록 위험 때문에 명시적으로 배제(원 설계 결정 유지) |
| **v3: 지연 POST 결과 유실 방지(경합 창 구조적 제거 + 감사망)** | `TossFetcher`의 조건주문 write(POST 등록/DELETE 취소) 단일 HTTP 호출 타임아웃을 명시 상수화(`_CONDITIONAL_ORDER_WRITE_TIMEOUT_SECONDS=15`), 401 재시도 1회까지 포함한 단일 write 호출 최악 소요시간을 `_CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS`(=4×15=60초)로 도출. `PortfolioConditionalOrderService`는 **모듈 import 시점에** `_RECONCILE_STALE_APPROVING_AFTER(10분) ≥ 10 × _CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS(60초=600초)` 관계를 `assert`로 강제 — 이 관계가 성립하는 한 살아있는 프로세스의 "아직 응답을 기다리는 중인" POST가 reconcile의 stale-claim 창(10분)이 열리는 시점까지 여전히 진행 중일 수 없다(claim이 stale로 판정될 만큼 오래됐다면 그 claim을 쥐고 있던 프로세스는 이미 죽은 것 — 경합할 "지연 POST" 자체가 존재하지 않는다). **잔여 방어(감사망)**: 그럼에도 `_resolve_registration_outcome`의 전이가 (호출 자체가 예상보다 늦게 실행되거나, `force_resolve_proposal`·reconcile이 그 사이 다른 terminal 상태로 이미 전이시켜) no-op이 되면 — 로컬 상태는 절대 덮어쓰지 않되 — audit 이벤트 `conditional_registration_conflict`(POST의 실제 outcome/ID + 현재 로컬 상태 모두 포함) + `ERROR` 로그를 남겨, 운영자가 Toss 측에서 고아/중복 등록 여부를 확인할 근거를 확보한다 | Codex 2차 리뷰 신규 blocker — v2의 "reconcile은 `approving` claim이 10분 초과 경과했을 때만 인수" 게이트가 *진짜* 살아있는 POST를 결코 앞지르지 않는다는 보장이 시간 상수 관계로 명시되지 않아, 이론상 지연된 POST 성공/명시적 4xx가 이미 terminal로 종결된 행 위에서 조용히 유실(no-op)될 수 있었음. v3는 그 경합 창 자체를 상수 관계로 구조적으로 제거하고, 남는 이론적 잔여(터미널 전이 이후 완전히 별개 경로로 지연 응답이 도착)는 침묵 대신 감사망으로 전환 |
| **v4: 채택 원자 트랜잭션(경쟁자 재검사~채택 TOCTOU 제거)** | v3의 (f)/(g) 소유권·로컬 경쟁자 검사는 일반 read session에서 끝나고, 실제 채택(상태 전이+ID 기록)은 별도 `BEGIN IMMEDIATE` write 트랜잭션이라 그 사이에 경합 창이 남아 있었다. v4는 이 둘을 **단일 write 트랜잭션**으로 합친 `PortfolioRepository.adopt_reconciled_order_if_uncontended` 신설로 닫는다: Toss 목록 조회(네트워크)는 여전히 트랜잭션 밖에서 먼저 수행하고, 그 결과(유일 후보의 conditionalOrderId/toss_status)를 들고 `BEGIN IMMEDIATE` 트랜잭션에 진입 — 커밋 직전에 (f) 소유권(다른 proposal이 이미 이 ID를 갖고 있는지)과 (g) 로컬 경쟁자(같은 계좌·동일 속성·미해결 다른 proposal 존재 여부)를 **write-lock 하에 재조회**한다. 둘 중 하나라도 걸리면 채택을 취소하고 `registration_unknown` 유지 + `local_contender_count`/`owned_by_other_proposal_count`를 audit에 기록(`error_code=local-contender` 또는 `owned-by-other-proposal`); 둘 다 통과하면 같은 트랜잭션 안에서 상태 전이와 ID 기록을 커밋한다. SQLite `BEGIN IMMEDIATE`는 트랜잭션 전체 동안 RESERVED 락을 쥐므로, `portfolio_write_session`을 거치는 모든 writer가 직렬화된다 — 재검사와 실제 write 사이에 다른 proposal이 끼어들 수 있는 창이 구조적으로 사라진다. 기존 v3의 unique-index `IntegrityError` catch는 belt-and-braces 방어로 유지(SQLite 하에서는 도달 불가 경로가 되어야 정상) | Codex 3차 리뷰 blocker(R1c) — 근인: "경쟁자 조회는 read session, 채택은 별도 write txn이라 비원자적"; 예시: B가 경쟁자 0건 확인 직후, 채택 트랜잭션이 열리기 전에 동일 속성 A가 ID 없이 `approving`에 진입하면 unique index만으로는 이 경합을 막지 못함(A가 아직 ID를 갖지 않으므로) — v4 회귀 테스트(`test_reconcile_stays_unknown_when_local_contender_appears_between_advisory_check_and_atomic_adopt`)가 이 정확한 시나리오를 재현·검증 |
| **v4: unique 위반 fallback ID NULL 전이** | `_resolve_registration_outcome`의 primary 전이(예: `approved` + `conditional_order_id` 기록)가 partial unique index 위반으로 실패하면(= 그 ID를 다른 proposal이 이미 소유), fallback은 **같은 ID를 다시 전달하지 않고 NULL로** `registration_unknown` 전이한다 — 그 ID는 남의 것이므로 이 행에 재기록하지 않는다. `IntegrityError`는 이 경로에서 명시적으로 catch해 처리(다른 예외로 새지 않게 `except IntegrityError` 를 `except Exception`보다 먼저 배치) + audit 이벤트 `conditional_registration_conflict`(POST가 반환한 실제 ID + `error_code=owned-by-another-proposal` 사유) 기록 + `ERROR` 로그. proposal은 `registration_unknown`(reconcile/force-resolve로 복구 가능)으로 안착 — `approving`+500 방치 금지 | Codex 3차 리뷰 blocker(R1d) — 근인: 기존 fallback이 **같은 conditional_order_id를 그대로 재전달**해 동일 unique index에서 두 번째 `IntegrityError`가 발생 → `OrderAuditPersistFailedError`(500)로 끝나 proposal이 `approving`에 영구 방치되던 결함. v4 회귀 테스트(`test_approved_write_unique_conflict_falls_back_to_registration_unknown_with_no_id`)가 실제 DB unique index 충돌로 이 경로를 검증 |
| **v4: 정상 수렴 DEBUG 로그(minor)** | 전이가 no-op이 아니라 정상 수렴(현재 `approved` + 기록된 ID == POST의 ID)일 때도 `logger.debug`로 1줄 남긴다(운영 추적성 — 이전에는 충돌 분기에서만 로그가 남고 정상 경로는 무흔적이었음) | Codex 3차 리뷰 minor(R2b) |
| **v2: registration_unknown 영구 잔류 대응** | `POST .../conditional-orders/proposals/{uuid}/force-resolve`(인증 필수, body `{"confirm": true, "reason": "<필수 문자열>"}`) 신설. `registration_unknown`에서만 허용, `registration_failed`로 전이(= reservation 해제, `registration_failed`는 한도 미산입) + audit 이벤트 `cond_force_resolved`(`reason` 포함). **운영 절차**: 이 엔드포인트는 오직 운영자가 Toss 앱/API에서 해당 조건주문이 실제로 존재하지 않음을 직접 확인한 뒤에만 사용한다 — 실제로는 live인 주문에 대해 호출하면 그 주문이 여전히 자동 체결될 수 있음에도 한도 산입이 조용히 풀린다. **v3**: force-resolve 이후 지연된 원 POST 결과가 도착해도 위 감사망(`conditional_registration_conflict`)이 동일하게 커버 | Codex BLOCK 리뷰 major 3(가용성): reconcile의 속성 매칭은 "찾지 못함"을 "미등록 확정"으로 격상시키지 않는 fail-closed 설계이므로, 실제로 미등록인 경우에도 자동으로는 절대 종결되지 않는다. 자동 종결 경로 없이 이 한계만 "알려진 제약"으로 남기고 출시하는 것은 권장되지 않는다는 리뷰 판정에 따라 인증된 수동 종결 경로를 추가 |
| 인증·FX·한도값 검증 | Phase 3 계약 그대로: 쓰기 전부 `ADMIN_AUTH_ENABLED=true`+세션 필수(403 `order-auth-required`), 비 KRW는 FX 미존재·stale·fallback 시 거부, `TOSS_ORDER_*` 환경변수 파싱 규칙 동일. **v2: Phase 4는 "쓰기"뿐 아니라 조회(제안 목록/단건, 조건주문 관찰 목록) 3개 엔드포인트도 전부 `_require_order_auth` 적용** — 원 설계 초안은 Phase 3의 "조회는 전역 인증만으로 충분" 관례를 그대로 가져왔으나, 이 조회들은 종목·방향·트리거가·지정가·수량·상태·Toss ID를 노출하므로 `ADMIN_AUTH_ENABLED=false`에서 인증 없이 노출되는 것은 부적절하다는 리뷰 판정. **신규 환경변수 없음** | 검증된 계약 재사용, 설정 표면 불증가. Codex BLOCK 리뷰 major 2 |

## 4. 데이터 흐름

```
[제안]  POST /portfolio/links/{account_id}/conditional-orders/proposals
        {symbol, side, trigger_price, limit_price, quantity, expire_date}
        → 검증(심볼 해석·expireDate≤7일·1회 한도·1억 거부·FX·sellable/buying-power)
        → PortfolioConditionalOrderProposal(pending, TTL 10분) + audit(cond_proposed)

[승인]  POST .../conditional-orders/proposals/{uuid}/approve  {confirm: true}
        → 원자적 claim(pending 재확인 + 일일 한도 재검증·reservation + approving 전이)
        → dry-run: 등록 없이 dry_run_approved 종결 + audit(mode=dry_run)
        → live: Toss POST /conditional-orders (clientOrderId=dc-{uuid_hex})
           ├─ 성공 → approved + conditionalOrderId 기록 + audit(cond_approved)
           ├─ 명시적 4xx → registration_failed + reservation 해제 + audit
           └─ 유실/timeout/ID누락/429(v2: 재시도 없이 즉시) → registration_unknown(산입 유지) + audit
        (v2) from_statuses={"approving","registration_unknown"} — reconcile이 먼저
        상태를 가져간 인터리빙에서도 이 POST의 실제 결과가 최종 기록으로 수렴

[수렴]  POST .../proposals/{uuid}/reconcile
        (v2) → 원자적 게이트: registration_unknown이면 즉시 진행; approving이면
        claim이 10분 초과 경과했을 때만 registration_unknown으로 인수 후 진행,
        그렇지 않으면 409 approval-in-progress
        → GET /conditional-orders(OPEN+CLOSED) 목록에서 symbol+속성+createdAt
        시간창 매칭(conditionalOrderId 기준 dedupe, v3)
        (v3) → 후보 중 타 proposal 소유 ID 제외 → 동일 속성 미해결 로컬
        proposal("local contender") 존재 시 채택 보류
        → (제외/보류 후) 후보 정확히 1개면 채택 시도 → DB 유니크 인덱스
        충돌(IntegrityError) 시 registration_unknown 유지; 성공하면
        approved|toss_expired|triggered_completed;
        0개 또는 ≥2개 또는 local contender 존재는 registration_unknown 유지
        (강등도 임의 선택도 하지 않음)

[강제종결] (v2) POST .../proposals/{uuid}/force-resolve  {confirm: true, reason: "..."}
        → registration_unknown에서만 허용 → registration_failed(reservation 해제)
        + audit(cond_force_resolved, reason 포함). 운영자가 Toss에서 실제
        미등록을 확인한 뒤에만 사용하는 수동 절차

[관찰]  GET .../conditional-orders (목록: 로컬 상태 + lazy Toss 상태 refresh)
        GET .../conditional-orders/proposals/{uuid} (단건)
        POST .../conditional-orders/sync (수동 일괄 상태 동기화)
        (v2) 위 조회 3종 전부 인증 필수(§3 인증 행 v2)

[취소]  DELETE .../conditional-orders/proposals/{uuid}
        → pending이면 로컬 취소만; approved/paused면 Toss DELETE 후 toss_canceled +
        reservation 해제; approving/registration_unknown이면 reconcile 우선 요구
        (v2) 취소 DELETE도 429 재시도 없음 — 호출자가 재시도 판단

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
- **429(v2)**: 조건주문 등록 POST/취소 DELETE는 **재시도하지 않는다** —
  429 응답을 응답 유실과 동일하게 취급해 즉시 표면화한다. 등록 경로는
  서비스가 이를 `registration_unknown`으로 처리(reconcile로 수렴); 취소
  경로는 호출자에게 에러를 그대로 전달해 재시도 여부를 맡긴다. 조회(get/list)
  경로는 기존 지수 백오프 재시도를 유지(멱등 read이므로 재시도가 안전).
  Phase 3의 일반 주문 POST(`place_order`)의 429 재시도-백오프는 이 결정과
  무관하게 무변경.
- **`PROFIT_RATE`·OCO/OTO 요청 유입**: 스키마 레벨에서 422 거부(요청
  스키마에 `type`/조건 타입 필드 자체가 없고 `extra=forbid`이므로 알 수
  없는 필드 포함 시 422).
- **reconcile 오매칭(v2/v3)**: §3 "reconcile 매칭 계약(소유권 배타성 추가)"
  참고 — 후보 0개/≥2개/타 proposal 소유/로컬 경쟁자 존재 모두
  `registration_unknown` 유지, 유일 매칭 + 소유권 검증 통과만 채택.
- **동일 속성 proposal 간 소유권 경합(v3/v4)**: 동일 계정에서 완전히 동일한
  속성(종목/방향/트리거가/지정가/수량/만료일)의 proposal이 둘 이상
  존재하면, 한쪽이 실제로 등록됐어도 다른 쪽의 reconcile은 그 등록을
  채택하지 않고 `registration_unknown`으로 남는다(§3 v3) — 이는 의도된
  안전한 실패 모드다: 어느 쪽이 실제 소유자인지 속성만으로 증명할 수
  없는 상황에서 잘못된 쪽이 채택하는 것보다 낫다. **v4**: 이 검사가
  "경쟁자 0건" 확인 *이후* 채택 트랜잭션이 열리기 *전* 사이에 경쟁자가
  나타나는 경우도 커버한다 — `adopt_reconciled_order_if_uncontended`가
  같은 write 트랜잭션 안에서 이 검사를 재수행하므로, 그 사이 창에서
  경쟁자가 진입해도 채택은 취소되고 `registration_unknown`으로 수렴한다
  (§3 v4).
- **채택 write 자체가 unique 위반인 경우(v4)**: approve의 POST가 성공해
  실제 `conditionalOrderId`를 반환했더라도, 그 ID가 이미 다른 proposal에
  기록돼 있으면(예: 그 사이 다른 경로로 먼저 채택됨) `approved` 저장
  자체가 partial unique index를 위반한다. 이 경우 fallback은 그 ID를
  다시 쓰지 않고 NULL로 `registration_unknown` 전이하며, 실제 ID는
  audit(`conditional_registration_conflict`)에만 남긴다 — proposal이
  `approving`에 500과 함께 방치되는 일은 없다(§3 v4).
- **approve/reconcile 경합(v2/v3)**: §3 "reconcile 대상 상태 게이팅" 참고 —
  fresh `approving`은 reconcile이 거부(409), stale `approving`만 원자적
  인수. v3: 타임아웃-임계 상수 관계(`_RECONCILE_STALE_APPROVING_AFTER ≥
  10 × 조건주문 write 최악값`)로 "살아있는 프로세스의 지연 POST"가 이
  창을 통과해 유실되는 경로를 구조적으로 제거 — 남는 이론적 잔여는
  `conditional_registration_conflict` 감사망이 커버(어떤 인터리빙에서도
  최종 기록 또는 최소한 감사 흔적은 유실되지 않음).
- **registration_unknown 영구 잔류(v2)**: §3 "registration_unknown 영구
  잔류 대응" 참고 — `force-resolve`로 운영자가 수동 종결. v3: force-resolve
  이후 지연 POST가 도착해도 감사망이 커버(로컬 상태는 덮어쓰지 않음).

## 6. 검증 계획

- 게이트 회귀(최우선): dry-run 상태에서 `place_conditional_order`·
  `cancel_conditional_order`·`_request_write`/`_request_delete` 직접 호출(쿼리
  문자열 포함 URL 변형 포함) 전부가 POST/DELETE를 발생시키지 않음을 단위
  테스트로 실증.
- 상태기계: 원자적 claim 경합(병렬 approve 한쪽만 성공), terminal 전이 거부,
  registration_unknown → reconcile 양방향 수렴, dry_run_approved 불변식.
- 한도: 등록 시 산입·해제·확정 전환 각 경로, WATCHING 다건 합산이 일일
  한도를 차단하는지, expireDate 7일 초과 422, **Phase 3 claim과 Phase 4
  approve claim의 교차 한도 합산(조건주문 예약이 일반 주문을 차단하고
  그 역도 성립)**.
- 감사: cond_* 이벤트가 append-only 트리거 하에 기록되는지.
- **v2 reconcile 회귀**: 동일 속성 후보 2개 이상 → unknown 유지(강등도
  임의 선택도 없음), symbol 불일치 후보 비매칭, createdAt 시간창 밖 후보
  비매칭, fresh approving reconcile 거부(409)/stale approving 인수, 인수
  후 원 POST 결과가 최종 기록으로 수렴(ID 유실 없음).
- **v3 소유권 회귀**: 동일 속성 두 proposal 중 하나만 실제 등록된 경우
  미등록 쪽 reconcile이 unknown 유지(타 proposal 소유 ID 제외), 후보
  ID가 타 proposal 소유면 후보에서 제외, DB 유니크 인덱스 위반 경로(앱
  레벨 소유권 체크를 우회해도 unknown으로 수렴), OPEN/CLOSED 동일
  conditionalOrderId dedupe(CLOSED 우선).
- **v3 지연 POST 감사망 회귀**: reconcile이 이미 terminal 종결한 뒤 원
  POST가 뒤늦게 성공(다른 conditionalOrderId) → no-op +
  `conditional_registration_conflict` audit 기록(로컬 상태는 불변), 지연된
  명시적 4xx도 동일 감사망, force-resolve 이후 지연 POST 도착도 동일
  감사망. `_RECONCILE_STALE_APPROVING_AFTER ≥ 10 ×
  _CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS` 상수 관계 자체도 회귀
  테스트로 재검증(모듈 import 시점의 assert와 별개로).
- **v4 채택 원자성 회귀(R1c)**: B의 advisory 로컬 경쟁자 사전 검사가
  "0건"을 반환한 *직후*, `adopt_reconciled_order_if_uncontended`의
  write 트랜잭션이 열리기 *전*에 동일 속성 A가 `approving`으로 커밋되는
  경합을 주입(repo 메서드를 감싸 그 안에서 A의 claim을 먼저 커밋) →
  B의 채택은 트랜잭션 내 재검사로 취소되고 `registration_unknown`
  유지, `local_contender_count=1` audit 기록, A는 영향 없이 `approving`
  유지.
- **v4 unique 위반 fallback 회귀(R1d)**: A가 실제로 `conditionalOrderId`를
  소유한 상태에서 B의 승인 POST가 (시나리오 재현을 위해) 같은 ID를
  반환하도록 구성 → B의 `approved` 저장이 실제 partial unique index
  위반으로 실패 → fallback이 ID 없이 `registration_unknown`으로
  전이(500도, 두 번째 IntegrityError도 없음), `conditional_registration_conflict`
  audit에 실제 ID가 기록되는지, 이후 `force-resolve`로 복구 가능한지
  확인. `adopt_reconciled_order_if_uncontended` 자체가 `IntegrityError`를
  던지는 경로(SQLite 하에서는 도달 불가이어야 하는 belt-and-braces
  방어)도 mock으로 별도 검증.
- **v4 정상 수렴 로그 회귀(R2b)**: 정상 수렴 시 DEBUG 로그가 남고
  `conditional_registration_conflict` audit이 추가되지 않는지
  `assertLogs`로 검증.
- **v2 429 회귀**: 조건주문 등록/취소가 429에서 재시도하지 않고 즉시
  표면화(등록은 registration_unknown으로 귀결)하는지, Phase 3 `place_order`
  의 429 재시도-백오프는 무변경인지.
- **v2 인증 매트릭스**: Phase 4 신규 엔드포인트 9개(제안 생성/목록/단건,
  승인, reconcile, 취소, 관찰 목록, sync, force-resolve) 전부
  `ADMIN_AUTH_ENABLED=false`에서 403.
- **v2 force-resolve**: 정상 경로(registration_unknown → registration_failed,
  한도 해제, audit 기록), 잘못된 상태에서의 거부, reason 필수 검증.
- 실계정 스모크(격리, live 게이트 on, 사용자 입회): 소액 SINGLE-STOP 등록 →
  목록 조회 → 취소 왕복. 트리거 실발동은 스모크 비범위(시장 조건 의존).
- 기존 게이트: `./scripts/ci_gate.sh` 전체 green + Phase 3 테스트 무회귀.

## 7. 리스크와 롤백

- **최대 리스크**: 승인 후 자동 체결이라는 계약 자체. 완화 — dry-run 기본,
  등록 시 한도 전액 산입, 7일 만료 상한, 승인 body `confirm: true` 필수,
  감사로그. 잔여 리스크는 사용자가 결정 2에서 명시적으로 수용.
- **게이트 확장 누락 회귀**: 성공 기준에 명시 + 회귀 테스트 최우선 —
  구현 리뷰에서 이 항목을 blocker 기준으로 본다.
- **v2/v3: reconcile 오매칭 잔여 리스크**: symbol+속성+시간창+유일성+
  소유권 배타 매칭(§3 v3)도 이론상 완전한 안전은 아니다 — **명시적으로
  수용된 잔여 리스크**: 사용자가 Toss 앱/API로 동일 계정에 동일
  속성(종목/방향/트리거가/지정가/수량/만료일)의 조건주문을 이 시스템의
  proposal과 같은 5분 시간창 안에 수동으로 등록하면, 그 수동 주문과 이
  시스템의 등록을 속성만으로 구분할 수 없다. 시간창·유일성·소유권 배타
  검증이 이 시나리오를 "극히 좁은, 의도적 충돌이 필요한 경우"로
  축소시키지만 구조적으로 완전히 제거하지는 못한다 — 발생 시의 실패
  모드는 여전히 안전측(`registration_unknown` 유지, 절대 오채택 아님)
  이므로, 자동화로 제거하지 않고 알려진 잔여 리스크로 남기기로 확정
  (Codex 2차 리뷰 R1 판정 수용). 동일 계정에서 완전히 동일한 속성의
  proposal을 이 시스템 스스로 두 개 이상 만드는 경우는 로컬 경쟁자
  검사(§3 v3 (g))로 커버됨 — 어느 proposal이 실제 소유자인지 신뢰성 있게
  가려질 때까지 양쪽 다 `registration_unknown`으로 남는다.
- **v2: registration_unknown 가용성 잔여 리스크**: `force-resolve`는 운영자
  판단에 의존하는 수동 절차이며, 자동 판정이 아니다. 운영자가 Toss에서
  실제 상태를 확인하지 않고 호출하면 여전히 live인 주문의 reservation을
  잘못 해제할 수 있다 — 이 리스크는 자동화로 제거하지 않고 운영 절차·
  경고 문서화로 완화하기로 확정(Codex 리뷰 major 3 판정 수용).
- **v3: 지연 POST 유실 잔여 리스크**: `_RECONCILE_STALE_APPROVING_AFTER ≥
  10 × _CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS` 상수 관계가 살아있는
  프로세스에서는 경합을 구조적으로 불가능하게 만들지만, 이 관계 자체가
  깨지면(예: 향후 누군가 타임아웃 값만 올리고 stale 임계는 그대로 두면)
  보호가 사라진다 — 모듈 import 시점 `assert`로 관계 위반을 즉시
  실패시켜(배포 전에 CI에서 드러남) 조용한 드리프트를 방지한다. 그
  관계가 유지된다는 전제 하에 남는 유일한 경로(터미널 전이 이후 완전히
  별개 지연 응답)는 로컬 상태를 절대 덮어쓰지 않고 `conditional_registration_conflict`
  감사 이벤트만 남기므로, 최악의 경우도 "운영자 확인 필요"이지 "잘못된
  상태로 조용히 수렴"이 아니다.
- **롤백**: 신규 테이블·엔드포인트·fetcher 메서드는 전부 additive — 코드
  리버트로 즉시 제거 가능. 이미 Toss에 등록된 조건주문은 리버트와 무관하게
  Toss 앱/API에서 직접 취소 가능(만료 상한 7일이 자연 소멸 상한).

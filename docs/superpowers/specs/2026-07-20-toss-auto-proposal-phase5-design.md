# 토스증권 방어 신호 자동 주문 제안 (Phase 5) — 설계 스펙

- 작성일: 2026-07-20
- 상태: 설계 확정 (그릴링 결정 7건 반영 — 아래). **v1.1: advisor 사전 검토 5건 반영 — 보유수량 소스 정정(risk_service 출력엔 수량 없음→원장 `PortfolioPosition.quantity`), confidence 0~1 스케일 실측 확정, 즉시매도 현재가 소스 확정(`get_realtime_quote`), 조건주문 STOP limit 갭다운 미체결 수정(slippage를 조건주문 경로에도 적용·기본 비영), 중복방지 TOCTOU→`source_signal_id` DB unique index.** **v2: Codex 독립 리뷰(BLOCK, blocker 1·major 4·minor 2) 반영 — (1) 활성화를 `source_signal_id` unique index 실존 확인에 묶어 인덱스 생성 실패의 fail-open을 구조적으로 제거(§3 "활성화·설정", §5), (2) 개별 신호 처리 전체를 배치 루프에서 try/except로 감싸 격리 범위를 create_proposal 밖까지 확장(§5), (3) 배치 시작 시 연동 계좌별 `sync_linked_account` 선행 시도를 명시(§3 "트리거 위치", 계좌별 격리·best-effort), (4) 활성 제안 판정에서 TTL 만료 `pending`을 제외(§3 "중복 방지", §5), (5) §3 필터 행의 `plan_quality` 제외값을 실제 도메인에 맞게 `unknown`/`minimal`로 정정(`poor`는 이 필드에 존재하지 않는 값 — v1.1 각주 이탈을 정식 계약으로 승격).** **v3: Codex 2차 독립 리뷰(BLOCK, blocker 2) 반영 — v2가 도입한 인덱스 게이트 자체의 정밀 결함 2건. B1-a: 인덱스 생성 실패 catch가 `OperationalError`만 잡아, 중복 `source_signal_id` 행이 있는 기존 DB에서는 SQLite `CREATE UNIQUE INDEX`가 실제로 던지는 `IntegrityError`가 그대로 `DatabaseManager` 초기화 호출부까지 전파되어 **DB 초기화 전체가 깨짐**(§3 "활성화·설정", §7) — `IntegrityError`/`OperationalError` 둘 다 catch하도록 수정, init은 항상 생존하고 인덱스 부재는 이후 게이트가 구조적으로 감지. B1-b(v3 시점 구현): 활성화 게이트 체커가 인덱스 **이름만** 확인해, 동일 이름의 비-unique·non-partial 인덱스가 이미 있으면 `CREATE UNIQUE INDEX IF NOT EXISTS`가 no-op이고 체커는 여전히 True를 반환해 **실제 유니크 제약 없이 배치가 활성화**되는 경로가 있었음(§3 "활성화·설정") — 체커를 인덱스 이름이 아닌 `sqlite_master`에 저장된 실제 DDL 텍스트를 정규식으로 파싱하도록 수정.** **v4: Codex 3차 독립 리뷰(BLOCK, blocker 1) 반영 — v3의 B1-b 수정(DDL 정규식 파싱) 자체가 취약함이 실증됨. Fail-open: 정규식이 `(source_signal_id)`를 원문 아무 위치에서나 매칭해, SQL 주석 안의 동일 문자열이나 실제로는 다른/추가 컬럼에 걸린 복합 unique 인덱스(`(x, source_signal_id)`)를 유효로 오판 — 두 경우 모두 실제로는 "source_signal_id당 최대 1행"을 보장하지 않음. False-negative(major): `("source_signal_id")`/`[source_signal_id]` 같은 유효한 SQLite 인용 식별자를 정규식이 거부해, 제약이 정상인 DB를 영구 fail-closed로 만듦. **DDL 텍스트 파싱을 전부 제거하고 SQLAlchemy `Inspector.get_indexes()`의 구조화 결과(`unique`/`column_names` — 파싱된 값, 인용·공백·주석 무관)로 대체**: `unique is True` AND `column_names == ["source_signal_id"]`인 인덱스가 있는지만 확인. `WHERE source_signal_id IS NOT NULL` partial predicate는 게이트 통과 조건에서 **의도적으로 제외** — SQLite unique 인덱스는 NULL을 서로 distinct로 취급하므로 `source_signal_id` 단일 컬럼 unique 인덱스는 partial이 아니어도 이 배치가 의존하는 유일한 안전 속성("non-null 값당 최대 1행, NULL은 무제한 허용")을 이미 보장함(§3 "활성화·설정"에 근거 명시). 정규식 대항 변형(주석 우회·복합 인덱스·인용 식별자) 전부 실제 SQLite DB로 실증(§6).** **v5: Codex 4차 독립 리뷰(BLOCK, blocker 1 — 이번엔 리뷰어 자신의 4차 지시 오류를 리뷰어가 스스로 정정) 반영 — v4의 "partial predicate는 검증 불필요" 결론이 **부분적으로만** 참이었다. 그 근거("SQLite unique 인덱스는 NULL을 distinct로 취급")는 predicate가 없거나 `source_signal_id IS NOT NULL`뿐일 때만 성립하며, `WHERE source_signal_id > 100` 같은 임의의 제한 predicate에는 적용되지 않는다 — 이런 인덱스는 unique+단일컬럼 구조 체크를 그대로 통과하면서도 predicate 밖의 행(예: `source_signal_id = 1`)에 대해서는 유일성을 전혀 강제하지 않아 멱등성 계약을 조용히 위반한다(Codex 실측 반례). **구조화 체크(unique+column_names, v4에서 확립한 정답)는 유지하되, 그 체크를 통과한 각 후보 인덱스에 대해서만 좁게 `sqlite_master.sql`에서 자신의 실제 정의를 조회해 WHERE 절 tail을 추출·정규화(대소문자·인용부호·공백 무시)한 뒤, "predicate 없음" 또는 "정확히 `source_signal_id is not null`"일 때만 안전으로 판정** — v3의 fail-open 정규식(DDL 전체에서 컬럼명 아무데나 검색)과는 근본적으로 다르다: 이미 구조화 체크로 unique+단일컬럼임이 확정된 바로 그 인덱스의 predicate 부분만 좁게 확인하므로, 3차/4차에서 지적된 주석·복합·인용 취약점이 재도입되지 않는다. 제한 predicate(Codex의 정확한 반례 `source_signal_id > 100` 포함) 실제 DDL로 거부 실증, 안전 predicate·non-partial 실제 DDL로 통과 실증(§6).**
- 선행: Phase 2(연동 계좌·보유 원장), Phase 3(수동 승인 일반 주문), Phase 4(서버측 조건주문 SINGLE-STOP), ADR 0003
- 사전 조사: `.claude/reviews/2026-07-20-toss-phase5-auto-proposal-probe.md`
- 관련 영역: `src/services/portfolio_risk_service.py`(재사용), `src/services/portfolio_order_service.py`, `src/services/portfolio_conditional_order_service.py`, `src/services/portfolio_broker_sync_service.py`(재사용, v2 sync 선행), `src/storage.py`, `src/core/`(일일 파이프라인 훅), `api/v1/endpoints/portfolio.py`, `src/notification.py`

## 1. 개요와 목표

일일 분석이 만든 **보유 종목 방어 신호(매도/손절/비중축소)**를 자동으로 주문 제안(draft)으로 변환한다. 승인은 **여전히 100% 수동** — Phase 5는 오직 `create_proposal`(draft 생성)까지만 하고, 실행은 Phase 3/4의 기존 승인 게이트(`confirm: true` + 인증)를 그대로 거친다. "분석 → 주문 제안"의 마지막 미연결 파이프를 잇되, 자동 매매는 도입하지 않는다.

핵심 제약 회피: 예약/일괄 분석 신호는 account_id·수량 근거가 없어(probe 실측) 완전 자동 매수 제안이 불가능하다. 그러나 **보유 종목의 방어 신호는 계좌·보유수량이 원장에서 파생**되므로 이 범위로 한정한다. `portfolio_risk_service`가 이미 보유 포지션→최신 방어 신호를 `(account_id, symbol, market, signal, held_qty)`로 역매핑하고 있어(`DEFENSIVE_DECISION_SIGNAL_ACTIONS = ("sell","reduce","alert")`), 이 계산을 **재사용**한다(평행 구현 금지).

### 그릴링 확정 결정 (7건)
1. **범위**: 보유 종목 방어 신호만(매도/손절/비중축소). 신규 매수 제안은 비범위.
2. **트리거**: 일일 분석 완료 후 일괄(batch). 이벤트/수동 아님.
3. **주문 형태**: 손절(stop_loss 보유 신호)→Phase 4 조건주문 STOP, 즉시매도→Phase 3 일반 지정가.
4. **노이즈 필터**: confidence 임계 + plan_quality 유효 + 중복 방지.
5. **수량**: 손절/전량매도=보유수량 전량, 비중축소=보유수량 절반(정수 절삭). `alert`는 제안 생성 안 함.
6. **노출**: 기존 알림 채널로 배치 요약 1건 + 기존 pending 목록 API.
7. **활성화**: opt-in 플래그 기본 off(`PHASE5_AUTO_PROPOSAL_ENABLED`). 미설정 시 기존 동작 불변.

### 명시적 비범위
- 신규 매수 제안·비보유 종목(계좌/수량 파생 불가), 자동 실행(승인은 수동 유지), OCO/OTO, 이벤트/실시간 트리거, 다중 사용자, 포지션 사이징 고급 규칙(비중/변동성 기반), Web UI 신규 화면(기존 목록 API + 알림만), decision_signal_outcome 후험 반영(독립 유지).

## 2. 입력 계약 (실측 — 재사용 자산)

| 자산 | 사실 | 재사용 방식 |
|---|---|---|
| 방어 신호 역매핑 | `portfolio_risk_service`가 보유 포지션 스냅샷 → 최신 active 방어 신호(`sell/reduce/alert`)를 `(account_id, symbol, market, signal)` risk_item으로 산출(`:104-165`) | 이 산출 로직을 공유 헬퍼로 추출/재사용 — 자동 제안 생성기의 입력 소스 |
| 보유수량 (v1.1 정정) | **risk_service 출력엔 수량이 없음**(position dict는 account_id/symbol/market/signal_stock_code만) — 실측 확인. 수량은 **로컬 원장 `PortfolioPosition.quantity`**(`repositories/portfolio_repo.py`, `quantity>0` 필터)를 `(account_id, symbol)`로 조회해 얻는다 | 사이징 소스=원장 보유수량. create_proposal이 라이브 `get_sellable_quantity`로 재검증(이중 안전) |
| confidence 스케일 (v1.1) | **0~1 Float 실측 확정**(프로덕션 샘플 0.4/0.6) — `score`(0~100 Integer)와 별개 컬럼 | 필터 임계 `PHASE5_MIN_CONFIDENCE` 기본 **0.6**은 유효(no-op 아님) |
| 현재가 (v1.1) | 서버측 `data_provider` `get_realtime_quote(stock_code)` 가용(`base.py:1739`) | 즉시매도 지정가 파생 소스. 조회 실패 시 fail-closed skip |
| 가격 필드 | `stop_loss`/`target_price`/`entry_*`는 `sniper_points.py` 정규식 파싱(LLM 텍스트) — 힌트지 API 실값 아님 | **조건주문 trigger로만 `stop_loss` 사용(구조화 Float). 즉시매도 지정가는 §3 규칙대로 파생, 불가 시 fail-closed skip** |
| action enum | 방어 = `sell`/`reduce`/`alert` | `sell`→전량, `reduce`→절반, `alert`→제안 생성 안 함(요약에만 표기) |
| 제안 생성 계약 | Phase 3 `create_proposal`(symbol/side/order_type/price/quantity 등), Phase 4 조건주문 `create_proposal`(symbol/side/trigger_price/limit_price/quantity/expire_date) — 둘 다 수량 필수·자체 검증(한도·buying-power/sellable·FX·1억 거부) | 생성기는 이 두 서비스의 `create_proposal`을 **그대로 호출**(검증 재사용). 신규 검증 로직 만들지 않음 |

## 3. 확정된 설계 결정

| 결정 | 값 | 근거 |
|---|---|---|
| 트리거 위치 | 일일 분석 파이프라인에서 **포트폴리오 sync + 신호 생성 완료 후** 단일 배치 훅으로 실행. `PHASE5_AUTO_PROPOSAL_ENABLED=true`(strict, Phase 3 `TOSS_ORDER_LIVE` 파싱 규칙 준용) + 연동 Toss 계좌 ≥1 일 때만. 미충족 시 no-op(로그 1줄). **(v2)** 배치 자신도 시작 시 연동 Toss 계좌별로 `PortfolioBrokerSyncService.sync_linked_account`를 선행 시도한다(계좌별 격리 — ADR 0003 IP 제약·자격증명 미설정·일시적 업스트림 오류로 한 계좌의 sync가 실패해도 그 계좌는 기존 원장 상태로 로그 남기고 계속, 배치 전체를 막지 않는 best-effort — sync는 하드 의존이 아님, 이미 아는 포지션의 방어 제안까지 막으면 안 됨) | 그릴링 결정 2. 신호가 최신이려면 sync·생성 이후여야 함 |
| 입력 소스 | `portfolio_risk_service`의 보유→방어신호 역매핑을 공유 헬퍼로 재사용해 `(account_id, symbol, market, signal, held_qty)` 목록 획득 | 그릴링 결정 1. 계좌·수량 파생 문제를 구조적으로 우회, 평행 구현 금지 |
| 필터 | `confidence ≥ PHASE5_MIN_CONFIDENCE`(env, 기본 0.6, NaN/범위밖 방어) **AND** `plan_quality` 유효(**v2 정정**: `unknown`/`minimal` 제외 — `DecisionSignalRecord.plan_quality`의 실제 도메인은 `{complete, partial, minimal, unknown}`이며 `poor`는 이 필드에 존재하지 않는 값. `poor`는 이 시스템의 별개 필드인 `data_quality_level` 어휘에만 존재하므로, 문자 그대로 `poor`를 걸러내면 아무것도 걸러지지 않아 스펙의 실제 의도(저품질 플랜 배제)를 무력화한다 — `minimal`이 이 필드의 실제 최저 등급 값이므로 이를 대신 사용) **AND** 신호에 필수 가격 필드 존재(§ 주문 매핑). 미달 신호는 skip + 사유 로그 | 그릴링 결정 4. 저품질·가격 결손 신호가 제안으로 새는 것 차단 |
| 중복 방지 (v1.1, v2 정정) | 배치 멱등성을 **DB가 강제**: 두 제안 테이블에 `source_signal_id` 부분 unique index(NULL 다중 허용 — 매뉴얼 제안은 NULL). 자동 생성은 단일 write 트랜잭션에서 insert하고 unique 위반이면 그 신호 skip(read-then-insert TOCTOU 금지 — Phase 4에서 Codex가 2회 BLOCK한 그 클래스). 추가로 같은 `(account_id, symbol, side)` 활성 제안 존재 시에도 skip(사용자 매뉴얼 제안과의 충돌 회피) — **실제 로컬 상태기계 기준**(v1.1 원안의 "pending/approving/registration_unknown/watching/executing"는 두 테이블의 실제 상태값과 불일치했다): Phase 3(`PortfolioOrderProposal`)은 `pending`/`executing`/`outcome_unknown`, Phase 4(`PortfolioConditionalOrderProposal`)은 `pending`/`approving`/`approved`/`paused`/`registration_unknown`. **(v2)** 두 상태 집합 중 `pending`은 10분 TTL(`expires_at`) 대상이므로, TTL이 지났지만 아무도 조회하지 않아 로컬 status가 아직 `expired`로 materialize되지 않은 `pending` 행은 "활성"으로 세지 않는다(`expires_at > now` 추가 필터) — 그렇지 않으면 사용자가 한 번도 조회하지 않은 만료 제안 하나가 같은 종목의 새 방어 신호를 무기한 차단한다. **(v2)** 이 멱등성 계약 자체가 §3 "활성화·설정"의 unique index 실존 확인에 의존한다 — 인덱스가 없으면 insert-then-catch가 아무것도 막지 못하므로 배치는 아예 실행되지 않는다 | 그릴링 결정 4 + advisor. DB 강제로 배치 재실행·경합 모두 멱등 |
| 수량 산정 | `sell`/전량 → **원장 보유수량 전량**. `reduce` → `floor(보유수량/2)`(0이면 skip). `alert` → 제안 생성 안 함(요약에만). 수량 소스=`PortfolioPosition.quantity`(v1.1). 매도수량 > sellable 불가(create_proposal이 라이브 재확인) | 그릴링 결정 5. 신호 의도 직관 반영, 설정 불필요 |
| 주문 형태 매핑 | 신호에 `stop_loss`(Float) 존재 → **Phase 4 조건주문 STOP**(side=sell, `trigger_price=stop_loss`, **`limit_price=stop_loss·(1−slippage)`**, `expire_date`=오늘+7일 상한, quantity=위 규칙). `stop_loss` 없음(즉시매도) → **Phase 3 일반 지정가**(side=sell, `limit_price`=§ 즉시매도 가격 규칙, quantity=위 규칙) | 그릴링 결정 3. 손절은 감시형, 즉시매도는 즉시형 |
| **조건주문 limit 갭다운 (v1.1)** | 조건주문 leg는 **LIMIT 전용**(MARKET 불가, probe A표) — `limit=trigger` 1:1이면 가격이 트리거를 관통해 갭다운할 때 미체결로 손실 지속(손절이 방어하려던 바로 그 시나리오). 따라서 **매도 조건주문 `limit_price = trigger_price·(1 − PHASE5_SELL_SLIPPAGE_BPS/10000)`** — 즉시매도 경로와 **동일** 슬리피지를 조건주문에도 적용. tick size 위반은 create_proposal이 거부(그 신호 skip) | 보호 경로(손절)가 즉시매도보다 체결 보호가 약하면 안 됨 — advisor 지적 |
| 즉시매도 가격 규칙 | 일반 지정가 limit은 **서버측 `get_realtime_quote(stock_code)` 현재가**(v1.1 확정 소스)에서 파생 — 하방 슬리피지 `PHASE5_SELL_SLIPPAGE_BPS`(env). **현재가를 신뢰 가능하게 얻지 못하면 그 신호는 skip + 사유 로그(fail-closed) — 정규식 파싱 가격을 지정가로 조작하지 않음** | probe: 정규식 가격은 지정가 부적합. 조작 대신 fail-closed |
| 승인=수동 유지 | 생성기는 **오직 `create_proposal`(draft)만** 호출. execute/approve는 여전히 `confirm: true` + 인증 필수. 생성기 경로에 자동 실행 없음 — 이 불변식을 런타임/테스트로 실증 | 자동 매매 없음(그릴링 재확인). load-bearing 안전 속성 |
| 생성기 인증 계약 | 배치 생성기는 신뢰된 내부 프로세스로 서비스 계층 직접 호출(HTTP 인증 게이트 밖). 단 **생성된 제안의 실행은 여전히 인증 필수** — 생성이 실행 인증을 우회하지 않음 | 인증 구멍 방지. 생성≠실행 |
| 출처 메타(additive) | 두 제안 테이블(`PortfolioOrderProposal`, `PortfolioConditionalOrderProposal`)에 **additive 컬럼** `generation_source`(`manual`|`auto`, 기본 `manual`) + `source_signal_id`(nullable int) 추가. 비파괴 마이그레이션. 중복 방지·감사·알림 요약·API 필터에 사용. 기존 행/계약 불변 | 출처 추적 + 멱등. 기존 매뉴얼 흐름 무변경 |
| 노출 | 배치 종료 시 기존 알림 채널로 **요약 1건**("승인 대기 자동 제안 N건: 종목·side·수량·형태, alert K건 수동 검토") 전송. **알림 실패는 격리** — 분석 파이프라인·제안 생성을 중단시키지 않음(CLAUDE.md 단일 채널 실패 격리). API는 기존 pending 목록에 `generation_source` 필터 additive 노출(값이 `manual`/`auto`가 아니면 422). **(v2)** 생성/스킵/alert 전부 0건이어도 배치가 실제 실행됐다면 요약은 전송된다(미실행과 구분). **(v2, minor)** 단, 인덱스 부재/무효로 **거부(refuse)**된 실행은 이 "0건 요약"과 **다른** 전용 error 등급 알림("Phase 5 refused: idempotency index missing/invalid — ...")으로 전송되어, 운영자가 "정상 실행했으나 대상 없음"과 "인덱스 장애로 미실행"을 알림 문구만으로 구분할 수 있다 | 그릴링 결정 6 |
| 활성화·설정 | 신규 env 3종, 전부 미설정 시 안전: `PHASE5_AUTO_PROPOSAL_ENABLED`(기본 false=전체 no-op), `PHASE5_MIN_CONFIDENCE`(기본 0.6, 0~1 스케일, NaN/범위밖 방어), `PHASE5_SELL_SLIPPAGE_BPS`(기본 **50**=0.5% — 매도 체결 보호, 조건주문·즉시매도 양 경로 공통; NaN/음수 방어). `reduce` 절반은 상수. `.env.example`+문서 동기화(CLAUDE.md). **(v2, Codex B1)** 플래그·계좌 조건을 만족해도, 배치 시작 시 두 제안 테이블의 `source_signal_id` partial unique index가 **둘 다** 실존하는지 확인하고 하나라도 없으면 ERROR 로그("Phase 5 idempotency index missing, refusing to run") 후 fail-closed no-op — 인덱스 생성은 DB init 시점에 (기존 배포 DB의 중복행으로) 실패할 수 있는데 그 실패는 조용히 로그만 남기고 DB init 자체는 계속 성공하므로(비파괴 마이그레이션 관례), 인덱스 부재 상태에서 "활성화"만으로 배치가 도는 경로를 이 확인이 구조적으로 차단한다. **(v3, Codex 2차 B1-a)** 위 "실패는 로그만 남기고 DB init은 계속 성공" 전제 자체가 v2에서는 절반만 참이었다 — SQLite의 `CREATE UNIQUE INDEX`는 중복 `source_signal_id` 행이 있는 DB에서 `OperationalError`가 아니라 `IntegrityError`를 던진다(in-memory DB로 실측: `OperationalError`는 이 케이스에서 발생하지 않음). v2 코드는 `OperationalError`만 catch했으므로 이 경우 `IntegrityError`가 `DatabaseManager.__init__`까지 그대로 전파되어 **DB 초기화 자체가 죽었다** — 인덱스 없이 배치가 도는 것보다 더 나쁜 결과(전체 서비스 기동 실패). `IntegrityError`/`OperationalError` 둘 다 catch하도록 수정, init은 항상 생존. **(v3, Codex 2차 B1-b)** "실존 확인"이 인덱스 **이름**만 보는 것으로는 불충분함이 밝혀졌다 — 동일 이름이지만 `UNIQUE`가 아니고 partial predicate도 없는 인덱스가 이미 존재하면 `CREATE UNIQUE INDEX IF NOT EXISTS`는 SQLite의 "이름만 보는 IF NOT EXISTS" 의미상 no-op이 되고, 이름만 확인하는 체커는 여전히 True를 반환한다 — 이 경우 배치는 **실제 유니크 제약이 전혀 없는 상태로 활성화**되어 B1이 막으려던 바로 그 경합을 다시 연다. v3 시점에는 체커를 `sqlite_master.sql`(SQLite가 실제로 저장·집행하는 DDL 원문)에 대한 정규식으로 재작성했었다. **(v4, Codex 3차 B1-b)** 그 정규식 자체가 취약함이 실증됨 — fail-open(주석 안의 `(source_signal_id)` 문자열이나, 실제 제약은 다른/추가 컬럼에 걸린 복합 unique 인덱스 `(x, source_signal_id)`를 정규식이 유효로 오판)과 false-negative(`("source_signal_id")` 같은 정상적인 SQLite 인용 식별자를 정규식이 거부해 제약이 올바른 DB를 영구 fail-closed로 만듦) 둘 다 실측됨. **DDL 텍스트 파싱을 전부 폐기하고 `Inspector.get_indexes(table)`가 반환하는 구조화 결과로 대체** — 각 제안 테이블에 `index["unique"] is True` AND `index["column_names"] == ["source_signal_id"]`인 인덱스가 있는지만 확인한다(파싱된 컬럼 리스트라 인용·공백·주석에 영향받지 않고, 복합·다른 컬럼 인덱스는 비교 자체에서 자동 배제됨). `WHERE source_signal_id IS NOT NULL` partial predicate는 게이트 조건에서 제외 — SQLite unique 인덱스는 NULL을 서로 distinct로 취급하므로, `source_signal_id` 단일 컬럼 unique 인덱스는 partial이 아니어도 "non-null 값당 최대 1행, NULL(매뉴얼 제안)은 무제한 허용"이라는 이 배치가 실제로 의존하는 유일한 안전 속성을 이미 보장한다(partial predicate는 인덱스 크기 최적화일 뿐 정확성 조건이 아님)**였으나, 이 결론은 v5에서 절반만 맞는 것으로 정정됨** — 그 근거는 predicate가 없거나 정확히 `source_signal_id IS NOT NULL`일 때만 성립한다. **(v5, Codex 4차 B1-b 정정)** `WHERE source_signal_id > 100` 같은 **임의의 제한 predicate**는 unique+단일컬럼 구조 체크를 그대로 통과하면서도, predicate 밖의 행(예: `source_signal_id = 1`)에는 유일성을 전혀 강제하지 않아 멱등성 계약을 조용히 위반한다(Codex 실측 반례). 구조화 체크(unique+column_names)는 그대로 유지하되, **그 체크를 통과한 후보 인덱스 각각에 대해서만** `sqlite_master.sql`에서 자신의 실제 DDL을 조회해 마지막 `)` 뒤의 WHERE tail만 추출·정규화(소문자화, 인용부호 제거, 공백 축약)한 뒤 "predicate 없음" 또는 "정규화 결과가 정확히 `source_signal_id is not null`"일 때만 안전으로 판정, 그 외(범위 조건·추가 AND 절 등)는 거부(fail-closed) — v3의 fail-open 정규식(DDL 전체 문자열에서 컬럼명을 아무데나 검색)과는 근본적으로 다르다: 이미 구조화 체크로 unique+단일컬럼임이 확정된 그 인덱스 하나의 predicate 부분만 좁게 확인하므로 주석·복합·인용 취약점이 재도입되지 않는다 | 그릴링 결정 7 + advisor. 손절 체결 보호를 위해 slippage 기본 비영. Codex B1(v2)/B1-a(v3)/B1-b(v3→v4 introspection 전환→v5 predicate 안전성 검증 복원): fail-open 인덱스 생성이 init을 깨지 않으면서도, 활성화 게이트가 텍스트 매칭이 아닌 구조화된 실제 정의(컬럼+predicate)로 인덱스를 검증하도록 |
| 후험 독립 | 자동 제안은 `decision_signal_outcome` 통계에 영향 없음(제안≠신호 평가) | probe: 두 계층 구조 분리 유지 |
| dry-run 상호작용 | 생성기는 draft만 만들어 Toss write 없음 → `TOSS_ORDER_LIVE`와 무관하게 실행 가능. 실제 Toss write는 수동 승인 시점에 기존 게이트가 관장 | 생성은 안전, 실행만 게이트 대상 |

## 4. 데이터 흐름

```
[일일 분석 완료 + 신호 생성]
  └─ PHASE5_AUTO_PROPOSAL_ENABLED=true 이고 연동 계좌 ≥1 이면:
     0. (v2) source_signal_id unique index 2개 실존 확인 — 하나라도 없으면
        ERROR 로그 + fail-closed no-op(이 아래 전부 스킵)
     0.5. (v2) 연동 계좌별 sync_linked_account 선행 시도(계좌별 격리,
          실패해도 그 계좌는 기존 원장으로 계속 — best-effort)
     1. portfolio_risk_service 재사용 헬퍼 → 보유 방어신호 목록
        [(account_id, symbol, market, signal), ...]
        각 항목의 보유수량은 원장 PortfolioPosition.quantity를 (account,symbol)로 조회
     2. 각 항목 필터: confidence≥임계 ∧ plan_quality 유효(unknown/minimal 제외)
                     ∧ 가격필드 존재 ∧ 활성(TTL 미만료)/동일-source 제안 중복 아님.
                     미달 → skip+로그
     3. action별 수량: sell→전량, reduce→floor(held/2)(0이면 skip), alert→skip(요약만)
     4. 주문 형태 (side=sell):
        stop_loss 있음 → conditional create_proposal(STOP, trigger=stop_loss,
                          limit=stop_loss·(1−slippage), expire=오늘+7d,
                          generation_source=auto, source_signal_id=signal.id)
        stop_loss 없음 → get_realtime_quote 현재가·(1−slippage) limit(조회 실패시 skip)
                          → order create_proposal(LIMIT sell, generation_source=auto,
                          source_signal_id=signal.id)
        (source_signal_id partial-unique로 배치 멱등, 위반시 그 신호 skip)
        (두 create_proposal이 한도·sellable·FX·1억 거부 등 자체 검증 수행)
     (v2) 위 2~4 단계 중 한 신호에서 발생하는 **모든** 예외(수량 조회·활성
          제안 조회·create_proposal 전부 포함)는 그 신호만 skip하고 배치는
          계속 — try/except가 create_proposal 호출에만 걸리지 않는다
     5. 생성/스킵/alert 집계 → 알림 채널로 요약 1건(전부 0건이어도 전송,
        실패 격리)
  └─ 승인/실행은 전적으로 기존 Phase 3/4 수동 경로(confirm:true + 인증)
```

## 5. 엣지 케이스 계약

- **연동 계좌 0개 / 플래그 off**: 전체 no-op, 분석 파이프라인 무영향.
- **(v2) source_signal_id unique index 중 하나라도 없음**: 배치 전체가 fail-closed no-op(ERROR 로그). 인덱스 없이 "활성화"만으로 배치가 도는 경로 자체가 없다.
- **(v2) 신호 처리 중 예상치 못한 예외**(수량 조회·활성 제안 조회·create_proposal 등 어디서든): 그 신호만 skip + 사유를 요약·로그에 기록, 배치는 계속(단일 실패가 배치 중단 아님) — create_proposal 실패만이 아니라 `_process_match` 전체가 격리 대상.
- **(v2) 연동 계좌의 sync_linked_account 실패**(자격증명 미설정·ADR 0003 IP 제약·업스트림 오류 등): 그 계좌만 로그 남기고 기존 원장 상태로 배치 계속 — 다른 계좌·배치 전체는 무영향. sync는 신선도 개선일 뿐 하드 의존이 아니다.
- **배치 재실행(같은 날 두 번)**: 중복 방지(활성 제안 존재 + source_signal_id)로 멱등 — 새 제안 0건.
- **(v2) TTL 만료된 `pending` 제안**(아무도 조회하지 않아 로컬 status가 아직 `expired`로 materialize되지 않음): 활성 제안 판정에서 제외(`expires_at > now` 필터) — 만료 제안이 같은 종목의 새 방어 신호를 무기한 차단하지 않는다.
- **신호가 `alert`**: 제안 생성 안 함. 요약에 "수동 검토 K건"으로만 표기.
- **`reduce`인데 보유수량 1주**: `floor(1/2)=0` → skip + 로그.
- **즉시매도인데 현재가 조회 실패**: 그 신호 skip(fail-closed) — 지정가 조작 안 함.
- **알림 채널 실패**: 제안은 이미 생성 완료(DB 커밋), 알림만 실패 로그 — 파이프라인·제안 무손상.
- **(v2) 생성/스킵/alert 전부 0건**: 배치가 실제로 실행됐다면(활성화+계좌+인덱스 조건 충족) 그래도 요약 알림 1건은 전송 — "성공했으나 없음"과 "미실행"을 알림 유무로 구분 가능해야 한다.
- **생성 후 신호가 stale/철회**: 제안은 draft로 남고 사용자가 승인 안 하면 기존 TTL/만료가 정리(Phase 3/4 수명 계약 상속).

## 6. 검증 계획

- **안전 불변식(최우선)**: 생성기 경로가 어떤 조건에서도 execute/approve(Toss write)를 호출하지 않음을 테스트로 실증 — create_proposal만 호출.
- 입력 재사용: risk_service 역매핑 헬퍼가 `(account_id, symbol, held_qty, signal)`을 정확히 산출, 비보유·비방어 신호 제외.
- 수량: sell→전량(원장 PortfolioPosition.quantity), reduce→floor/2, floor=0 skip, alert skip.
- 주문 형태 분기: stop_loss 유무로 conditional vs plain, **조건주문·즉시매도 양쪽 limit에 slippage 적용(갭다운 체결 보호)**, expire 7일 상한, 즉시매도 현재가(get_realtime_quote) 파생·조회 실패 skip.
- 필터·중복: confidence(0~1)/plan_quality(`unknown`/`minimal`) 경계, **source_signal_id DB unique index 위반 시 skip(read-then-insert TOCTOU 아님) — 활성 제안 사전조회를 우회해 실제 unique 충돌 경로에 도달시키는 테스트로 실증(단순 pending 재실행만으로는 활성조회에서 끝나 이 경로를 검증하지 못함). 조건주문(Phase 4)·일반주문(Phase 3) 테이블 양쪽 모두 이 경로로 실증(v3)**, 동일 (account,symbol,side) 활성 제안 skip(**TTL 만료 pending은 비활성으로 취급**), 배치 재실행 0건.
- **(v2) 활성화 게이트**: `source_signal_id` unique index가 하나라도 없으면 배치가 fail-closed no-op임을 테스트로 실증; 기존 Phase 3/4 DB 마이그레이션 경로 이후 두 인덱스가 모두 존재함을 검증.
- **(v3, B1-a) 중복행 DB의 init 생존**: `source_signal_id`가 같은 값인 행 2개를 미리 심어둔 실제 SQLite DB에 `DatabaseManager.get_instance()`를 실행 — 예외 없이 초기화가 완료되고(이 지점에 도달한 것 자체가 증거), 이어서 `has_..._unique_indexes()`가 False, `AutoProposalService.run_batch()`가 `refused=True`로 귀결됨을 end-to-end로 실증(mock 아님, 실제 인덱스 상태).
- **(v3, B1-b) 이름만 같은 비-unique 인덱스 우회 탐지**: Phase 5 마이그레이션이 실행되기 전에 동일 이름·비-unique·predicate 없는 인덱스를 실제 SQLite DB에 미리 심고, 체커가 이를 "부재/무효"로 정확히 판정함을 실증(mock으로 체커만 False로 만드는 테스트와는 별개로, 실제 온-디스크 인덱스 정의 기반 판정 경로 자체를 검증).
- **(v4, B1-b introspection 전환) 정규식 대항 변형 3종을 실제 SQLite DB로 실증**: (a) 실제로는 다른 컬럼(`quantity`)에 걸린 UNIQUE 인덱스이면서 SQL 주석에만 `(source_signal_id)`를 포함한 DDL → 체커 False(구 정규식이면 True로 오판했을 케이스), (b) `(quantity, source_signal_id)` 복합 UNIQUE 인덱스 → 체커 False(단일 컬럼 아님), (c) 인용 식별자 `("source_signal_id") WHERE "source_signal_id" IS NOT NULL`(정상 제약) → 체커 True(구 정규식이면 거부했을 false-negative 케이스가 여기서는 통과함을 실증). 세 경우 모두 `get_indexes()`의 파싱된 `column_names`/`unique`만으로 자연스럽게 옳게 판정됨을 확인.
- **(v5, predicate 안전성 검증) 제한 partial predicate 실제 DDL로 거부 실증**: `CREATE UNIQUE INDEX ... ON t (source_signal_id) WHERE source_signal_id > 100`(Codex가 재현한 정확한 반례) → 체커 **False**. 대조군 2종도 실제 DDL로 통과 실증: `WHERE source_signal_id IS NOT NULL`(안전 partial) → **True**, WHERE 절 없는 non-partial 단일컬럼 unique → **True**. v3/v4의 주석·복합·인용 대항 변형과 B1-a·M4(a/b/c) 테스트는 회귀 없이 유지.
- **(v2) 격리 범위**: `_process_match` 내부 어디서 예외가 나든(수량 조회·활성 제안 조회 포함, create_proposal만이 아님) 그 신호만 skip하고 배치가 계속됨을 테스트로 실증.
- **(v2) sync 선행**: 연동 계좌별 sync_linked_account가 배치 시작 시 시도되고, 한 계좌의 실패가 다른 계좌·배치 전체를 막지 않음을 테스트로 실증(자격증명 미설정 시의 실패 경로로 충분 — 실 네트워크 불필요).
- 출처 메타: additive 컬럼 비파괴 마이그레이션, 기존 매뉴얼 제안 `generation_source=manual` 기본, API 필터(**v2: `generation_source` 필터에 `manual`/`auto` 외 값은 422, 빈 결과가 아님**).
- 알림 실패 격리: 알림 예외가 제안/파이프라인을 중단시키지 않음. **(v2)** 생성/스킵/alert 전부 0건이어도 배치가 실행됐다면 요약 알림 1건은 전송됨을 실증. **(v2, minor)** 인덱스 부재로 인한 refuse는 이 "0건 요약"과 문구·severity가 다른 전용 알림임을 실증(0건 요약 문자열이 포함되지 않고, `refused` 문구와 `severity="error"`가 전달됨).
- 활성화: 플래그 off/계좌 0개 no-op, env 파싱 방어(NaN/범위밖 기본값 강제).
- 전체 게이트: `./scripts/ci_gate.sh` green + Phase 2/3/4 무회귀.

## 7. 리스크와 롤백

- **최대 리스크**: 자동 생성이 잘못된 방어 제안을 양산. 완화 — 승인 수동 유지(자동 실행 0), confidence+plan_quality 필터, 중복 방지, 즉시매도 가격 fail-closed, opt-in 기본 off, 배치 요약 알림으로 가시성.
- **가격 신뢰성**: 정규식 파싱 가격을 지정가로 쓰지 않음 — 조건주문 trigger는 stop_loss만, 즉시매도는 현재가 파생·불가 시 skip.
- **롤백**: 신규 env(전부 기본 off/안전), additive 컬럼 2개, 신규 배치 훅·서비스는 리버트로 즉시 제거. 이미 생성된 draft 제안은 사용자가 승인 안 하면 무해(실행 없음), 기존 만료가 정리.
- **단일 채널/단일 신호 실패 격리**: 알림·개별 신호 실패가 분석 파이프라인을 중단시키지 않음.

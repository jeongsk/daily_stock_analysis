# 토스증권 방어 신호 자동 주문 제안 (Phase 5) — 설계 스펙

- 작성일: 2026-07-20
- 상태: 설계 확정 (그릴링 결정 7건 반영 — 아래). **v1.1: advisor 사전 검토 5건 반영 — 보유수량 소스 정정(risk_service 출력엔 수량 없음→원장 `PortfolioPosition.quantity`), confidence 0~1 스케일 실측 확정, 즉시매도 현재가 소스 확정(`get_realtime_quote`), 조건주문 STOP limit 갭다운 미체결 수정(slippage를 조건주문 경로에도 적용·기본 비영), 중복방지 TOCTOU→`source_signal_id` DB unique index.**
- 선행: Phase 2(연동 계좌·보유 원장), Phase 3(수동 승인 일반 주문), Phase 4(서버측 조건주문 SINGLE-STOP), ADR 0003
- 사전 조사: `.claude/reviews/2026-07-20-toss-phase5-auto-proposal-probe.md`
- 관련 영역: `src/services/portfolio_risk_service.py`(재사용), `src/services/portfolio_order_service.py`, `src/services/portfolio_conditional_order_service.py`, `src/storage.py`, `src/core/`(일일 파이프라인 훅), `api/v1/endpoints/portfolio.py`, `src/notification.py`

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
| 트리거 위치 | 일일 분석 파이프라인에서 **포트폴리오 sync + 신호 생성 완료 후** 단일 배치 훅으로 실행. `PHASE5_AUTO_PROPOSAL_ENABLED=true`(strict, Phase 3 `TOSS_ORDER_LIVE` 파싱 규칙 준용) + 연동 Toss 계좌 ≥1 일 때만. 미충족 시 no-op(로그 1줄) | 그릴링 결정 2. 신호가 최신이려면 sync·생성 이후여야 함 |
| 입력 소스 | `portfolio_risk_service`의 보유→방어신호 역매핑을 공유 헬퍼로 재사용해 `(account_id, symbol, market, signal, held_qty)` 목록 획득 | 그릴링 결정 1. 계좌·수량 파생 문제를 구조적으로 우회, 평행 구현 금지 |
| 필터 | `confidence ≥ PHASE5_MIN_CONFIDENCE`(env, 기본 0.6, NaN/범위밖 방어) **AND** `plan_quality` 유효(`unknown`/`poor` 제외) **AND** 신호에 필수 가격 필드 존재(§ 주문 매핑). 미달 신호는 skip + 사유 로그 | 그릴링 결정 4. 저품질·가격 결손 신호가 제안으로 새는 것 차단 |
| 중복 방지 (v1.1) | 배치 멱등성을 **DB가 강제**: 두 제안 테이블에 `source_signal_id` 부분 unique index(NULL 다중 허용 — 매뉴얼 제안은 NULL). 자동 생성은 단일 write 트랜잭션에서 insert하고 unique 위반이면 그 신호 skip(read-then-insert TOCTOU 금지 — Phase 4에서 Codex가 2회 BLOCK한 그 클래스). 추가로 같은 `(account_id, symbol, side)` 활성(pending/approving/registration_unknown/watching/executing) 제안 존재 시에도 skip(사용자 매뉴얼 제안과의 충돌 회피) | 그릴링 결정 4 + advisor. DB 강제로 배치 재실행·경합 모두 멱등 |
| 수량 산정 | `sell`/전량 → **원장 보유수량 전량**. `reduce` → `floor(보유수량/2)`(0이면 skip). `alert` → 제안 생성 안 함(요약에만). 수량 소스=`PortfolioPosition.quantity`(v1.1). 매도수량 > sellable 불가(create_proposal이 라이브 재확인) | 그릴링 결정 5. 신호 의도 직관 반영, 설정 불필요 |
| 주문 형태 매핑 | 신호에 `stop_loss`(Float) 존재 → **Phase 4 조건주문 STOP**(side=sell, `trigger_price=stop_loss`, **`limit_price=stop_loss·(1−slippage)`**, `expire_date`=오늘+7일 상한, quantity=위 규칙). `stop_loss` 없음(즉시매도) → **Phase 3 일반 지정가**(side=sell, `limit_price`=§ 즉시매도 가격 규칙, quantity=위 규칙) | 그릴링 결정 3. 손절은 감시형, 즉시매도는 즉시형 |
| **조건주문 limit 갭다운 (v1.1)** | 조건주문 leg는 **LIMIT 전용**(MARKET 불가, probe A표) — `limit=trigger` 1:1이면 가격이 트리거를 관통해 갭다운할 때 미체결로 손실 지속(손절이 방어하려던 바로 그 시나리오). 따라서 **매도 조건주문 `limit_price = trigger_price·(1 − PHASE5_SELL_SLIPPAGE_BPS/10000)`** — 즉시매도 경로와 **동일** 슬리피지를 조건주문에도 적용. tick size 위반은 create_proposal이 거부(그 신호 skip) | 보호 경로(손절)가 즉시매도보다 체결 보호가 약하면 안 됨 — advisor 지적 |
| 즉시매도 가격 규칙 | 일반 지정가 limit은 **서버측 `get_realtime_quote(stock_code)` 현재가**(v1.1 확정 소스)에서 파생 — 하방 슬리피지 `PHASE5_SELL_SLIPPAGE_BPS`(env). **현재가를 신뢰 가능하게 얻지 못하면 그 신호는 skip + 사유 로그(fail-closed) — 정규식 파싱 가격을 지정가로 조작하지 않음** | probe: 정규식 가격은 지정가 부적합. 조작 대신 fail-closed |
| 승인=수동 유지 | 생성기는 **오직 `create_proposal`(draft)만** 호출. execute/approve는 여전히 `confirm: true` + 인증 필수. 생성기 경로에 자동 실행 없음 — 이 불변식을 런타임/테스트로 실증 | 자동 매매 없음(그릴링 재확인). load-bearing 안전 속성 |
| 생성기 인증 계약 | 배치 생성기는 신뢰된 내부 프로세스로 서비스 계층 직접 호출(HTTP 인증 게이트 밖). 단 **생성된 제안의 실행은 여전히 인증 필수** — 생성이 실행 인증을 우회하지 않음 | 인증 구멍 방지. 생성≠실행 |
| 출처 메타(additive) | 두 제안 테이블(`PortfolioOrderProposal`, `PortfolioConditionalOrderProposal`)에 **additive 컬럼** `generation_source`(`manual`|`auto`, 기본 `manual`) + `source_signal_id`(nullable int) 추가. 비파괴 마이그레이션. 중복 방지·감사·알림 요약·API 필터에 사용. 기존 행/계약 불변 | 출처 추적 + 멱등. 기존 매뉴얼 흐름 무변경 |
| 노출 | 배치 종료 시 기존 알림 채널로 **요약 1건**("승인 대기 자동 제안 N건: 종목·side·수량·형태, alert K건 수동 검토") 전송. **알림 실패는 격리** — 분석 파이프라인·제안 생성을 중단시키지 않음(CLAUDE.md 단일 채널 실패 격리). API는 기존 pending 목록에 `generation_source` 필터 additive 노출 | 그릴링 결정 6 |
| 활성화·설정 | 신규 env 3종, 전부 미설정 시 안전: `PHASE5_AUTO_PROPOSAL_ENABLED`(기본 false=전체 no-op), `PHASE5_MIN_CONFIDENCE`(기본 0.6, 0~1 스케일, NaN/범위밖 방어), `PHASE5_SELL_SLIPPAGE_BPS`(기본 **50**=0.5% — 매도 체결 보호, 조건주문·즉시매도 양 경로 공통; NaN/음수 방어). `reduce` 절반은 상수. `.env.example`+문서 동기화(CLAUDE.md) | 그릴링 결정 7 + advisor. 손절 체결 보호를 위해 slippage 기본 비영 |
| 후험 독립 | 자동 제안은 `decision_signal_outcome` 통계에 영향 없음(제안≠신호 평가) | probe: 두 계층 구조 분리 유지 |
| dry-run 상호작용 | 생성기는 draft만 만들어 Toss write 없음 → `TOSS_ORDER_LIVE`와 무관하게 실행 가능. 실제 Toss write는 수동 승인 시점에 기존 게이트가 관장 | 생성은 안전, 실행만 게이트 대상 |

## 4. 데이터 흐름

```
[일일 분석 완료 + 포트폴리오 sync + 신호 생성]
  └─ PHASE5_AUTO_PROPOSAL_ENABLED=true 이고 연동 계좌 ≥1 이면:
     1. portfolio_risk_service 재사용 헬퍼 → 보유 방어신호 목록
        [(account_id, symbol, market, signal), ...]
        각 항목의 보유수량은 원장 PortfolioPosition.quantity를 (account,symbol)로 조회
     2. 각 항목 필터: confidence≥임계 ∧ plan_quality 유효 ∧ 가격필드 존재
                     ∧ 활성/동일-source 제안 중복 아님. 미달 → skip+로그
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
     5. 생성/스킵 집계 → 알림 채널로 요약 1건(실패 격리)
  └─ 승인/실행은 전적으로 기존 Phase 3/4 수동 경로(confirm:true + 인증)
```

## 5. 엣지 케이스 계약

- **연동 계좌 0개 / 플래그 off**: 전체 no-op, 분석 파이프라인 무영향.
- **create_proposal이 검증 실패(한도 초과·sellable 부족·FX·1억)**: 그 신호만 skip + 사유를 요약·로그에 기록, 배치는 계속(단일 실패가 배치 중단 아님).
- **배치 재실행(같은 날 두 번)**: 중복 방지(활성 제안 존재 + source_signal_id)로 멱등 — 새 제안 0건.
- **신호가 `alert`**: 제안 생성 안 함. 요약에 "수동 검토 K건"으로만 표기.
- **`reduce`인데 보유수량 1주**: `floor(1/2)=0` → skip + 로그.
- **즉시매도인데 현재가 조회 실패**: 그 신호 skip(fail-closed) — 지정가 조작 안 함.
- **알림 채널 실패**: 제안은 이미 생성 완료(DB 커밋), 알림만 실패 로그 — 파이프라인·제안 무손상.
- **생성 후 신호가 stale/철회**: 제안은 draft로 남고 사용자가 승인 안 하면 기존 TTL/만료가 정리(Phase 3/4 수명 계약 상속).

## 6. 검증 계획

- **안전 불변식(최우선)**: 생성기 경로가 어떤 조건에서도 execute/approve(Toss write)를 호출하지 않음을 테스트로 실증 — create_proposal만 호출.
- 입력 재사용: risk_service 역매핑 헬퍼가 `(account_id, symbol, held_qty, signal)`을 정확히 산출, 비보유·비방어 신호 제외.
- 수량: sell→전량(원장 PortfolioPosition.quantity), reduce→floor/2, floor=0 skip, alert skip.
- 주문 형태 분기: stop_loss 유무로 conditional vs plain, **조건주문·즉시매도 양쪽 limit에 slippage 적용(갭다운 체결 보호)**, expire 7일 상한, 즉시매도 현재가(get_realtime_quote) 파생·조회 실패 skip.
- 필터·중복: confidence(0~1)/plan_quality 경계, **source_signal_id DB unique index 위반 시 skip(read-then-insert TOCTOU 아님)**, 동일 (account,symbol,side) 활성 제안 skip, 배치 재실행 0건.
- 출처 메타: additive 컬럼 비파괴 마이그레이션, 기존 매뉴얼 제안 `generation_source=manual` 기본, API 필터.
- 알림 실패 격리: 알림 예외가 제안/파이프라인을 중단시키지 않음.
- 활성화: 플래그 off/계좌 0개 no-op, env 파싱 방어(NaN/범위밖 기본값 강제).
- 전체 게이트: `./scripts/ci_gate.sh` green + Phase 2/3/4 무회귀.

## 7. 리스크와 롤백

- **최대 리스크**: 자동 생성이 잘못된 방어 제안을 양산. 완화 — 승인 수동 유지(자동 실행 0), confidence+plan_quality 필터, 중복 방지, 즉시매도 가격 fail-closed, opt-in 기본 off, 배치 요약 알림으로 가시성.
- **가격 신뢰성**: 정규식 파싱 가격을 지정가로 쓰지 않음 — 조건주문 trigger는 stop_loss만, 즉시매도는 현재가 파생·불가 시 skip.
- **롤백**: 신규 env(전부 기본 off/안전), additive 컬럼 2개, 신규 배치 훅·서비스는 리버트로 즉시 제거. 이미 생성된 draft 제안은 사용자가 승인 안 하면 무해(실행 없음), 기존 만료가 정리.
- **단일 채널/단일 신호 실패 격리**: 알림·개별 신호 실패가 분석 파이프라인을 중단시키지 않음.

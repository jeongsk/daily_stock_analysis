# 토스증권 포트폴리오 하이브리드 동기화 (Phase 2) — 설계 스펙

- 작성일: 2026-07-17
- 상태: 설계 확정 (구현 계획: `../plans/2026-07-17-toss-portfolio-sync-phase2.md`)
- 선행 컨텍스트: `2026-07-17-toss-openapi-design.md` §5, ADR 0003, CONTEXT.md(연동 계좌·대사)
- 관련 영역: `src/services/`, `src/storage.py`, `api/v1/endpoints/portfolio.py`, `data_provider/toss_fetcher.py`

## 1. 개요와 목표

Phase 1에서 구축한 TossFetcher(자격증명 게이트)를 확장해, 사용자의 토스증권
계좌를 기존 포트폴리오 서브시스템에 **연동 계좌**로 동기화한다. 의미론은
그릴링 세션에서 확정된 하이브리드: 최초 연동 시 holdings 스냅샷으로 기초
포지션 → 이후 체결주문 증분 동기화 → 주기적 **대사**로 드리프트 감지.

### 성공 기준

- `POST /portfolio/links/toss` 한 번으로 토스 계좌가 연동 계좌로 생성되고,
  현재 보유 종목이 기초 포지션으로 잡힌다 (`market='kr'`,
  `base_currency='KRW'` — KR KRW P&L 정밀도 계약 준수).
- `POST /portfolio/links/{account_id}/sync`가 마지막 동기화 이후의 체결
  주문만 idempotent하게 원장에 추가하고, 대사 결과(드리프트 목록)를
  응답으로 돌려준다. 같은 호출을 반복해도 거래가 중복되지 않는다.
- 대사는 감지·보고까지만 한다 — 원장을 자동 수정하지 않는다 (CONTEXT.md).
- Toss 자격증명 미설정 시 신규 엔드포인트는 명확한 4xx로 응답하고, 기존
  포트폴리오 기능은 바이트 단위로 무변경.
- 403(허용 IP 미등록)은 Phase 1의 프로세스당 1회 경고 계약을 그대로 탄다.

### 명시적 비범위 (후속 분리)

- **현금 원장 합성 없음**: 입출금·배당은 orders API로 알 수 없고, 부분적
  현금 원장은 오도적이므로 만들지 않는다. 연동 계좌는 포지션·손익 추적용
  이며 현금 잔고는 다루지 않는다 — 기존 CSV 임포트와 동일한 관례.
- 배당·기업행위 동기화, 자동 스케줄 동기화(수동 트리거 우선 — 확정 결정),
  Web UI 화면, 봇 명령, 멀티 프로바이더 추상화(제너릭 설계 금지 —
  provider 컬럼 하나로 확장 여지만 남긴다), 드리프트의 portfolio_alerts
  통합.
- 주문 생성·정정·취소 (Phase 3 예약 — 읽기 전용 API만 사용).

## 2. 조사로 확정된 제약 (2026-07-17)

| 사실 | 귀결 |
|---|---|
| 포트폴리오는 이벤트 소싱: 포지션은 trade/corporate_action 리플레이의 파생 캐시. 스냅샷 직접 세팅 경로 없음 | 기초 포지션은 **합성 opening trade**(buy, price=평단, qty=보유수량)로 표현 — 리플레이 모델 보존 |
| `PortfolioTrade.trade_uid`가 계좌 스코프 유니크(String 128) | 토스 `orderId`를 `trade_uid`로 사용 → idempotent 증분의 자연 키. CSV의 라인번호 기반 `dedup_hash`는 쓰지 않는다 |
| `record_trade`는 현금 원장에 쓰지 않음 | §1 비범위와 일치 — 합성하지 않음 |
| `PortfolioAccount`에 커넥터/외부계좌 메타 필드 없음(`broker` 자유 문자열뿐) | 기존 테이블 ALTER 대신 **신규 테이블 `PortfolioBrokerLink`** (additive) |
| 매도 오버셀 검증이 리플레이 기반 | 기초 스냅샷이 과거 전체 손익의 결과를 포함하므로 이후 매도는 커버됨. 스냅샷 시각 경계만 정확하면 안전 |
| Toss `Order.execution`: filledQuantity/averageFilledPrice/**commission/tax**/filledAt(KST) | fee·tax까지 원장에 정확 반영 가능 |
| Toss rate limits: ACCOUNT 1 TPS, ASSET 5 TPS, ORDER_HISTORY 5 TPS, orders 페이지 100건 | 동기화는 사용자 트리거 단발 호출이라 여유. 페이지네이션 필수 |
| KR KRW 계약(commit 3e619653): 계좌를 `market='kr'`, `base_currency='KRW'`로 생성해야 정밀 P&L. 거래별 market/currency override 가능 | 연동 계좌는 KR/KRW로 생성, US 보유·체결은 거래 단위로 `market='us'`, `currency='USD'` |
| 계좌 API는 `BROKERAGE`만 노출, `accountSeq`가 사용자 컨텍스트 헤더 키 | 링크 메타에 accountSeq(호출용)·accountNo(표시용) 저장 |

## 3. 확정된 설계 결정

| 결정 | 값 | 근거 |
|---|---|---|
| 기초 포지션 표현 | holdings 항목당 합성 opening trade 1건: side=buy, quantity=보유수량, price=averagePurchasePrice, fee=tax=0, trade_date=스냅샷일, `trade_uid="toss:opening:{accountSeq}:{symbol}"`, note에 스냅샷 시각 명기 | 이벤트 소싱 보존. 재연동 시도에도 trade_uid 유니크로 중복 불가 |
| 스냅샷 경계 (불변) | 링크 시 T0(holdings 요청 직전)·T1(응답 수신 직후)을 기록하고 **`snapshot_boundary_at = T1`을 링크 행에 영구 저장**. 증분 반영은 언제나 `filledAt > snapshot_boundary_at`인 주문으로 한정 — opening 스냅샷과의 이중 계상을 막는 유일한 시각 경계다. (T0~T1 사이 체결은 모호 구간: holdings에 반영됐는데 T1 이후로 오인될 수는 없으나, holdings에 미반영됐는데 경계 이전으로 제외될 수 있음 — 이 잔여 리스크는 대사가 감지하며 §7에 명시) | 스냅샷-주문 커서 간 원자적 cutover 부재 해소 (Codex 스펙 결함 2). 오버랩 재조회로는 opening UID ≠ order UID라 수렴 불가하므로 시각 경계가 필수 |
| 증분 동기화 커서 (재설계 2026-07-17) | `last_synced_at`은 **조회 범위 최적화용 커서**이며 중복 방지 수단이 아니다. 매 sync는 `from = max(snapshot_boundary_at, last_synced_at − 3일).date()`부터 재조회(오버랩 재스캔)하고, 중복 수렴은 **`trade_uid` 유니크 제약이 1차 방어** (`PortfolioConflictError` → skip 집계). 커서 전진 규칙: (a) 실패 주문(오버셀·필수 필드 결손)이 있으면 **가장 이른 실패 주문 filledAt 직전(−1초)까지만** 전진 — 실패분은 다음 sync 조회 범위에 남아 재시도된다, (b) 실패가 없으면 sync 시작 시각으로 전진, (c) DB 갱신은 **monotonic**(현재 저장값보다 클 때만) | 실패 주문 영구 유실 차단(Codex blocker), API 지연 노출·동일시각 체결 유실 차단(스펙 결함 1), 동시 sync 커서 역행 차단(Codex major 4) |
| 반영 대상 주문 | `execution.filledQuantity > 0`인 CLOSED 주문 전부 (FILLED뿐 아니라 부분 체결 후 CANCELED/REPLACED 포함) | 부분 체결도 실제 보유 변화. `trade_uid="toss:{orderId}"`로 중복 방지 |
| 거래 필드 매핑 | quantity=filledQuantity, **price=averageFilledPrice(필수 — filledQuantity>0인데 결손/비정상이면 그 주문은 '실패 주문'으로 분류해 응답에 표시하고 커서를 그 이전에 묶는다; order.price 대체 금지)**, fee=commission(없으면 0), tax=tax(없으면 0), trade_date=filledAt(ISO 8601 +09:00 — KST naive로 정규화 후 날짜 추출), currency=order.currency, market=심볼로 판별(kr/us) | 시장가·부분체결에서 주문가≠체결평균가 — 원가 왜곡 방지 (Codex major 3) |
| 연동 메타 저장 | 신규 테이블 `PortfolioBrokerLink`: id, account_id(FK, 유니크), provider(='toss'), external_account_seq, external_account_no, linked_at, **snapshot_boundary_at**, last_synced_at, last_reconciled_at, **active(bool)**, created_at/updated_at | 기존 테이블 무변경(additive). active로 unlink=비활성화 표현 |
| 링크 원자성 | 계좌 생성 + opening trades 전체 + 링크 행 생성은 **단일 DB 트랜잭션** — 중간 실패 시 전부 롤백, 고아 계좌·부분 원장 금지 | Codex major 5 |
| unlink/재링크 | `DELETE`는 링크 행을 삭제하지 않고 `active=false`로 비활성화(커서·경계 보존). 같은 `external_account_seq`로 재링크하면 비활성 링크를 **재활성화**하고 보존된 커서에서 sync 재개 — opening을 다시 만들지 않으므로 미연동 기간의 매도/매수가 오버랩 재조회로 복구된다. 완전히 새로 시작하려면 포트폴리오 계좌 자체를 삭제(기존 DELETE /accounts)한 뒤 새 링크를 만든다. **API body의 `account_id` 재사용 파라미터는 제공하지 않는다(계약: name?, account_seq?만)** | 미연동 기간 복구 의미 부여(Codex 스펙 결함 3), KR/KRW 계약 우회 차단(Codex major 2) |
| 대사 산출 | sync 응답에 포함: 심볼별 {ledger_qty, broker_qty, diff} 중 |diff| > 1e-9인 것 + 원장에만/브로커에만 있는 심볼. 감지 시 WARNING 로그. 자동 정정 없음 | CONTEXT.md 대사 정의 그대로. US 소수점 거래 대비 epsilon 비교 |
| API 표면 | `POST /portfolio/links/toss` (body: name?, account_seq? — 미지정 시 계좌 1개면 자동, 복수면 400으로 목록 반환), `POST /portfolio/links/{account_id}/sync`, `GET /portfolio/links`, `DELETE /portfolio/links/{account_id}` (링크만 해제, 계좌·원장 보존) | 기존 portfolio 엔드포인트 관례(auth 포함) 준수. CLI/봇/스케줄은 비범위 |
| TossFetcher 확장 | `get_accounts()`, `get_holdings(account_seq)`, `get_closed_orders(account_seq, from_date, cursor)` — `X-Tossinvest-Account` 헤더 지원을 `_request`에 추가(opt 파라미터). 토큰·429·403 로직 재사용. **orders 응답은 실확인 envelope `{"result": {"orders": [...], "nextCursor": str\|null, "hasNext": bool}}`를 엄격 검증** — 필수 키 결손, `hasNext=true`인데 `nextCursor` 없음, 페이지 상한 도달은 부분 결과 반환이 아니라 `DataFetchError`로 실패시켜 커서를 보존한다. 대체 키 추측 분기 금지 | 스키마 오류의 빈-동기화 위장 차단 (Codex major 1) |
| 동시성 | 커서 DB 갱신은 monotonic(위 커서 규칙 (c)) + 계좌 단위 in-process 락. trade_uid 유니크가 거래 중복의 최종 방어 | 커서 last-writer 역행 차단 (Codex major 4) |

## 4. 데이터 흐름

```
[링크]  POST /portfolio/links/toss
  → TossFetcher.get_accounts() (1 TPS 주의)
  → 계좌 선택/검증 → create_account(market='kr', base_currency='KRW', broker='toss')
  → snapshot_ts = now(KST) → get_holdings(accountSeq)
  → 보유 항목당 opening trade 기록 (trade_uid="toss:opening:...")
  → PortfolioBrokerLink 생성 (last_synced_at=snapshot_ts)
  → 응답: 계좌 id, 기초 포지션 수, 스냅샷 시각

[동기화]  POST /portfolio/links/{account_id}/sync
  → sync_start = now(KST)
  → get_closed_orders(accountSeq,
        from=max(snapshot_boundary_at, last_synced_at−3일).date(), 페이지네이션)
  → filledAt > snapshot_boundary_at && filledQuantity > 0 필터
  → 주문별: averageFilledPrice 검증 → record_trade(trade_uid="toss:{orderId}")
      · PortfolioConflictError → skipped_duplicates (오버랩 재조회의 정상 경로)
      · PortfolioOversellError/필드 결손 → failed[] 집계 (커서를 묶는다)
  → last_synced_at = failed 있으면 min(failed.filledAt)−1s, 없으면 sync_start
      — monotonic 갱신(저장값보다 클 때만)
  → [대사] get_holdings() vs 리플레이 포지션 → 드리프트 목록
  → last_reconciled_at 갱신
  → 응답: {imported, skipped_duplicates, failed[], drift[]}
```

## 5. 엣지 케이스 계약

- **자격증명 미설정**: 링크/동기화 엔드포인트는 409가 아닌 명확한
  400/`toss-not-configured` 류 에러 페이로드. 목록/해제는 동작.
- **403 (IP 미등록)**: Phase 1 가드가 발동해 DataFetchError → 엔드포인트는
  502류로 그 사유를 그대로 전달. 조용한 성공(0건 동기화)으로 위장 금지.
- **재링크 시도**: 활성 링크가 있는 계좌면 409. unlink(비활성화) 후 같은
  `external_account_seq`로 재링크하면 비활성 링크를 재활성화하고 보존된
  커서에서 재개 — opening 재생성 없음, 미연동 기간 체결은 오버랩
  재조회로 복구 (§3 unlink/재링크 결정).
- **오버셀·필수 필드 결손 주문**: 해당 주문을 `failed[]`로 응답에 표시하고
  sync는 계속하되, **커서는 가장 이른 실패 주문 이전에 묶여** 다음 sync에서
  재시도된다. 실패가 지속되면 커서가 전진을 멈추고 매 sync가 같은 실패를
  재보고한다 — 조용한 유실 대신 가시적 정체를 택한다.
- **매도 후 잔고 0 종목**: holdings에 없고 원장 리플레이도 0이면 드리프트
  아님 (양쪽 0 비교).
- **KR 심볼 매핑**: Toss 6자리 ↔ 저장소 `.KS`/`.KQ` 접미사. holdings/orders
  응답은 접미사 정보가 없으므로 기존 종목 인덱스(`stock_index_loader`)로
  KS/KQ를 해석하고, 미해석 시 `.KS` 기본 + note 표기(드리프트 대사에서
  잡히도록). US는 티커 그대로.

## 6. 검증 계획

- 오프라인(unittest + 임시 파일 sqlite 관례): 링크 생성(계좌·opening
  trade·링크 행 — 단일 트랜잭션 롤백 포함), 스냅샷 경계 필터, 오버랩
  재조회 + trade_uid 중복 skip 수렴, **실패 주문의 커서 held-back과 재시도**,
  monotonic 커서 갱신, 부분 체결 반영, 대사 드리프트 산출, unlink 비활성화
  →재링크 커서 승계, 미설정 4xx, KR/US 심볼·통화 매핑. **fixture는 실확인
  envelope `{"result":{"orders":[...],"nextCursor":...,"hasNext":...}}` 형태의
  다중 페이지 + 미국 소수점 체결 실사례(filledQuantity="0.002686") +
  `hasNext=true`인데 nextCursor 없음 오류 케이스를 포함**한다. API 레벨은
  `_ThreadlessTestClient` 관례.
- 온라인(-m network, 자격증명+허용 IP 필요 skip 사유 명시): accounts/
  holdings 실조회 스모크 (read-only GET만 — 주문 API 호출 없음).
- 문서: `docs/CHANGELOG.md`, `docs/full-guide.md`·`docs/full-guide_EN.md`의
  포트폴리오 API 표에 신규 4 엔드포인트 반영.

## 7. 리스크와 롤백

- **리스크**: 평단 기반 opening trade는 실제 취득원가의 근사(수수료 미포함,
  평단 반올림). 손익 절대액이 브로커 앱과 소액 차이 날 수 있음 — 링크
  응답과 문서에 명시.
- **리스크**: orders 전체 기간 조회가 계정 역사에 따라 페이지 수 증가 —
  from=링크일 이후만 조회하므로 실질 부담 낮음.
- **리스크**: 링크 시 T0~T1 모호 구간(holdings 요청~응답 사이 체결)은
  holdings 미반영이면 경계 이전으로 제외돼 과소 계상될 수 있음 — 장중
  활발한 매매 중 링크하면 발생 가능, 대사가 드리프트로 감지한다. 링크는
  장 마감 후 수행을 권장(문서화).
- **롤백**: 신규 테이블·엔드포인트·fetcher 메서드 전부 additive. 링크
  해제(DELETE)로 기능 중단, 자격증명 제거로 전면 비활성. 기존 포트폴리오
  경로 무변경.

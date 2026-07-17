# Toss 포트폴리오 동기화 Phase 2 — 구현 계획

- 작성일: 2026-07-17
- 설계 스펙: `../specs/2026-07-17-toss-portfolio-sync-design.md` (이 문서가 계약의 진원)
- 상태: 계획 확정 (구현 착수 대기)

## 작업 분해

### 1. TossFetcher 계좌 컨텍스트 확장 (`data_provider/toss_fetcher.py`)

- `_request`에 선택 파라미터로 `account_seq`(→ `X-Tossinvest-Account` 헤더) 추가.
- `get_accounts()` → `GET /api/v1/accounts` (ACCOUNT 그룹 1 TPS).
- `get_holdings(account_seq)` → `GET /api/v1/holdings` (ASSET 5 TPS).
- `get_closed_orders(account_seq, from_date=None, to_date=None)` →
  `GET /api/v1/orders?status=CLOSED` cursor 페이지네이션 전량 순회
  (limit 100, ORDER_HISTORY 5 TPS — 페이지 간 0.25s sleep).
- 전부 Phase 1의 토큰·429·403 계약 재사용. 숫자 필드는 문자열임에 유의.

### 2. 스토리지 (`src/storage.py`)

- 신규 테이블 `PortfolioBrokerLink` (스펙 §3 컬럼). `account_id` 유니크
  FK. 기존 테이블 무변경. 테이블 자동 생성 경로는 기존 관례를 따른다.

### 3. 동기화 서비스 (`src/services/portfolio_broker_sync_service.py` 신규)

- `link_toss_account(name=None, account_seq=None)` — 스펙 §4 [링크] 흐름.
- `sync_linked_account(account_id)` — 스펙 §4 [동기화] 흐름 + 대사.
- `list_links()` / `unlink(account_id)`.
- 심볼 매핑: `stock_index_loader` 재사용(6자리 → .KS/.KQ, 미해석 시 .KS
  기본), US 티커 그대로. 매핑 로직은 서비스에 두고 fetcher는 raw 반환.
- 오버셀·중복 충돌은 잡아서 집계(스펙 §5), 전체 실패로 승격하지 않는다.

### 4. API (`api/v1/endpoints/portfolio.py`)

- `POST /portfolio/links/toss`, `POST /portfolio/links/{account_id}/sync`,
  `GET /portfolio/links`, `DELETE /portfolio/links/{account_id}`.
- 기존 포트폴리오 엔드포인트의 인증·에러 페이로드 관례를 그대로 따른다.
- 미설정/403 에러 계약은 스펙 §5.

### 5. 테스트

- `tests/test_portfolio_broker_sync.py` (unittest + 임시 파일 sqlite 관례,
  TossFetcher는 mock): 스펙 §6 오프라인 목록 전부.
- API 테스트는 `test_portfolio_api.py` 관례(`_ThreadlessTestClient`)로
  링크→동기화→목록→해제 왕복.
- `-m network` read-only 스모크 1건 (accounts+holdings 조회만).

### 6. 문서

- `docs/CHANGELOG.md` `[Unreleased]` 扁平 1줄.
- 포트폴리오 관련 문서(존재 시)와 `.env.example` 주석에 연동 계좌 한 줄
  언급 — 신규 환경변수는 없다 (Phase 1 자격증명 재사용).

## 검증 순서

1. `uv run pytest -m "not network"` (신규 테스트 포함 전체)
2. `uv run ./scripts/ci_gate.sh`
3. (로컬) read-only 실측: accounts/holdings 조회, 링크→동기화 왕복 후
   드리프트 0 확인 — 실제 매매 주문은 절대 발생시키지 않는다
4. Codex 독립 코드 리뷰 (원장 정확성·경계 시각·idempotency 중심)

## 리스크 / 롤백

- 전부 additive (신규 테이블·엔드포인트·메서드). 롤백은 링크 해제 또는
  자격증명 제거. 기존 포트폴리오 경로 무변경이 회귀 기준.

# Toss OpenAPI Phase 1 — KR/US 시세 소스 + 종목 마스터 보강 구현 계획

- 작성일: 2026-07-17
- 설계 스펙: `../specs/2026-07-17-toss-openapi-design.md`
- ADR: `docs/adr/0003-toss-openapi-credential-gated-source.md`
- 상태: 계획 확정 (구현 착수 대기)

## 작업 분해

### 1. 설정 배선

- `src/config.py`: `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` 필드 +
  `from_env` 로딩 (Longbridge 키 패턴 참조).
- `.env.example` / `.env.example.ko`: 두 키 추가, 허용 IP 등록 필요
  주석 명시.

### 2. TossFetcher 본체 (`data_provider/toss_fetcher.py`)

- OAuth2 토큰 매니저: 발급·메모리 캐싱·만료 전 갱신, `expired-token`
  401 시 1회 재발급 후 재시도.
- 심볼 변환: `005930.KS`/`.KQ` ↔ `005930`, US 티커는 그대로.
- `get_realtime_quote`: `GET /api/v1/prices` → `UnifiedRealtimeQuote`
  (market=`kr`/`us`, currency=KRW/USD).
- `get_daily_data`: `GET /api/v1/candles?interval=1d&adjusted=true`,
  200봉 초과 요청 시 `before` 페이지네이션 (최대 2페이지면 충분).
- 429 처리: `Retry-After` 대기 + 지수 백오프(jitter), 짧은 재시도 상한
  후 예외 → 매니저 fallback에 위임.
- **403 가시화 (요구사항)**: 허용 IP 미등록으로 403(`edge-blocked`/
  `forbidden`) 수신 시 조용히 강등하지 않고, 프로세스당 1회 WARNING
  로그로 뚜렷하게 표시 — 현재 공인 IP + "토스증권 WTS > 설정 > Open API >
  허용 IP 관리에서 등록" 안내 포함. 이후 호출은 해당 런에서 스킵해
  로그 스팸과 불필요한 백오프를 방지.

### 3. 매니저 등록·라우팅 (`data_provider/base.py`, `__init__.py`)

- `data_provider/__init__.py`: export + 우선순위 주석 갱신.
- `DataFetcherManager.__init__`: 자격증명 존재 시에만 append.
- `_DAILY_MARKET_FETCHER_SUPPORT`: `TossFetcher: {"kr", "us"}`.
- 실시간 KR 분기: Toss 시도 → 실패 시 yfinance (JP/TW는 불변).
- 일봉 KR: 별도 라우팅 분기를 두지 않는다 — `YfinanceFetcher`(Priority 4)가
  `TossFetcher`(Priority 6)보다 먼저 오는 매니저의 일반 우선순위 정렬 루프에
  그대로 맡긴다(yfinance 1순위, Toss는 fallback). 최초 설계는 "Toss 일봉
  1순위"였으나 실측 검증(§ 검증 순서 4)에서 Toss 일봉이 KRX 공식 종가가
  아님이 확인되어 재결정됨.
- US 일봉 `source_order` 두 변형 모두 말미에 `TossFetcher` 추가.

### 4. 종목 마스터 보강 (`scripts/fetch_kr_stock_list.py`)

- FDR 목록 취득 후 자격증명 존재 시: 200개 배치 × ~13회
  `GET /api/v1/stocks` (5 TPS 준수) → 상장상태/거래정지/한글명/
  발행주식수 검증·보강. 미설정/실패 시 기존 FDR 결과 그대로 (fail-open).

### 5. 테스트

- `tests/`: 토큰 발급·갱신·401 재발급, 429 백오프, 심볼 변환, 현재가/
  캔들 파싱, 페이지네이션, KR 실시간 라우팅(Toss→yfinance 강등), KR 일봉
  라우팅(yfinance→Toss 강등), US 최후순위 — 전부 mock 기반 오프라인.
- `-m network` 스모크: 자격증명+허용 IP 환경에서만 (skip 사유 명시).
- 회귀: 자격증명 미설정 시 fetcher 미등록·기존 KR 경로 불변 검증.

### 6. 문서

- `docs/market-support.md`: KR/US 소스 표에 Toss(opt-in) 반영.
- `docs/CHANGELOG.md` `[Unreleased]`: `- [新功能] ...` 1줄 (扁平 형식).

## 검증 순서

1. `uv run ./scripts/ci_gate.sh`
2. `uv run pytest -m "not network"`
3. (로컬, 자격증명 보유 시) `uv run pytest -m network -k toss` +
   `--stocks 005930.KS,AAPL --dry-run` 실측
4. 주요 KR 종목 표본으로 Toss vs yfinance 일봉 대조 (수정주가 정합) —
   **실측 결과(2026-07-17, 005930)**: Toss 일봉 종가가 KRX 공식 종가가
   아니라 KRX+NXT(대체거래소) 통합 최종 체결가임을 확인(07-15 공식
   279,500 vs Toss 273,500; 07-14 공식 263,000 vs Toss 268,000, 양방향
   최대 3.8% 괴리). 캔들 API에 세션 분리 파라미터 없음. 이 결과로 KR
   일봉 우선순위를 yfinance 1순위 + Toss fallback으로 재결정(설계 스펙
   §2/§3 갱신).

## 리스크 / 롤백

- 미설정 경로는 조건부 등록이라 diff 영향 없음 — 롤백은 환경변수 제거.
- KR 실시간 분기 수정이 유일한 기존 경로 변경점 → 회귀 테스트 필수.

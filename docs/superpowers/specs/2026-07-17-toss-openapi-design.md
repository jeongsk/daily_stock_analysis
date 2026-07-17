# 토스증권 OpenAPI 도입 — 설계 스펙

- 작성일: 2026-07-17
- 상태: 설계 확정 (Phase 1 구현 계획: `../plans/2026-07-17-toss-openapi-phase1.md`)
- 관련 영역: KR/US 시세 소스, KR 종목 마스터, 포트폴리오 동기화(Phase 2)
- 선행 컨텍스트: ADR 0001(무인증 소스), ADR 0003(자격증명 게이트), `2026-07-12-kr-full-stock-list-design.md`
- API 문서: https://developers.tossinvest.com/docs (LLM용: `/llms.txt`, OpenAPI JSON: `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`)

## 1. 개요와 목표

토스증권 OpenAPI 승인을 받아 정식 브로커 API를 데이터 소스로 도입한다.
현재 KR 시세는 **yfinance 단일 소스이며 실패 시 fallback이 없다**
(`data_provider/base.py` 실시간 분기 — JP/KR/TW는 yfinance만 호출).
Toss를 자격증명 게이트 opt-in 소스로 추가해 이 단일 장애점을 해소하고,
이후 포트폴리오 자동 동기화(Phase 2)로 확장한다.

### 성공 기준 (Phase 1)

- `TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET` 설정 시 KR 현재가(실시간)는 Toss에서
  공급되고, Toss 실패 시 yfinance로 강등된다. KR 일봉은 yfinance(KRX 공식
  종가)를 1순위로 유지하고, yfinance 실패 시 Toss로 강등된다(2026-07-17
  재결정 — §2 NXT 통합 시세 제약 참고).
- 미설정 시 기존 동작과 완전히 동일하다 (신규 필수 설정 없음).
- US는 기존 4중 체인(Finnhub→AlphaVantage→Yfinance→Longbridge) 뒤
  최후순위 fallback으로만 추가된다 (기존 동작 무변경).
- `scripts/fetch_kr_stock_list.py`가 자격증명 존재 시 Toss 배치 조회로
  상장상태·거래정지·한글명·발행주식수를 검증/보강한다.
- 429/네트워크 오류가 분석 주 흐름을 중단시키지 않는다 (fallback 강등).

### 명시적 비범위

- 정기 파이프라인(GitHub Actions)에서의 Toss 사용 — IP 허용제로 불가 (ADR 0003).
- KR breadth(#11)·섹터의 Toss 전환 — 정기 파이프라인이 소비 주체이므로
  기존 Naver/Daum 계획 유지. Toss 현재가 배치(200종목/호출)는 로컬 실행
  한정 대안으로만 남겨둔다.
- 매수유의사항(warnings)·환율·장 캘린더 API 소비 — 후속 검토.
- 주문/조건주문 — Phase 3으로 예약만 (§6).
- KR 종목 유니버스 발견의 Toss 전환 — 열거 API가 없어 불가 (§2 제약).

## 2. 확인된 스펙 제약 (2026-07-17 실측)

| 항목 | 사실 | 귀결 |
|---|---|---|
| 인증 | OAuth 2.0 Client Credentials, **허용 IP 등록제** (미등록 IP는 403) | GH Actions(동적 IP) 사용 불가 → 자격증명 게이트 opt-in (ADR 0003) |
| 종목 마스터 | `GET /api/v1/stocks`는 `symbols` 필수(배치 200) — **전체 열거 API 없음** | 유니버스 발견은 FDR 유지, Toss는 보강/검증만 (~2,600종목 = 13회 호출) |
| 캔들 | 200봉/호출, `before` 페이지네이션, 수정주가(`adjusted`) 지원, 1분봉/일봉 | 기술지표용 250일 히스토리 = 2페이지, 문제 없음 |
| 현재가 | 배치 200종목/호출, 10 TPS (`MARKET_DATA` 그룹) | 다종목 분석 시 호출 수 절감 |
| Rate limit | 그룹별 TPS (STOCK 5, MARKET_DATA 10, MARKET_DATA_CHART 5), 429 시 `Retry-After` 헤더 | 백오프 + fallback 강등으로 대응 |
| 시장 범위 | KRX + 미국 주식, 지수는 별도 `market-indicators` API | Phase 1은 개별 종목만; KR 지수는 yfinance 유지 |
| 계좌 | `BROKERAGE`(종합매매)만 노출, `X-Tossinvest-Account` 헤더 필요 | Phase 2 전제 조건 |
| 주문 이력 | `GET /api/v1/orders?status=CLOSED`는 기간 미지정 시 전체 기간 | Phase 2 백필 가능 |
| NXT 통합 시세 | 캔들·현재가 모두 KRX+NXT(대체거래소, ~20:00까지 거래) **통합 체결 기준**이며 세션 분리 파라미터 없음(장 캘린더 문서 명시). 실측(2026-07-17, 005930): 07-15 KRX 공식 종가 279,500 vs Toss 273,500, 07-14 공식 263,000 vs Toss 268,000(양방향 괴리, 최대 3.8%) | KR 일봉은 yfinance(KRX 공식 종가) 1순위 유지, Toss는 일봉 fallback으로만 사용; KR 실시간은 NXT 통합 최신 체결가가 오히려 의도된 동작(Toss 앱 표기와 일치)이므로 Toss 1순위 유지 |

## 3. 확정된 설계 결정

| 결정 | 값 | 근거 |
|---|---|---|
| 활성화 방식 | `TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET` 설정 시에만 fetcher 등록 | Tushare/Longbridge 선례; "미설정시에도 동작" 가드레일; IP 제약 (ADR 0003) |
| KR 우선순위 (2026-07-17 재결정) | **실시간: Toss 1순위 + yfinance fallback** (NXT 통합 최신 체결가가 Toss 앱 표기와 일치하는 의도된 동작) / **일봉: yfinance(KRX 공식 종가) 1순위 + Toss fallback** (Toss 일봉 캔들은 KRX+NXT 통합 체결가라 KRX 공식 종가와 최대 3.8% 괴리 실측, §2) | 최초 설계 시 "Toss 일봉 1순위"로 확정했으나, 실측 검증에서 Toss 일봉이 공식 종가가 아님이 드러나 기술지표 정합성을 위해 일봉만 yfinance로 재확정; 단일 소스 취약점 해소는 실시간 경로로 유지 |
| US 우선순위 | 기존 체인 뒤 **최후순위 fallback** | US는 이미 4중 체인으로 안정적; 기존 동작 무변경 우선 |
| Phase 1 범위 | 현재가 + 일봉 + 종목 마스터 보강 (KR·US) | 기존 fetcher 인터페이스와 정합; 마스터는 오프라인 스크립트 영역이라 리스크 분리 |
| 마스터 전략 | FDR 유니버스 + Toss 보강 | 열거 API 부재 제약의 직접 귀결 |
| Phase 2 | 포트폴리오 하이브리드 동기화 | §5; 사용자 트리거형이라 IP 제약과 무관 |
| Phase 3 | 주문/조건주문 — 예약만 | 실행 능력이 리포지토리에 전무; 안전장치 설계가 선행돼야 하는 별도 프로젝트 규모 |

## 4. Phase 1 아키텍처

### TossFetcher (`data_provider/toss_fetcher.py`)

- Base: `https://openapi.tossinvest.com`
- 토큰: `POST /oauth2/token` (client_credentials) — 메모리 캐싱, 만료 전
  갱신, 401 `expired-token` 시 1회 재발급 후 재시도.
- capability: `get_realtime_quote`(→ `UnifiedRealtimeQuote`),
  `get_daily_data`(일봉, `adjusted=true`).
- 심볼 정규화: 저장소 표기(`005930.KS`/`.KQ`, US 티커) ↔ Toss 표기
  (KR 6자리 나코드, US 티커) 상호 변환. 변환은 fetcher 내부에 갇힌다.
- 429: `Retry-After` 대기 + 지수 백오프(jitter), 재시도 소진 시 예외 →
  매니저의 fallback 강등에 맡긴다 (fail-fast 아님).
- 등록 3개소: `data_provider/__init__.py` export,
  `DataFetcherManager.__init__` 조건부 append,
  `_DAILY_MARKET_FETCHER_SUPPORT`에 `{"kr", "us"}`.
- 라우팅 수정: KR/JP/TW 실시간 분기에서 KR만 Toss 우선 시도 후 yfinance
  강등으로 변경. US 일봉 `source_order` 말미에 `TossFetcher` 추가.

### 종목 마스터 보강 (`scripts/fetch_kr_stock_list.py`)

- FDR 전체 목록 취득(기존) 후, 자격증명 존재 시 200개씩 배치로
  `GET /api/v1/stocks` 조회 (STOCK 그룹 5 TPS 준수).
- 보강 필드: 한글명 검증, `status`(상폐 감지), `krxTradingSuspended`,
  `sharesOutstanding`. 실패/미설정 시 FDR 결과 그대로 (fail-open).

### 설정

- `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` — `src/config.py` + `.env.example`
  (+ `.env.example.ko`) 동기 갱신. 그 외 신규 스위치는 추가하지 않는다.

## 5. Phase 2 — 포트폴리오 하이브리드 동기화 (개요)

세부 설계는 Phase 1 완료 후 별도 스펙으로 작성한다. 확정된 의미론만 기록:

- **연동 계좌**(CONTEXT.md 용어) 도입: 최초 연동 시 `GET /api/v1/holdings`
  스냅샷으로 기초 포지션 생성 → 이후 체결주문(`orders?status=CLOSED`,
  FILLED)을 증분 동기화해 기존 거래 원장(`record_trade`)에 기록.
- 주기적 **대사**(CONTEXT.md 용어): 원장 포지션 vs holdings 스냅샷 비교로
  드리프트(이관·배당·미기록 거래) 감지·보고. 자동 정정은 하지 않는다.
- `X-Tossinvest-Account`(accountSeq) 필요 — `GET /api/v1/accounts`로 발견.
- 사용자 트리거(수동 동기화) 우선; 로컬 스케줄은 후속 검토.

## 6. Phase 3 — 예약 (설계 없음)

주문/조건주문(SINGLE·OCO·OTO) 연동은 수동 승인 기반 반자동 주문을
방향성으로만 기록한다. 착수 전 승인 흐름·한도·감사로그·dry-run·멱등성
(`clientOrderId`) 설계가 선행돼야 하며 별도 세션에서 결정한다.

## 7. 검증 계획

- 오프라인: 토큰 발급/갱신·429 백오프·심볼 변환·응답 파싱 단위테스트
  (mock 응답), `./scripts/ci_gate.sh`.
- 온라인: `pytest -m network` 스모크 + `data-source-smoke` skill —
  자격증명·허용 IP가 있는 로컬 환경에서만 실행 가능함을 테스트 skip
  사유에 명시.
- 문서: `docs/market-support.md` KR 소스 표, `.env.example`,
  `docs/CHANGELOG.md` 갱신.

## 8. 리스크와 롤백

- **리스크**: Toss 응답 필드 의미(수정주가 기준, 거래정지 종목의 캔들
  동작 등)가 yfinance와 미묘하게 다를 수 있음 → Phase 1 검증에서 주요
  종목 표본으로 두 소스 일봉 대조.
- **리스크**: 자격증명 설정 환경에서 Toss 장애 시 지연 증가(백오프 후
  강등) → 재시도 상한을 짧게 유지.
- **롤백**: 환경변수 제거만으로 완전 비활성화(코드 롤백 불필요).
  fetcher 등록이 조건부라 미설정 경로는 diff 영향 없음.

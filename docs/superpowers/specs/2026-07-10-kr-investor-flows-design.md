# KR 투자자별 매매동향(수급) 데이터 — 설계 스펙

- 작성일: 2026-07-10
- 상태: 사용자 승인된 설계 (구현 전) — 그릴링 세션(2026-07-10) 결정 반영
- 관련 선례: `data_provider/tw_institutional_fetcher.py` (업스트림 #1777, TW 三大法人)
- 관련 문서: `CONTEXT.md`(도메인 용어),
  `docs/adr/0001-kr-investor-flows-no-auth-sources.md`,
  `docs/adr/0002-investor-flows-global-quality-block.md`

## 1. 개요와 목표

한국 주식(`.KS`/`.KQ`)의 투자자별 매매동향(외국인/기관/개인 순매수)을 수집하는
`KrInstitutionalFetcher`를 신설하고, 이를 (1) 개별 종목 분석 리포트와
(2) KR 마켓 리뷰에 연결한다.

### 성공 기준

- KR 종목 분석 시 LLM 컨텍스트에 최근 수급 데이터가 포함되고, 리포트에 수급
  요약이 zh/en/ko 3언어로 렌더링된다.
- KR 마켓 리뷰(`MARKET_REVIEW_REGION=kr`)에 KOSPI/KOSDAQ 시장 전체 투자자별
  수급 요약이 포함된다.
- 데이터 소스가 전부 실패해도 분석/리뷰 파이프라인은 죽지 않고 수급 섹션만
  생략된다 (fail-open).
- KR 종목의 데이터 품질 점수에 수급 블록이 반영되고, 비KR 시장의 품질 점수는
  변하지 않는다 (NOT_SUPPORTED 제외 정규화 — 동작 중립).
- 신규 필수 설정 없음 — 설정 없이 바로 동작한다.

### 단계 분리 (각각 독립 PR)

| Phase | 내용 | 소비자 |
| --- | --- | --- |
| 1 | fetcher + 데이터 계약 (`data_provider/`만) | 없음 (TW 선례와 동일한 additive 데이터 계층) |
| 2 | 개별 종목 리포트 연결 | 컨텍스트 팩 → 프롬프트 → 리포트/알림 |
| 3 | KR 마켓 리뷰 연결 | 마켓 리뷰 프롬프트/템플릿 |

### 명시적 비범위

- 투자 판단 신호에는 **연결하지 않는다** — `capital_flow_signal` 결정 신호,
  signal_attribution 가중치, 매수/매도 스코어의 입력으로 쓰지 않는다. 수급은
  LLM 참고 정보 + 표시용이다.
- 단, **데이터 품질 점수에는 완결성 지표로 반영한다**(§3의 3단계). 품질
  점수는 데이터 수집 상태 지표이지 투자 판단 신호가 아니므로 위 비범위와
  모순되지 않는다.
- KIS 등 API 키 기반 소스, pykrx 등 신규 라이브러리 의존성은 도입하지 않는다.
- 외국인 보유율 등 순매수 이외의 수급 지표는 이번 범위에서 제외한다.

## 2. Phase 1 — 데이터 계층

### 새 파일

`data_provider/kr_institutional_fetcher.py` — TW fetcher 구조(캐시·스로틀·
서킷브레이커·fail-open)를 미러링한다.

### 공개 인터페이스

```python
class KrInstitutionalFetcher:
    def get_investor_flows(self, stock_code: str, days: int = 10) -> Optional[dict]:
        """`.KS`/`.KQ` 종목만. 최근 N거래일의 일별 외국인/기관/개인 순매수.
        비대상 종목/실패 시 None (fail-open)."""

    def get_market_investor_flows(self, market: str, days: int = 5) -> Optional[dict]:
        """market: "kospi" | "kosdaq". 시장 전체 투자자별 일별 순매수.
        실패 시 None (fail-open)."""
```

- 종목 suffix 판별은 저장소에 이미 집중화된 JP/KR/TW suffix 규칙을 재사용한다.

### 정규화 레코드 계약 (두 소스 공통)

```python
{
  "code": "005930",            # 시장 레코드에는 없음
  "market": "kospi",           # "kospi" | "kosdaq"
  "unit": "KRW",               # 금액 단위는 KRW 원으로 정규화
  "days": [                     # 최신 확정 거래일부터 내림차순 정렬
    {"date": "2026-07-09",
     "foreign_net": -123400000000,
     "institution_net": 45600000000,
     "individual_net": 77800000000},
    ...
  ],
  "summary": {                  # 요청한 days 범위 내에서 계산 가능한 것만 포함
    "foreign_net_5d": ..., "institution_net_5d": ...,
    "foreign_net_10d": ..., "institution_net_10d": ...,   # days=10 기준
  },
  "source": "KRX",             # "KRX" | "NAVER"
}
```

- 당일 데이터는 장 마감 후 확정되므로 "가장 최근 확정 거래일 기준 lookback"이
  계약이다. 소스 간 날짜 불일치 시 최신 확정일 기준으로 절단한다.
- 투자주체는 **외국인/기관계/개인 3분류**로 고정한다. 기타법인은 집계에서
  제외하므로 3주체 순매수의 합은 0이 아니다. 연기금·투신 등 세부 기관 분류는
  주체로 승격하지 않는다 (`CONTEXT.md` 용어 정의 참조).
- 단위는 KRW 금액 — TW fetcher의 주수(shares) 기준과 **의도적으로 다르다**.
  한국 시장의 수급 관례가 금액 기준이기 때문이다.
- 필수 구성요소(`foreign_net`, `institution_net`)가 결측인 날짜 행은
  **폐기**한다 — 0으로 조작하지 않는다 (TW 패턴 동일).
- `individual_net`은 nullable — 네이버 fallback은 외국인·기관만 제공할 수
  있다. 이 경우 행을 유지하고 null로 두며, 소비자(프롬프트/렌더링)는
  존재하는 필드만 표시한다.

### 소스 체인 (KRX 기본 + 네이버 fallback)

1. **KRX 정보데이터시스템** (기본) — data.krx.co.kr JSON POST 엔드포인트
   (개별종목/시장별 투자자별 거래실적 통계). 무인증이지만 Referer/User-Agent
   헤더가 필요하다. 금액(원)+거래량 제공.
2. **네이버금융** (fallback) — 종목별 외국인·기관 일별 매매 페이지 파싱.
   단위(백만원 등)를 KRW로 정규화하고 `source: "NAVER"`를 명시한다.
3. 둘 다 실패 → `None`. 호출자는 섹션을 생략한다.

### 안정성 장치

- 소스별 `CircuitBreaker` 재사용 (연속 3회 실패 시 ~5분간 해당 소스 스킵).
- 요청 스로틀 (KRX ~1초 간격).
- (엔드포인트, 종목/시장, 기간) 단위 TTL 캐시 — 비어있지 않은 응답만 캐시해
  일시적 빈 응답이 TTL 동안 고착되지 않게 한다.
- 스레드 안전: 키별 in-flight 락 (TW 패턴과 동일).

### 신규 설정/의존성

없음. stdlib + 기존 HTTP 유틸만 사용한다. `.env.example` 변경 없음.

## 3. Phase 2 — 개별 종목 리포트 연결

데이터 흐름: 파이프라인에서 KR 종목이면 fetcher 호출 →
`PipelineAnalysisArtifacts`에 신규 필드 `investor_flows` →
컨텍스트 팩 신규 블록 → 프롬프트 → LLM 분석 반영 + 결정적 요약 렌더링.

1. **수집**: KR 종목 분석 시 `get_investor_flows(code, days=10)` 호출.
   실패/비대상은 `None` → 이후 단계 전부 자동 생략.
2. **컨텍스트 팩 블록**: `src/services/analysis_context_builder.py`에
   `_build_investor_flows_block()` 추가 — 상태 매핑:
   - KR 외 시장: `NOT_SUPPORTED`
   - KR인데 fetcher가 `None` 반환: `FETCH_FAILED` — 상장 KR 종목은 KRX에
     데이터가 항상 존재하므로 `None`은 사실상 수집 실패를 뜻한다.
   - KR + 네이버 fallback 데이터: `FALLBACK`
   - KR + KRX 데이터: `AVAILABLE`
   - 스키마(`src/schemas/`)에는 **optional 필드로 추가** — 기존 Web/Desktop
     클라이언트는 모르는 필드를 무시하므로 하위 호환.
3. **품질 점수 반영** (ADR 0002 참조):
   - `_QUALITY_BLOCK_WEIGHTS`에 `investor_flows` **전역 가중치 5** 추가 —
     KR 전용 슬롯이 아닌 전 시장 공통 블록.
   - 채점을 **NOT_SUPPORTED 제외 정규화**로 변경: `NOT_SUPPORTED` 블록은
     분자·분모에서 모두 제외하고, 하드코딩 분모 100 대신 참여 가중치 합으로
     나눈다. 현재 `NOT_SUPPORTED`가 되는 블록은 없으므로 기존 시장 점수는
     **동작 중립**이며 회귀 테스트로 고정한다.
   - 효과: KR은 가중치 풀 105로 채점(`AVAILABLE` 100이면 점수 개선,
     `FETCH_FAILED` 25면 수집 장애가 점수에 드러남), 비KR은 100 그대로.
   - limitation 노트: aux limitation 키 튜플(`news`/`fundamentals`/`chip`)에
     `investor_flows`를 추가한다. aux 키는 `FETCH_FAILED`/`FALLBACK`/`STALE`
     상태일 때만 limitation으로 표기된다 (`MISSING`은 미표기 — 기존 의미론
     유지).
4. **프롬프트**: `src/analysis_context_pack_prompt.py`에 수급 섹션 추가 —
   일별 나열 대신 압축 요약(최근 5일/10일 누적 + 최근 연속 순매수/순매도
   일수)을 zh/en/ko로 렌더링. "수급은 보조 신호"라는 가이드 한 줄 포함
   (과대해석 방지).
5. **리포트/알림 렌더링**: LLM 서술과 별개로 종목 섹션에 결정적 요약 한 줄 —
   예: `수급(5일 · 07-09 기준): 외국인 -1,234억 / 기관 +567억 · KRX`.
   - 기준일 = 최신 확정 거래일. 장중 실행이나 연휴 직후에 "오늘 수급"으로
     오독하는 것을 방지한다.
   - 단위는 로케일별 표기: ko 억원 / zh 亿韩元 / en `₩B`(예: `₩-123.4B`).
   - 출처는 실제 사용 소스를 표기한다 (`· KRX` / `· NAVER`).
   - 개인은 요약 라인에서 제외한다 — nullable인 데다 외국인·기관 합의
     역방향이라 정보가 중복된다. 프롬프트 컨텍스트에는 포함한다.
   - 데이터 없으면 줄 자체를 생략. 기본 알림 렌더링과 Jinja2 템플릿 모두 반영.
6. **Agent 경로 동기화**: agent/multi-agent 경로(executor, decision_agent)가
   같은 컨텍스트 블록을 받는지 확인하고 필요 시 프롬프트 동기화.
   (정확한 훅은 구현 계획에서 확정)

## 4. Phase 3 — KR 마켓 리뷰 연결

- KR 마켓 리뷰 데이터 수집 시 `get_market_investor_flows("kospi", 5)` /
  `("kosdaq", 5)` 호출.
- 리뷰 프롬프트에 시장 수급 요약 섹션(외국인/기관 5일 누적 + 당일 방향),
  리뷰 리포트 템플릿에 결정적 요약 라인 추가 — 기준일·단위·출처 표기 규칙은
  Phase 2 요약 라인과 동일. zh/en/ko 3언어 — 기존 KR 마켓 리뷰 한국어
  로컬라이즈 경로(중국어 혼입 거부 게이트 포함)와 정합 유지.
- fail-open: 수급 데이터 없으면 섹션 생략, 리뷰는 정상 진행.
- KR 마켓 프로파일/전략 계층에 붙이며 정확한 훅은 구현 계획에서 확정.

## 5. 테스트 전략

오프라인 우선 — `pytest -m "not network"` 통과 필수:

- KRX JSON / 네이버 HTML **픽스처 기반 파싱·정규화 단위 테스트**
  (단위 환산, 날짜 처리, summary 계산).
- fallback 체인(KRX 실패 → 네이버), 서킷브레이커, 캐시(빈 응답 미캐시),
  비KR 종목 `None`, fail-open 회귀 테스트.
- 컨텍스트 블록 상태 4종(NOT_SUPPORTED/FETCH_FAILED/FALLBACK/AVAILABLE)
  테스트.
- 품질 점수 정규화 회귀 테스트 — 비KR 시장 점수 불변(동작 중립), KR 점수에
  `investor_flows` 반영, limitation 노트 표기(FETCH_FAILED/FALLBACK만) 검증.
- 프롬프트·리포트 라인 zh/en/ko 렌더링 테스트.
- 실제 엔드포인트 형식 드리프트 감지용 `-m network` 스모크 테스트
  (기존 network-smoke 워크플로가 관측용으로 실행).

## 6. 문서

- `docs/market-support.md`에 KR 수급 지원 추가.
- `docs/CHANGELOG.md` `[Unreleased]`에 Phase별 플랫 항목 추가.
- 도메인 용어는 `CONTEXT.md`, 결정 기록은 `docs/adr/0001`·`0002` —
  이 스펙과 함께 리뷰/커밋한다.
- README는 변경하지 않는다. `.env.example` 변경 없음(신규 설정 없음).

## 7. 리스크와 완화

| 리스크 | 완화 |
| --- | --- |
| KRX/네이버 모두 비공식 웹 엔드포인트 → 형식 변경 가능 | 이중 소스 + fail-open(최악의 경우 섹션 생략) + network 스모크로 드리프트 조기 감지 |
| KRX가 해외 IP(GitHub Actions 러너 등)를 차단할 가능성 | 네이버 fallback으로 완화, 문서에 명시 |
| 배치 분석 시 종목당 1콜(스로틀 ~1초) → 관심종목 30개면 약 +30초 | TTL 캐시로 동일 실행 내 중복 제거, 스로틀 간격은 상수로 조정 가능 |

## 8. 롤백

Phase별 독립 PR + 전부 additive(스키마 optional 필드) → PR revert만으로 완전
롤백. 하위 호환 파손 없음.

## 9. 구현 계획에서 확정할 항목

- KRX 통계 엔드포인트의 정확한 요청 파라미터(bld 코드 등)와 응답 필드 매핑.
- 네이버금융 페이지의 실제 파싱 대상(HTML 표 vs 모바일 API)과 단위 확정
  (시장 전체 수급의 네이버 fallback 제공 가능 여부 포함 — 불가하면 시장
  수급은 KRX 단일 소스 + fail-open으로 확정).
- 파이프라인에서 fetcher를 호출하는 정확한 위치(수집 단계 훅).
- agent/multi-agent 경로의 컨텍스트 전달 방식 확인 및 필요한 프롬프트 동기화 지점.
- KR 마켓 프로파일/전략 계층의 정확한 확장 지점.

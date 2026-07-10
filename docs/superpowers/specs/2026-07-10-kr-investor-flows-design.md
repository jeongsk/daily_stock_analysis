# KR 투자자별 매매동향(수급) 데이터 — 설계 스펙

- 작성일: 2026-07-10
- 상태: 사용자 승인된 설계 (구현 전) — 그릴링 세션(2026-07-10) 결정 +
  소스 라이브 실측(2026-07-10: KRX 로그인 게이트 확인, 종목=주수/시장=KRW
  이원화 승인) 반영
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
    def get_investor_flows(self, stock_code: str, days: int = 5) -> Optional[dict]:
        """`.KS`/`.KQ` 종목만. 최근 N거래일의 일별 외국인/기관/개인 순매수(주수).
        비대상 종목/실패 시 None (fail-open)."""

    def get_market_investor_flows(self, market: str, days: int = 5) -> Optional[dict]:
        """market: "kospi" | "kosdaq". 시장 전체 투자자별 일별 순매수(KRW).
        실패 시 None (fail-open)."""
```

- 종목 suffix 판별은 저장소에 이미 집중화된 JP/KR/TW suffix 규칙을 재사용한다.

### 정규화 레코드 계약 (두 레벨 공통 구조, 단위는 `unit`으로 자기서술)

```python
{
  "code": "005930",            # 시장 레코드에는 없음
  "market": "kospi",           # "kospi" | "kosdaq"
  "unit": "shares",            # 종목 레코드 "shares"(주수) | 시장 레코드 "KRW"(원)
  "days": [                     # 최신 확정 거래일부터 내림차순 정렬
    {"date": "2026-07-10",
     "foreign_net": 625985,
     "institution_net": 2313745,
     "individual_net": -2851466},
    ...
  ],
  "summary": {                  # 실제 보유 행(최대 요청 days) 기준으로 계산
    "foreign_net_5d": ..., "institution_net_5d": ...,
  },
  "source": "NAVER",           # 종목: "NAVER" | "DAUM", 시장: "NAVER"
}
```

- 당일 데이터는 장 마감 후 확정되므로 "가장 최근 확정 거래일 기준 lookback"이
  계약이다. 소스 간 날짜 불일치 시 최신 확정일 기준으로 절단한다.
- 투자주체는 **외국인/기관계/개인 3분류**로 고정한다. 기타법인은 집계에서
  제외하므로 3주체 순매수의 합은 0이 아니다. 연기금·투신 등 세부 기관 분류는
  주체로 승격하지 않는다 (`CONTEXT.md` 용어 정의 참조).
- **단위 이원화** (2026-07-10 실측 결과 사용자 승인): 종목 수급은
  **주수(shares)** — KRX 통계가 로그인 게이트로 전환되어 무인증 공개
  소스(네이버·다음)가 종목 레벨 KRW 금액을 제공하지 않기 때문이다(§9,
  ADR 0001). 결과적으로 TW fetcher와 같은 단위가 된다. 시장 수급은 **KRW
  원**(네이버 억원 표기를 ×1e8 정규화). 소비자는 `unit` 필드로 분기하며,
  주수×종가 등 금액 추정 환산은 하지 않는다.
- lookback 기본값은 5거래일 — 종목 primary(네이버 integration)가 최근
  5거래일만 제공한다. summary는 실제 보유 행 기준 5일 누적만 계산한다.
  (그릴링 시점의 10일 lookback은 KRX 전제였으므로 실측에 맞춰 축소.)
- 필수 구성요소(`foreign_net`, `institution_net`)가 결측인 날짜 행은
  **폐기**한다 — 0으로 조작하지 않는다 (TW 패턴 동일).
- `individual_net`은 nullable — 다음(DAUM) fallback은 외국인·기관만
  제공한다(네이버 primary는 개인 포함, 시장 레코드는 항상 3주체). null 행도
  유지하며, 소비자(프롬프트/렌더링)는 존재하는 필드만 표시한다.

### 소스 체인 (2026-07-10 라이브 실측 검증 기준)

**종목 수급** (`get_investor_flows`):

1. **네이버 모바일 API** (기본) — `GET
   https://m.stock.naver.com/api/stock/{종목코드}/integration` JSON의
   `dealTrendInfos`. 3주체(개인 포함) × 최근 5거래일, 주수 단위
   (`foreignerPureBuyQuant` 등, `"+625,985"` 부호·콤마 문자열). UA 헤더 필요.
   `source: "NAVER"`.
2. **다음 금융 API** (fallback) — `GET
   https://finance.daum.net/api/investor/days?symbolCode=A{종목코드}&page=1&perPage={n}`
   JSON. 외국인·기관 주수(`foreignStraightPurchaseVolume`,
   `institutionStraightPurchaseVolume`), 개인 없음(`individual_net: null`),
   `Referer: https://finance.daum.net/quotes/A{종목코드}` 헤더 필수.
   `source: "DAUM"`.
3. 둘 다 실패 → `None`. 호출자는 섹션을 생략한다.

**시장 수급** (`get_market_investor_flows`) — 단일 소스 + fail-open:

1. **네이버 PC 페이지** — `GET
   https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate={YYYYMMDD}&sosok={01|02}`
   (01=KOSPI, 02=KOSDAQ). EUC-KR HTML 표, 억원 단위 → KRW 원으로
   정규화(×1e8). 개인/외국인/기관계(+기관 세부·기타법인) 중 3주체만 추출.
   당일 확정치가 장 마감 후 제공됨을 실측 확인. `source: "NAVER"`.
2. 실패 → `None`. (모바일 `api/index/{지수}/trend` JSON은 최신 1일만 제공해
   이력 fallback으로 부적합 — 채택하지 않음.)

**폐기된 소스**: KRX 정보데이터시스템(data.krx.co.kr) 통계는 로그인 게이트로
전환되어(2026-07-10 실측: 전 통계 bld가 `LOGOUT` 반환, 메뉴 로더가 "로그인
또는 회원가입이 필요합니다" 표시) 무인증 소스로 사용 불가 — ADR 0001 참조.

### 안정성 장치

- 소스별 `CircuitBreaker` 재사용 (연속 3회 실패 시 ~5분간 해당 소스 스킵).
- 소스별 요청 스로틀 (~1초 간격).
- (엔드포인트, 종목/시장, 기간) 단위 TTL 캐시 — 비어있지 않은 응답만 캐시해
  일시적 빈 응답이 TTL 동안 고착되지 않게 한다.
- 스레드 안전: 키별 in-flight 락 (TW 패턴과 동일).

### 신규 설정/의존성

없음(신규 의존성 0). JSON 소스는 기존 HTTP 유틸로 충분하고, 시장 HTML 표
파싱은 이미 설치된 lxml을 사용한다. `.env.example` 변경 없음.

## 3. Phase 2 — 개별 종목 리포트 연결

데이터 흐름: 파이프라인에서 KR 종목이면 fetcher 호출 →
`PipelineAnalysisArtifacts`에 신규 필드 `investor_flows` →
컨텍스트 팩 신규 블록 → 프롬프트 → LLM 분석 반영 + 결정적 요약 렌더링.

1. **수집**: KR 종목 분석 시 `get_investor_flows(code, days=10)` 호출.
   실패/비대상은 `None` → 이후 단계 전부 자동 생략.
2. **컨텍스트 팩 블록**: `src/services/analysis_context_builder.py`에
   `_build_investor_flows_block()` 추가 — 상태 매핑:
   - KR 외 시장: `NOT_SUPPORTED`
   - KR인데 fetcher가 `None` 반환: `FETCH_FAILED` — 상장 KR 종목은 포털에
     수급 데이터가 항상 존재하므로 `None`은 사실상 수집 실패를 뜻한다.
   - KR + 다음(DAUM) fallback 데이터: `FALLBACK`
   - KR + 네이버 primary 데이터: `AVAILABLE`
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
   일별 나열 대신 압축 요약(최근 5일 누적 + 최근 연속 순매수/순매도 일수)을
   zh/en/ko로 렌더링. 단위가 주수임을 명시하고, "수급은 보조 신호"라는
   가이드 한 줄 포함 (과대해석 방지).
5. **리포트/알림 렌더링**: LLM 서술과 별개로 종목 섹션에 결정적 요약 한 줄 —
   예: `수급(5일 · 07-10 기준): 외국인 +62.6만주 / 기관 +231.4만주 · NAVER`.
   - 기준일 = 최신 확정 거래일. 장중 실행이나 연휴 직후에 "오늘 수급"으로
     오독하는 것을 방지한다.
   - 종목 수급은 주수 레코드(`unit: "shares"`)를 그대로 렌더링하며 금액으로
     환산 추정하지 않는다. 로케일별 표기: ko `만주` / zh `万股` /
     en `M shares`(예: `+0.63M shares`).
   - 출처는 실제 사용 소스를 표기한다 (`· NAVER` / `· DAUM`).
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
  리뷰 리포트 템플릿에 결정적 요약 라인 추가 — 기준일·출처 표기 규칙은
  Phase 2 요약 라인과 동일하되, 시장 레코드는 KRW(`unit: "KRW"`)이므로
  단위는 ko 억원 / zh 亿韩元 / en `₩B`(예: `₩-780.5B`)로 표기. 예:
  `수급(07-10): 외국인 -3,228억 / 기관 +11,314억 · NAVER`. zh/en/ko 3언어 —
  기존 KR 마켓 리뷰 한국어 로컬라이즈 경로(중국어 혼입 거부 게이트 포함)와
  정합 유지.
- fail-open: 수급 데이터 없으면 섹션 생략, 리뷰는 정상 진행.
- KR 마켓 프로파일/전략 계층에 붙이며 정확한 훅은 구현 계획에서 확정.

## 5. 테스트 전략

오프라인 우선 — `pytest -m "not network"` 통과 필수:

- 네이버 integration JSON / 다음 JSON / 네이버 시장 HTML(EUC-KR) **픽스처
  기반 파싱·정규화 단위 테스트** (부호·콤마 문자열 파싱 `"+625,985"`,
  EUC-KR 디코딩, 억원→KRW 정규화, 날짜 처리, summary 계산).
- fallback 체인(네이버 실패 → 다음), 서킷브레이커, 캐시(빈 응답 미캐시),
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
| 네이버·다음 모두 비공식 웹 엔드포인트 → 형식 변경 가능 | 종목은 이중 소스(네이버+다음) + JSON 우선으로 파싱 취약면 축소, fail-open(최악의 경우 섹션 생략) + network 스모크로 드리프트 조기 감지 |
| 시장 수급은 네이버 단일 소스 (KRX 로그인 게이트로 무인증 대안 부재) | fail-open(섹션 생략) + network 스모크. 모바일 `trend` JSON(최신 1일)이 부분 대안 후보임을 기록해 둠 |
| 포털이 해외 IP(GitHub Actions 러너 등)를 차단/제한할 가능성 | 종목 이중 소스 + fail-open으로 완화, 문서에 명시 |
| 종목 primary(네이버 integration)가 최근 5거래일만 제공 | 계약 자체를 5일 lookback으로 축소(§2). 더 긴 lookback이 필요해지면 다음 API(pagination 지원)로 확장 |
| 배치 분석 시 종목당 1콜(스로틀 ~1초) → 관심종목 30개면 약 +30초 | TTL 캐시로 동일 실행 내 중복 제거, 스로틀 간격은 상수로 조정 가능 |

## 8. 롤백

Phase별 독립 PR + 전부 additive(스키마 optional 필드) → PR revert만으로 완전
롤백. 하위 호환 파손 없음.

## 9. 구현 계획에서 확정할 항목

2026-07-10 라이브 실측으로 확정 완료(스펙 본문 반영):

- ~~KRX bld 코드/응답 필드 매핑~~ → KRX 정보데이터시스템 통계는 로그인
  게이트로 전환되어 폐기 (전 통계 bld가 `LOGOUT` 반환, 종목 finder만 무인증
  잔존).
- ~~네이버 파싱 대상과 단위~~ → 종목: 모바일 integration JSON(주수·3주체
  개인 포함·5일) + 다음 JSON fallback(주수·외국인/기관), 시장: PC
  investorDealTrendDay HTML(억원). §2 소스 체인 참조.
- ~~시장 수급 네이버 fallback 가능 여부~~ → 시장은 네이버 단일 소스 +
  fail-open으로 확정 (모바일 trend JSON은 최신 1일만 제공해 부적합).

남은 확정 항목 (Phase 2/3 구현 계획에서):

- 파이프라인에서 fetcher를 호출하는 정확한 위치(수집 단계 훅).
- agent/multi-agent 경로의 컨텍스트 전달 방식 확인 및 필요한 프롬프트 동기화 지점.
- KR 마켓 프로파일/전략 계층의 정확한 확장 지점.

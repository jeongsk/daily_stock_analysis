# KR 시장 폭·업종 순위 — 설계 스펙

- 작성일: 2026-07-16
- 상태: 설계 확정 (Probe 완료·소스 승인, 구현 대기)
- 관련 영역: KR 마켓 리뷰 데이터 계층·프롬프트·결정적 본문·구조화 payload·Web 렌더
- 선행 컨텍스트: KR 수급(`2026-07-10-kr-investor-flows-design.md`, ADR 0001), 로드맵 B
- 근거 실측: `docs/superpowers/research/2026-07-16-kr-breadth-sector-probe.md`
- 도메인 용어: `CONTEXT.md` 「KR 시장 폭·업종」 절

## 1. 개요와 목표

KR 마켓 리뷰는 현재 지수·뉴스·투자자 수급만 제공하고, 시장 폭(상승·하락·보합 종목 수)과 업종 순위는 제공하지 않는다(`KR_PROFILE.has_market_stats=False`, `has_sector_rankings=False`). 기존 프롬프트는 KR에서 빈 시장 폭 슬롯을 수급으로 **덮어써** 두 개념이 섞여 있다.

이번 작업은 (1) **시장 폭=참여 범위**, **수급=투자주체 방향**, **업종 순위=시장 구조**로 의미를 분리하고, (2) KOSPI/KOSDAQ별 실제 폭·업종 데이터를 KR 마켓 리뷰(프롬프트·결정적 본문·구조화 payload)와 Web에 추가한다.

### 성공 기준

- KR 리뷰 payload에 optional `kr_market_context.breadth.{kospi,kosdaq}`(up/down/flat+as_of+session)와 `kr_market_context.sector_rankings.{kospi,kosdaq}`(top/bottom+as_of)가 실린다.
- 프롬프트에서 시장 폭과 수급이 **각각 독립 섹션**으로 제공되고, 교차 해석(일치/엇갈림) 가이드가 붙는다.
- Web에서 KOSPI/KOSDAQ별 폭(3셀)·업종(상·하위) 블록이 기존 구조화 UI로 렌더된다.
- 비KR 시장의 프롬프트·payload·Web 동작이 바이트/동작 동일하다.
- 모든 소스 실패는 fail-open — 해당 블록만 생략되고 리뷰는 정상 생성된다.
- Market Light·매수/매도 점수는 불변이다.

### 명시적 비범위

- CN식 개념/테마(概念板块) 순위의 KR 대응 — 제공하지 않음.
- 상·하한가 종목 수, 거래대금 — 시장 폭 계약에서 제외(소스에 존재하나 미수집).
- KOSPI+KOSDAQ 통합 KR 수치 — 정의하지 않음.
- Market Light KR 확장, 결정 신호/귀인/스코어 연결 — 리뷰 참고용으로 한정.
- JP 시장 폭/업종 — KR 전용.

## 2. 확정된 설계 결정

| # | 결정 | 값 |
|---|---|---|
| D1 | 폭·수급 관계 | 별개 신호로 분리, 교차 해석만 (기존 stats_block 덮어쓰기 제거) |
| D2 | 1차 범위 | 시장 폭 + KR 업종 순위 (테마 제외) |
| D3 | 시장 경계 | KOSPI/KOSDAQ 독립 레코드, 통합값 없음, **유효 시장만 노출** |
| D4 | 폭 지표 | up/down/flat + as_of만 (상·하한가/거래대금 제외) |
| D5 | 업종 용어·체계 | canonical "KR 업종 순위", 승인 소스 1곳의 분류 체계 고정 |
| D6 | 소스 정책 | 무인증 공개 소스만, 신규 필수 설정 없음 (ADR 0001 선례) |
| D7 | 릴리스 단위 | 폭/업종 독립 optional 블록, 검증된 기능만 출시 |
| D8 | 기준 시점 | 소스 스냅샷 as_of + `intraday\|close` session 명시 |
| D9 | 유효성 | 폭=3카운트+as_of 전부, 업종=top+bottom+as_of 전부. 불완전 블록은 통째 생략 |
| D10 | 점수 영향 | 리뷰 참고만 — Market Light/매수·매도 불변 |
| D11 | Web | 기존 공통 breadth/sectors UI 재사용, KR은 3셀 폭 변형 |
| D12 | payload 호환 | 기존 평면 계약 유지 + KR 전용 optional `kr_market_context` 추가 |
| D13 | 캐시/실패 | 짧은 timeout+TTL+key lock+circuit breaker, stale은 **동일 거래일만** |
| D14 | 단계화 | Probe(완료) → 폭 → 업종, 각각 독립 PR |

## 3. 승인된 데이터 소스 (2026-07-16 실측)

| 기능 | 소스 | 요청 | 검증 앵커 |
|---|---|---|---|
| 시장 폭 | 네이버 `finance.naver.com/sise/sise_index.naver?code={KOSPI\|KOSDAQ}` (EUC-KR HTML) | 시장별 1호출 | blind 라벨 `상승종목수/보합종목수/하락종목수` + `id="time"`의 `YYYY.MM.DD` + `장중\|장마감\|개장전` |
| 업종 순위 | 다음 `finance.daum.net/api/sectors?market={KOSPI\|KOSDAQ}&change={RISE\|FALL}&...` (JSON, **Referer 필수**) | 시장×방향 4호출 | `data[].sectorName`/`changeRate`/`date`/`market` |

- 네이버 업종 페이지(`sise_group.naver?type=upjong`)는 KOSPI/KOSDAQ 분리 불가로 **기각**(실측).
- 다음 API는 session 정보가 없으므로 session은 저장소 KR 거래 캘린더/market_phase(XKRX)로 파생한다.
- 폭·업종의 소스 호스트가 다르므로 breaker/캐시 키를 기능·시장별로 분리한다.
- 두 소스 모두 기존 수급 fetcher가 쓰는 호스트(무인증·저빈도·fail-open 선례, ADR 0001)와 동일하다.

## 4. 데이터 계약

### 폭 레코드 (시장별)

```python
{
  "market": "kospi" | "kosdaq",
  "up_count": int,      # 필수, 0 유효
  "down_count": int,    # 필수
  "flat_count": int,    # 필수
  "as_of": "YYYY-MM-DD",# 필수 (id="time" 날짜)
  "session": "intraday" | "close",
  "source": "NAVER",
  "stale": bool,        # 최신 호출 실패 시 동일 거래일 캐시 제공
}
```

- 음수/비정수 카운트, as_of 결측 → 레코드 무효(생략). 결측을 0으로 조작하지 않는다.
- `개장전`(PREOPEN) 상태는 전 거래일 확정치가 아닌 예상지수 구간이므로 레코드를 생성하지 않는다.

### 업종 순위 레코드 (시장별)

```python
{
  "market": "kospi" | "kosdaq",
  "top":    [{"name": str, "change_pct": float}, ...],  # 최대 n=5, 내림차순
  "bottom": [{"name": str, "change_pct": float}, ...],  # 최대 n=5, 하락 큰 순
  "as_of": "YYYY-MM-DD",  # 다음 API date 필드
  "session": "intraday" | "close",  # KR 캘린더 파생
  "source": "DAUM",
  "stale": bool,
}
```

- `changeRate`(비율)→`change_pct`(%) 변환. top과 bottom이 **모두** 비어 있지 않아야 유효. 이름 중복 제거.

### payload (KR 전용 optional 최상위 키)

```python
payload["kr_market_context"] = {
  "breadth":         {"kospi": rec, "kosdaq": rec},   # 유효 시장만
  "sector_rankings": {"kospi": rec, "kosdaq": rec},   # 유효 시장만
}
```

- 각 서브키·서브시장은 독립 존재/부재(D3/D7). 비KR payload에는 키 자체가 없다.
- 기존 평면 `breadth`/`sectors`/`concepts`/`markets` 계약은 불변(D12).

## 5. 컴포넌트 (파일별 책임)

### 신규

- **`data_provider/kr_market_context_fetcher.py`** — `KrMarketContextFetcher`. `KrInstitutionalFetcher`의 TTL 캐시·key lock·throttle·소스별 `CircuitBreaker`·fail-open·헤더/라벨 검증 패턴을 미러. 공개 메서드 `get_market_breadth(market)`, `get_sector_rankings(market, n=5)` — 어떤 실패에도 raise하지 않고 None. 빈/무효 결과 미캐시. stale fallback은 캐시 as_of가 현재 KR 거래일과 같을 때만(D13).

### 수정

- **`data_provider/base.py`** — `get_kr_market_investor_flows`(:2516) 패턴의 lazy singleton 진입점 `get_kr_market_breadth`/`get_kr_sector_rankings` 추가. 기존 region 없는 `get_market_stats()`/`get_sector_rankings()`(CN 공통 순회)는 재사용하지 않는다 — KR에 물리면 A주 fetcher가 오염되기 때문.
- **`src/market_analyzer.py`**
  - `MarketOverview`에 `kr_market_breadth`/`kr_sector_rankings` optional 필드.
  - `get_market_overview`의 KR 훅(investor_flows 옆)에서 KOSPI/KOSDAQ 독립 수집, 유효 레코드만 보존.
  - `build_market_review_payload`에 `kr_market_context` 직렬화(기존 평면 breadth(:978) 불변).
  - `_build_review_prompt`: KR의 stats_block **덮어쓰기(:1912-1915) 제거** → 폭(실데이터 시)·수급을 독립 섹션으로 구성, 교차 해석 1줄 가이드.
  - `_inject_data_into_review`: 결정적 폭/업종 블록 빌더 추가(수급 블록(:1108) 선례 — 시장 요약 주입+fallback append, ko 순수 한글).
- **`apps/dsa-web/src/types/analysis.ts`** — `KrMarketContext` 타입 + `MarketReviewPayload.krMarketContext?` (additive).
- **`apps/dsa-web/src/components/report/MarketReviewReportView.tsx`** — `getStructuredMarketData`에서 KR 하위 시장을 기존 `StructuredMarketData` 엔트리로 합성. KR 폭은 up/down/flat 3셀 변형(미지원 limit/turnover를 `-`로 위장하지 않음, D11). 지수는 상위 payload에서 1회만 렌더.

### 불변 (검증만)

- `src/core/market_profile.py` — `KR_PROFILE.has_market_stats/has_sector_rankings=False` 유지(회귀 테스트로 고정).
- `src/schemas/market_light.py` — `MARKET_LIGHT_REGIONS`에 KR 없음 → 점수 자동 불변(D10).
- `src/core/market_review.py` — `markets` 분기(다중 리전 결합)와 `kr_market_context`가 충돌하지 않음 확인.

## 6. 오류 처리 & 엣지 케이스

| 상황 | 처리 |
|---|---|
| 소스 HTTP 오류/timeout/파싱 drift | fail-open None → 해당 시장·기능 블록 생략, 리뷰 정상 |
| blind 라벨/JSON 키 rename | 앵커 검증 실패 → 레코드 폐기 (0 조작 금지) |
| 한 시장만 유효 | 유효 시장만 노출(D3) |
| 상승 업종 5개 미만 | 짧은 top 목록 허용(비어 있지만 않으면 유효) |
| PREOPEN(개장전) | 레코드 미생성 — 예상지수 구간 |
| 최신 호출 실패+동일 거래일 캐시 존재 | `stale=true`로 제공. 타 거래일 캐시는 폐기 |
| KR 캘린더 판정 불가 | stale 판정 보수적으로 생략(fail-closed) |
| 다음 API Referer 누락 | 403 — fetcher가 Referer 상수 포함(수급 fetcher 선례) |
| 비KR IP 차단(Actions 등) | fail-open으로 블록만 생략(수급과 동일 리스크) |

## 7. 테스트 전략 (오프라인·결정적)

- **fetcher 단위**: 파싱(정상/0값/쉼표/부호/％), 필수 결측→None, 시장·기능별 캐시/breaker 분리, 동일 거래일 stale 허용·타 거래일 거부, fail-open 전수.
- **wiring**(`test_kr_market_flows_wiring.py` 미러): KR만 신규 manager 메서드 호출, 비KR 미호출(call_count==0), 일부 시장 결측, 전체 실패→필드 None.
- **payload**: `kr_market_context` 서브키 독립성(둘 다/폭만/업종만/한 시장만/없음), 비KR payload 키 부재, 기존 평면 breadth/sectors 불변.
- **prompt/report**: 폭·수급 독립 섹션, 교차 해석 가이드, ko 결정적 블록 순수 한글(중국어 거부 게이트), KR profile 플래그 False 고정 회귀.
- **Web**: KR 3셀 폭 변형, 업종 상·하위 패널, concept 패널 부재, CN 평면 렌더 회귀.
- **network smoke**(`@pytest.mark.network`): 네이버 blind 라벨·다음 sectors 키 드리프트 감시(`test_kr_institutional_network.py` 규약 — 비200 skip, 200+형식 상이 FAIL).
- **게이트**: `./scripts/ci_gate.sh` + web `npm run lint && npm run build`.

## 8. 롤백

- 폭/업종 PR 각각 독립 revert 가능(D14).
- 긴급 비활성화: KR 훅 제거 또는 manager 메서드 None 고정 — 지수·뉴스·수급 리뷰 불변.
- 스키마/DB 마이그레이션 없음, additive JSON 필드 → 데이터 롤백 불필요.

## 9. 구현 계획에서 확정할 항목

- fetcher TTL 값(수급 900s 대비 장중 갱신 주기 고려), throttle 간격.
- session 파생 로직의 정확한 소스(네이버 `id="time"` 문자열 vs market_phase 유틸) 및 `장중` 문자열 파싱 규칙.
- 결정적 본문 블록의 zh/en/ko 문구와 주입 위치(시장 요약 vs 별도 헤딩).
- Web 3셀 변형의 구현 방식(라벨 맵 오버라이드 vs KR 전용 컴포넌트 분기).
- 프롬프트 데이터 경계 문구(en/zh `data_limits_block`)에서 폭 제공 시 문구 조정 범위.

# KR 시장 폭·업종 순위 무인증 공개 소스 실측

- **날짜**: 2026-07-16 (KST, 장마감 후)
- **맥락**: KR 로드맵 B(시장 폭·업종 순위) Phase 0 — 소스 승인 게이트
- **방법**: 후보 엔드포인트별 curl 3회(timeout 12s), 응답 원문 파싱으로 필수 필드·기준 시점·시장 분리·안정성 검증. KRX 직접 엔드포인트는 로그인 게이트(ADR 0001 실측)로 후보에서 제외.

## 1. 시장 폭 (상승·하락·보합 종목 수)

### 1.1 네이버 지수 페이지 — **승인**

`https://finance.naver.com/sise/sise_index.naver?code=KOSPI` / `?code=KOSDAQ`

| 항목 | 결과 |
|---|---|
| HTTP | 3회 모두 200, 62~92ms, `text/html;charset=EUC-KR` |
| 시장 분리 | `code=KOSPI` / `code=KOSDAQ` 파라미터로 완전 분리 |
| 필수 필드 | `상승종목수`/`보합종목수`/`하락종목수` blind 라벨 + 인접 `<span>` 정수. 실측: KOSPI 384/40/488, KOSDAQ 501/56/1182 — 3회 값 동일 |
| 부가 필드 | `상한종목수`/`하한종목수`도 존재하나 범위 결정(#4)상 미수집 |
| as_of/session | `id="time"` 요소: `2026.07.16 장마감`. 페이지 JS(`displayTime`)가 `개장전`(PREOPEN)/`장중`(HH:MM)/`장마감`(CLOSE) 3상태를 렌더 — **as_of 날짜와 세션 상태를 한 요소에서 판별 가능** |
| 파싱 앵커 | `<li class="lst2"><span class="blind">상승종목수</span><a ...><span>N</span></a>` 패턴. blind 라벨 이름 검증으로 드리프트 방어 가능(기존 `_MARKET_HEAD` 헤더 검증 패턴과 동일 접근) |

세션 상태 보조 소스(선택): `https://polling.finance.naver.com/api/realtime?query=SERVICE_INDEX:KOSPI|KOSDAQ` — JSON `ms` 필드(`CLOSE`/`PREOPEN`/장중), 3회 200, ~40-64ms. 폭 카운트는 없으므로 세션 판별 보조용으로만 후보.

### 1.2 robots/이용 제한 — 주의점

`https://finance.naver.com/robots.txt`(200):

```text
User-agent: *
Disallow: /
User-agent: yeti
Disallow: /
Allow: /sise/
...
```

일반 UA에는 전체 Disallow, `/sise/` Allow는 네이버 자체 크롤러(yeti) 한정. **단, 동일 호스트·동일 `/sise/` 경로(`investorDealTrendDay.naver`)를 기존 KR 시장 수급 fetcher가 이미 사용 중이며 ADR 0001에서 무인증 공개 소스 사용이 수용된 선례가 있다.** 본 기능도 동일 선례를 따르되 저빈도(리뷰 1회당 시장별 1호출) + 스로틀 + TTL 캐시 + fail-open을 유지한다. `polling.finance.naver.com`은 robots.txt 자체가 404.

## 2. 업종 순위

### 2.1 네이버 업종 페이지 — **기각**

`https://finance.naver.com/sise/sise_group.naver?type=upjong` — 3회 200(~60-73ms), 79개 업종의 `업종명`+`전일대비` 등락률 표는 존재하나:

- **KOSPI/KOSDAQ 시장 분리 불가**: `&market=KOSDAQ` 등 파라미터를 무시하고 동일 목록 반환(실측: 업종 no 목록 완전 일치). 결정 #3(두 시장 독립 레코드)을 충족하지 못함.
- 페이지 내 as_of 날짜 표기 부재.

### 2.2 다음 sectors API — **승인**

`https://finance.daum.net/api/sectors?market={KOSPI|KOSDAQ}&change={RISE|FALL}&page=1&perPage=10&fieldName=changeRate&order=desc&pagination=true`

| 항목 | 결과 |
|---|---|
| HTTP | 4개 조합(시장×방향) 3회씩 모두 200, 44~83ms, `application/json` |
| 헤더 요건 | **`Referer: https://finance.daum.net/domestic/sectors` 필수** — 없으면 403 `{"code":403,"message":"Forbidden"}` (기존 다음 종목 수급 fetcher와 동일 요건) |
| 시장 분리 | `market` 파라미터로 완전 분리, 응답 각 row에 `market` 필드 회신 |
| 필수 필드 | `sectorName`(업종명), `changeRate`(소수 비율, ×100=%) — 실측 KOSPI RISE 1위 통신업 +3.39%, FALL 1위 전기,전자 −9.43%; KOSDAQ RISE 1위 출판·매체복제 +3.29%, FALL 1위 기계·장비 −6.78% |
| as_of | 각 row `date` 필드(`2026-07-16`) — 네이버 폭 페이지 날짜와 일치 |
| session | API 자체에는 장중/마감 구분 없음 → 저장소의 KR 거래 캘린더/market_phase(XKRX)로 파생 필요 |
| 정렬/방향 | `change=RISE`가 상위(내림차순), `change=FALL`이 하위(가장 큰 하락부터). 상승 업종이 N개 미만이면 짧은 목록 반환(실측 KOSPI RISE 9건) — 엄격 검증(#9)과 호환 |
| 분류 체계 | KRX 표준 업종 분류(KOSPI: 통신업·음식료품·전기,전자…, KOSDAQ: 출판·매체복제·기계·장비…) — 시장별 고유 업종명, 단일 소스 체계 유지(#5) |
| robots | `finance.daum.net/robots.txt` 404 — 명시적 크롤링 제한 없음 |

## 3. 승인 결론 (Phase 1/2 진입 판정)

| 기능 | 소스 | 판정 |
|---|---|---|
| 시장 폭 | 네이버 `sise_index.naver` (KOSPI/KOSDAQ 각 1호출) | **승인** — 필수 3카운트+as_of+session 충족, 3회 안정 |
| 업종 순위 | 다음 `api/sectors` (시장×RISE/FALL 4호출) | **승인** — 시장 분리+이름/등락률+as_of 충족, session은 저장소 캘린더로 파생 |

- 두 기능 모두 승인 → Phase 1(폭), Phase 2(업종) 진행 가능.
- 폭과 업종의 **소스가 서로 다름**(네이버/다음) → fetcher 내부에서 소스·breaker·캐시 키를 기능별로 분리해야 함(설계 스펙에 반영).
- 미검증/한계: (a) 단일 시점(장마감 후) 실측 — 장중/개장전 응답은 페이지 JS 계약(PREOPEN/장중 렌더)으로만 확인, 라이브 장중 실측은 미수행. (b) 비KR IP(GitHub Actions 등)에서의 차단 여부 미실측 — 기존 수급 소스와 동일한 리스크로 fail-open이 방어. (c) 다음 sectors API의 `date`가 휴장일에 어떤 값을 반환하는지 미실측.

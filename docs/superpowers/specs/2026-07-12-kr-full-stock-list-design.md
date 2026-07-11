# KR 전체 종목 리스트 — 설계 스펙

- 작성일: 2026-07-12
- 상태: 설계 확정 (구현 계획 대기)
- 관련 영역: 종목 자동완성 인덱스, 백엔드 이름→코드 해석, 빌드타임 스크립트
- 선행 컨텍스트: JP/KR suffix-only MVP(#1815), KR 투자자별 수급(`2026-07-10-kr-investor-flows-design.md`)

## 1. 개요와 목표

현재 KR 종목 자동완성은 `scripts/stock_index_seeds/stock_list_kr.csv`의 **손수 큐레이션한 30개 시드**만 커버한다. 임의의 한국 상장 종목(삼성전자·SK하이닉스 외)을 분석하려면 사용자가 `.KS`/`.KQ` 코드를 외워 직접 입력해야 한다. 이번 작업은 **KOSPI/KOSDAQ 전체 상장 종목(~2,700)**을 자동완성 인덱스와 백엔드 한글명 해석에 반영해, 코드/한글명 검색으로 임의의 KR 종목을 발견·분석할 수 있게 한다.

### 성공 기준

- Web 자동완성에서 임의의 KOSPI/KOSDAQ 종목이 **코드** 또는 **한글명**으로 검색된다.
- CLI/API/봇에서 `"삼성전자"` 같은 **한글명 입력**이 `005930.KS`로 해석된다.
- 기존 큐레이션 시드 30개의 다국어명(中/英)·별칭이 보존된다.
- 신규 데이터가 CN 병음 검색·`REPORT_LANGUAGE=ko` 중문 거부 게이트를 오염시키지 않는다.
- **런타임·Docker·CI가 pykrx를 import하지 않는다**(생성물만 소비).
- 취득 실패가 기존 인덱스를 손상시키지 않는다(fail-open).

### 명시적 비범위 (YAGNI — 각각 별도 프로젝트/후속)

- 나코드 `000660` → KR 시장 판별 (6자리 A주 규칙과 모호성 충돌; 고위험).
- 예약 GitHub Action 자동 재생성 (현재 인덱스용 Action 자체가 없음; 수동 패턴 유지).
- 중문/영문명 번역·별칭 자동 생성 (신규 종목은 한글명만).
- JP 전체 리스트, 포트폴리오 KRW 정확도, 시장 폭/섹터 (별도 프로젝트 B/C).

## 2. 확정된 설계 결정

| 결정 | 값 | 근거 |
|---|---|---|
| 스코프 | Web 자동완성 + 백엔드 한글명 해석 | 나코드 모호성(고위험) 제외한 균형점 |
| 데이터 소스 | **pykrx** | KRX 직접 래핑, 한국 핀테크 표준 |
| 이름 처리 | **한글명 우선 + 시드 override** | 중문/영문 소스 없음; ko 사용자·`REPORT_LANGUAGE=ko` 최적 |
| 갱신 방식 | 일회성 재생성 + 문서화 명령 | 현재 CN/HK/US 수동 갱신 패턴과 동일; 최소 스코프 |
| pykrx 위치 | `[project.optional-dependencies]` (스크립트 전용) | 런타임 deps·uv.lock 런타임 경로 불변 |

## 3. 아키텍처와 데이터 흐름

### 빌드타임 파이프라인 (수동 재생성)

```
pykrx  ──►  scripts/fetch_kr_stock_list.py
             │  KOSPI+KOSDAQ 티커·한글명, ETF/ETN 제외
             │  + 30개 큐레이션 시드 병합 (시드 우선/override)
             ├─►  data/stock_list_kr.csv          (전체 KR, 생성기 입력; data/ 미커밋)
             └─►  src/data/stock_names_kr.json     (한글명→코드 맵, 커밋; 백엔드 해석용)
                     │
scripts/refresh_stock_index.py  ──(KR 단계 추가)──►  scripts/generate_index_from_csv.py
                     │
                     └─►  apps/dsa-web/public/stocks.index.json   (KR 30 → ~2,700, 커밋)
```

### 런타임 소비 (신규 네트워크 호출 없음)

- **Web 자동완성**: 재생성된 `stocks.index.json` 로드 → `searchStocks.ts`가 이미 `nameZh/nameEn/nameKo`를 검색하므로 **프론트 코드 변경 없음**. 순수 데이터 변경.
- **백엔드 한글명 해석**: `src/services/name_to_code_resolver.resolve_name_to_code("삼성전자")` → 커밋된 `stock_names_kr.json` 조회 → `005930.KS`. 기존 CN `STOCK_NAME_MAP`·AkShare 경로는 불변, KR은 additive(한글↔한자 문자 비충돌).

### 핵심 원칙

- pykrx는 빌드타임 전용 → 런타임/Docker/CI 청결.
- 모든 실패는 fail-open, 기존 생성물 비손상.

## 4. 컴포넌트 (파일별 책임)

### 신규

- **`scripts/fetch_kr_stock_list.py`** — pykrx 취득기.
  - `stock.get_market_ticker_list(<business_date>, "KOSPI")` + `"KOSDAQ")` → 티커, `stock.get_market_ticker_name(ticker)` → 한글명.
  - ETF/ETN 티커 집합(`get_etf_ticker_list`/`get_etn_ticker_list`)을 빼서 보통주만 남김. 우선주는 포함(한글명 그대로).
  - `ts_code`: KOSPI=`{code}.KS`, KOSDAQ=`{code}.KQ`. 출력 스키마는 기존 시드와 동일(`ts_code,symbol,name,enname,name_ko,aliases`); 신규 행은 `name`=`name_ko`=한글명, `enname`·`aliases` 공란.
  - **시드 병합**: `scripts/stock_index_seeds/stock_list_kr.csv` 로드 → 동일 `ts_code`는 시드로 덮어씀.
  - 산출: `data/stock_list_kr.csv` + `src/data/stock_names_kr.json`(한글명→코드; 모호명 제외).
  - Sanity check: 취득 종목 수 최소 임계 미만이거나 빈 결과면 기존 파일 비덮어쓰기 + 비0 종료.

- **`src/data/stock_names_kr.json`** — 생성물이지만 **커밋**(런타임 해석 소비). 생성 방법을 스크립트 docstring·문서에 기재.

### 수정

- **`scripts/refresh_stock_index.py`** — Tushare 단계 뒤에 KR 취득 단계 추가. `--skip-kr` 플래그로 오프라인 재생성 지원. pykrx 미설치/실패 시 경고 후 기존 KR 데이터로 진행(단계 격리).
- **`src/services/name_to_code_resolver.py`** — `stock_names_kr.json`을 지연 로드해 로컬 역방향 맵에 병합(모듈 로드 시 1회). 로딩 실패 시 KR 해석만 조용히 비활성, 기존 CN 경로 무영향.
- **`apps/dsa-web/public/stocks.index.json`** — 재생성·커밋 (KR 30 → ~2,700).
- **`pyproject.toml`** — pykrx를 `[project.optional-dependencies]` 스크립트 그룹에 추가. 런타임 deps·`requirements.txt`·`uv.lock` 런타임 경로 불변.

### 문서

- `docs/market-support.md` — KR 자동완성이 시드 30 → 전체 상장으로 확장됨을 명시(불보장 항목 "전체 종목 리스트" 갱신). 재생성 명령·pykrx optional 설치 기재.
- `docs/CHANGELOG.md` `[Unreleased]` — `- [新功能] ...` 플랫 1줄.

## 5. 오류 처리 & 엣지 케이스

| 상황 | 처리 |
|---|---|
| pykrx 미설치/취득 실패/네트워크 오류 | 빌드타임 fail-open. 명확한 경고 + 비0 종료, 기존 인덱스/맵 비덮어쓰기. `refresh_stock_index.py` KR 단계 격리. |
| 부분/빈 응답(KOSDAQ만 실패 등) | 최소 종목 수 sanity check; 급감/빈 결과로 덮어쓰지 않음. |
| ETF/ETN/스팩 | 보통주 취득 후 ETF/ETN 집합 차집합. |
| 우선주(말미 5/7/9·K/L/M) | 포함, 한글명 그대로("삼성전자우"). |
| 시드 override 충돌 | 동일 `ts_code`는 시드 큐레이션 우선; 병합 후 중복 코드 0 보장. |
| 한글 → 병음 오염 | 신규 KR 행 병음 공란(생성기가 CN/BSE만 병음 생성). |
| 해석기 모호명 | 한 이름이 복수 코드면 제외(`_build_reverse_map_no_duplicates` 규칙 재사용). |
| 런타임 KR 맵 로딩 실패 | KR만 조용히 비활성, CN `STOCK_NAME_MAP`·AkShare 무영향. |
| `REPORT_LANGUAGE=ko` 순수성 | 신규 데이터는 한글/코드/라틴만 → 중문 거부 게이트 안전. |

## 6. 테스트 전략 (오프라인·결정적)

- **취득 스크립트**: pykrx를 mock(네트워크 없음) — 고정 티커·이름 입력 → CSV/JSON 산출 검증. 커버: `.KS`/`.KQ` 매핑, ETF 제외, **시드 override 우선**, ko-primary(`name==name_ko`), 병음 공란, sanity check 임계 동작.
- **생성기**: 소규모 KR 전체 CSV(시드+신규) → 압축 인덱스 인코딩 검증(nameKo/enName 위치, KR 종목 수 증가).
- **해석기**: `resolve_name_to_code("삼성전자")→005930.KS`, 모호명 제외, KR 맵 로딩 실패 시 CN 경로 회귀 무영향, 한글↔한자 비충돌.
- **인덱스 무결성**: 재생성된 `stocks.index.json` — 중복 canonicalCode 0, KR 수 임계 이상, 시드 30 존재·큐레이션명 보존.
- **CI 게이트**: `./scripts/ci_gate.sh`(flake8 + pytest `-m "not network"`) 통과. Docker 스모크가 pykrx 미import 확인.

## 7. 리스크와 완화

| 리스크 | 완화 |
|---|---|
| pykrx가 KRX 변경으로 취약 | 빌드타임 전용 → 런타임 무영향; 실패 시 기존 인덱스 유지. |
| ~2,700행 추가로 인덱스 JSON 비대 | 압축 배열 포맷; KR은 US(23k)·CN(5k) 대비 소규모. 원격 서비스는 upstream URL이라 무관. |
| 우선주/스팩 노이즈 | 한글명 그대로 노출, ETF/ETN만 제외. 필요 시 후속 필터 강화. |
| `stock_names_kr.json` 커밋 크기 | 코드→이름만 담은 소형 JSON(수십 KB). |

## 8. 롤백

- `src/data/stock_names_kr.json` 제거 + `name_to_code_resolver` KR 병합 revert → 백엔드 KR 해석 원복.
- `stocks.index.json`을 시드 30 기준으로 재생성(`--skip-kr`) → Web 자동완성 원복.
- `fetch_kr_stock_list.py`·`refresh_stock_index.py` KR 단계·pykrx optional dep 제거.
- 본 커밋 전체 revert로 일괄 원복.

## 9. 구현 계획에서 확정할 항목

- pykrx `business_date` 결정 방식(최신 거래일 계산 vs. 고정 인자).
- ETF/ETN 외 추가 제외 대상(리츠·인프라펀드 등) 포함/제외 정책.
- `stock_names_kr.json` 정확한 스키마(평면 `{name: code}` vs. 메타 포함).
- `name_to_code_resolver` 내 KR 맵 우선순위 슬롯(로컬 CN 역맵 전/후).
- sanity check 최소 종목 수 임계값.
- optional-dependency 그룹 이름(예: `scripts` / `krlist`).

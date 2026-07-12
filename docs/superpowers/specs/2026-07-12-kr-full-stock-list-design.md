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
- 신규 데이터가 `REPORT_LANGUAGE=ko` 중문 거부 게이트를 오염시키지 않는다.
- **런타임·Docker·CI가 pykrx를 import하지 않는다**(생성물만 소비).
- 취득 실패가 기존 인덱스를 손상시키지 않는다(fail-open).

### 명시적 비범위 (YAGNI — 각각 별도 프로젝트/후속)

- 예약 GitHub Action 자동 재생성 (현재 인덱스용 Action 자체가 없음; 수동 패턴 유지).
- 중문/영문명 번역·별칭 자동 생성 (신규 종목은 한글명만).
- JP 전체 리스트, 포트폴리오 KRW 정확도, 시장 폭/섹터 (별도 프로젝트 B/C).
- **나코드 시장 판별 신규 로직**: 스코프 C의 신규 코드는 만들지 않는다. 단, 인덱스 확장의 자연스러운 부작용으로 기존 `resolve_index_stock_code` 경로가 KR 나코드를 더 넓게 해석하는 것은 **수용**한다(§2 결정, §5 참조).

## 2. 확정된 설계 결정

| 결정 | 값 | 근거 |
|---|---|---|
| 스코프 | Web 자동완성 + 백엔드 한글명 해석 | 나코드 신규 로직 제외한 균형점 |
| 데이터 소스 | **pykrx** | KRX 직접 래핑, 한국 핀테크 표준 |
| 이름 처리 | **한글명 우선 + 시드 override** | 중문/영문 소스 없음; ko 사용자·`REPORT_LANGUAGE=ko` 최적 |
| 갱신 방식 | 일회성 재생성 + 문서화 명령 | 현재 CN/HK/US 수동 갱신 패턴과 동일; 최소 스코프 |
| pykrx 위치 | `[dependency-groups]` 스크립트 그룹 (스크립트 전용) | 런타임 deps·uv.lock 런타임 경로 불변 |
| **백엔드 해석 소스** | **기존 `stock_index_loader.py` 재사용** (별도 JSON 신설 안 함) | 인덱스가 단일 진실원; `复用现有模块，不新增平行实现`(AGENTS.md) |
| **나코드 부작용** | 인덱스 확장에 따른 KR 나코드 해석 확장 **수용** | 문서화된 계약(풀 적중→KR, 미적중→A주)과 일치; 회귀 테스트로 CN 우선 고정 |

## 3. 아키텍처와 데이터 흐름

### 빌드타임 파이프라인 (수동 재생성)

```
pykrx  ──►  scripts/fetch_kr_stock_list.py
             │  KOSPI+KOSDAQ 티커·한글명, ETF/ETN 제외
             │  + 30개 큐레이션 시드 병합 (시드 우선/override)
             └─►  data/stock_list_kr.csv          (전체 KR, 생성기 입력; data/ 미커밋)
                     │
scripts/refresh_stock_index.py  ──(KR 단계 추가)──►  scripts/generate_index_from_csv.py
                     │
                     ├─►  apps/dsa-web/public/stocks.index.json   (KR 30 → ~2,700, 커밋)
                     └─►  static/stocks.index.json                (백엔드 사본, 기존 sync 자동)
```

### 런타임 소비 (신규 네트워크 호출 없음, 신규 커밋 아티팩트 없음)

- **Web 자동완성**: 재생성된 `stocks.index.json` 로드 → `searchStocks.ts`가 이미 `nameZh/nameEn/nameKo`를 검색하므로 **프론트 코드 변경 없음**. 순수 데이터 변경.
- **백엔드 한글명 해석**: 기존 `src/data/stock_index_loader.py`가 이미 인덱스를 로드·캐싱한다. 여기에 **KR `nameKo→canonicalCode` 역방향 맵 함수**를 추가하고, `src/services/name_to_code_resolver.resolve_name_to_code`가 그것을 조회한다. `resolve_name_to_code("삼성전자")` → `005930.KS`. 기존 CN `STOCK_NAME_MAP`·AkShare·pinyin·fuzzy 경로는 불변, KR은 additive(한글↔한자 문자 비충돌).

### 핵심 원칙

- pykrx는 빌드타임 전용 → 런타임/Docker/CI 청결.
- 인덱스가 **단일 진실원**: 자동완성·code→name·name→code·나코드 조회 모두 동일 인덱스에서 파생.
- 모든 실패는 fail-open, 기존 생성물 비손상.

## 4. 컴포넌트 (파일별 책임)

### 신규

- **`scripts/fetch_kr_stock_list.py`** — pykrx 취득기.
  - `stock.get_market_ticker_list(<business_date>, "KOSPI")` + `"KOSDAQ")` → 티커, `stock.get_market_ticker_name(ticker)` → 한글명.
  - ETF/ETN 티커 집합(`get_etf_ticker_list`/`get_etn_ticker_list`)을 빼서 보통주만 남김. 우선주는 포함(한글명 그대로).
  - `ts_code`: KOSPI=`{code}.KS`, KOSDAQ=`{code}.KQ`. 출력 스키마는 기존 시드와 동일(`ts_code,symbol,name,enname,name_ko,aliases`); 신규 행은 `name`=`name_ko`=한글명, `enname`·`aliases` 공란.
  - **시드 병합**: `scripts/stock_index_seeds/stock_list_kr.csv` 로드 → 동일 `ts_code`는 시드로 덮어씀(큐레이션 zh/en/별칭 보존).
  - 산출: `data/stock_list_kr.csv` (생성기 입력).
  - Sanity check: 취득 종목 수 최소 임계 미만이거나 빈 결과면 기존 파일 비덮어쓰기 + 비0 종료.

### 수정

- **`src/data/stock_index_loader.py`** — 인덱스에서 KR `nameKo→canonicalCode` 역방향 맵을 만드는 함수 추가(예: `get_kr_name_to_code_map()`): market=`KR`(index 6) 엔트리의 `nameKo`(index 11)→`canonicalCode`(index 0), **모호명(복수 코드) 제외**. 기존 lazy-load·RLock·멀티경로·캐시 인프라 재사용. `clear_stock_index_cache()`에 신규 캐시도 리셋 추가.
- **`src/services/name_to_code_resolver.py`** — step 2(로컬 역맵)와 `_contains_cjk` 조기 리턴(현재 line 190) **사이**에 KR 맵 조회 추가: `if s in kr_map: return kr_map[s]`. 한글은 CJK 한자 범위 밖이라 이 게이트 이전에 처리해야 해석됨. 로딩 실패 시 KR만 조용히 비활성(fail-open), 기존 CN 경로 무영향.
- **`scripts/refresh_stock_index.py`** — Tushare 단계 뒤에 KR 취득 단계 추가. `--skip-kr` 플래그로 오프라인 재생성 지원. pykrx 미설치/실패 시 경고 후 기존 KR 데이터로 진행(단계 격리). 기존 `_sync_static_index()`가 static 사본 동기화(변경 없음).
- **`apps/dsa-web/public/stocks.index.json`** (+ `static/stocks.index.json`) — 재생성·커밋 (KR 30 → ~2,700).
- **`pyproject.toml`** — pykrx를 `[dependency-groups]` 스크립트 그룹에 추가. 런타임 `dependencies`·`requirements.txt`·`uv.lock` 런타임 경로 불변.

### 미변경 (확인 완료)

- **`scripts/generate_index_from_csv.py`** — 이미 `data/stock_list_kr.csv` 우선 로드, `name_ko`/`enname`/`aliases` 파싱, `.KS`/`.KQ` 접미사 보존, 압축 포맷에 `nameEn`(10)/`nameKo`(11) 인코딩. **코드 변경 불필요.** (신규 KR 행은 병음 필드가 한글 passthrough가 되나 무해 — §5.)
- **프론트엔드** — `searchStocks.ts`가 이미 `nameKo` 검색. 변경 없음.

### 문서

- `docs/market-support.md` — KR 자동완성이 시드 30 → 전체 상장으로 확장됨을 명시(불보장 항목 "전체 종목 리스트" 갱신). 재생성 명령·pykrx 설치·나코드 해석 확장 계약 기재.
- `docs/CHANGELOG.md` `[Unreleased]` — `- [新功能] ...` 플랫 1줄.

## 5. 오류 처리 & 엣지 케이스

| 상황 | 처리 |
|---|---|
| pykrx 미설치/취득 실패/네트워크 오류 | 빌드타임 fail-open. 명확한 경고 + 비0 종료, 기존 `data/stock_list_kr.csv` 비덮어쓰기. `refresh_stock_index.py` KR 단계 격리. |
| 부분/빈 응답(KOSDAQ만 실패 등) | 최소 종목 수 sanity check; 급감/빈 결과로 덮어쓰지 않음. |
| ETF/ETN/스팩 | 보통주 취득 후 ETF/ETN 집합 차집합. |
| 우선주(말미 5/7/9·K/L/M) | 포함, 한글명 그대로("삼성전자우"). |
| 시드 override 충돌 | 동일 `ts_code`는 시드 큐레이션 우선; 병합 후 중복 코드 0 보장. |
| **한글 → 병음 passthrough** | `lazy_pinyin`은 비중문을 그대로 통과(US도 영문명 passthrough). 신규 KR 행 병음=한글 → **중문 아님, ko 거부 게이트 안전**, nameKo 검색과 중복될 뿐 무해. 생성기 미변경. |
| 해석기 모호명 | KR `nameKo`가 복수 코드면 제외(기존 `_build_reverse_map_no_duplicates` 규칙 재사용). |
| 런타임 KR 맵 로딩 실패 | KR만 조용히 비활성, CN `STOCK_NAME_MAP`·AkShare 무영향. |
| **나코드 KR 해석 확장** | 기존 `resolve_index_stock_code`가 인덱스에서 jp/kr 나베이스를 유도 → 인덱스 확장 시 KR 나코드 해석이 넓어짐(수용). 대표 A주 나코드가 여전히 CN으로 해석됨을 **회귀 테스트로 고정**. 문서화된 계약(풀 적중→KR, 미적중→A주 기본)과 일치. |
| `REPORT_LANGUAGE=ko` 순수성 | 신규 데이터는 한글/코드/라틴만 → 중문 거부 게이트 안전. |

## 6. 테스트 전략 (오프라인·결정적)

- **취득 스크립트**: pykrx를 mock(네트워크 없음) — 고정 티커·이름 입력 → `data/stock_list_kr.csv` 산출 검증. 커버: `.KS`/`.KQ` 매핑, ETF 제외, **시드 override 우선**, ko-primary(`name==name_ko`), sanity check 임계 동작.
- **생성기 회귀**: 소규모 KR 전체 CSV(시드+신규) → 압축 인덱스 인코딩 검증(nameKo/nameEn 위치, KR 종목 수 증가). 기존 `tests/test_generate_index_from_csv.py` 관례 준수.
- **로더 KR 맵**: `get_kr_name_to_code_map()` — nameKo→canonical, 모호명 제외, 파일 부재/깨짐 시 `{}` 반환(fail-open), 캐시 클리어 동작.
- **해석기**: `resolve_name_to_code("삼성전자")→005930.KS`, 모호명 제외, KR 맵 로딩 실패 시 CN 경로 회귀 무영향, 한글↔한자 비충돌.
- **나코드 회귀**: 대표 A주 나코드(예: 실재 `600xxx`/`000001`)가 KR로 오해석되지 않고 CN 유지됨을 고정.
- **인덱스 무결성**: 재생성된 `stocks.index.json` — 중복 canonicalCode 0, KR 수 임계 이상, 시드 30 존재·큐레이션명 보존.
- **CI 게이트**: `./scripts/ci_gate.sh`(flake8 + pytest `-m "not network"`) 통과. Docker 스모크가 pykrx 미import 확인.

## 7. 리스크와 완화

| 리스크 | 완화 |
|---|---|
| pykrx가 KRX 변경으로 취약 | 빌드타임 전용 → 런타임 무영향; 실패 시 기존 인덱스 유지. |
| ~2,700행 추가로 인덱스 JSON 비대 | 압축 배열 포맷; KR은 US(23k)·CN(5k) 대비 소규모. |
| 나코드 CN/KR 충돌 확대 | 문서화된 계약과 일치; 회귀 테스트로 대표 CN 나코드 고정; 사용자 승인(§2). |
| 우선주/스팩 노이즈 | 한글명 그대로 노출, ETF/ETN만 제외. 필요 시 후속 필터 강화. |

## 8. 롤백

- `stock_index_loader` KR 맵 함수 + `name_to_code_resolver` KR 조회 revert → 백엔드 KR 해석 원복.
- `stocks.index.json`을 시드 30 기준으로 재생성(`--skip-kr`) → Web 자동완성·나코드 확장 원복.
- `fetch_kr_stock_list.py`·`refresh_stock_index.py` KR 단계·pykrx 그룹 제거.
- 본 커밋 전체 revert로 일괄 원복.

## 9. 구현 계획에서 확정할 항목

- pykrx `business_date` 결정 방식(최신 거래일 계산 vs. 고정 인자).
- ETF/ETN 외 추가 제외 대상(리츠·인프라펀드 등) 포함/제외 정책.
- `get_kr_name_to_code_map()` 정확한 시그니처·nameKo 외 별칭 포함 여부.
- `name_to_code_resolver` 내 KR 맵 조회 슬롯(step 2 직후 확정).
- sanity check 최소 종목 수 임계값.
- `[dependency-groups]` 스크립트 그룹 이름.

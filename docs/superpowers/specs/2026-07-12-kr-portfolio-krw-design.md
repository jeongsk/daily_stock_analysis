# KR 포트폴리오 KRW 손익 정확도 — 설계 스펙

- 작성일: 2026-07-12
- 상태: 설계 확정 (구현 계획 대기)
- 관련 영역: 포트폴리오 스냅샷 집계·통화·정밀도, `data_quality` 시맨틱, Web 통화 포매팅
- 선행 컨텍스트: KR 전체 종목 리스트(`2026-07-12-kr-full-stock-list-design.md`, 로드맵 KR-A). 본 스펙은 로드맵 KR-C.
- 근거: wayfinder map [Map: KR 포트폴리오 KRW 손익 정확도 스펙](https://github.com/jeongsk/daily_stock_analysis/issues/4) — 결정 티켓 [#5](https://github.com/jeongsk/daily_stock_analysis/issues/5)·[#6](https://github.com/jeongsk/daily_stock_analysis/issues/6)·[#7](https://github.com/jeongsk/daily_stock_analysis/issues/7)·[#8](https://github.com/jeongsk/daily_stock_analysis/issues/8) 종합. 코드 실측 2026-07-12.

## 1. 개요와 목표

한국(KR) 포트폴리오 계정의 원가·평가액·손익이 지금은 **CNY로 집계**된다. `PortfolioService`가 계정의 `base_currency`(스키마에 이미 존재, KR은 KRW여야 함)를 무시하고 집계 통화를 `"CNY"`로 하드코딩하기 때문(`src/services/portfolio_service.py:488`)이며, 그 결과 KRW 원가·평가액에 KRW→CNY 환산이 강제 적용되어 손익이 왜곡된다. 동시에 KR은 `PARTIAL_VALUATION_MARKETS`(`:34`)로 강등되어 `data_quality=partial`과 `fx_and_cost_basis_partial` 한계 라벨을 달지만, 실측(#5)상 KRW 원가·평가액·손익 산출에 필요한 데이터(price, `currency=KRW`, FX 페어)는 온전하다 — 이 강등은 과보수적이다.

이번 작업은 KR 포트폴리오가 **KRW 원가·평가액·손익을 정식 구경으로 산출**하고, 집계를 **계정별 `base_currency`**(KR=KRW)로 수행하며, KR을 `data_quality=ok`로 재분류하도록 서비스·Web·테스트·문서를 정정한다.

**범위: KRW만.** JP/TW는 같은 코드 갈이지만 out of scope(§1 비범위). A주(CN)·HK·US·JP·TW 동작은 무회귀가 절대 요건.

### 성공 기준 (load-bearing 정확성)

- **순수 KR 계정**의 집계 스냅샷은 `currency=KRW`이고, `unrealized_pnl`이 **네이티브 KRW** 손익 `(last_price − avg_cost) × qty`를 **identity FX**(KRW→CNY 왕복 없음)로 산출한다.
- 순수 KR 스냅샷의 `data_quality=ok`이고 `fx_and_cost_basis_partial`이 **부재**한다.
- **혼합 포트폴리오**(예: CN+KR)는 집계 통화 `CNY` fallback을 유지한다 — **기존 동작 무회귀**.
- 신규 KR 계정·거래의 기본 통화가 자동으로 KRW로 추론된다.
- Web에서 `formatMoney(x,'KRW')`가 **소수 0자리**로 렌더되고, KR 스냅샷에 partial 배너가 노출되지 않는다.
- `get_portfolio_snapshot` agent-tool의 KR 스냅샷 compact 계약이 `KRW`+`data_quality=ok`를 노출한다(LLM 소비 계약).
- JP/TW는 3개 한계 라벨·`partial`을 그대로 유지한다(회귀 없음). CN/HK/US 단일계정은 이제 네이티브 통화로 집계된다(의도된 정확도 개선).

### 명시적 비범위 (map Out of scope)

- **JPY / TWD 통화 지원**: 동일 코드 갈(`PARTIAL_VALUATION_MARKETS`·`_default_currency_for_market`)이지만 KRW-only 범위 결정으로 제외. 재개 시 별도 여정.
- **신규 전용 FX 데이터 소스**: 기존 yfinance 경로(`{from}{to}=X`)만 재사용. `docs/market-support.md`의 "신규 JPY/KRW 汇率源 미신설" 방침 유지.
- **다중시장 혼합 포트폴리오의 집계통화 재선택 UI**: 계정별 `base_currency`를 넘어서는 통화 선택 기능.
- **`data_quality`의 동적 평가실패 반영**: 시세결측(`price_available=False`)·FX stale을 `data_quality`가 반영하도록 만드는 것은 CN/HK/US 포함 전 시장 공통 행동 변경 → 별도 여정. 동적 신호는 기존 전용 필드(`fx_stale`/`price_available`/`price_stale`) 유지.
- **기존 CNY-라벨 KR 계정 자동 백필**: 신규 전용. 자동 마이그레이션·추론 기반 재라벨링 없음(§5 위험 참조).

## 2. 확정된 설계 결정

| # | 결정 | 값 | 출처 |
|---|---|---|---|
| D1 | 통화 범위 | **KRW만** (JP/TW out of scope) | 사용자 확정 |
| D2 | 집계 통화 | **in-scope 계정들의 `base_currency`가 전부 일치하면 그 통화, 아니면 `CNY` fallback** | #7 |
| D3 | KR 기본통화 배선 | `_default_currency_for_market`에 `kr→KRW`. 신규 KR 계정·거래 통화 자동 KRW, 명시 override 유지, JP/TW 불변 | #7 |
| D4 | `data_quality` 재정의 | **평가 정확도 신호**로 재정의 — "평가영향 라벨"이 있을 때만 `partial`, 정보성 라벨만 있으면 `ok` | #6 |
| D5 | KR 한계 라벨 3종 판정 | `fx_and_cost_basis_partial` **제거**; `realtime_quote_best_effort`·`sector_and_risk_metrics_limited`는 **정보성 유지**(평가 강등 안 함) → **KR `data_quality=ok`** | #6, #5 |
| D6 | 정보성 라벨 분류 방식 | 명시 상수 `INFORMATIONAL_LIMITATIONS`(전 시장 공통). KR만 `_portfolio_limitations_for_market`에서 `fx_and_cost_basis_partial` 제거, JP/TW 완전 불변 | #6 |
| D7 | KRW 정밀도 | **표시 전용** — KRW 소수 **0자리** + 천단위 구분, 그 외(CNY/USD/HKD) 2자리. 저장·내부 반올림·마이그레이션 불변 | #7 |
| D8 | 소수자릿수 테이블 | 통화→소수자릿수 규칙을 작은 단일 테이블로 **web(`portfolioFormat.ts`)+Python(report/notification) 미러링**, drift 방지 문서화 | #7 |
| D9 | 기존 KR 계정 백필 | **자동 없음(신규 전용)**. 수동 교정(`update_account`) 문서화, 거래 재라벨 위험 명시, CLI 도구는 후속 옵션 | #7 |
| D10 | 리스크 서비스 | `portfolio_risk_service`의 `... or "CNY"`는 null-guard로 D3 이후 KR에 이미 올바름 — **변경 불필요, 검증만** | #7 |
| D11 | 의무 변경 계층 | **service**(source of truth) + **web**(`formatMoney` 1지점) + **tests**. storage=no-op, report_renderer=드롭, alerts=verify-only, desktop=상속 | #8 |

## 3. 아키텍처와 데이터 흐름

### 버그의 본질 (load-bearing)

현재 버그는 정밀도가 아니라 **통화 집계**다. `_calculate_aggregate_snapshot`가 집계 통화를 `"CNY"`로 고정(`portfolio_service.py:488`)하고, 각 포지션 원가·평가액을 `_convert_amount(..., to_currency="CNY")`로 환산(`:532`–`:568`)한다. 순수 KR 계정도 KRW 값이 KRW→CNY로 환산되어 손익이 FX 왜곡을 탄다. 수정의 핵심은 **집계 통화를 계정 `base_currency`에서 파생**시켜, 순수 KR 포트폴리오가 KRW로 집계 → `KRW→KRW` **identity 변환**(FX 왕복 없음)이 되게 하는 것이다.

### 집계 통화 파생 규칙 (D2)

```
aggregate_currency =
    (in-scope 계정들의 {base_currency} 집합이 단일값 C) ? C
                                                         : "CNY"   # 혼합 fallback
```

- 단일 KR 계정, 전체-KR 포트폴리오 → `KRW`.
- 혼합(CN+KR 등) → `CNY` fallback = **기존 동작과 동일**(무회귀).
- 부작용(의도됨): 단일 CN/HK/US 계정도 이제 네이티브 통화로 집계 — 더 정확. 계정별 payload는 이미 native `base_currency`를 유지하므로 이 변경의 영향면은 **집계 레벨 통화 선택**에 국한.

### `data_quality` 재정의 (D4~D6)

```
INFORMATIONAL_LIMITATIONS = {"realtime_quote_best_effort", "sector_and_risk_metrics_limited"}  # 전 시장 공통

data_quality = "partial" if (limitations - INFORMATIONAL_LIMITATIONS) else "ok"
```

- 3개 `data_quality` 사이트(`portfolio_service.py:604`·`:973`·`:1115`)의 `"partial" if limitations else "ok"`를 위 규칙으로 교체.
- `_portfolio_limitations_for_market`(`:43`)에서 **KR만** `fx_and_cost_basis_partial` 제거(`realtime_quote_best_effort`·`sector_and_risk_metrics_limited`는 투명성 위해 유지).
- **JP/TW 불변**: 3개 라벨 유지 → `fx_and_cost_basis_partial`이 평가영향 라벨로 남아 여전히 `partial`. 회귀 없음.
- `data_quality`는 **정적 분류만**. 동적 실패는 계속 전용 필드로(§1 비범위) — CN/HK/US와 동일 계약에 KR을 "완전 평가" 시장으로 편입.

### 데이터 흐름 (순수 KR 계정)

```
PortfolioAccount(base_currency=KRW)         # 신규 계정: _default_currency_for_market('kr')→KRW (D3)
      │
PortfolioTrade(currency=KRW)                # 거래 통화도 같은 함수로 KRW 추론 (D3)
      │
_calculate_aggregate_snapshot
      │  aggregate_currency = KRW           # D2: 단일 base_currency
      │  _convert_amount(KRW→KRW) = identity # FX 왕복 없음 → 손익 왜곡 제거
      │  limitations = [realtime_quote_best_effort, sector_and_risk_metrics_limited]  # D5
      │  data_quality = ok                  # D4: 정보성 라벨만 남음
      ▼
snapshot { currency: KRW, unrealized_pnl: 네이티브 KRW, data_quality: ok }
      │
      ├─► agent tool get_portfolio_snapshot   # KRW + data_quality=ok 계약 (LLM 소비)
      ├─► web formatMoney(x,'KRW')            # 0자리 (D7/D8)
      └─► portfolio_alerts                    # currency passthrough (verify-only)
```

### 핵심 원칙

- **인프라 신설 없음**: `PortfolioAccount.base_currency` 컬럼·FX 테이블·yfinance 페치·`_convert_amount`는 이미 통화 일반형. 갭은 서비스가 이를 무시하고 CNY로 하드코딩하는 것뿐. **마이그레이션 불필요**.
- **무회귀 우선**: 혼합 포트폴리오·JP/TW·동적 실패 계약은 전부 불변. 변경은 "순수 단일통화 집계 정확화" + "KR 강등 해제"로 국한.
- **단일 진실원**: 집계 통화·정밀도·`data_quality`는 모두 service가 산출, 다른 계층은 소비만.

## 4. 컴포넌트 (파일별 책임)

### 수정 — 의무

- **`src/services/portfolio_service.py`** (source of truth)
  - `_default_currency_for_market`(`:1727`)에 `kr→KRW` 분기 추가(`if market == "kr": return "KRW"`). JP/TW는 `"CNY"` 유지(`:1732`). — D3
  - 집계 통화 하드코딩 `aggregate_currency = "CNY"`(`:488`)를 D2 파생 규칙으로 교체: in-scope 계정 `base_currency` 집합이 단일이면 그 값, 아니면 `"CNY"`.
  - `INFORMATIONAL_LIMITATIONS` 상수 신설(모듈 레벨, `:34` 인근). — D6
  - `_portfolio_limitations_for_market`(`:43`)에서 **KR만** `fx_and_cost_basis_partial` 제외. JP/TW 3라벨 불변. — D5
  - `data_quality` 3개 사이트(`:604`·`:973`·`:1115`)를 `"partial" if (limitations - INFORMATIONAL_LIMITATIONS) else "ok"`로 교체. — D4
- **`apps/dsa-web/src/utils/portfolioFormat.ts`** (좁게)
  - `formatMoney`(`:69`)의 하드코딩 `minimumFractionDigits:2`/`maximumFractionDigits:2`(`:72`–`:73`)를 통화→소수자릿수 헬퍼(KRW:0, 그 외:2)로 교체. Python 테이블 미러. — D7/D8
  - `formatValuationMoney`(`:99`, `row.valuationCurrency` 경유)는 `formatMoney`를 위임하므로 자동 반영.

### 미변경 — verify-only / 상속 (확인 완료)

- **`src/storage.py`** — `PortfolioAccount.base_currency` 컬럼 이미 존재(default `'CNY'`). **no-op, 마이그레이션 없음**. — D11
- **`src/services/portfolio_alerts.py`** — `currency` **passthrough**(`:546`·`:565`), 통화별 포매팅 로직 없음. 서비스가 KRW를 내면 자동 전파. **코드 변경 없음**, 수용 기준에 "passthrough가 KRW 운반" 확인만. — D11
- **`src/reports/report_renderer.py`** — 포트폴리오를 **전혀 소비하지 않음**(실측: `portfolio` 참조 0). 티켓의 "report_renderer KRW 표시" 전제는 공집합 → **드롭**. — D11
- **`src/services/portfolio_risk_service.py`** — `... or "CNY"`(`:285`/`:341`/`:490`)는 null-guard. D3 이후 KR 계정은 `base_currency=KRW`를 가지므로 이미 올바름. **변경 불필요**, "검증 완료" 명시. — D10
- **partial 배지(Web)** — `PortfolioPage.tsx`의 partial 배너는 backend `data_quality=ok`의 **자동 결과**로 미노출. **코드 변경 없음**, 수용 assertion만. — D11
- **desktop(`apps/dsa-desktop`)** — dsa-web 빌드 래핑 → 자체 변경 없음, **상속**. — D11

### 문서

- **`docs/market-support.md`** — 실측상 KR 포트폴리오 경계 프로즈는 **`:44`**(`不补齐 Portfolio 的 JPY/KRW 汇率、成本、市值完整口径；相关字段仅放开市场类型以避免前后端校验拒绝。`). 이 줄에서 **KRW를 제거**(JPY만 남김)하고, KR 포트폴리오가 KRW 원가·평가액·손익을 정식 구경으로 산출하며 `data_quality=ok`(남은 한계는 정보성)임을 명시. "신규 전용 FX 소스 미신설"(out of scope)은 유지.
  - 주의: 티켓 #6이 인용한 "Phase 3 `:174-182`" 라인 참조는 **stale**(당시 문서 버전 기준). 현행 문서는 중국어 단일 파일, 실제 앵커는 `:44`. 구현 시 이 줄 재확인.
  - 이 문서는 단일(중국어) 파일 — 별도 영문 미러 없음. 이중언어 동기화 대상 아님.
- **`docs/CHANGELOG.md`** `[Unreleased]` — 플랫 1줄: `- [改进] KR 포트폴리오 KRW 손익 정확도 ...`(집계 통화=계정 base_currency, KR data_quality=ok, KRW 0자리 표시). 필요 시 `[修复]`로도 무방(손익 왜곡 정정 성격).

## 5. 오류 처리 & 엣지 케이스

| 상황 | 처리 |
|---|---|
| **혼합 포트폴리오**(CN+KR 등) | 집계 통화 `CNY` fallback(D2). 기존 동작 무회귀 — KR 포지션은 계정 payload에 네이티브 KRW로 유지, 집계 레벨만 CNY. |
| **JP/TW 계정** | `_default_currency_for_market` CNY 유지, 3개 한계 라벨 유지 → `data_quality=partial` 유지. 완전 불변(회귀 없음). |
| **기존 CNY-라벨 KR 계정 백필** | 자동 없음(D9). 수정 전 KR 거래는 currency가 CNY로 기본 저장(KRW 크기값이 CNY 라벨). 계정 base만 뒤집으면 그 거래들이 KRW로 **이중 환산**되어 더 틀림 → **진짜 위험 지점은 거래 재라벨링**. 스펙: (a) 계정 레벨은 `update_account` base_currency 경로로 수동 교정 가능함을 문서화, (b) 거래 재라벨 위험을 명시. 전용 CLI 백필 도구는 실 KR 과거 데이터가 유의미할 때만 후속. |
| **FX 얇은 방향 페어**(#5) | `CNYKRW=X`가 윈도우 1행뿐(약 0.8% 역수 편차). 유동적 방향(`KRWCNY=X`·`USDKRW=X`)·USD 크로스가 견고. 순수 KR 집계는 identity(FX 미사용)라 무영향. 혼합 fallback 경로에서만 관여. |
| **KR 실시간 시세 결측** | 기존 전용 필드(`price_available`/`price_stale`)로 표현, `data_quality` 불변(비범위 계약). |
| **`data_quality` 정보성 라벨만 남는 시장** | KR: `["realtime_quote_best_effort","sector_and_risk_metrics_limited"]` → `ok`. 다른 시장이 향후 이 집합만 갖게 되면 동일 규칙 적용(전 시장 공통 상수). |
| **KRW 표시 정밀도** | 표시 전용(D7). 저장은 `Float`·기존 6/8자리 내부 반올림 그대로. KRW 정수성은 순전히 포맷 관심사. |
| **소수자릿수 테이블 drift** | web `portfolioFormat.ts`와 Python(report/notification) 미러(D8). 한쪽만 바뀌면 표시 불일치 → 문서화로 방지, 테스트로 가드. |

## 6. 테스트 전략 (오프라인·결정적)

**의무 3 + 선택 1** (#8):

- **의무** `tests/test_portfolio_service.py`
  - `test_jp_kr_portfolio_snapshot_marks_partial_valuation_boundaries`(`:475`)가 현재 **KR partial을 assert** → **반전**: KR→`data_quality=ok`+`fx_and_cost_basis_partial` 부재, JP는 partial 유지로 분리.
  - 추가: `_default_currency_for_market('kr')=='KRW'`; 집계 통화(단일 KR·전체-KR→`KRW`, 혼합 CN+KR→`CNY` fallback); 순수 KR `unrealized_pnl`이 네이티브 KRW 손익(identity FX, CNY 왕복 없음)임을 관측.
- **의무** `apps/dsa-web/src/utils/__tests__/portfolioFormat.test.ts` — `formatMoney(x,'KRW')`→0자리, 그 외(USD/CNY)→2자리. 유일 web 변경 지점 가드.
- **의무** `tests/test_data_tools_portfolio_snapshot.py` — KR 변형 계약 테스트: KR 스냅샷이 `get_portfolio_snapshot` agent-tool을 거쳐 **LLM으로 전달**되므로 compact 계약에 `KRW`+`data_quality=ok` 고정.
- **선택** `apps/dsa-web/src/pages/__tests__/PortfolioPage.test.tsx` — KR 픽스처 partial 배너 미노출(backend `data_quality=ok`로 이미 커버되는 자동 결과).

**게이트**: `./scripts/ci_gate.sh`(flake8 + pytest `-m "not network"`) + `cd apps/dsa-web && npm ci && npm run lint && npm run build`.

### 계층별 수용 기준

| 계층 | 수용 기준 |
|---|---|
| service | 순수 KR: `currency=KRW`, `unrealized_pnl`=네이티브 KRW 손익(identity FX), `data_quality=ok`, `fx_and_cost_basis_partial` 부재. 혼합 KR+CN: `CNY` fallback(무회귀). JP/TW: 3라벨·`partial` 유지 |
| agent tool | `get_portfolio_snapshot`(KR 계정)이 compact 계약에 `KRW`+`data_quality=ok` 노출 |
| web | `formatMoney(x,'KRW')`→0자리; KR 스냅샷 partial 배너 미노출 |
| alerts | passthrough가 `currency=KRW` 운반(verify-only) |

## 7. 리스크와 완화

| 리스크 | 완화 |
|---|---|
| 집계 통화 파생이 CN/HK/US 단일계정 동작을 바꿈(부작용) | 의도된 개선(네이티브 통화 집계 = 더 정확). 계정 payload는 이미 native → 영향면은 집계 레벨 통화 선택에 국한. 회귀 테스트로 혼합 fallback·기존 CN 단일 동작 고정. |
| `data_quality` 규칙 변경이 타 시장 회귀 | KR만 라벨 제거, JP/TW 3라벨 불변으로 `partial` 유지. `INFORMATIONAL_LIMITATIONS`는 명시 상수 — 다른 시장은 평가영향 라벨 보유로 영향 없음. 반전 테스트가 JP partial 유지를 가드. |
| 기존 CNY-라벨 KR 거래 이중 환산 | 자동 백필 안 함(D9). 수동 교정 절차 + 거래 재라벨 위험을 문서에 명시. |
| web/Python 소수자릿수 테이블 drift | 단일 규칙 미러 + 문서화 + 양쪽 테스트. |
| LLM 계약 변화(agent tool) | data_tools KR 변형 테스트로 `KRW`+`data_quality=ok` 고정 — 프롬프트 소비 계약 회귀 방지. |

## 8. 롤백

- `portfolio_service.py`의 `_default_currency_for_market` kr 분기, 집계 통화 파생 로직, `INFORMATIONAL_LIMITATIONS`/`data_quality` 규칙, `_portfolio_limitations_for_market` KR 제외 revert → KR 강등·CNY 집계 원복.
- `portfolioFormat.ts` `formatMoney` 소수자릿수 헬퍼 revert → 2자리 하드코딩 원복.
- `docs/market-support.md` `:44` 및 CHANGELOG 항목 revert.
- 본 커밋 전체 revert로 일괄 원복. 스토리지·마이그레이션 무변경이라 데이터 롤백 불필요.

## 9. 구현 계획에서 확정할 항목

- 집계 통화 파생에서 "in-scope 계정" 집합의 정확한 경계(현재 활성/전체 계정 vs. 스냅샷 대상 필터)와 빈 집합·단일 계정 엣지의 처리.
- `INFORMATIONAL_LIMITATIONS` 상수의 배치 위치·네이밍, `data_quality` 3개 사이트의 공통 헬퍼 추출 여부.
- 통화→소수자릿수 테이블의 Python 측 배치(report/notification 공통 유틸 vs. 개별) 및 web 미러의 export 형태.
- `docs/market-support.md` `:44` 재확인 및 정확한 KR 프로즈 문구(구현 시 최신 라인 검증).
- CHANGELOG 항목 타입(`改进` vs `修复`) 최종 결정.
- (후속 옵션, 이 스펙 밖) 기존 KR 계정용 전용 백필 CLI 도구 필요성 판단.

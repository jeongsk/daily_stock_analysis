# KR FX 페어 + 실시간 시세 데이터 품질 실측

- **날짜**: 2026-07-12
- **맥락**: wayfinder map #4 (KR 포트폴리오 KRW 손익 정확도 스펙), 티켓 #5
- **방법**: 저장소 코드 경로 그대로 — FX는 `portfolio_service._fetch_fx_rate_from_yfinance` 로직(`{from}{to}=X`, 7일 윈도우 마지막 Close), 시세는 `DataFetcherManager.get_realtime_quote`. 실 네트워크 호출(yfinance).

## 1. FX 페어 (yfinance `{from}{to}=X`)

| 페어 | 결과 | 값 | 반환 행수 |
|---|---|---|---|
| `KRWCNY=X` | OK | 0.004484 | 6 |
| `CNYKRW=X` | OK | 221.252 | 1 |
| `KRWUSD=X` | OK | 0.000667169 | 5 |
| `USDKRW=X` | OK | 1498.87 | 5 |
| `USDCNY=X` | OK | 6.7667 | 6 |
| `KRWJPY=X` | OK | 0.107795 | 5 |

**역수 일관성**
- `KRWUSD=X × USDKRW=X = 1.000000` — 완벽 일치.
- `KRWCNY=X × CNYKRW=X = 0.992094` — 약 0.8% 편차. 원인: `CNYKRW=X`가 윈도우에서 **1행만** 반환(얇은/비유동 방향 시계열). 유동적인 방향(`KRWCNY=X`, `USDKRW=X`)이 더 조밀·신뢰.

**함의**: KRW↔CNY, KRW↔USD 모두 직접 페어 가용. USD-크로스가 가장 견고. 계정 base_currency가 무엇이든(KRW/CNY/USD) 필요한 FX 페어를 yfinance에서 얻을 수 있음 — **신규 FX 소스 불필요**(기존 경로로 충분). 단, 얇은 방향 페어의 편차·행수 부족은 존재하므로 스펙에서 크로스 경로(예: KRW→USD→base) 또는 방향 선택을 고려할 여지.

## 2. KR 실시간 시세 (`get_realtime_quote`, YfinanceFetcher 라우팅)

| 종목 | price | currency | market | data_quality | source | missing_fields | change_pct |
|---|---|---|---|---|---|---|---|
| `005930.KS` (삼성전자, KOSPI) | 285000.0 | **KRW** | kr | partial | FALLBACK | `amount`, `pe_ratio`, `pb_ratio` | 2.52 |
| `247540.KQ` (에코프로비엠, KOSDAQ) | 121600.0 | **KRW** | kr | partial | FALLBACK | `amount`, `pe_ratio`, `pb_ratio` | 9.06 |

**핵심**: 두 종목 모두 **평가에 필요한 필드(price, currency=KRW)는 정상 반환**. `data_quality=partial`은 밸류에이션 입력이 아니라 **애널리틱스 필드(`amount`=거래대금, `pe_ratio`, `pb_ratio`) 결핍** 때문. 즉 KRW 원가·평가액·손익 산출에 필요한 데이터는 신뢰 가능하게 존재하며, partial 라벨은 손익 정확도와 무관한 축에서 붙는다.

## 3. 결론 (티켓 #6 입력)

1. **FX**: KRW 관련 페어는 yfinance만으로 충분히 가용. 신규 전용 FX 소스 불필요(map Out of scope와 일치). 얇은 역방향 페어의 편차는 크로스/방향 선택으로 완화 가능 — 스펙에서 다룰 소재.
2. **시세**: KR .KS/.KQ 실시간 시세는 price+currency(KRW)를 안정적으로 제공. 손익 산출 관점에서 best-effort로 남는 실질적 결핍 **없음**.
3. **`fx_and_cost_basis_partial` 재평가**: 이 강등은 밸류에이션 데이터 부재를 시사하지만, 실측상 밸류에이션 입력은 온전. 따라서 KR에서 `fx_and_cost_basis_partial`은 **과보수적**이며 제거 후보. 단, quote 레벨의 `data_quality=partial`(애널리틱스 결핍)과 포트폴리오 레벨의 손익 정확도 라벨은 **분리해서** 다뤄야 한다(#6에서 결정).

**주의/한계**: 단일 시점(2026-07-12) 실측. 장중/휴장 시점, 상장폐지·거래정지 종목, 소형주에서의 결측은 별도 확인 대상. FX 얇은 방향 페어의 행수 부족은 재현될 수 있음.

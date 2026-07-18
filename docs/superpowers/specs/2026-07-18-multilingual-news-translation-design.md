# 다국어 뉴스 확장 + 한국어 보고서 뉴스 번역 — 설계 스펙 초안

- 작성일: 2026-07-18
- 상태: **개정 v2 (사용자 승인: pool→카드 병합 포함) — 사용자 설계 리뷰 대기**
- 관련 영역: intelligence source 템플릿(`src/services/intelligence_service.py`) · 역사 뉴스 API(`api/v1/endpoints/history.py`, `src/services/history_service.py`) · intelligence pool 조회(`src/repositories/intelligence_repo.py`) · API 스키마(`api/v1/schemas/history.py`) · Web 뉴스 패널(`apps/dsa-web/src/components/report/ReportNews.tsx`) · 번역 캐시 스토리지(`src/storage.py`)
- 선행 컨텍스트: 코드 실측 2026-07-18 (main HEAD, intelligence baseline #1708/#1709, ko i18n 완료)
- 관련 문서: `docs/intelligence-sources.md`(소스 라이프사이클·SSRF 가드·자동 수집), `AGENTS.md`(fail-open·additive 필드·변경문서화), `src/report_language.py`(언어 감지·no-Chinese-in-ko 게이트)

> **v2 변경 요약**: 사용자가 "새로 수집된 RSS `intelligence_items`를 분석 입력 전용이 아니라 **보고서 뉴스 카드에도 노출**"을 명시 승인함. 이에 따라 D3/D8, 데이터 흐름, 컴포넌트, API 스키마, dedup/랭킹/cap, 테스트, 단계, 리스크, 비범위, 미결정을 전면 개정. 번역/캐시/LLM/소스확장 메커니즘은 v1과 동일.

## 0. 사용자 승인 요구사항 (변경 불가 범위)

본 설계는 아래 사용자 확정 요구사항을 하드 제약으로 삼는다. 이 범위를 벗어나는 결정은 본 문서에 명시하고 사용자 재확정 대상으로 표기한다.

1. 무료/공개 RSS 또는 API 만 사용. 유료 키 기반 번역 API(DeepL Pro, Google Translate API 등)는 도입하지 않는다.
2. 소스를 현재 중국어 편중 기본값을 넘어 **영어권·한국어권 지역**으로 확장.
3. **한국어(ko) 보고서**의 경우, 한국어가 아닌 모든 뉴스 title/snippet을 한국어로 번역.
4. 번역된 한국어를 **먼저** 보여주고 원본 title/snippet을 **함께** 표시.
5. 이미 한국어인 항목은 원본을 **중복해서 복제하지 않는다**.
6. 번역 실패는 **fail-open**: 원본 + 명시적인 unavailable 상태를 함께 반환.
7. 권장 평가/지정 기준선 소스(2026-07-18 HTTP 200 XML/RSS 라이브 확인):
   - 한국어: 연합뉴스 경제 `https://www.yna.co.kr/rss/economy.xml`, 한국은행 전체 보도자료 `https://www.bok.or.kr/portal/bbs/B0000552/news.rss?menuNo=200690`
   - 영어: Federal Reserve 전체 보도자료 `https://www.federalreserve.gov/feeds/press_all.xml`, Nasdaq Stocks `https://www.nasdaq.com/feed/rssoutbound?category=Stocks`
   - 기존 유지: SEC `sec-company-news`, MarketWatch `global-marketwatch`, HKEX `hkex-news`, NewsNow 기본 소스(cls-hot / xueqiu-hotstock / wallstreetcn-quick / jin10 / gelonghui)
8. **[v2 신규]** 새로 수집된 RSS `intelligence_items`는 분석 입력 전용이 아니라 **보고서 뉴스 카드에도 노출**한다. 단, 광역 피드(Fed/Nasdaq/MarketWatch/BOK/Yonhap)가 종목 특정 뉴스를 압도하지 않도록 deterministic 한 정합·랭킹·cap·fail-open을 갖춘다.

## 1. 개요와 목표

현재 시스템은 두 가지 **서로 다른** 뉴스 데이터 저장을 가진다. v1에서는 카드가 `news_intel`만 읽었으나, v2에서는 **두 저장을 병합**해 카드에 노출한다. 두 저장의 의미를 먼저 확정한다.

### 1.1 저장소 현실 (코드 실측 기반)

| 테이블 | 채널 | 생성 주체 | 카드 노출 |
|---|---|---|---|
| `news_intel` | 검색 기반 뉴스(`query_id` 단위) | `search_service` → `db.save_news_intel`(pipeline에서 검색 후 저장) | **v1부터 카드 단일 소스**(`GET /api/v1/history/{record_id}/news` → `HistoryService.get_news_intel` → `db.get_news_intel_by_query_id` + `_fallback_news_by_analysis_context`). |
| `intelligence_items` | RSS/Atom/NewsNow 풀(`source_id`+`scope` 단위) | `IntelligenceService.fetch_source` / 자동 수집 | **v2부터 카드에 병합 노출**(기존엔 분석 입력 `news_context` 전용). 병합 규칙은 §5·§7. |

- 두 테이블 모두 `url`/`source`/`published_at`(또는 `published_date`)/`title` + (`snippet`|`summary`)를 가지므로, **동일 정규화 스키마로 병합** 가능. `IntelligenceItem`은 추가로 `source_type`(rss/newsnow)·`scope_type`·`scope_value`·`market`·`source_name`을 가져 provenance/정합에 사용.
- `IntelligenceRepository.list_items(scope_type=, scope_value=, market=, query=, days=, published_days=, page=, page_size=)`가 존재하나, `days`/`published_days`는 `datetime.now()` 기준(코드 실측). **역사 보고서**는 `record.created_at` 기준 시창이 필요하므로, 본 설계는 `created_at` 기준 explicit 시간 하한/상한을 받는 조회를 추가한다(§4.2, 신규 repo 헬퍼 또는 service 단 필터).

### 1.2 세 개의 목표 (v2: 병합 추가)

- **목표 A — 소스 확장(분석 입력 + 카드 풀 다양화)**: `intelligence_items` 풀에 한국어권·영어권 공개 RSS 소스를 additive 하게 추가. 이 풀은 분석 입력 `news_context`와 **카드 병합** 양쪽에 모두 공급된다(v2).
- **목표 B1 — 카드 병합(직접 news_intel + 관련 intelligence_items)**: 선택된 역사 보고서의 종목 코드/시장/분석 시각을 기준으로 직접 뉴스(쿼리 연결)와 관련 풀 항목을 **deterministic** 하게 병합. 직접 뉴스 우선, 광역 피드 억제 cap/ranking/dedup 포함, 풀 조회 실패는 fail-open.
- **목표 B2 — 한국어 보고서 뉴스 카드 번역**: 병합된 항목(출처 무관)에 대해 ko 보고서만 비한국어 title/snippet을 한국어로 번역. 원본을 함께 표시, 한국어 항목은 중복 제외, 실패 시 원본 + unavailable로 fail-open.

### 1.3 성공 기준

- 목표 A: 새 RSS 소스 4종이 built-in 템플릿으로 등록, 자동 수집 모드에서 기존과 동일한 fail-open 라이프사이클로 수집·보존.
- 목표 B1: 카드가 (a) 직접 `news_intel`을 우선 포함 (b) 동일 보고서의 종목/시장·시창에 해당하는 `intelligence_items`를 추가 병합 (c) URL/콘텐츠 dedup (d) per-source·per-pool cap으로 광역 피드 억제 (e) 안정적 순서(direct → symbol-scope → market-scope, 분석시각 근접순) (f) 각 항목 provenance/source 메타데이터 포함. 풀 조회 실패 시 직접 뉴스만 반환(fail-open), 500 아님.
- 목표 B2: ko 보고서 카드가 병합 항목 전체에 대해 (a) 비한국어는 번역 우선 + 원본 (b) 한국어는 원본만(중복 없음) (c) 실패는 원본 + unavailable. zh/en 보고서 응답은 현행 + additive 필드(번역은 skipped, 병합 메타데이터는 포함).
- 공통: API는 additive 필드만 추가(기존 `title`/`snippet`/`url` 의미·위치 불변). 신규 provider/model/base URL/인증 없이 기존 `GenerationBackend` 재사용. 신규 필수 환경변수 없음(신규 설정은 opt-in 기본값 안전).

### 1.4 명시적 비범위 (YAGNI)

- zh/en 보고서의 뉴스 카드 **번역** 안 함(번역은 ko 타깃 전용). 단, zh/en 보고서에서도 **카드 병합(풀 항목 노출)은 수행**한다(번역만 스킵).
- 보고서 본문(analysis_summary/markdown/dashboard 등) 번역 안 함 — 본문은 이미 LLM이 보고서 언어로 생성.
- **ML/점수 기반 relevance 랭킹 모델 도입 안 함** — 랭킹은 deterministic 티어(provenance/scope) + 시간 근접도 + id 만 사용(§7, §D18). "최소 구현 가능 계약" 우선.
- 번역 결과의 스트리밍/부분 렌더 없음 — 동기 배치 후 일괄 반환.
- 언어 자동 감지 결과의 사용자 수동 오버라이드 UI 없음.
- 번역 품질 A/B 평가·역번역 검증 파이프라인 없음 — 품질은 prompt 제약 + 원본 보존 + no-Chinese-in-ko 게이트로 방어.
- NewsNow 계열 외부 인스턴스 신규 구축/교체 없음 — `NEWSNOW_BASE_URL` 계약 불변.
- 카드에 **실시간**(요청 시점) 풀 재수집 트리거 없음 — 카드는 저장된 풀을 읽기만. 수집은 기존 intelligence 자동/수동 수집 경로에 위임.

## 2. 확정할 설계 결정 (권장안)

| # | 결정 | 권장값 | 근거 |
|---|---|---|---|
| D1 | 소스 확장 형태 | `_BUILTIN_SOURCE_TEMPLATES`에 RSS 항목 4종 **additive 추가**(기존 3종·NewsNow 5종 불변) | 기존 템플릿/생성/테스트/수집 라이프사이클 1:1 재사용 |
| D2 | 신규 소스 기본 활성화 | 템플릿 자체는 enabled 미명시; `create_default_sources` 기본 `enabled=False`; `ensure_default_sources_enabled`(auto mode)가 True 보장 | AGENTS.md "미설정 시 안전한 기본값"; 기존 SEC/HKEX/MarketWatch와 동일 opt-in |
| D3 | **[v2 개정]** 카드 병합 범위 | **직접 `news_intel`(query_id 연결) + 관련 `intelligence_items`(풀) 병합 노출. 직접 우선.** 병합은 stock·market_review 양쪽 모두 적용 | 사용자 승인(요구사항 8); 카드 단일 소스 한계 제거; 번역 계층은 콘텐츠 기반이라 두 저장을 동일 파이프라인으로 처리 |
| D4 | 번역 트리거 | `GET /{record_id}/news` 응답 시점 lazy 동기 배치, **`report_language=ko`일 때만**(병합 후) | zh/en 응답 바이트 불변(옵션 필드 제외); 동기 def 엔드포인트 |
| D5 | 번역 타깃 언어 해석 | **서비스(`HistoryService`)가 record에서 `report_language` 해석**(`normalize_report_language` 직접 사용 — 이미 import됨), query param 미추가, 엔드포인트 시그니처 불변 | 엔드포인트는 raw_result/context_snapshot을 들고 있지 않아 재해석이 중복; `_extract_report_language`는 api 계층 헬퍼라 src→api 역참조 회피 |
| D6 | 캐시 키 | 정규화된 원본(title+snippet 페어) 해시 + target_language; 페어 단위 1 행 | 페어 캐싱이 호출·행 수 최소; 정규화로 whitespace/대소문자 차이 hit |
| D7 | 캐시 저장소 | 신규 additive 테이블 `news_translation_cache` + SQLite 비파괴 마이그레이션(`_ensure_news_translation_cache_schema`, 기존 `_ensure_decision_signal_outcome_attribution_schema` 패턴) | 범용 cache 테이블 부재(실측); 안전한 additive 패턴 |
| D8 | **[v2 개정]** 병합 활성화 | **기본 ON, fail-open**(풀 조회 실패/빈 결과면 직접 뉴스만 반환). 비활성화 옵션은 `NEWS_CARD_MERGE_INTEL_ENABLED`(기본 true, opt-out) | 사용자 승인(요구사항 8); fail-open으로 가용성 보존; opt-out으로 환경별 회피 가능(AGENTS.md: 신규 설정은 `.env.example` 동기) |
| D9 | 언어 감지 | 기존 `detect_report_script_mismatch`/Hangul·Hanzi 카운터 재사용; 항목별 source_language 산출(ko/zh/en/mixed/unknown) | 신규 감지기 평행 구현 금지 |
| D10 | LLM 백엔드 | 기존 `resolve_generation_backend_id(config)` + `create_generation_backend`(fallback 포함) 재사용; 신규 provider/model/base URL/인증 **없음** | AGENTS.md "기존 모듈 재사용" |
| D11 | API 필드 전략 | `NewsIntelItem`에 additive optional: 번역 4종(`original_title`/`original_snippet`/`translation_status`/`source_language`) + 병합 메타데이터(`provenance`/`source`/`source_type`/`published_at`); 기존 `title`/`snippet`/`url`은 표시용(번역 우선) | 기존 클라이언트는 기존 필드만 소비; 병합/번역 정보는 추가 |
| D12 | 실패 처리 | timeout·malformed·스키마 위반·Hanzi 누출 → per-item `translation_status=unavailable`, 표시용 `title`/`snippet`은 원본 유지(fail-open); 풀 조회 실패 → 직접 뉴스만(병합 fail-open) | 요구사항 6; intelligence fetch fail-open 원칙과 일치 |
| **D13** | **[v2 신규] 정합 — stock 보고서** | `market = _infer_market(code)`(cn/hk/us/jp/kr/tw), `symbol = canonical_stock_code(code)`. 풀 정합: `scope_type="symbol", scope_value=symbol`(종목 특정) **및** `scope_type="market", market=market`(시장 맥락). symbol-scope이 market-scope보다 상위 티어 | `_infer_market`은 `decision_signal_reassess_service` 기존 헬퍼(재사용); IntelligenceItem.market 어휘와 일치 |
| **D14** | **[v2 신규] 정합 — market_review 보고서** | `report_type=="market_review"`. 풀 정합: `scope_type="market"` for 리뷰 region(들). region은 record의 market_review payload market key들에서 파생(있을 때); 불가 시 read-time `market_review_region` config 기본값(cn) best-effort; 그래도 불가 시 풀 스킵(직접 뉴스만, fail-open) | market_review는 `code="MARKET"`라 `_infer_market` 무의미; region은 `_resolve_market_review_regions` 산물이나 record에 명시 컬럼 없어 payload 파생 |
| **D15** | **[v2 신규] 역사 시창** | **`record.created_at` 기준**(now 아님). 풀 항목 `published_at`(미존재 시 `fetched_at`) ∈ `[created_at − lookback, created_at + forward]`. `lookback` = `resolve_news_window_days(...)` anchor 분석일(기본 3일), `forward` = +1 calendar day. 기존 `_fallback_news_by_analysis_context` 시창(±6h fetched + 분석일 published 창)과 동일 원칙 | 역사 보고서는 과거 시점 맥락 보존이 목적; `list_items`의 now-기준 `days`/`published_days`로는 재현 불가 → explicit 시간 경계 조회 추가(D16) |
| **D16** | **[v2 신규] 풀 조회 구현** | 신규 `IntelligenceRepository.list_items_for_report(*, scope_type, scope_value=None, market=None, start_at, end_at, limit)` — explicit `start_at`/`end_at`(published_at coalesce fetched_at) 경계. 기존 `list_items`는 now-기준이라 재사용 불가(시맨틱 충돌); 평행 메서드 추가 | minimal additive; 기존 `list_items` 호출자(무결); SQL은 `coalesce(published_at, fetched_at) between start_at and end_at` |
| **D17** | **[v2 신규] 교차 저장 dedup** | (1) canonical URL 정규화(트레일링 슬래시·scheme·query 정렬·fragment 제거) 동일 시 1건, (2) 정규화 title 해시 동일 시 1건. **동률 시 직접 news_intel이 풀을 이김**(직접 우선). URL이 `no-url:intel:*` placeholder면 title 해시만 적용 | 두 저장이 동일 기사를 중복 인용(예: 연합→검색재인용)하는 것 방지; 직접 우선 원칙 유지 |
| **D18** | **[v2 신규] 랭킹(deterministic)** | 티어: T1 직접 news_intel → T2 symbol-scope 풀 → T3 market-scope 풀. 티어 내: `|published_at − created_at|` asc(분석 시각에 가까울수록 상위) → `id` asc tiebreak. **점수/ML 모델 없음** | 결정적·재현 가능·설명 가능; 광역 market 피드가 symbol 증거를 덮지 않음; 비범위(ML) 존중 |
| **D19** | **[v2 신규] cap 구조** | (a) **per-source cap**(기본 3): 단일 피드(Fed/Nasdaq/MarketWatch/BOK/Yonhap/NewsNow 등)가 카드를 독점 못 함. (b) **per-pool market-scope cap**(기본 6): T3 총량 제한. (c) **direct reserve**: 직접 뉴스가 먼저 `limit`를 채우되, 풀은 직접이 채운 만큼을 제외한 잔여 슬롯만. 총 반환 = `limit`(기본 8, 최대 100). cap은 상수(설정 비노출 권장, §18) | 광역 피드 swamping 방지(요구사항 8); 직접 우선 보장; 최소 설정 |
| **D20** | **[v2 신규] provenance/메타데이터** | 각 병합 항목: `provenance`("direct"\|"pool") · `source`(feed/검색 provider 명, 기존 `source`/`provider` 컬럼에서) · `source_type`("rss"\|"newsnow"\|"search" — 풀은 IntelligenceItem.source_type, 직접은 고정 "search") · `published_at`(ISO, 표시용). scope_type/market은 공개 API에서 생략(내부 정합용, minimal contract) | 출처 투명성; UI provenance 라벨; minimal public 표면 |

> Draft "preferred architecture" 대비: lazy batch server-side translation(ko only)·persistent cache(normalized hash+lang)·additive API fields·unchanged zh/en·fail-open·translation-first UI는 v1과 동일하게 유지. v2에서 (a) 카드 병합을 scope-in 하고 (b) deterministic 한 정합/랭킹/cap/dedup/provenance를 추가하며 (c) 캐시를 페어 해시로 구체화한 것이 정합 보강.

## 3. 아키텍처와 데이터 흐름

### 3.1 목표 A — 소스 확장 (수집 → 분석 입력 + 카드 풀)

```
[Built-in 템플릿]  _BUILTIN_SOURCE_TEMPLATES += 4종 RSS (yna-economy / bok-press / fed-press / nasdaq-stocks)
      │
[IntelligenceService.list_source_templates / create_source_from_template / create_default_sources]  (기존 흐름 1:1)
      │
NEWS_INTEL_AUTO_FETCH_ENABLED=true → ensure_default_sources_enabled (누락 생성 + 활성화)
      │
fetch_enabled_sources → fail-open per-source fetch → intelligence_items (URL+scope dedup, 기존 uix)
      │
      ├─→ [분석 pipeline] symbol→market scope 순 news_context 병합 (기존 경로, LLM 현지화)
      └─→ [v2 카드 병합] report 시창/정합으로 읽기 (§3.2)
```

### 3.2 목표 B1+B2 — 카드 병합 + 한국어 번역 (v2)

```
GET /api/v1/history/{record_id}/news?limit=N   (동기 def → threadpool)
      │
HistoryService.resolve_and_get_news(record_id, limit)
      │   └─ record 해석(_resolve_record) → query_id / code / report_type / created_at
      │   └─ report_language 파생(normalize_report_language 직접 사용)
      │
[병합 단계 — NewsMergeService 또는 HistoryService 헬퍼]
      ├─ (T1) 직접: db.get_news_intel_by_query_id(query_id) + _fallback_news_by_analysis_context (기존)
      │        → provenance="direct", source_type="search"
      ├─ (T2/T3) 풀: NewsCardMergeEnabled? → IntelligenceRepository.list_items_for_report(...)
      │     • stock: _infer_market(code) → symbol-scope(T2) + market-scope(T3)
      │     • market_review: report region(들) → market-scope(T3)
      │     • 시창: published_at ∈ [created_at − lookback, created_at + forward]  (now 아님)
      │     → provenance="pool", source_type=item.source_type(rss/newsnow)
      │     풀 조회 예외 → 스킵(fail-open, 직접만 유지)
      ├─ 교차 저장 dedup: canonical URL → title 해시; 동률 시 직접 우선 (D17)
      ├─ 랭킹: T1→T2→T3, 티어 내 |published_at−created_at| asc, id asc (D18)
      ├─ cap: per-source(3) · per-pool-market(6) · direct-reserve · total=limit (D19)
      └─ 메타데이터 부여: provenance/source/source_type/published_at (D20)
      │
[번역 단계 — NewsTranslationService.translate_items(items, target=report_language)]
      │   report_language != "ko" → 각 항목 translation_status="skipped", 원본 (zh/en 응답 바이트 불변, 병합 메타데이터는 유지)
      │   report_language == "ko" → per-item 언어 감지 → ko: "original"(중복 없음) / 비ko: 캐시→배치 LLM→검증
      │     실패 → "unavailable" + 원본 유지 (fail-open)
      │
API 응답: NewsIntelItem { title(번역우선), snippet(번역우선), url,
                          original_title?, original_snippet?, translation_status?, source_language?,
                          provenance?, source?, source_type?, published_at? }
      │
[Web] ReportNews.tsx → translation-first + muted 원본 + unavailable 배지 + provenance 라벨
```

핵심 원칙:

- **병합은 읽기 경로 후처리** — 저장 데이터 의미 불변. 풀 조회는 별도 세션/트랜잭션, 실패해도 직접 뉴스 응답을 보존(fail-open).
- **직접 우선 + deterministic 랭킹** — ML 점수 없이 티어/시간/id 만으로 재현 가능 순서. 광역 피드는 per-source cap으로 억제.
- **역사 시창은 created_at 기준** — `now()` 기준이면 역사 보고서가 최신 풀만 보게 됨. 기존 `_fallback_news_by_analysis_context`의 분석일 anchor 원칙을 풀에까지 확장.
- **번역은 병합 결과 전체에 적용** — 출처(direct/pool) 무관, 동일 캐시/파이프라인(ko only).

## 4. 컴포넌트 (파일별 책임)

### 4.1 목표 A — 소스 확장

#### 수정 — `src/services/intelligence_service.py`

- `_BUILTIN_SOURCE_TEMPLATES`에 RSS 항목 4종 **append**(기존 3종 순서·내용 불변):

```python
{
    "template_id": "kr-yna-economy",
    "name": "Yonhap Economy (연합뉴스 경제)",
    "source_type": "rss",
    "url": "https://www.yna.co.kr/rss/economy.xml",
    "scope_type": "market",
    "market": "kr",
    "description": "Public Yonhap economy RSS for Korean market evidence. Test before enabling.",
},
{
    "template_id": "kr-bok-press",
    "name": "Bank of Korea Press Releases (한국은행 보도자료)",
    "source_type": "rss",
    "url": "https://www.bok.or.kr/portal/bbs/B0000552/news.rss?menuNo=200690",
    "scope_type": "market",
    "market": "kr",
    "description": "Bank of Korea all press releases RSS for Korean macro evidence. Test before enabling.",
},
{
    "template_id": "us-fed-press",
    "name": "Federal Reserve All Press Releases",
    "source_type": "rss",
    "url": "https://www.federalreserve.gov/feeds/press_all.xml",
    "scope_type": "market",
    "market": "us",
    "description": "Federal Reserve all press releases RSS for US macro evidence. Test before enabling.",
},
{
    "template_id": "us-nasdaq-stocks",
    "name": "Nasdaq Stocks Feed",
    "source_type": "rss",
    "url": "https://www.nasdaq.com/feed/rssoutbound?category=Stocks",
    "scope_type": "market",
    "market": "us",
    "description": "Nasdaq Stocks RSS for US market context. Test before enabling.",
},
```

- 그 외 intelligence_service 코드 변경 **없음** — `create_source_from_template` / `create_default_sources` / `ensure_default_sources_enabled` / `_validate_url`(SSRF·DNS·proxy·redirect 가드) / `_fetch_feed_entries` / `_parse_feed` / `upsert_items` / retention가 신규 RSS를 그대로 처리.
- `market` 값 `"kr"`/`"us"`는 이미 `_ALLOWED_MARKETS = {"cn","hk","us","jp","kr","tw","global"}`에 포함(실측). 신규 enum 불필요.

> 정합 메모: draft는 Fed/Nasdaq을 `market=us/global`로 언급했으나 스키마는 단일 `market` 값만 허용. 본 설계는 **각각 `market="us"`**로 지정. `global` 커버리지 필요 시 운영자가 `POST /sources`로 동일 URL `market=global` 복제 생성 가능(기존 `test_same_url_can_be_saved_for_different_scopes` 계약). 다중 market 태그는 비범위.

### 4.2 목표 B1 — 카드 병합

#### 신규 — `src/repositories/intelligence_repo.py`: `list_items_for_report`

```python
def list_items_for_report(
    self,
    *,
    scope_type: str,
    scope_value: Optional[str] = None,
    market: Optional[str] = None,
    start_at: datetime,          # published_at coalesce fetched_at 하한 (created_at - lookback)
    end_at: datetime,            # 상한 (created_at + forward)
    limit: int = 50,
) -> List[IntelligenceItem]:
    # where: scope_type(필수) · scope_value(정규화) · market ·
    #        coalesce(published_at, fetched_at) BETWEEN start_at AND end_at
    # order: |coalesce(published_at, fetched_at) - :center| asc 는 SQL 직접 어려우므로
    #        우선 desc(coalesce(published_at, fetched_at)), desc(id) 로 충분히 가져오고
    #        서비스 단에서 center 근접순 재정렬(±미세 차이는 무의미). limit 상한으로 과적 쿼리 방지.
```

- 기존 `list_items`는 `days`/`published_days`가 `now()` 기준이라 **재사용 불가**(시맨틱 충돌). 신규 메서드는 explicit 시간 경계만 받고 now를 참조하지 않는다.
- `scope_type`은 필수(symbol/market). `scope_value`는 `_normalize_scope_value` 경유(기존 헬퍼 재사용). `market`은 단일 값(스키마 제약).
- 정렬은 DB에서 최근순으로 충분히 가져오고 서비스가 `|published_at − created_at|` asc로 재정렬(D18). SQL 절대거리 정렬은 엔진 의존적이라 회피.

#### 신규 — `src/services/news_merge_service.py` (또는 `HistoryService` 내 헬퍼)

```
class NewsCardMerger:
    def __init__(self, intel_repo=None, config=None): ...
    def merge_for_report(self, *, record, direct_items, limit) -> list[dict]:
        # 1) market/symbol/regions 파생 (D13/D14)
        #    stock: market=_infer_market(record.code); symbol=canonical_stock_code(record.code)
        #    market_review: report regions from record payload (best-effort) else default
        # 2) 시창 lookback/forward 계산 (D15) — resolve_news_window_days(anchor=분석일) + forward=+1d
        # 3) T2 symbol-scope 풀 조회 (stock only): list_items_for_report(scope_type="symbol", scope_value=symbol, market=market, ...)
        # 4) T3 market-scope 풀 조회: list_items_for_report(scope_type="market", market=market or regions, ...)
        # 5) 정규화 스키마로 변환(provenance="pool", source=item.source_name or item.source, source_type=item.source_type, published_at=...)
        # 6) 교차 저장 dedup(direct ∪ pool): canonical URL → title 해시; 동률 direct 우선 (D17)
        # 7) 랭킹 T1→T2→T3, 티어 내 |published_at−created_at| asc, id asc (D18)
        # 8) cap 적용: per-source(3)·per-pool-market(6)·direct-reserve·total=limit (D19)
        # 모든 예외 로그 후 direct_items 만 반환 (fail-open)
```

- `_infer_market`은 `src/services/decision_signal_reassess_service._infer_market`을 재사용(또는 동일 로직을 공유 유틸로 추출 — 평행 구현 금지). `canonical_stock_code`는 `data_provider.base` 재사용.
- market_review region 파생: record의 `raw_result`/`context_snapshot`에 market_review_payload의 market key들이 있으면 그것들 사용; 불가 시 read-time `config.market_review_region`을 `_resolve_market_review_regions`로 전개(동일 헬퍼 재사용, 단 config는 현재 값이라 과거 region과 불일치 가능 — best-effort, 문서 명시); 그래도 불가 시 T3 스킵(fail-open).
- dedup 정규화: URL은 scheme 소문자화·host 소문자화·트레일링 슬래시 정규화·fragment 제거·대표 query 정렬(단순 구현); `no-url:intel:*` placeholder는 URL 비교에서 제외하고 title 해시만 사용. title 해시는 번역 캐시 정규화(NFKC·소문자·whitespace 단일화)와 동일 정규화 재사용(일관성).
- cap 상수: `_PER_SOURCE_CAP = 3`, `_PER_POOL_MARKET_CAP = 6`(권장, §18 튜닝 대상). direct reserve: 직접 뉴스 개수만큼 우선 할당 후 잔여 슬롯을 풀(T2 우선, 그 다음 T3)로 채움.

#### 수정 — `src/services/history_service.py`

- `resolve_and_get_news(record_id, limit)` 확장(이미 `_resolve_record`로 record 해석 — line 406):
  1. record에서 `report_language` 파생(`normalize_report_language(raw_result.get("report_language") or context_snapshot.get("report_language"))`, 기존 사용 패턴 line 610/861/919와 동일).
  2. 직접 뉴스 확보: 기존 `get_news_intel(query_id, limit)`(truncate 포함). provenance/source_type/source/published_at 부여(direct/search).
  3. **병합**: `config.news_card_merge_intel_enabled`(기본 true)이면 `NewsCardMerger.merge_for_report(record=record, direct_items=..., limit=limit)`. 병합 서비스 예외 시 직접 뉴스만 유지(fail-open, 로그).
  4. **번역**: ko인 경우만 `NewsTranslationService.translate_items(merged_items, target="ko")`. 번역 예외 시 원본 유지(fail-open).
  5. 반환: 병합+번역 적용된 dict 리스트.
- snippet 200자 truncate는 **원본 기준**(기존 `get_news_intel`) 유지 — 번역/병합 모두 잘린 원본을 입력으로(원본 truncate가 single source of truth).
- **레이어 일관성**: `_extract_report_language`는 api 계층 헬퍼라 서비스에서 import 시 src→api 역참조. 서비스는 이미 import한 `normalize_report_language`로 동일 시맨틱 직접 구현.
- 시창 `lookback` 계산에 `resolve_news_window_days` 재사용(기존 `_fallback_news_by_analysis_context`와 동일 헬퍼·동일 anchor 원칙).

#### 수정 — `api/v1/endpoints/history.py`

- `get_history_news`: **시그니처·라우트·응답 모델(`NewsIntelResponse`)·query param 전부 불변**. 서비스가 병합+번역+메타데이터를 모두 적용하므로 엔드포인트는 기존 `service.resolve_and_get_news(record_id, limit)` 호출만 유지. 응답 직렬화는 dict에서 additive 필드를 읽어 `NewsIntelItem`에 전달(미전달 시 pydantic 기본 None). 200/500 불변.

### 4.3 목표 B2 — 번역 (v1 동일, 병합 결과에 적용)

#### 신규 — `src/services/news_translation_service.py`

```
class NewsTranslationService:
    def __init__(self, repository=None, config=None, generation_backend=None): ...
    def translate_items(self, items: list[dict], target_language: str) -> list[dict]:
        # target != "ko" → translation_status="skipped", 원본 (병합 메타데이터 보존)
        # ko → per-item 언어 감지 → ko: "original" / 비ko: 캐시→배치 LLM→검증→unavailable fail-open
```

- **언어 감지**: `src/report_language.py`의 Hangul(U+AC00–U+D7A3)/Hanzi(U+4E00–U+9FFF) 카운터 재사용. 번역 판정용 임계값은 별도 상수(`_KO_RATIO_THRESHOLD = 0.15`), 기존 `detect_report_script_mismatch`(0.4)와 분리.
- **배치 prompt**(고정 system): 충실 번역·의역/요약 금지·JSON 구조/순서/`id` 보존·이미 한국어/빈 값은 그대로·JSON 배열만 출력. `response_validator`로 JSON·배열 길이·id 집합 일치 검증.
- **한국어 출력 검증**: `has_disallowed_report_script("ko", translated)`로 Hanzi 누출 시 해당 항목 `unavailable`(no-Chinese-in-ko 게이트 재사용).
- **백엔드**: `resolve_generation_backend_id(config)` → `create_generation_backend`. fallback 시맨틱은 config 기존 정의에 위임(신규 정책 없음). `audit_context={"feature":"news_translation","target_language":"ko","batch_size":N}`.
- **동시성**: 프로세스 내 `threading.Lock`(intelligence `_auto_fetch_condition` 패턴); DB unique(`content_hash`+`target_language`)로 다중 프로세스 멱등.
- **제한**: 배치 상한 20 초과 시 청크; generation_config는 기존 분석 기본값 재사용.

#### 수정 — `src/storage.py`

- 신규 모델 `NewsTranslationCache`(v1 동일):

```python
class NewsTranslationCache(Base):
    __tablename__ = "news_translation_cache"
    id = Column(Integer, primary_key=True, autoincrement=True)
    content_hash = Column(String(64), nullable=False, index=True)
    target_language = Column(String(8), nullable=False, index=True)
    source_language = Column(String(16), nullable=True)
    translated_title = Column(String(600), nullable=True)
    translated_snippet = Column(Text, nullable=True)
    translation_status = Column(String(16), nullable=False)   # translated/unavailable
    model_used = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)
    __table_args__ = (UniqueConstraint("content_hash","target_language",name="uix_news_translation_hash_lang"),)
```

- `_ensure_news_translation_cache_schema()` 추가(기존 `_ensure_decision_signal_outcome_attribution_schema` 패턴). `DatabaseManager.__init__`의 `_ensure_*` 목록에 추가. 비SQLite skip(기존 관례, 문서 명시).
- 기존 `news_intel`/`intelligence_items`/`intelligence_sources` 스키마·인덱스·unique 제약 **무변경**(병합은 읽기 전용).

### 4.4 API 스키마

#### 수정 — `api/v1/schemas/history.py`

- `NewsIntelItem`에 additive optional 필드 추가(기존 `title`/`snippet`/`url` 순서·필수 불변):

```python
class NewsIntelItem(BaseModel):
    title: str = Field(..., description="표시용 제목(ko 번역 성공 시 한국어, 그 외 원본)")
    snippet: str = Field("", description="표시용 요약(ko 번역 성공 시 한국어, 그 외 원본)")
    url: str = Field(..., description="뉴스 링크")
    # 번역(v1)
    original_title: Optional[str] = Field(None, description="원본 제목(번역 적용 시에만)")
    original_snippet: Optional[str] = Field(None, description="원본 요약(번역 적용 시에만)")
    translation_status: Optional[str] = Field(None, description="translated|unavailable|original|skipped")
    source_language: Optional[str] = Field(None, description="zh|en|ko|mixed|unknown (항목별 감지)")
    # 병합 메타데이터(v2)
    provenance: Optional[str] = Field(None, description="direct|pool (직접 검색 뉴스 vs intelligence 풀)")
    source: Optional[str] = Field(None, description="피드/검색 provider 명")
    source_type: Optional[str] = Field(None, description="rss|newsnow|search")
    published_at: Optional[str] = Field(None, description="발행 시각(ISO, 표시용)")
```

- `translation_status`: `translated`/`unavailable`/`original`(ko 항목)/`skipped`(non-ko).
- `provenance`: `direct`(news_intel)/`pool`(intelligence_items). `source_type`: 직접은 `"search"`, 풀은 IntelligenceItem.source_type(`rss`/`newsnow`).
- `NewsIntelResponse`·기타 스키마 무변경.

#### 수정 — `apps/dsa-web/src/types/analysis.ts`

- `NewsIntelItem`에 camelCase 필드 추가: `originalTitle?`/`originalSnippet?`/`translationStatus?`/`sourceLanguage?`/`provenance?`/`source?`/`sourceType?`/`publishedAt?`. `toCamelCase` 자동 변환.

#### 수정 — `apps/dsa-web/src/api/history.ts`

- `getNews`: 응답 매핑 변경 없음(additive 필드는 `toCamelCase` 통과). JSDoc 보충(선택).

#### 수정 — `apps/dsa-web/src/components/report/ReportNews.tsx`

- 렌더 로직 확장(`language` prop은 `ReportSummary`에서 `language={reportLanguage}`로 이미 전달):
  - **번역 분기**(v1): `translated`→주 블록(한국어) + muted 원본 블록; `original`→단일; `unavailable`→원본 + "번역 불가" 배지; `skipped`/`undefined`→단일(현행 호환).
  - **provenance 라벨**(v2 신규): `provenance === "pool"`일 때 작은 출처 칩(예: source/source_type 표시 — "RSS · Federal Reserve" / "NewsNow · cls-hot"). `provenance === "direct"`는 기존 `sourceText.sourceLabel` 유지. `NEWS_SOURCE_TEXT.{zh,en,ko}`에 provenance 라벨 문구 추가.
  - 접근성: 원본 블록·provenance 칩에 aria-label 명시.
- 빈 결과/로딩/에러/새로고침 동작 불변.

### 4.5 불변 (검증만)

- `search_service.format_intel_report`·`save_news_intel`·`get_news_intel_by_query_id`·`_fallback_news_by_analysis_context` — 직접 뉴스 수집/조회 계약 불변(병합은 조회 결과 후처리).
- 기존 `IntelligenceRepository.list_items` 시그니처·시맨틱(now-기준) — 무변경(신규 `list_items_for_report` 평행 추가).
- `intelligence_service` SSRF 가드·fail-open 수집·cooldown·retention — 신규 RSS에 동일 적용, 로직 무변경.
- `report_language.normalize_report_language`/`SUPPORTED_REPORT_LANGUAGES`/`detect_report_script_mismatch`/`has_disallowed_report_script` — 재사용, 시그니처 불변.
- `GenerationBackend` Protocol·`create_generation_backend`·`resolve_generation_backend_id`·fallback 래퍼 — 불변.
- 보고서 본문/markdown/대시보드/알림 렌더 — 무변경.
- NewsNow 계열(`_NEWSNOW_DEFAULT_SOURCE_DEFS`, `NEWSNOW_BASE_URL`) — 무변경.

## 5. 데이터 계약

### 5.1 intelligence source 템플릿 (목표 A)

기존 `_BUILTIN_SOURCE_TEMPLATES` 형식 1:1 준수. `GET /api/v1/intelligence/sources/templates?market=kr`/`?market=us`에 신규 항목 등장(additive). `POST /sources/defaults` 시 신규 소스도 idempotent 생성.

### 5.2 번역 캐시 (`news_translation_cache`) — v1 동일

```jsonc
{
  "content_hash": "<sha256 hex of normalized(title + '\n\n' + snippet)>",
  "target_language": "ko",
  "source_language": "zh",
  "translated_title": "...",
  "translated_snippet": "...",
  "translation_status": "translated",
  "model_used": "<진단 표시 전용>",
  "created_at": "...", "updated_at": "..."
}
```

- `content_hash` 정규화: NFKC → 소문자 → whitespace 단일화 → strip. **병합 dedup의 title 해시 정규화와 동일 알고리즘 재사용**(일관성, 단일 진실).
- `unavailable`도 캐싱(반복 실패 LLM 호출 회피), 짧은 TTL(설정 가능 `NEWS_TRANSLATION_UNAVAILABLE_TTL_HOURS`, 기본 24h, opt-in). translated는 정적.
- unique(`content_hash`,`target_language`)로 멱등 upsert.

### 5.3 병합된 API 응답 (`NewsIntelItem`) — v2

ko stock 보고서, 직접 뉴스(번역 성공):

```json
{
  "title": "페더럴익스프레스, 실적 호조… 주가 상승",
  "snippet": "...",
  "url": "https://example.com/news/123",
  "original_title": "FedEx reports strong earnings...",
  "original_snippet": "...",
  "translation_status": "translated",
  "source_language": "en",
  "provenance": "direct",
  "source": "search",
  "source_type": "search",
  "published_at": "2026-07-17T10:00:00"
}
```

ko stock 보고서, 풀 항목(번역 성공, provenance=pool):

```json
{
  "title": "미 연준, 기준금리 동결… 인플레이션 경계 지속",
  "snippet": "연방공개시장위원회(FOMC)는 ...",
  "url": "https://www.federalreserve.gov/...",
  "original_title": "Federal Reserve holds rate steady amid inflation watch",
  "original_snippet": "The FOMC kept ...",
  "translation_status": "translated",
  "source_language": "en",
  "provenance": "pool",
  "source": "Federal Reserve All Press Releases",
  "source_type": "rss",
  "published_at": "2026-07-16T18:00:00"
}
```

ko 보고서, 한국어 풀 항목(연합뉴스, 중복 없음):

```json
{
  "title": "한국은행, 기준금리 3.0% 동결",
  "snippet": "...",
  "url": "https://www.bok.or.kr/...",
  "original_title": null,
  "original_snippet": null,
  "translation_status": "original",
  "source_language": "ko",
  "provenance": "pool",
  "source": "Bank of Korea Press Releases",
  "source_type": "rss",
  "published_at": "2026-07-17T09:00:00"
}
```

zh/en 보고서(번역 스킵, 병합은 수행):

```json
{
  "title": "Federal Reserve holds rate steady amid inflation watch",
  "snippet": "The FOMC kept ...",
  "url": "https://www.federalreserve.gov/...",
  "original_title": null,
  "original_snippet": null,
  "translation_status": "skipped",
  "source_language": "en",
  "provenance": "pool",
  "source": "Federal Reserve All Press Releases",
  "source_type": "rss",
  "published_at": "2026-07-16T18:00:00"
}
```

## 6. 소스 라이프사이클 / 기본 활성화 시맨틱

- **템플릿 등록 ≠ 자동 활성화**. 신규 4종은 `_BUILTIN_SOURCE_TEMPLATES`에만 존재. DB에 소스 행이 없으면 수집·카드 병합 대상 아님.
- **명시적 생성 경로**(기존): `POST /sources/templates/{template_id}`·`POST /sources/defaults`(기본 `enabled=false`)·`POST /sources`.
- **자동 모드(`NEWS_INTEL_AUTO_FETCH_ENABLED=true`)**에서만 `ensure_default_sources_enabled`가 (a) 누락 built-in 생성 후 `enabled=true`, (b) 기존 built-in 비활성 시 재활성화. 자동 모드 off(기본)면 템플릿만 존재, 수집 미발생 — 기존과 동일.
- **GitHub Actions daily workflow**: `00-daily-analysis.yml`이 env allowlist 매핑을 쓰므로, `NEWS_INTEL_AUTO_FETCH_ENABLED` 명시 매핑 없이는 자동 수집 미발생. 본 작업은 workflow 변경 안 함.
- **SSRF/보안**: 신규 4종 URL은 공개 호스트, 라이브(HTTP 200 XML, 2026-07-18). 기존 `_validate_url`(사설/루프백/링크로컬/예약/멀티캐스트 차단) + DNS 가드 + proxy 차단 + redirect 재검증 + 2MB 상한 + 5회 redirect 상한이 동일 적용. 단일 소스 실패는 fail-open.

## 7. 중복 / 관련성 / 균형 (v2: 카드 병합에 직접 적용)

### 7.1 수집 단계 dedup(기존, 무변경)

- `uix_intel_item_source_scope_url`(`source_id`+`url`+`scope_type`+`scope_value`+`market`)이 풀 내 중복 차단. URL 미존재 시 `no-url:intel:<hash>`. `news_intel`은 `uix_news_url`.

### 7.2 카드 병합 단계 정합·dedup·랭킹·cap (v2 신규)

- **정합(deterministic)**:
  - **stock 보고서**(D13): `market=_infer_market(code)`, `symbol=canonical_stock_code(code)`. T2 `scope_type="symbol", scope_value=symbol`; T3 `scope_type="market", market=market`.
  - **market_review 보고서**(D14): `scope_type="market"` for region(들). region 파생 순서: record market_review payload market keys → (불가 시) read-time `config.market_review_region` via `_resolve_market_review_regions` → (불가 시) T3 스킵(fail-open).
- **역사 시창**(D15): `published_at`(coalesce `fetched_at`) ∈ `[created_at − lookback, created_at + forward]`. `lookback` = `resolve_news_window_days(anchor=분석일)`(기본 3일), `forward` = +1 calendar day. **`now()` 미참조**(재현성).
- **교차 저장 dedup**(D17): canonical URL 정규화 동일 → 1건; 정규화 title 해시 동일 → 1건. 동률 시 **직접 news_intel 우선**. `no-url:intel:*` placeholder는 URL 비교 제외, title 해시만.
- **랭킹**(D18, deterministic·ML 없음): T1 direct → T2 symbol-scope pool → T3 market-scope pool. 티어 내 `|published_at − created_at|` asc → `id` asc.
- **cap**(D19): per-source(`_PER_SOURCE_CAP=3`) — 단일 피드 독점 방지(Fed/Nasdaq/MarketWatch/BOK/Yonhap/NewsNow 모두 동일 cap). per-pool-market(`_PER_POOL_MARKET_CAP=6`) — T3 총량 제한. direct reserve — 직접 뉴스가 우선 `limit`를 채우고 풀은 잔여 슬롯. 총 반환 = `limit`.
- **안정적 순서**: 동일 입력 → 동일 출력(시간 의존성 없음, now 미참조). 페이지네이션은 `limit` 단일 페이지(카드는 단발 조회).

### 7.3 광역 피드 swamping 방지 (요구사항 8 직접 처리)

- per-source cap(3)으로 Fed/Nasdaq/MarketWatch/BOK/Yonhap 각각이 카드의 최대 3건만 기여.
- T3 market-scope cap(6)으로 시장 맥락 총량 제한.
- direct reserve + T1 우선으로 종목 특정 직접 뉴스가 항상 최상단.
- T2 symbol-scope가 T3보다 상위라 종목 특정 풀 증거가 시장 광역 뉴스보다 먼저.
- 결과: 광역 피드가 종목 특정 결과를 압도할 수 없음(deterministic 보장).

## 8. 언어 감지

- per-item 감지에 `src/report_language.py`의 Hangul/Hanzi 카운터 재사용. 신규 감지 평행 구현 금지.
- 분류(권장): Hangul 비율 ≥ `_KO_RATIO_THRESHOLD`(0.15) → `source_language="ko"`, 번역 스킵(`status="original"`). Hanzi 우세 → `"zh"`; Latin 우세 → `"en"`; 혼합 → `"mixed"`. 빈/판정불가 → `"unknown"` → ko 보고서면 번역 시도.
- `detect_report_script_mismatch`(보고서 본문용, 0.4)와 번역 판정용 상수(`_KO_RATIO_THRESHOLD`) 분리. 단일 진실(상수 1곳 노출).

## 9. LLM 통합 (기존 백엔드 재사용, 신규 config 없음)

- `resolve_generation_backend_id(config)` + `create_generation_backend(backend_id)`. `GenerationBackend.generate(prompt, generation_config, system_prompt=, response_validator=, audit_context=)`.
- **신규 `.env`/provider/base URL/model/API key config 없음**. 번역은 기존 분석용 백엔드·키 사용(요구사항 1).
- **fallback**: 1차 실패 → config 정의 fallback 시도 → 둘 다 실패 시 per-item `unavailable` fail-open. 신규 fallback 정책/설정 없음.
- **배치 프롬프트**(고정 system, 권장):
  - system: `"You are a faithful news translator. Translate each JSON object's 'title' and 'snippet' into Korean. Preserve JSON structure, array order, and the 'id' field. Do not summarize, add, or omit content. If a field is already Korean or empty, return it unchanged. Output only a JSON array."`
  - user: `[{id, title, snippet}, ...]`
  - `response_validator`: JSON + 배열 길이 == 입력 + id 집합 일치. 위반 시 `GenerationError` → 전체 배치 per-item unavailable.
- **비용/지연**: 캐시 히트 우선; 배치 상한 20 초과 시 청크; `audit_context`로 배치 크기 기록; 번역 전용 짧은 timeout(서비스 상수, 설정 비노출 권장).

## 10. 캐시 지속성 / 마이그레이션 / 동시성

- **지속성**: `news_translation_cache`(additive). 재시작 후 유지.
- **마이그레이션**: `_ensure_news_translation_cache_schema()` 부팅 시 idempotent 생성. 비SQLite skip. 신규 테이블이라 기존 데이터 이동 불필요.
- **동시성**: 프로세스 내 `threading.Lock`(동시 배치 직렬화); DB unique로 다중 프로세스 멱등 upsert. `IntegrityError` 무시 후 select 회수(기존 intelligence 패턴).
- **무효화**: 자동 무효화 없음(번역은 원본 기반, 정적). unavailable만 짧은 TTL(`NEWS_TRANSLATION_UNAVAILABLE_TTL_HOURS`, 기본 24h, opt-in). translated는 명시적 API/스크립트로만 삭제(비범위).

## 11. 오류 처리 & 엣지 케이스

| 상황 | 처리 |
|---|---|
| record 미해석(record_id 무효) | 빈 리스트 200 반환(기존). 병합/번역 미개입. |
| stock 코드로 market 추론 불가(`_infer_market` None) | T2/T3 풀 스킵, 직접 뉴스만(fail-open). |
| market_review region 파생 불가 | T3 스킵, 직접 뉴스만(fail-open, D14). |
| 풀 조회 DB 오류 | 직접 뉴스만 반환(fail-open, 로그). 500 아님. |
| 풀 시창 내 항목 0건 | 직접 뉴스만 반환(정상, fail-open 아님). |
| report_language != ko | 병합은 수행, 번역만 skipped. zh/en 응답(옵션 필드 제외) 현행 + 병합 메타데이터. |
| 항목 자체 한국어 | `status="original"`, original_* 생략(중복 금지). |
| LLM timeout / malformed / 스키마 위반 | per-item `unavailable`, 원본 유지(fail-open). unavailable 캐시 TTL. |
| 번역 결과 Hanzi 누출 | `has_disallowed_report_script("ko", text)` → 해당 항목 `unavailable`. |
| 백엔드 미구성(`GenerationError` BACKEND_NOT_CONFIGURED) | 전체 배치 `unavailable`, 원본 유지. ko 카드는 원본 + "번역 불가". 500 아님. |
| 캐시 DB 오류 | 캐시 없이 번역 진행(캐시 오류가 번역·병합 막지 않음); 로그. |
| 신규 RSS fetch 실패 | 기존 intelligence fail-open: 해당 소스 `last_status=failed`, 다른 소스·분석·카드 영향 없음. |
| 신규 RSS redirect → 사설망 | 기존 `_validate_url` redirect 재검증 차단. |
| 빈 title/snippet | 감지 `unknown` → 빈 입력은 빈 번역 stub; 원본 빈값 그대로. |
| 동일 기사 direct+pool 중복 | 교차 저장 dedup(D17): direct 우선 1건만. |
| 광역 피드 다량 유입 | per-source cap(3) + per-pool-market cap(6) + direct reserve로 억제(D19). |
| 구 버전 클라이언트(additive 필드 미인지) | `title`/`snippet`/`url`만 소비 → 현행 동작(필드 optional). |
| 구 버전 백엔드 응답(필드 누락) | `ReportNews`는 undefined/skipped 시 단일 블록 렌더(현행 폴백). |
| `NEWS_CARD_MERGE_INTEL_ENABLED=false` | 병합 스킵, 직접 뉴스만(v1 동등). opt-out. |

## 12. 관측가능성 / 보안

- **관측**: `audit_context`로 번역 호출을 기존 `llm_usage_telemetry` 집계(`feature=news_translation`). 병합: direct/pool 비율, dedup 제거 건수, cap 도달 건수, 풀 조회 실패 건수 INFO 로그(PII 아님 — 공개 뉴스). `provenance`/`translation_status` 분포 디버그 로그.
- **보안**:
  - 번역·병합을 위한 신규 외부 HTTP **없음** — 기존 `GenerationBackend`(번역)·기존 DB(풀 조회)만. 번역 프롬프트엔 공개 뉴스 title/snippet만.
  - 캐시 테이블은 번역된 공개 뉴스 텍스트만 저장(비밀/PII 아님).
  - `_sanitize_error`(기존)로 오류 메시지 token/key/secret 탈의.
  - 신규 RSS URL은 기존 SSRF 가드 검증.
  - 병합은 URL 불변 — 사용자 클릭 시 원문 사이트로 이동(출처 투명성). provenance/source 라벨로 출처 명시.
- **품질 방어**: prompt 충실번역 제약 + 원본 보존 + Hanzi 누출 게이트. 역번역 검증 비범위.

## 13. 테스트 전략 (오프라인·결정적)

- **목표 A (intelligence)**:
  - `tests/test_intelligence_service.py`: `list_source_templates(market="kr")`/`"us"` 신규 항목; `create_source_from_template("kr-yna-economy", {"enabled": False})`; `create_default_sources` idempotent 신규 4종 포함; SSRF 가드 공개 호스트 통과/사설 변형 거부; RSS fixture 파싱 신규 템플릿 URL에서 기존 `_parse_feed` 경로.
  - 라이브 URL 검증은 `pytest -m network`(비블로킹) 보관.
- **목표 B1 (병합)**:
  - `list_items_for_report`: explicit 시간 경계 동작(now 미참조); scope_type/scope_value/market 필터; `coalesce(published_at, fetched_at) between start_at and end_at`; 빈 결과.
  - `NewsCardMerger.merge_for_report`:
    - stock 정합: `_infer_market`/`canonical_stock_code` 파생; T2 symbol-scope + T3 market-scope 조회.
    - market_review 정합: region 파생(payload 있음/불가/fail-open).
    - 시창: created_at 기준 lookback/forward; now-독립 재현성(같은 입력 같은 출력).
    - dedup: canonical URL 동일(직접 우선); title 해시 동일; `no-url:intel:*` placeholder.
    - 랭킹: T1→T2→T3; 티어 내 시간 근접순; id tiebreak.
    - cap: per-source(3) 도달 시 추가 차단; per-pool-market(6); direct reserve; total=limit.
    - fail-open: 풀 조회 예외 시 직접만; market 추론 불가 시 직접만; opt-out(`NEWS_CARD_MERGE_INTEL_ENABLED=false`) 시 직접만.
- **목표 B2 (번역)**: v1 동일 — 언어 감지 단위; ko 타깃 translated/original/unavailable; non-ko skipped; 캐시 hit/miss/upsert 멱등; unavailable TTL; 배치 매핑; 백엔드 미구성 → 전체 unavailable.
- **API/스키마**: `NewsIntelItem` additive(번역+병합) 필드 직렬화; zh/en skipped+병합 메타데이터; ko translated/original/unavailable+provenance. `tests/test_history_news_fallback.py` 확장(병합이 기존 fallback과 양립). pydantic 스키마 정합.
- **저장소/마이그레이션**: 신규 DB/기존 DB 양쪽에서 `news_translation_cache` 테이블·unique 인덱스; duplicate 무시; 비SQLite skip.
- **Web**: `ReportNews.test.tsx` — translated(이중)/original(단일)/unavailable(배지)/skipped+undefined(단일) + provenance 라벨(pool 칩/direct 기존 라벨) 렌더 스냅샷; i18n. `apps/dsa-web && npm run lint && npm run build`.
- **게이트**: `./scripts/ci_gate.sh` + `pytest -m "not network"` + Web lint/build.

## 14. 문서 / CHANGELOG

- `docs/intelligence-sources.md`: 신규 RSS 4종 추가 — 라이프사이클·SSRF·자동 수집 opt-in이 기존과 동일(확장). **v2: intelligence 풀이 이제 보고서 뉴스 카드에도 병합 노출됨을 명시**(병합 규칙은 본 설계 문서 참조). 중·영 동기화 평가(비범위 시 사유 명시).
- `docs/CHANGELOG.md` `[Unreleased]` 플랫 포맷(AGENTS.md 준수, `###` 금지):
  - `- [新功能] 다국어 intelligence RSS 소스(연합뉴스 경제·한국은행·Fed·Nasdaq Stocks) additive 추가 — 자동 수집 opt-in 시만 활성, 기존 소스·SSRF·fail-open 불변`
  - `- [新功能] 보고서 뉴스 카드에 intelligence 풀 병합 노출: 직접 검색 뉴스 우선 + 종목/시장·역사 시창 정합 풀 항목을 deterministic 랭킹(URL/타이틀 dedup, per-source/per-pool cap)으로 병합, provenance/source/source_type 메타데이터, 풀 조회 실패 시 직접 뉴스만 fail-open. opt-out NEWS_CARD_MERGE_INTEL_ENABLED(기본 true)`
  - `- [新功能] 한국어(ko) 보고서 뉴스 카드 번역: 병합 항목 전체에 서버 측 lazy 배치 번역(기존 GenerationBackend 재사용, 신규 provider 없음), 원본 함께 표시, 한국어 항목 중복 제외, 실패 시 원본+unavailable로 fail-open. additive API 필드(original_title/original_snippet/translation_status/source_language). zh/en 응답 불변(번역 skipped, 병합 메타데이터는 포함)`
- 본 설계 문서(`docs/superpowers/specs/2026-07-18-...`)를 구현 PR에 참조.
- AGENTS.md 검증: `python scripts/check_ai_assets.py`(본 작업은 AI 자산 변경 없음).

## 15. 단계 분리 (각각 독립 PR 권장)

| Phase | 내용 | 스키마 변경 | 사용자 가시성 |
|---|---|---|---|
| A1 | 소스 템플릿 4종 + intelligence 테스트 + `docs/intelligence-sources.md` | 없음(템플릿만) | 자동 수집 모드에서 분석 입력 + 카드 풀 다양화(간접) |
| B1 | **병합**: `list_items_for_report` + `NewsCardMerger` + HistoryService 병합 + API 병합 메타데이터 필드(provenance/source/source_type/published_at) + dedup/랭킹/cap + 테스트 + `NEWS_CARD_MERGE_INTEL_ENABLED` | 없음(조회 후처리) | 모든 언어 카드에 풀 항목 병합 노출(번역 없이도) |
| B2 | **번역 백엔드+캐시**: `NewsTranslationService` + `news_translation_cache` 테이블/마이그레이션 + HistoryService 번역 적용 + API 번역 필드 + 테스트 | `news_translation_cache` 신규 테이블 + unique 인덱스 | ko 보고서 카드 번역(직접+풀 동일 파이프라인) |
| B3 | Web 타입/ReportNews 렌더(번역 분기 + provenance 라벨) + i18n + 테스트 | 없음 | ko 카드 이중 블록/unavailable 배지/출처 칩 |

- A1은 B1/B2/B3와 독립. B1(병합)을 B2(번역)보다 먼저 배포하면 카드에 풀이 먼저 보이고 번역은 후속. B3 없이 B1/B2 배포해도 additive 필드라 Web 미대응 시 현행 동작 유지(무해).
- **권장 순서**: A1 → B1 → B2 → B3. 단, 병합과 번역이 모두 `resolve_and_get_news` 후처리라 B1+B2를 한 PR로 묶어도 무방(§18).

## 16. 롤아웃 / 롤백

- **롤아웃**: A1·B1·B2·B3 각각 독립 PR. 기본 설정(자동 수집 off, 병합 ON, 기존 LLM 구성)에서는 A1 소스 미수집, 카드 병합은 동작(풀이 비어 있으면 직접만). 자동 수집 on 환경에서 A1 자동 활성 → 병합이 풀 항목을 카드에 노출.
- **롤백**:
  - A1: revert — 템플릿 제거. DB 신규 소스 행은 운영자 비활성화/삭제. `intelligence_items` 잔존는 retention 자연 정리(무해).
  - B1: revert — 병합 코드 제거; API 병합 필드 사라지고 `title`/`snippet`/`url`만(직접 뉴스). `NEWS_CARD_MERGE_INTEL_ENABLED=false`로 런타임 opt-out도 가능(revert 없이 즉시 비활성).
  - B2: revert — `news_translation_cache` 테이블은 additive라 잔존해도 미소비(무해); API 번역 필드 사라지고 원본 `title`/`snippet`.
  - B3: revert — Web 단일 블록 렌더로 복귀; 백엔드 additive 필드 무시.
- **데이터 정리 불필요** — 모든 변경 additive이고 병합·번역은 조회 후처리라 저장 데이터 의미 불변.

## 17. 리스크

| 리스크 | 영향 | 완화 |
|---|---|---|
| 카드 응답 지연(직접 조회 + 풀 조회 + 번역 LLM 직렬) | UX·지연 | 풀 조회 단일 쿼리(시창+정합 결합); 번역 캐시; 배치 1회; 짧은 timeout; 병합·번역 모두 fail-open |
| 광역 피드(Fed/Nasdaq/MarketWatch/BOK/Yonhap) swamping | 종목 특정 뉴스 묻힘 | per-source cap(3) + per-pool-market cap(6) + direct reserve + T2>T3 랭킹(deterministic, §7.3) |
| 병합이 오래된/무관한 풀 항목 노출 | 정보 관련성 저하 | 역사 시창(created_at 기준 lookback/forward); symbol-scope 우선; market 정합 |
| market_review region 파생 불일치(과거 region ≠ 현재 config) | 잘못된 시장 풀 노출 | payload 우선 파생; config는 best-effort; 불가 시 T3 스킵(fail-open); 문서 명시 |
| 번역 품질/환각 | 정보 정확성 | prompt 충실번역 제약 + 원본 보존(대조 가능) + Hanzi 누출 게이트 |
| LLM 미구성 환경 | ko 카드 번역 불가 | fail-open(원본+unavailable 배지) → 기존 카드와 기능적 동등 |
| 신규 RSS 가용성 변동 | 소스 단위 수집 실패 | 기존 fail-open + `last_status` 가시성; 운영자 비활성화 |
| 캐시 오염 | 잘못된 번역 반복 | unavailable은 짧은 TTL; translated는 정적(원본 기반) — 오염 시 명시적 삭제(운영 도구 비범위) |
| 번역이 URL/출처 의미 변경 | 출처 투명성 | URL 불변, 원본 블록·provenance 라벨 명시 |
| 비SQLite 환경 마이그레이션 미지원 | 번역 캐시 미작동 | 기존 `_ensure_*` skip 관례 + 문서 명시; 해당 환경은 매번 번역(기능 유지, 비용 증가) |
| 언어 감지 오판(영어+한국어 혼합 헤드라인) | 중복 번역/미번역 | 보수적 `_KO_RATIO_THRESHOLD`(0.15); 수동 오버라이드 비범위 |
| 병합으로 인한 직접 뉴스 누락 | 사용자가 본 기사가 사라짐 | direct reserve + T1 최상단 + 직접 우선 dedup 동률 처리 → 직접 뉴스는 항상 포함 |

## 18. 미결정 / 사용자 리뷰 필요 항목

1. **`market` 태깅(§4.1 정합)**: Fed/Nasdaq을 `market="us"` 단일 지정. `global` 커버리지 필요 시 운영자 `POST /sources` 복제 — 템플릿 자체를 global로 둘지(권장은 `us` 단일).
2. **`_KO_RATIO_THRESHOLD`(§8)**: 한국어 항목 판정 임계값(권장 0.15). 구현 시 샘플 튜넝.
3. **cap 상수(§7.2/D19)**: `_PER_SOURCE_CAP=3`·`_PER_POOL_MARKET_CAP=6`·`forward=+1d`. 샘플 데이터로 튜닝; 설정 노출 여부(권장은 상수, 최소 설정).
4. **unavailable 캐시 TTL(§5.2/§10)**: 기본 24h. `NEWS_TRANSLATION_UNAVAILABLE_TTL_HOURS` opt-in 추가 여부(AGENTS.md: `.env.example` 동기).
5. **번역 timeout(§9)**: 분석 기본 대비 짧은 고정(서비스 상수) vs config 노출 — 권장은 상수.
6. **B1/B2 PR 분리(§15)**: 병합·번역이 같은 후처리라 한 PR 묶음 vs 분리 — 권장은 분리(가시성/롤백 단위), 무방하면 묶음.
7. **market_review region 파생(§4.2/D14)**: record payload market keys의 정확한 위치/키명 구현 시 확인(코드 실측 보강 필요). 불가 시 config fallback의 시맨틱 한계(과거 region 불일치) 문서화 수준 유지 vs record에 region 명시 저장(스키마 변경 — 비범위 권장).
8. **`docs/intelligence-sources.md` 중/영 동기(§14)**: 영문 인덱스 대상 동기 필요 여부.
9. **`NEWS_CARD_MERGE_INTEL_ENABLED` 기본값(§D8)**: true 제안(사용자 승인). false로 기본 둘지(점진적) — 권장은 true(opt-out 제공).

---

## 부록 A — 구현 전 자가 점검 (placeholder/모순/모호/scope/AGENTS 준수)

- **placeholder**: 없음. 모든 결정에 권장값·근거 명시. 미확정은 §18(사용자 리뷰)에 한정.
- **모순 점검(v2 재확인)**:
  - "zh/en 번역 안 함" ↔ "병합은 zh/en에서도 수행" → 번역만 skipped, 병합 메타데이터는 포함(§1.4·§5.3 양립).
  - "직접 우선" ↔ "풀 병합" → direct reserve + T1 최상단 + dedup 동률 direct 우선으로 양립(§7).
  - "광역 피드 확장" ↔ "swamping 방지" → per-source/per-pool cap + deterministic 랭킹으로 양립(§7.3).
  - "역사 시창" ↔ "`list_items` now-기준" → 신규 `list_items_for_report`(explicit 경계)로 해결(now 미참조, §4.2·D16).
  - "병합 ON 기본" ↔ "fail-open" → 풀 조회 실패 시 직접만 반환, opt-out 제공(§D8·§11).
  - "ko 전용 번역" ↔ "source-agnostic 계층" → 계층은 범용이되 트리거 ko only(D4).
  - "최소 계약" ↔ "ML 점수 랭킹" → ML 비범위, deterministic 티어/시간/id만(§1.4·D18).
- **모호성 점검**: 병합 정합(stock vs market_review 각각), 시창(created_at 기준), dedup(URL→title, direct 우선), 랭킹(T1→T2→T3, 시간 근접), cap(per-source/per-pool/total), status/provenance enum 모두 명시.
- **scope**: 본문 번역·ML 랭킹·NewsNow 교체·유료 번역 API·실시간 재수집 트리거 모두 비범위(§1.4). "顺手 최적화" 회피(AGENTS.md).
- **AGENTS.md 준수**: 디렉토리 경계(src/·api/·apps/dsa-web/)·additive 필드·fail-open 데이터소스·`.env.example` 동기(신규 설정 `NEWS_CARD_MERGE_INTEL_ENABLED`/`NEWS_TRANSLATION_UNAVAILABLE_TTL_HOURS` 시)·`docs/CHANGELOG.md` 플랫 포맷·commit/push 미수행(본 작업은 설계 문서 only)·스크린샷 요건(Web UI 변경은 B3 구현 PR 시 첨부 — 본 설계 단계 아님) 반영.
- **저장소 현실 우선(AGENTS.md)**: (a) `list_items` now-기준 시맨틱 → 신규 `list_items_for_report`(created_at 기준); (b) AnalysisHistory에 market 컬럼 없음 → `_infer_market`/`canonical_stock_code` 파생; (c) market_review `code="MARKET"` → region payload 파생; (d) 기존 언어 감지/SSRF/마이그레이션/news-fallback 시창 선례 재사용; (e) 두 뉴스 테이블을 동일 정규화 스키마로 병합. 코드 실측 기반으로 본문 각처에 명시.

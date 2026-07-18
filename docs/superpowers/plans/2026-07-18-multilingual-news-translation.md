# 구현 계획 — 다국어 뉴스 확장 + 한국어 보고서 뉴스 카드 번역 (v2)

- 작성일: 2026-07-18
- 상태: **구현 계획 (코드 미작성) — 구현은 별도 PR에서, 본 worktree 변경은 미커밋 상태로 유지**
- 설계 근거: `docs/superpowers/specs/2026-07-18-multilingual-news-translation-design.md` (승인된 v2)
- 규약: `AGENTS.md` (커밋·push·PR·tag 금지 without 명시 확인; additive 필드; fail-open; flat CHANGELOG)
- 대상 worktree: 현재 `daily_stock_analysis` worktree (변경은 본 worktree에만, 무관 변경 보존)

> **[중요] 커밋 정책**: 설치된 implement skill이 커밋을 지시하더라도, 본 저장소 `AGENTS.md` §1 ("未经明确确认，不执行 git commit、git tag、git push")이 우선한다. **본 계획의 모든 태스크는 변경을 커밋·push·PR·tag 하지 않고 worktree에 미커밋(uncommitted)으로 남긴다.** 최종 단계는 "중지 후 사람에게 review/커밋 확인 요청"이다. 파괴적 동작(`reset --hard`, `checkout` 강제, `stash` 강제, `clean -fd`) 금지.

## 0. 하드 제약 (구현 전체에 걸침)

- 신규 LLM provider/model/base URL/API key config **추가 없음** — 번역은 기존 `resolve_generation_backend_id(config)` + `create_generation_backend` 재사용.
- 유료/키 기반 번역 API(DeepL/Google Translate 등) **도입 없음**.
- `NEWS_CARD_MERGE_INTEL_ENABLED` **기본 `true`**, opt-out(`false` 설정 시 병합 스킵, v1 동등).
- 직접 뉴스(direct news) reserve + per-source/per-pool cap이 카드 관련성을 보호 — 광역 피드(Fed/Nasdaq/MarketWatch/BOK/Yonhap)가 종목 특정 뉴스를 덮지 않음.
- 번역·병합 모두 **fail-open** — 단일 실패가 응답을 500으로 만들지 않음.
- 모든 API 변경은 **additive optional 필드** — 기존 `title`/`snippet`/`url` 의미·위치·필수 불변, 구 클라이언트 무해.
- 커밋·push·PR·tag·파괴적 git 동작 **금지**.

## 1. §18 권장안 해석 (기본값 채택, 코드 기반 이탈 기록)

| §18 항목 | 채택값 | 근거/이탈 여부 |
|---|---|---|
| 1. market 태깅 | Fed/Nasdaq 각 `market="us"` 단일 | 권장안 채택. 스키마가 단일 market 값만 허용(실측). `global` 커버리지는 운영자 `POST /sources` 복제로 확장(비범위). 이탈 없음. |
| 2. `_KO_RATIO_THRESHOLD` | `0.15` | 권장안 채택. 번역 판정용 상수(`news_translation_service` 내 사유 상수). 이탈 없음. |
| 3. cap 상수 | `_PER_SOURCE_CAP=3`, `_PER_POOL_MARKET_CAP=6`, `forward=+1d` | 권장안 채택. `news_merge_service` 내 상수(설정 비노출). 이탈 없음. |
| 4. unavailable 캐시 TTL | `NEWS_TRANSLATION_UNAVAILABLE_TTL_HOURS=24`(기본), opt-in `.env.example` 동기 | 권장안 채택. Config 필드 추가. |
| 5. 번역 timeout | 서비스 상수(분석 기본의 절반 수준) | 권장안 채택. config 비노출. 이탈 없음. |
| 6. B1/B2 PR 분리 | 분리 권장(B1→B2), 무방하면 묶음 허용 | 권장안 채택. 본 계획은 분리 순서로 기재. |
| 7. market_review region 파생 | **payload에서 결정적 추출**(top-level `region` 단일 / `markets` dict multi) → 불가 시 read-time `market_review_region` config → 그래도 불가 시 T3 스킵(fail-open) | **코드 실측으로 해소**: `_build_combined_market_review_payload`가 payload에 `region`(단일) 또는 `markets`(multi, market 키 dict)을 저장(`src/core/market_review.py:488-517`). record의 `context_snapshot`/`raw_result`에서 읽어 결정적 파생 가능 → "best-effort" 한계 제거. 이탈: 설계의 "best-effort config fallback"을 **3순위**로 강등(결정적 payload 추출이 1순위). |
| 8. `docs/intelligence-sources.md` 중/영 동기 | 구현 단계에서 `docs/INDEX_EN.md` 링크 대상 확인 후 동기 | 평가 후 동기(비동기 시 사유 명시). |
| 9. `NEWS_CARD_MERGE_INTEL_ENABLED` 기본값 | `true`(opt-out 제공) | 권장안 + 태스크 하드 제약. |

> 모든 §18 항목이 코드 기반에서 안전하게 권장 기본값으로 해소됨. 이탈은 §18-7의 우선순위 강등 1건만 기록.

## 2. 단계 의존성 그래프

```
A1 (소스 템플릿 4종, 스키마 무변경)
   │   └─ 독립. B1의 풀 데이터를 풍부화하지만, B1은 기존 소스(SEC/HKEX/MarketWatch/NewsNow)만으로도 동작·테스트 가능.
   ▼
B1 (풀→카드 병합, 스키마 무변경, 읽기 후처리)
   │   └─ 병합된 item 형태(title/snippet/url + provenance/source/source_type/published_at)를 B2/B3가 소비.
   ▼
B2 (ko 번역 + 캐시, 신규 테이블 1개)
   │   └─ 병합 items에 ko 번역 적용. direct-only로도 단위 테스트 가능(병합 무의존 단위).
   ▼
B3 (Web 렌더, 스키마 무변경)
       └─ B1+B2 API 필드 소비. undefined/skipped 시 현행 단일 블록으로 우아한 degradation.
```

- **병합 가능**: A1·B1·B2·B3는 additive라 어느 것을 먼저 배포해도 무해(B3 없이 B1/B2 배포 시 Web은 기존 필드만 소비).
- **권장 순서**: A1 → B1 → B2 → B3(데이터 흐름·리뷰 단위 명확).
- **의존**: B2/B3는 B1의 item 스키마에 의존(병합 메타데이터). B1은 A1 데이터에 의존(풍부화만, 필수 아님).

## 3. 단계별 태스크 (순서대로, 각각 red-green)

---

### Phase A1 — 소스 확장 (스키마 무변경)

#### A1.1 — RSS 템플릿 4종 추가
- **파일**: `src/services/intelligence_service.py`
- **함수/심볼**: 모듈 상수 `_BUILTIN_SOURCE_TEMPLATES`(현재 3종: sec-company-news / hkex-news / global-marketwatch). **기존 3종 순서·내용 불변**, 뒤에 4종 **append**.
- **내용**: spec §4.1 의 4 딕셔너리 그대로(`kr-yna-economy` / `kr-bok-press` / `us-fed-press` / `us-nasdaq-stocks`, `source_type="rss"`, `scope_type="market"`, `market` 각 `"kr"`/`"kr"`/`"us"`/`"us"`).
- **변경 불가**: `_NEWSNOW_DEFAULT_SOURCE_DEFS`, `_validate_url`, `_fetch_feed_entries`, `_parse_feed`, `create_source_from_template`, `create_default_sources`, `ensure_default_sources_enabled`, `upsert_items`, retention — **무수정**(신규 RSS가 기존 로직으로 처리됨을 전제).
- **market 값 검증**: `"kr"`/`"us"`는 이미 `_ALLOWED_MARKETS`에 포함(실측, 신규 enum 불필요).

#### A1.2 — A1 테스트 (red→green)
- **파일**: `tests/test_intelligence_service.py` (`IntelligenceServiceTestCase`, unittest 스타일, temp DB)
- **red 단계(먼저 작성)**:
  - `test_source_templates_include_kr_and_us_feeds`: `service.list_source_templates(market="kr")`에 `kr-yna-economy`, `kr-bok-press` 등장; `market="us"`에 `us-fed-press`, `us-nasdaq-stocks` 등장.
  - `test_create_source_from_template_kr_yonhap`: `create_source_from_template("kr-yna-economy", {"enabled": False, "name": "yna-copy"})` → 반환 `name`/`url`/`market` 일치.
  - `test_create_default_sources_includes_new_feeds`: `create_default_sources({"enabled": False})` 후 `list_sources(market="kr")`/`"us"`에 신규 4종 포함; 2회 호출 idempotent(`created_count==0` 둘째).
  - `test_new_rss_urls_pass_ssrf_guard`: `_validate_url("https://www.federalreserve.gov/feeds/press_all.xml")` 통과; 사설 변형(`http://127.0.0.1/...`)은 기존과 동일 거부.
  - `test_parse_new_rss_feed_via_existing_parser`: RSS fixture(`_parse_feed`)가 신규 템플릿 URL에서도 기존 파싱 경로로 동작(`FeedEntry` 산출) — URL 무관 단위 테스트.
- **green 단계**: A1.1 적용 후 통과. (라이브 URL 검증은 `pytest -m network`로 별도 보관, 비블로킹 — 본 계획은 오프라인 게이트 우선.)

#### A1.3 — A1 문서/체인지로그
- **파일**: `docs/intelligence-sources.md` ("NewsNow 기본 소스"/"후속 접속 제안" 인근), `docs/CHANGELOG.md` `[Unreleased]` 플랫.
- **CHANGELOG 라인**: `- [新功能] 다국어 intelligence RSS 소스(연합뉴스 경제·한국은행·Fed·Nasdaq Stocks) additive 추가 — 자동 수집 opt-in 시에만 활성, 기존 소스·SSRF 가드·fail-open 불변`
- **intelligence-sources.md**: 라이프사이클·SSRF·자동 수집 opt-in이 기존과 동일(확장) 명시; v2로 풀이 카드 병합에도 공급됨을 B1과 함께 명시(교차 참조).

#### A1 검증
```bash
python -m py_compile src/services/intelligence_service.py
./scripts/ci_gate.sh syntax && ./scripts/ci_gate.sh flake8
uv run pytest -m "not network" tests/test_intelligence_service.py
```

---

### Phase B1 — 풀→카드 병합 (스키마 무변경, 읽기 후처리)

#### B1.1 — Config: 병합 토글
- **파일**: `src/config.py` (필드 선언 ~line 917 인근, `from_env` 파싱 ~line 1837 인근), `.env.example` (~line 444 인근 news 섹션)
- **필드**: `news_card_merge_intel_enabled: bool = True` (기본 true, opt-out). 파싱은 `parse_env_bool(os.getenv("NEWS_CARD_MERGE_INTEL_ENABLED"), default=True)` 패턴(기존 `news_intel_auto_fetch_enabled` line 1833 참조).
- **`.env.example`**: 주석 포함 옵션(기본 true 명시): `# NEWS_CARD_MERGE_INTEL_ENABLED=true  # 보고서 뉴스 카드에 intelligence 풀 병합(opt-out 시 false)`.
- **신규 provider/model/base URL config 추가 없음**.

#### B1.2 — `IntelligenceRepository.list_items_for_report` (신규 메서드)
- **파일**: `src/repositories/intelligence_repo.py`
- **시그니처**: `list_items_for_report(self, *, scope_type, scope_value=None, market=None, start_at, end_at, limit=50) -> List[IntelligenceItem]`
- **SQL**: 기존 `list_items` 패턴 준수하되, `coalesce(IntelligenceItem.published_at, IntelligenceItem.fetched_at) BETWEEN :start_at AND :end_at` 경계 사용. **`now()` 미참조**(재현성). `scope_value`는 기존 `_normalize_scope_value` 재사용. 정렬은 `desc(coalesce(published_at, fetched_at)), desc(id)`(서비스가 시간 근접순 재정렬).
- **기존 `list_items` 무변경**(now-기준 시맨틱 보존, 기존 호출자 무영향).
- **red**: `tests/test_intelligence_service.py` 또는 신규 `tests/test_intelligence_repo.py`에 `list_items_for_report` 단위 — explicit 경계 동작(now 미참조), scope/market 필터, `published_at` 미존재 시 `fetched_at` coalesce, 빈 결과.

#### B1.3 — `NewsCardMerger` (신규 서비스)
- **파일**: `src/services/news_merge_service.py` (신규)
- **클래스/메서드**: `class NewsCardMerger: def __init__(self, intel_repo=None, config=None): ...; def merge_for_report(self, *, record, direct_items, limit) -> list[dict]:`
- **핵심 로직**(spec §4.2/D13-D20):
  - **stock 정합**: `market = _infer_market(record.code)`(`src/services/decision_signal_reassess_service._infer_market` import 재사용, 평행 구현 금지); `symbol = canonical_stock_code(record.code)`(`data_provider.base` 재사용). `_infer_market`이 None → T2/T3 스킵(direct만, fail-open).
  - **market_review 정합(§18-7 해소)**: `record.report_type == "market_review"`일 때, `context_snapshot`/`raw_result`에서 market_review payload 탐색 → `payload.get("region")`(단일) 또는 `list((payload.get("markets") or {}).keys())`(multi) 추출. 불가 시 read-time `config.market_review_region`을 `_resolve_market_review_regions`(`src/core/market_review` 재사용)로 전개. 그래도 불가 → T3 스킵(fail-open).
  - **시창**: `lookback = resolve_news_window_days(cfg.news_max_age_days, cfg.news_strategy_profile)`(`src/config` 재사용, anchor=분석일); `forward = +1 calendar day`. `start_at = record.created_at - lookback`, `end_at = record.created_at + forward`.
  - **조회**: T2 `list_items_for_report(scope_type="symbol", scope_value=symbol, market=market, ...)`(stock만); T3 `list_items_for_report(scope_type="market", market=market_or_regions, ...)`.
  - **정규화 변환**: 각 풀 항목 → `{title, snippet(=summary), url, provenance="pool", source=item.source_name or item.source, source_type=item.source_type, published_at=iso(coalesce(published_at, fetched_at))}`.
  - **dedup(D17)**: canonical URL 정규화(scheme/host 소문자, 트레일링 슬래시 정규화, fragment 제거) 동일 → 1건; 정규화 title 해시(NFKC·소문자·whitespace 단일화 — 번역 캐시 정규화와 동일 알고리즘, 단일 헬퍼로 추출 권장) 동일 → 1건. 동률 시 **direct 우선**. `no-url:intel:*` placeholder는 URL 비교 제외, title 해시만.
  - **랭킹(D18)**: T1 direct → T2 symbol-scope → T3 market-scope; 티어 내 `abs(published_at - record.created_at)` asc → `id` asc. **점수/ML 없음**.
  - **cap(D19)**: 상수 `_PER_SOURCE_CAP=3`, `_PER_POOL_MARKET_CAP=6`. direct reserve: direct 개수만큼 우선 할당 후 잔여 슬롯을 T2→T3 순으로. 총 반환 `limit`.
  - **예외**: 모든 예외 로그 후 `direct_items` 만 반환(fail-open).
- **red**: `tests/test_news_merge_service.py`(신규) — stock 정합(symbol+market), market_review 정합(region/markets 추출 + config fallback + fail-open), 시창(now 독립 재현성), dedup(URL/title, direct 우선, placeholder), 랭킹(T1>T2>T3, 시간 근접), cap(per-source 도달 차단, per-pool, direct reserve, total=limit), fail-open(풀 조회 예외, market 추론 불가), opt-out(`news_card_merge_intel_enabled=False` 시 direct만).

#### B1.4 — `HistoryService.resolve_and_get_news` 병합 배선
- **파일**: `src/services/history_service.py`
- **함수**: `resolve_and_get_news(self, record_id, limit=20)` (현재 line 394; 이미 `_resolve_record`로 record 해석 line 406).
- **변경**: 직접 뉴스 확보 후(`get_news_intel(query_id, limit)` 결과에 provenance="direct", source_type="search", source, published_at 부여) → `cfg.news_card_merge_intel_enabled`면 `NewsCardMerger(...).merge_for_report(record=record, direct_items=..., limit=limit)`. 병합 서비스 예외 시 직접 뉴스 유지(fail-open, 로그). snippet 200자 truncate는 **원본 기준**(기존 `get_news_intel`) 유지.
- **report_language 파생**: `normalize_report_language(raw_result.get("report_language") or context_snapshot.get("report_language"))` (기존 사용 패턴 line 610/861/919 준수). `_extract_report_language`(api 계층 헬퍼) import **금지**(src→api 역참조).
- **red**: `tests/test_history_news_fallback.py` 확장 또는 `tests/test_history_service_merge.py`(신규) — 병합 배선이 direct+pool 결합; `news_card_merge_intel_enabled=False` 시 direct-only; 병합 예외 시 direct-only(fail-open).

#### B1.5 — API 스키마 + 엔드포인트 통과
- **파일**: `api/v1/schemas/history.py` (`NewsIntelItem`), `api/v1/endpoints/history.py` (`get_history_news`)
- **스키마**: `NewsIntelItem`에 additive optional 4종 추가(`provenance`, `source`, `source_type`, `published_at`). 기존 `title`/`snippet`/`url` 순서·필수 불변. (번역 필드는 B2에서 추가.)
- **엔드포인트**: `get_history_news`의 `NewsIntelItem(title=..., snippet=..., url=...)` 생성부(line 인근 `response_items = [...]`)를 dict에서 additive 필드까지 읽도록 최소 수정(미전달 시 pydantic 기본 None). 시그니처·라우트·응답 모델·query param·200/500 **불변**.
- **red**: `tests/test_history_news_api.py`(신규, FastAPI TestClient) — ko/zh/en record에서 병합 메타데이터 직렬화; `provenance`/`source`/`source_type`/`published_at` 포함; 구 클라이언트 관점(`title`/`snippet`/`url`만) 호환.

#### B1.6 — B1 문서/체인지로그
- **CHANGELOG 라인**: `- [新功能] 보고서 뉴스 카드에 intelligence 풀 병합 노출: 직접 검색 뉴스 우선 + 종목/시장·역사 시창 정합 풀 항목을 deterministic 랭킹(URL/타이틀 dedup, per-source/per-pool cap)으로 병합, provenance/source/source_type 메타데이터, 풀 조회 실패 시 직접 뉴스만 fail-open. opt-out NEWS_CARD_MERGE_INTEL_ENABLED(기본 true)`
- `docs/intelligence-sources.md`: 풀이 카드에 병합 노출됨을 A1과 교차 명시(병합 규칙은 spec 참조).

#### B1 검증
```bash
python -m py_compile src/repositories/intelligence_repo.py src/services/news_merge_service.py src/services/history_service.py api/v1/endpoints/history.py api/v1/schemas/history.py src/config.py
./scripts/ci_gate.sh
uv run pytest -m "not network" tests/test_intelligence_service.py tests/test_news_merge_service.py tests/test_history_news_fallback.py tests/test_history_news_api.py
```

---

### Phase B2 — ko 번역 + 캐시 (신규 테이블 1개)

#### B2.1 — `NewsTranslationCache` 모델 + 마이그레이션
- **파일**: `src/storage.py`
- **모델**: `class NewsTranslationCache(Base)` (spec §4.3). 컬럼: `id`, `content_hash`(String64, indexed), `target_language`(String8, indexed), `source_language`(String16 nullable), `translated_title`(String600 nullable), `translated_snippet`(Text nullable), `translation_status`(String16 not null), `model_used`(String128 nullable), `created_at`/`updated_at`(indexed). `__table_args__ = (UniqueConstraint("content_hash","target_language",name="uix_news_translation_hash_lang"),)`.
- **마이그레이션**: `_ensure_news_translation_cache_schema(self)` — 기존 `_ensure_decision_signal_outcome_attribution_schema`(line 1541) 패턴 준수: `if not self._is_sqlite_engine: return` → `inspector.has_table` 검사 → `CREATE TABLE IF NOT EXISTS` + `CREATE UNIQUE INDEX IF NOT EXISTS` → duplicate 무시(`_is_sqlite_duplicate_column_error`류 헬퍼 재사용 또는 `OperationalError` 무시 가드).
- **등록**: `DatabaseManager.__init__`(line 1454-1460 인근 `_ensure_*` 목록)에 `self._ensure_news_translation_cache_schema()` 추가.
- **기존 `news_intel`/`intelligence_items`/`intelligence_sources` 스키마·인덱스·unique 무변경**.

#### B2.2 — `NewsTranslationRepository` (또는 헬퍼)
- **파일**: `src/repositories/intelligence_repo.py`에 추가 **또는** `src/repositories/news_translation_repo.py`(신규). 권장: `IntelligenceRepository`와 동일 디자인의 작은 repo.
- **메서드**: `get_by_hash_lang(content_hash, target_language) -> Optional[NewsTranslationCache]`, `upsert(content_hash, target_language, *, source_language, translated_title, translated_snippet, translation_status, model_used)`. 기존 `get_session()` 컨텍스트 + `IntegrityError` 무시 후 select 회수 패턴.
- **red**: 신규 테스트 — get/upsert 멱등(unique 충돌 시 select 회수); unavailable 캐시 TTL 만료 판정(`updated_at` 기준, 모킹 시계).

#### B2.3 — `NewsTranslationService` (신규)
- **파일**: `src/services/news_translation_service.py` (신규)
- **클래스/메서드**: `class NewsTranslationService: def __init__(self, repository=None, config=None, generation_backend=None): ...; def translate_items(self, items, target_language) -> list[dict]:`
- **로직**(spec §4.3):
  - `target != "ko"` → 각 항목 `translation_status="skipped"`, 원본 유지(병합 메타데이터 보존).
  - `ko`: per-item 언어 감지(Hangul U+AC00–U+D7A3 / Hanzi U+4E00–U+9FFF 카운터, `src/report_language.py`의 기존 카운트 로직 재사용; 임계값 `_KO_RATIO_THRESHOLD=0.15`). ko → `status="original"`, `original_*` 생략(중복 금지).
  - 비ko: 캐시 조회(페어 해시 `sha256(normalize(title + "\n\n" + snippet))` + target). hit → 캐시값. miss → 1회 배치 LLM.
  - **배치 prompt**(고정 system, spec §9): 충실 번역·JSON 구조/순서/`id` 보존·한국어/빈 값 그대로·JSON 배열만 출력. `response_validator`로 JSON + 길이 + id 집합 검증.
  - **백엔드**: `resolve_generation_backend_id(config)`(`src/llm/backend_registry`) → `create_generation_backend`(`src/llm/backend_factory`). fallback 시맨틱은 config 기존 정의에 위임. `audit_context={"feature":"news_translation","target_language":"ko","batch_size":N}`. **신규 provider/model config 없음**.
  - **검증**: `has_disallowed_report_script("ko", translated)`(`src/report_language`)로 Hanzi 누출 시 해당 항목 `unavailable`.
  - **실패**: timeout·malformed·스키마 위반·백엔드 미구성 → per-item `unavailable`, 원본 유지(fail-open). unavailable 캐시(TTL) upsert.
  - **동시성**: 프로세스 내 `threading.Lock`(intelligence `_auto_fetch_condition` 패턴 참조). 배치 상한 20 초과 시 청크. timeout 서비스 상수.
- **정규화 헬퍼**: `normalize_for_hash(text)`(NFKC·소문자·whitespace 단일화·strip)를 단일 헬퍼로 — B1.3 dedup title 해시와 **동일 알고리즘 공유**(단일 진실; 중복 구현 금지).

#### B2.4 — Config: unavailable TTL
- **파일**: `src/config.py`, `.env.example`
- **필드**: `news_translation_unavailable_ttl_hours: int = 24`. 파싱 `parse_env_int(os.getenv("NEWS_TRANSLATION_UNAVAILABLE_TTL_HOURS"), default=24)`.
- **`.env.example`**: `# NEWS_TRANSLATION_UNAVAILABLE_TTL_HOURS=24  # 번역 실패 캐시 재시도 주기(시)`.

#### B2.5 — HistoryService 번역 배선
- **파일**: `src/services/history_service.py`
- **변경**: B1.4 병합 후, `report_language == "ko"`면 `NewsTranslationService(...).translate_items(merged, "ko")`. 예외 시 원본 유지(fail-open). non-ko면 skipped(원본).
- **red**: 병합+번역 통합 테스트(ko translated/original/unavailable + 병합 메타데이터 보존; zh/en skipped + 병합 메타데이터 포함).

#### B2.6 — API 번역 필드
- **파일**: `api/v1/schemas/history.py` (`NewsIntelItem`)
- **필드**: additive optional 4종(`original_title`, `original_snippet`, `translation_status`, `source_language`). 엔드포인트는 dict에서 읽어 전달(B1.5와 동일 패턴).
- **red**: `tests/test_history_news_api.py` 확장 — ko translated/original/unavailable 직렬화; zh/en skipped.

#### B2.7 — B2 테스트 (단위)
- **파일**: `tests/test_news_translation_service.py`(신규), `tests/test_storage.py`(마이그레이션 확장)
- **케이스**: 언어 감지 단위(ko/zh/en/mixed/unknown, 임계값 경계); ko translated(모킹 백엔드 성공)/original(한국어 항목)/unavailable(timeout·malformed·Hanzi 누출·백엔드 미구성); non-ko skipped; 캐시 hit/miss/upsert 멱등; unavailable TTL(모킹 시계); 배치 입력-출력 매핑(id 순서 보존); 정규화 헬퍼(NFKC/whitespace).
- **마이그레이션**: 신규 DB(`create_all`)와 기존 DB(`_ensure_*` 경로) 양쪽에서 `news_translation_cache` 테이블·unique 인덱스 존재; duplicate 무시; 비SQLite skip 가드.

#### B2.8 — B2 문서/체인지로그
- **CHANGELOG 라인**: `- [新功能] 한국어(ko) 보고서 뉴스 카드 번역: 병합 항목 전체에 서버 측 lazy 배치 번역(기존 GenerationBackend 재사용, 신규 provider 없음), 원본 함께 표시, 한국어 항목 중복 제외, 실패 시 원본+unavailable로 fail-open. additive API 필드(original_title/original_snippet/translation_status/source_language). zh/en 응답 불변(번역 skipped, 병합 메타데이터는 포함)`

#### B2 검증
```bash
python -m py_compile src/storage.py src/services/news_translation_service.py src/services/history_service.py api/v1/schemas/history.py src/config.py
./scripts/ci_gate.sh
uv run pytest -m "not network" tests/test_news_translation_service.py tests/test_storage.py tests/test_history_news_api.py
# 마이그레이션 회귀: 기존 DB 파일 대상 부팅 시 _ensure_news_translation_cache_schema 무해 동작 확인(수동)
```

---

### Phase B3 — Web 렌더 (스키마 무변경)

#### B3.1 — 타입 확장
- **파일**: `apps/dsa-web/src/types/analysis.ts`
- **변경**: `NewsIntelItem`에 camelCase optional 추가: `originalTitle?`, `originalSnippet?`, `translationStatus?`, `sourceLanguage?`, `provenance?`, `source?`, `sourceType?`, `publishedAt?`. `apps/dsa-web/src/api/history.ts` `getNews`의 `toCamelCase<NewsIntelItem>` 경로 무변경(자동 변환).

#### B3.2 — `ReportNews.tsx` 렌더 분기 + provenance 라벨
- **파일**: `apps/dsa-web/src/components/report/ReportNews.tsx`
- **변경**:
  - 번역 분기: `translationStatus === "translated"` → 주 블록(한국어) + muted 원본 블록(`home-news-snippet` 클래스 재사용, 낮은 대비); `"original"` → 단일; `"unavailable"` → 원본 + "번역 불가" 배지; `"skipped"`/`undefined` → 단일(현행 호환).
  - provenance 라벨: `provenance === "pool"` → 작은 출처 칩(`source`/`sourceType` 표시, 예: "RSS · Federal Reserve All Press Releases"). `"direct"` → 기존 `sourceText.sourceLabel` 유지.
  - `NEWS_SOURCE_TEXT`에 `{zh,en,ko}` provenance/번역불가 문구 추가(ko: "원본", "번역 불가", "출처"; en/zh 대응).
  - 접근성: 원본 블록·칩에 `aria-label` 명시.
- **빈 결과/로딩/에러/새로고침 동작 불변**.

#### B3.3 — B3 테스트
- **파일**: `apps/dsa-web/src/components/report/__tests__/ReportNews.test.tsx` (vitest, `historyApi.getNews` 모킹)
- **케이스**: translated(이중 블록, 한국어+원본); original(단일); unavailable(원본+배지); skipped/`undefined`(구 응답 호환 단일); provenance=pool(출처 칩 렌더); provenance=direct(기존 라벨). 기존 4케이스(styling/empty/i18n/retry) 무회귀.

#### B3.4 — UI 스크린샷 증거 (AGENTS.md §1/§6 Web gate)
- **요건**: ko 보고서 뉴스 카드의 (a) 번역 성공(한국어 먼저 + 원본 muted), (b) 번역 불가(배지), (c) 풀 항목 provenance 칩, (d) zh/en 카드(번역 스킵 + 병합 메타데이터) 화면.
- **배치**: 스크린샷은 **PR 설명/댓글/GitHub 첨부/Actions artifact**에(AGENTS.md: "제품 장기 문서 외 임시 스크린샷은 repo 파일 합입 금지"). 본 계획 단계에서는 캡처하지 않음(구현 PR에서).

#### B3 검증
```bash
cd apps/dsa-web && npm ci && npm run lint && npm run build
# vitest: npm test -- ReportNews (또는 저장소 기본 test 스크립트)
```

---

## 4. 검증 매트릭스 (AGENTS.md §6 준거)

| 변경면 | 게이트 | 명령 | 비고 |
|---|---|---|---|
| Python 후端 (A1/B1/B2) | backend-gate | `./scripts/ci_gate.sh` (syntax+flake8+deterministic+`pytest -m "not network"`) |阻断 |
| 마이그레이션 (B2.1) | storage 회귀 | `pytest -m "not network" tests/test_storage.py` + 기존 DB 부팅 무해 확인 |신규/기존 DB 양쪽 |
| API/스키마 (B1.5/B2.6) | api 정합 | `pytest -m "not network" tests/test_history_news_api.py` + `py_compile` |additive 필드 |
| Web (B3) | web-gate | `cd apps/dsa-web && npm ci && npm run lint && npm run build` |阻断(트리거 시) |
| AI 자산 | ai-governance | `python scripts/check_ai_assets.py` |본 작업은 AI 자산 변경 없음(설계/계획 문서만) — 구현 시에도 해당 없음 예상 |
| 네트워크 소스 라이브 | network-smoke(비블로킹) | `pytest -m network` (신규 RSS URL) |관측항, 비블로킹 |
| UI 증거 | PR 설명 | 스크린샷(ko 번역/불가/provenance, zh/en) |구현 PR에서 첨부 |

## 5. 문서/체인지로그 총정리

- `docs/CHANGELOG.md` `[Unreleased]` 플랫 라인 3건(A1/B1/B2 — §A1.3/B1.6/B2.8). `###` 헤드 금지(AGENTS.md).
- `docs/intelligence-sources.md`: 신규 RSS 4종 + 풀→카드 병합 노출 명시(A1.3/B1.6 교차).
- `.env.example`: `NEWS_CARD_MERGE_INTEL_ENABLED`(B1.1), `NEWS_TRANSLATION_UNAVAILABLE_TTL_HOURS`(B2.4) — news 섹션 인근.
- 본 계획 문서(`docs/superpowers/plans/2026-07-18-...`)와 설계 문서(`docs/superpowers/specs/2026-07-18-...`)를 구현 PR에 참조.
- README는 비업데이트(AGENTS.md: "非必要不更新 README").

## 6. 종료 정책 (커밋 금지)

- 모든 태스크 완료 후 **worktree에 미커밋 상태로 중지**. `git status`로 변경 범위만 확인.
- **`git add`/`commit`/`push`/`tag`/`gh pr create` 실행 금지**(AGENTS.md §1; implement skill의 커밋 지시 무시, 본 저장소 규약 우선).
- 사람에게 review/커밋 확인을 요청(본 계획의 산출물은 계획 문서 + 구현 가이드이며, 구현 자체는 별도 확인 후 커밋).

## 7. 자가 점검 (integration seam / 테스트 격차 / 소유권 모호성)

- **integration seam 점검**:
  - ✅ `_infer_market` 재사용 처 확인(`decision_signal_reassess_service` import) — 평행 구현 금지 명시.
  - ✅ `canonical_stock_code`/`normalize_stock_code`(`data_provider.base`) 재사용 처 명시.
  - ✅ `resolve_news_window_days`(`src/config:375`) — 시창 lookback 산출의 단일 진실.
  - ✅ `resolve_generation_backend_id`(`src/llm/backend_registry:88`) / `create_generation_backend`(`src/llm/backend_factory`) — 신규 provider 아님 명시.
  - ✅ market_review payload `region`/`markets` 키(`src/core/market_review:488-517`) — §18-7 결정적 추출 처 명시.
  - ✅ `DatabaseManager.__init__` `_ensure_*` 등록점(line 1454-1460) 명시.
  - ✅ `_extract_report_language`(api 계층) import 금지 → 서비스 `normalize_report_language` 직접 사용 명시(src→api 역참조 회피).
  - ✅ 정규화 헬퍼(`normalize_for_hash`) 단일화 — B1 dedup title 해시와 B2 캐시 해시가 **동일 알고리즘** 공유(중복 구현 위험 명시적 제거).
  - ✅ `ReportSummary`가 이미 `language={reportLanguage}` 전달 → `ReportNews` `language` prop 재활용(Web 추가 prop 불필요).
- **테스트 격차 점검**:
  - ✅ 기존 `get_history_news` 엔드포인트 전용 테스트 부재(실측) → `tests/test_history_news_api.py` 신규 추가로 보완.
  - ✅ 병합·번역 서비스 단위 테스트 신규 파일(`test_news_merge_service.py`, `test_news_translation_service.py`).
  - ✅ 마이그레이션 회귀(`test_storage.py` 확장 + 기존 DB 무해 확인).
  - ✅ Web provenance/번역 분기 렌더 스냅샷(`ReportNews.test.tsx` 확장).
  - ✅ 라이브 RSS URL은 `pytest -m network`(비블로킹)로 보관 — 오프라인 게이트가 주 회귀.
- **소유권 모호성 점검**:
  - ✅ 각 태스크에 단일 파일/함수 귀속. `resolve_and_get_news`는 B1.4(병합 배선) + B2.5(번역 배선)로 2회 건드리나, 단계 분리로 충돌 회피(같은 PR 묶음 시 한 커밋 권장).
  - ✅ `NewsIntelItem` 스키마는 B1.5(병합 필드) + B2.6(번역 필드)로 2회 — 단계 분리.
  - ✅ Config는 B1.1(merge toggle) + B2.4(TTL)로 2회 — 단계 분리.
- **잔여 위험/블로커**:
  - ⚠️ `_infer_market` import 경로: `decision_signal_reassess_service`에서 가져오면 해당 모듈 의존성 증가. 권장: 공유 유틸(`src/services/market_symbol_utils.py`에 이미 `get_suffix_market` 존재)로 이동 후 양쪽이 재사용 — 구현 시 평가(사소한 리팩터, 비범위 확장 아님). 본 계획은 import 재사용을 기본으로 기재.
  - ⚠️ SQL `coalesce(published_at, fetched_at) between` 인덱스 활용: `ix_intel_item_fetch_time`/`ix_intel_item_scope_time` 기존 인덱스로 커버되나, `coalesce` 함수 인덱스는 DB 의존적 — limit 상한으로 풀스캔 방지(이미 상한 존재). 성능 게이트 비블로킹.
  - ⚠️ 배치 LLM 응답 형상 provider 의존적: 일부 백엔드는 JSON 배열 출력 신뢰성 낮음 → `response_validator` 강제 + per-item unavailable fail-open으로 흡수(이미 명시).
- **블로커**: 없음(모든 seam 실측 확인, §18 해소). 구현 시작 가능.

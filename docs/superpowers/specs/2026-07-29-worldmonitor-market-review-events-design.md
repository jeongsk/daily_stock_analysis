# World Monitor 이벤트 → 시장 리뷰 연동 설계

## 1. 목적

self-hosting 단계에서 확보한 로컬 World Monitor로부터 지정학·인프라·공급망 이벤트를
DSA가 직접 수집·정규화·저장하고, **시장 리뷰 프롬프트에만** 주입한다.

이번 단계의 목표는 시장 리뷰가 참조하는 글로벌 리스크 맥락을 넓히는 것이다.
개별 종목 분석, 점수, 매매 판단, DSA liveness는 이번 단계에서 일절 건드리지 않는다.

## 2. 범위

포함한다.

- 세 카테고리의 이벤트를 로컬 World Monitor HTTP API에서 수집한다.
- 전용 정규화 이벤트 테이블에 저장한다.
- 원본 발생 시각과 DSA 수집 시각을 함께 보존한다.
- 카테고리별 신선도를 판정하고 시장 리뷰 프롬프트에 명시한다.
- 시장 리뷰 프롬프트에 카테고리별 요약 블록을 상한과 함께 추가한다.
- World Monitor 장애 시 저장된 이벤트로 계속 진행한다(fail-open).

포함하지 않는다.

- 개별 종목 분석, 종목 점수, 매매 판단, `AnalysisContextPack` 변경
- `intelligence_items` 테이블과의 병합
- 자연재해·기후 및 나머지 World Monitor 도메인
- 신규 공개 API 엔드포인트 (기존 진단 표면만 확장)
- World Monitor 소스 수정
- 백테스트 실행 (저장 모델이 이를 **가능하게** 만드는 것까지가 이번 범위)

## 3. 상류 계약 조사 결과

엔드포인트는 이름이 아니라 실제 요청/응답 계약과 라우팅을 확인해 선정했다.
세 엔드포인트 모두 **GET + query string**이며 `submodule 6c48a33c` 기준이다.

| 카테고리 | 경로 | 백킹 저장소 | 상류 실패 시 |
| --- | --- | --- | --- |
| `geopolitical_conflict` | `/api/conflict/v1/list-acled-events` | read-through 캐시(TTL 900s) — miss 시 ACLED 실시간 fetch (§3.2) | 빈 배열 |
| `infrastructure_outage` | `/api/infrastructure/v1/list-internet-outages` | 순수 seeder 스냅샷(`infra:outages:v1`) | 빈 배열 |
| `supply_chain_energy` | `/api/supply-chain/v1/list-energy-disruptions` | seeder 레지스트리 스냅샷 | `upstreamUnavailable: true` |

필드 매핑:

| 정규화 필드 | ACLED | Internet outage | Energy disruption |
| --- | --- | --- | --- |
| `external_id` | `id` (`acled-<event_id_cnty>`) | `id` | `id` |
| `title` | `eventType` + `country` 조합 | `title` | `shortDescription` |
| `occurred_at` | `occurredAt` (epoch ms) | `detectedAt` (epoch ms) | `startAt` (ISO 8601) |
| `ended_at` | 없음 (항상 null) | `endedAt` (0 = 진행 중) | `endAt` (`''` = 진행 중) |
| `countries` | `country` 단일 | `country`, `region` | `countries[]` (ISO2) |
| `severity_raw` | `fatalities` | `severity` (enum) | `capacityOfflineMbd`, `capacityOfflineBcmYr` |
| `url` | 없음 | `link` | `sources[].url` 첫 항목 |

세 소스 모두 **원본 발생 시각을 제공**하므로 승인된 결정 3(원본 발생 시각 + 수집
시각 보존)은 상류 계약상 충족 가능하다. 상류가 발생 시각을 주지 못하는 레코드는
저장하지 않는다 — 발생 시각 없는 이벤트는 미래정보 누출 차단을 보장할 수 없다.

### 3.1 세 소스의 신뢰성이 서로 다르다

이것이 이 설계에서 가장 중요한 제약이다.

- `list-energy-disruptions`만 `upstreamUnavailable`로 "Redis가 비었음"과
  "이벤트가 없음"을 구분한다.
- `list-acled-events`와 `list-internet-outages`는 **둘 다 빈 배열을 반환**하므로
  응답만으로는 구분이 불가능하다.

즉 seeder가 죽어 있어도 인프라 카테고리는 조용히 "장애 없음"처럼 보인다. 이를
그대로 프롬프트에 넣으면 시장 리뷰에 거짓 확신이 들어간다. §7이 이 문제를 다룬다.

### 3.2 ACLED 캐시는 기본적으로 적중하지 않는다

`list-acled-events`의 Redis 캐시 키는 요청 창의 시작·종료 밀리초를 포함한다
(`conflict:acled:v1:<country>:<startMs>:<endMs>`). 따라서 요청마다 시각이 조금이라도
다르면 키가 매번 달라지고 **900초 TTL은 한 번도 적중하지 않는다.** 파라미터를 아예
생략해도 서버가 `Date.now()`로 채우므로 결과는 같다.

두 가지 결론이 따라온다.

1. 이 카테고리는 사실상 **매 동기화가 실시간 제3자 요청**이다. 따라서 §6의 쿨다운이
   유일한 rate-limit 보호 장치이고, §6.1의 예산은 예외 상황이 아니라 정상 경로를
   감당해야 한다.
2. 그래서 요청 창의 경계를 **자연일로 정렬**한다. 같은 날 반복 동기화는 동일한 키를
   만들어 상류 캐시를 실제로 재사용할 수 있다. 창은 최대 하루 넓어지지만 30일
   조회 창이 흡수한다.

## 4. 정규화 스키마

신규 테이블 `world_events`. `intelligence_items`와 병합하지 않는다.

| 컬럼 | 타입 | 비고 |
| --- | --- | --- |
| `id` | Integer PK | |
| `category` | String(32), index | 세 값 중 하나 |
| `source` | String(32) | 현재는 `worldmonitor` 고정 |
| `source_endpoint` | String(128) | 재현·감사용 상류 경로 |
| `external_id` | String(200), index | 상류 안정 ID |
| `title` | String(300) | |
| `summary` | Text | nullable |
| `url` | String(1000) | nullable |
| `occurred_at` | DateTime, index | **원본 발생 시각**, NOT NULL |
| `ended_at` | DateTime | nullable, null = 진행 중 |
| `collected_at` | DateTime, index | **DSA 수집 시각** |
| `countries` | Text | ISO2 JSON 배열 |
| `markets` | Text | 매핑된 DSA 시장 JSON 배열 (§8) |
| `scope` | String(16), index | `market` / `global` / `unmapped` (§8.1) |
| `severity_rank` | Integer, index | 카테고리 내부 정렬용 (§9) |
| `raw_payload` | Text | 상류 원본 JSON |

- `UniqueConstraint('category', 'external_id')` — 재동기화 멱등성의 근거.
- `Index('ix_world_events_cat_time', 'category', 'occurred_at')` — 프롬프트 조회 경로.
- 갱신 정책: 동일 `(category, external_id)` 재수신 시 `ended_at`, `severity_rank`,
  `raw_payload`를 갱신하고 **`occurred_at`과 `collected_at`은 최초 값을 유지**한다.
  진행 중 이벤트가 종료되는 것을 반영하되, 최초 관측 시각은 감사·백테스트를 위해
  불변으로 둔다.

`raw_payload`를 보존하는 이유는 후속 단계에서 정규화 규칙이 바뀌어도 재정규화가
가능하게 하기 위해서다. `IntelligenceItem.raw_payload`와 같은 관례를 따른다.

### 4.1 미래 발생 시각 거부

`occurred_at`이 수집 시점보다 미래인 레코드는 **저장하지 않는다.** 상류가 잘못된
타임스탬프를 주거나 시간대 처리가 어긋나면 아직 일어나지 않은 사건이 오늘의 시장
리뷰에 들어가고, 그대로 저장되면 후속 백테스트의 미래정보 누출 원인이 된다.
저장 시점과 프롬프트 조회 시점 양쪽에서 `occurred_at <= now`를 강제한다(저장
이후에 시계가 되감기는 환경을 고려해 조회 측 필터도 함께 둔다).

## 5. 저장 계층

기존 관례를 그대로 따르고 평행 구현을 만들지 않는다.

- 모델: `src/storage.py`에 `WorldEvent` 추가 (`IntelligenceItem` 인접).
- 리포지토리: `src/repositories/world_event_repo.py`
  (`IntelligenceRepository`의 세션 사용 패턴을 따름).
- 서비스: 기존 `src/services/worldmonitor_service.py`를 확장한다. 새 서비스
  모듈을 만들지 않는다 — 이미 base URL 정규화, timeout 정책, 진단 문자열
  sanitize가 이 모듈에 있고 이를 재사용해야 한다.
- 카테고리별 상류 지식(엔드포인트, 응답 배열 키, 정규화 함수, 시간 창 수용
  여부)은 **한 곳에 선언한다**(`CategorySpec`). 후속 단계에서 자연재해 등
  카테고리를 추가할 때 세 군데를 고치게 만들면 안 되고, 특히 "이 카테고리만
  예외" 분기는 카테고리가 늘수록 `if category in (...)`으로 퇴화한다.

조회 시 시장 필터는 **SQL로 내려보낸다.** `global` 스코프는 설계상 모든 시장에
포함되므로 이 조건은 선택성이 낮고, Python 측에서 거르면 조회 창 전체 행을
`raw_payload` 대문자열까지 포함해 인스턴스화하게 된다. 이 코드는 시장 리뷰 시작
직전 인라인 경로에서 돈다. 진단용 건수도 행을 읽어 `len()`하지 않고 count 질의를
쓴다.

## 6. 수집 시점

**시장 리뷰 직전 동기화**로 한다.

- `run_market_review`가 시장별 분석기를 만들기 전에 1회 동기화한다.
- 쿨다운 `WORLDMONITOR_SYNC_COOLDOWN_SECONDS`(기본 1800)보다 최근에 시도한
  동기화가 있으면 건너뛴다.
- 쿨다운 판정은 **영속 상태**(`world_event_sync_state.last_attempt_at`)로 한다.
  `run_market_review`는 호출마다 서비스 인스턴스를 새로 만들기 때문에 인스턴스
  속성으로 판정하면 운영 경로에서 쿨다운이 한 번도 걸리지 않는다.
- DSA 측 별도 백그라운드 스케줄러를 만들지 않는다. 3개 중 2개 소스가 이미
  seeder의 1800초 주기 스냅샷이므로 DSA가 더 자주 폴링해도 신선도 이득이 없고,
  스케줄러 스레드만 추가되어 기존 wedge 리스크 표면이 넓어진다.
- 쿨다운은 신선도를 위한 장치가 아니라 **중복 HTTP 억제** 장치다. 실제 신선도는
  seeder 주기가 결정하며, 그 사실을 §7의 신선도 판정이 명시적으로 다룬다.

동기화는 전 과정이 fail-open이다.

- 연동 비활성(`WORLDMONITOR_ENABLED=false`) → 즉시 no-op.
- HTTP 실패·timeout·파싱 실패 → 해당 카테고리만 실패로 기록하고 나머지는 계속.
- 전체 실패 → 저장된 기존 이벤트로 시장 리뷰를 계속 진행한다.
- **어떤 경우에도 예외를 시장 리뷰 파이프라인으로 전파하지 않는다.**

### 6.1 시간 예산

동기화는 시장 리뷰 시작 **직전 인라인**으로 실행되므로 상류 지연이 곧 시장 리뷰
지연이다. `list-acled-events`는 캐시 miss 시 rate-limit이 걸린 제3자(ACLED)로
실제 아웃바운드 요청을 하므로 로컬 응답만 가정할 수 없다.

- 기존 `worldmonitor_read_timeout_seconds`(기본 5.0)를 **재사용하지 않는다.**
  그 값은 로컬 `/api/health` 프로브용으로 정해진 값이다.
- 신규 `WORLDMONITOR_SYNC_BUDGET_SECONDS`(기본 20)를 **동기화 전체의 마감시한**으로
  둔다. 카테고리별 개별 timeout이 아니라 전체 예산이며, 예산이 소진되면 남은
  카테고리는 요청하지 않고 이전 상태를 유지한 채 즉시 반환한다.
- 전체 예산으로 두는 이유는 카테고리별 timeout을 곱하면 최악의 경우 지연이
  카테고리 수에 비례해 늘어나 시장 리뷰 시작이 예측 불가능해지기 때문이다.

### 6.2 카테고리별 적재 상한

전체 예산은 **카테고리 사이에서만** 검사되므로 단일 카테고리 안에서 벌어지는
작업은 예산으로 막을 수 없다. ACLED의 30일 전역 창은 수천 건이 될 수 있고
적재는 건당 SELECT를 수반하므로, 카테고리마다 자체 상한이 필요하다.

- `WORLDMONITOR_EVENT_MAX_PER_SYNC`(기본 500)로 1회 동기화당 카테고리별 적재를
  제한한다.
- 상한에 걸려 버린 건수는 **반드시 로그로 남긴다.** 조용한 절단은 "전부
  수집했다"로 읽히며, 이는 §3.1이 막으려는 거짓 확신과 같은 종류의 문제다.
- 아울러 `list-acled-events`에는 `start`/`end`를 명시해 실제로 읽을 조회 창만
  요청한다. 이 엔드포인트는 캐시 miss 시 rate-limit이 걸린 제3자로 실제 요청을
  보내므로, 쓰지도 않을 데이터를 받아오지 않는 편이 낫다. 나머지 두 엔드포인트는
  seeder 스냅샷 조회라 시간 창 파라미터를 받지 않는다.

### 6.3 시각 기준

`occurred_at`은 **naive 로컬 시각**으로 통일한다.

epoch ms 경로(`datetime.fromtimestamp`)는 로컬을, ISO 경로는 자칫 UTC 벽시계
값을 남기기 쉽다. 두 값이 같은 컬럼에 섞이고 로컬 `datetime.now()`와 비교되므로,
KST에서는 9시간이 어긋나 카테고리 간 정렬이 뒤집히고 프롬프트에 표시되는 날짜가
하루 틀어진다. 따라서 tz-aware 입력은 반드시 로컬로 변환한 뒤 tzinfo를 제거한다.

## 7. 신선도 판정

카테고리별로 마지막 **성공** 동기화 시각을 기록하고, 프롬프트에 신선도를 명시한다.
"이벤트 없음"과 "확인 불가"를 절대 같은 문장으로 표현하지 않는다.

| 판정 | 조건 | 프롬프트 표현 |
| --- | --- | --- |
| `fresh` | 마지막 성공 동기화가 임계값 이내 | 이벤트 목록 + 수집 시각 |
| `stale` | 임계값 초과 | 저장된 이벤트 + "최신 확인 실패, N시간 전 데이터" |
| `unavailable` | 성공 이력 없음 또는 `upstreamUnavailable` | "이 카테고리는 확인할 수 없음" (이벤트 없음으로 표기하지 않음) |
| `unverified` | 동기화는 성공하지만 이벤트를 한 번도 받지 못함 (§7.1) | "이 카테고리는 확인할 수 없음" |

- 임계값: `WORLDMONITOR_EVENT_STALE_AFTER_SECONDS`(기본 7200 = seeder 주기의 4배).
- `list-energy-disruptions`의 `upstreamUnavailable: true`는 즉시 `unavailable`로
  판정한다. 응답이 명시적으로 알려주는 유일한 소스이므로 이를 버리지 않는다.
  이 플래그는 **별도 컬럼으로 영속화**하고 시각 계산보다 **먼저** 평가한다.
  시각만 보면 "10분 전 동기화 성공, 지금 상류 다운"이 `fresh`로 읽혀 오래된
  저장 이벤트가 현재 상황처럼 프롬프트에 들어간다.
- 반면 단순 연결 실패는 즉시 `unavailable`로 올리지 않고 `stale` 임계값까지
  기존 저장 이벤트를 계속 쓴다. seeder 주기의 4배 안에서는 일시적 실패보다
  직전 스냅샷이 더 유용하다는 판단이며, 임계값을 넘기면 `stale`로 표기된다.
- 나머지 두 소스는 빈 배열과 장애를 구분할 수 없으므로 **DSA 측 마지막 성공
  동기화 시각**이 유일한 판정 근거다. 이 때문에 성공 이력을 반드시 영속화한다.
- 성공 동기화 이력은 `world_event_sync_state` 테이블(카테고리, 마지막 성공 시각,
  마지막 **비어있지 않은** 성공 시각, 마지막 결과, sanitize된 마지막 오류)에
  저장한다.

빈 응답을 "이벤트 없음"으로 단정하지 않는 것이 이 절의 핵심 계약이다.

### 7.1 "성공했지만 항상 비어 있음"

마지막 성공 시각만으로는 계약이 닫히지 않는다. 구체적 반례:

상류에 ACLED API 키가 설정돼 있지 않으면 `fetchAcledConflicts`가 예외를 삼키고
**영구히 빈 배열**을 반환한다. DSA 입장에서는 HTTP 200이므로 동기화가 성공한
것으로 기록되고, 신선도는 `fresh`가 되며, 프롬프트에는 "분쟁 이벤트 해당 없음"이
들어간다. 이는 §3.1이 막으려던 바로 그 거짓 확신이 §7의 시각 기준을 우회해
들어오는 경로다.

따라서 신선도는 **마지막 성공 시각과 마지막 비어있지 않은 성공 시각을 함께** 본다.

| 상태 | 이벤트 수 | `last_nonempty_at` | 판정 |
| --- | --- | --- | --- |
| 성공 | 1건 이상 | 갱신됨 | `fresh` |
| 성공 | 0건 | 조회 창 이내에 존재 | `fresh`, "해당 없음" 표기 허용 |
| 성공 | 0건 | 없음 또는 조회 창보다 오래됨 | `unverified`, "확인 불가" |

즉 **"해당 없음"이라고 말하려면 그 카테고리가 실제로 이벤트를 산출할 수 있다는
증거가 조회 창 안에 있어야 한다.** 한 번도 이벤트를 준 적 없는 카테고리는 조용히
"이상 없음"이 되는 대신 "확인 불가"로 표기된다.

`unverified`는 `unavailable`과 프롬프트 표현이 같지만 원인이 다르므로(도달 실패 vs
도달은 되나 산출 없음) 진단에서는 구분해 노출한다.

## 8. 시장·지역 매핑

상류는 ISO2 국가 코드를 주고 DSA는 `cn`/`hk`/`us`/`kr`/`jp` 시장 단위로 동작한다.

- 정적 매핑 테이블을 명시적으로 둔다. ISO2 국가 코드 1개가 DSA 시장 1개에
  대응한다: `CN`→`cn`, `HK`→`hk`, `US`→`us`, `KR`→`kr`, `JP`→`jp`.
- 국가 간 파급(예: 중국 이벤트의 홍콩 시장 영향)은 이 표에서 다루지 않는다.
  파급 관계를 임의로 넣으면 검증되지 않은 인과를 프롬프트에 주입하게 되므로,
  1:1 대응만 두고 판단은 LLM에 맡긴다.
- 어느 시장에도 매핑되지 않는 국가의 이벤트도 **저장은 한다.** 중동 분쟁이나
  홍해 항로 차질처럼 발생 국가가 어느 DSA 시장도 아니면서 전 시장에 영향을 주는
  사례가 이 카테고리의 본질이기 때문이다.
- 시장 리뷰 프롬프트에는 **해당 시장 매핑 이벤트 + `global` 이벤트**를 넣는다.

### 8.1 `global`과 `unmapped`는 다르다

`markets`가 빈 배열이 되는 경로는 두 가지이고 의미가 정반대다.

| 스코프 | 조건 | 의미 | 프롬프트 |
| --- | --- | --- | --- |
| `global` | `countries`가 **비어 있지 않고** 어느 것도 DSA 시장에 매핑되지 않음 | 발생지를 알며, DSA 시장 밖에서 일어남 | 모든 시장에 포함 |
| `unmapped` | `countries`가 **비어 있음** | 발생지를 모름 | 제외 |

`EnergyDisruptionEntry.countries`는 상류 주석이 명시하듯 denorm 이전에 기록된
레거시 행에서 빈 배열일 수 있고, 상류는 소비자가 길이로 이를 감지하기를 기대한다.
두 경로를 하나의 `global`로 합치면 **발생지를 모르는 레거시 행이 조용히 "전 시장
영향"으로 승격된다.** 따라서 빈 `countries`는 `unmapped`로 분리해 저장하되
프롬프트에서 제외하고, 건수만 진단에 노출한다.

`InternetOutage`는 `country`와 `region`을 모두 제공하므로 `country`를 우선 쓰고
비어 있으면 `region`으로 보완한다. 둘 다 비면 `unmapped`다.
- 이번 단계에서 산업/섹터 노출도 매핑은 하지 않는다. 근거 없는 정밀도를 만들지
  않는다. 후속 단계로 미룬다.

## 9. 프롬프트 표현과 상한

`MarketAnalyzer._build_review_prompt`에 카테고리별 블록을 추가한다. 기존
`_build_kr_*_prompt_block` 관례를 그대로 따르고 en/ko/zh 3개 언어를 모두 제공한다.

- 카테고리마다 최대 `WORLDMONITOR_EVENT_PROMPT_LIMIT`(기본 5)건.
- 선정 기준: 진행 중 이벤트 우선 → `severity_rank` 내림차순 → `occurred_at` 최신순.
- `severity_rank`는 카테고리 **내부** 정렬용이며 카테고리 간 비교 의미는 없다.
  재현 가능하도록 정수 매핑을 여기에 고정한다.

| 카테고리 | 원본 | `severity_rank` |
| --- | --- | --- |
| conflict | `fatalities` | 그 값 그대로 (없으면 0) |
| outage | `OUTAGE_SEVERITY_TOTAL` | 3 |
| outage | `OUTAGE_SEVERITY_MAJOR` | 2 |
| outage | `OUTAGE_SEVERITY_PARTIAL` | 1 |
| outage | `OUTAGE_SEVERITY_UNSPECIFIED` / 미상 | 0 |
| energy | `capacityOfflineMbd` | `round(값 × 10)` |
| energy | `capacityOfflineMbd`가 0이고 `capacityOfflineBcmYr` > 0 | `round(BcmYr × 0.172 × 10)` (BCM/yr → Mbd 환산) |
| energy | 둘 다 0 | 0 |

  상류 projector가 두 용량 필드 모두 결측 시 `0`으로 강등하므로 `severity_rank`가
  0으로 동률인 이벤트가 정상적으로 발생한다. 이때는 `occurred_at` 최신순 tiebreak가
  순서를 결정하며, 이는 결함이 아니라 의도된 동작이다.

- 각 블록 머리에 신선도 한 줄을 넣는다.
- 조회 창은 `now - WORLDMONITOR_EVENT_LOOKBACK_DAYS <= occurred_at <= now`(기본 30일).
  상한을 함께 거는 이유는 §4.1과 같다.
- 이벤트가 하나도 없고 신선도가 `fresh`일 때만 "해당 없음"으로 표기한다
  (§7.1의 `last_nonempty_at` 조건을 함께 만족해야 한다).
- `unmapped` 스코프 이벤트는 프롬프트에서 제외한다(§8.1).
- 카테고리별 상한으로 두는 이유는 전역 상한을 쓰면 분쟁 이벤트가 상한을 독점해
  인프라·에너지 카테고리가 프롬프트에서 사라질 수 있기 때문이다.

## 10. 설정

기존 `WORLDMONITOR_*` 네임스페이스를 이어서 사용한다. 전부 미설정 시 기존 동작과
동일하다(연동 자체가 `WORLDMONITOR_ENABLED=false` 기본값에 걸려 no-op).

| 키 | 기본값 | 의미 |
| --- | --- | --- |
| `WORLDMONITOR_EVENTS_ENABLED` | `false` | 이벤트 수집·주입 마스터 스위치 |
| `WORLDMONITOR_SYNC_COOLDOWN_SECONDS` | `1800` | 재동기화 최소 간격 |
| `WORLDMONITOR_SYNC_BUDGET_SECONDS` | `20` | 동기화 전체 마감시한 (§6.1) |
| `WORLDMONITOR_EVENT_STALE_AFTER_SECONDS` | `7200` | `stale` 판정 임계값 |
| `WORLDMONITOR_EVENT_RETENTION_DAYS` | `90` | 보관 기간 |
| `WORLDMONITOR_EVENT_LOOKBACK_DAYS` | `30` | 프롬프트 조회 창 |
| `WORLDMONITOR_EVENT_PROMPT_LIMIT` | `5` | 카테고리별 프롬프트 상한 |
| `WORLDMONITOR_EVENT_MAX_PER_SYNC` | `500` | 1회 동기화 시 카테고리별 적재 상한 (§6.2) |

`WORLDMONITOR_EVENTS_ENABLED`를 `WORLDMONITOR_ENABLED`와 분리하는 이유는
self-hosting 단계의 상태 확인만 쓰던 사용자가 업그레이드만으로 프롬프트 변화를
겪지 않게 하기 위해서다. 이벤트 주입은 명시적 opt-in이다.

## 11. 보관과 정리

- 보관 기간 90일. `occurred_at` 기준으로 초과분을 삭제한다.
- 정리는 동기화 성공 직후에만 수행한다. 별도 정리 스케줄러를 만들지 않는다.
- 90일은 ACLED 기본 조회 창(30일)의 3배로, 재조회 갭을 흡수하면서 후속 단계의
  미래정보 누출 차단 백테스트에 쓸 이력을 남기는 균형점이다.

## 12. 진단 노출

신규 공개 API를 만들지 않는다. 기존 진단 표면에 읽기 전용 요약만 추가한다.

- 카테고리별 마지막 성공 동기화 시각, 마지막 비어있지 않은 성공 시각, 신선도
  판정, 저장 건수.
- `unverified`와 `unavailable`은 프롬프트 문구가 같지만 원인이 다르므로(도달은
  되나 산출 없음 vs 도달 실패) 진단에서는 반드시 구분해 노출한다. 운영자가
  "상류 API 키 미설정"과 "World Monitor 다운"을 구별할 수 있어야 한다.
- `unmapped` 건수를 노출한다. 이 값이 계속 커지면 상류 denorm 미적용을 뜻한다.
- 마지막 오류는 `sanitize_diagnostic_text`를 거친 짧은 문자열만 노출한다.
- `/api/health`의 기존 성공/실패 의미를 바꾸지 않는다.

## 13. 오류 처리

| 상황 | 동작 |
| --- | --- |
| 연동/이벤트 비활성 | no-op, 프롬프트 블록 없음 |
| World Monitor 도달 불가 | 카테고리 전부 실패 기록, 저장 이벤트로 계속 |
| 일부 카테고리만 실패 | 실패 카테고리만 `stale`/`unavailable`, 나머지 정상 |
| `upstreamUnavailable: true` | 해당 카테고리 `unavailable` |
| 성공하지만 항상 빈 응답 (상류 API 키 미설정 등) | `unverified`, "해당 없음" 표기 금지 (§7.1) |
| 시간 예산 소진 | 남은 카테고리 미요청, 이전 상태 유지 (§6.1) |
| 발생 시각 없는 레코드 | 저장하지 않고 건너뜀 |
| 발생 시각이 미래인 레코드 | 저장·조회 양쪽에서 배제 (§4.1) |
| `countries` 비어 있음 | `unmapped`로 저장, 프롬프트 제외 (§8.1) |
| 상류 스키마 변경 | 해당 레코드만 건너뛰고 로그, 동기화 전체는 계속 |
| DB 쓰기 실패 | 시장 리뷰는 계속, 진단에 기록 |

## 14. 검증

단위·통합(오프라인):

- 세 소스 각각의 정규화 매핑과 발생 시각 보존
- 재동기화 멱등성 (`(category, external_id)` 중복 삽입)
- 진행 중 → 종료 전이 시 `occurred_at`/`collected_at` 불변
- 발생 시각 없는 레코드 거부
- `fresh`/`stale`/`unavailable` 3분기 판정
- **빈 응답이 "이벤트 없음"으로 표기되지 않는 회귀 테스트** (§3.1 핵심 계약)
- **항상 빈 응답을 주는 카테고리가 `unverified`가 되는 회귀 테스트** (§7.1,
  상류 ACLED 키 미설정 시나리오)
- 0건이라도 `last_nonempty_at`이 조회 창 이내면 "해당 없음"이 허용되는 경로
- `upstreamUnavailable`가 `unavailable`로 이어지는 경로
- World Monitor 장애가 시장 리뷰를 실패시키지 않는 fail-open 회귀 테스트
- **시간 예산 소진 시 남은 카테고리를 요청하지 않고 반환하는지** (§6.1)
- 카테고리별 적재 상한과 절단 시 플래그·로그 (§6.2)
- `list-acled-events` 요청이 조회 창으로 제한되는지 (§6.2)
- **epoch 경로와 ISO 경로가 같은 순간에 대해 같은 값을 내는지** (§6.3)
- **`upstreamUnavailable`가 직전 성공을 덮어쓰는지** (§7, 시각만 보는 판정의 반례)
- **새 서비스 인스턴스에서도 쿨다운이 유지되는지** (§6, 운영 경로 재현)
- 카테고리별 프롬프트 상한과 카테고리 독점 방지
- `severity_rank` 정수 매핑과 동률 시 `occurred_at` tiebreak 순서 고정
- **빈 `countries`가 `global`이 아니라 `unmapped`가 되고 프롬프트에서 빠지는지** (§8.1)
- 보관 기간 정리
- 미래 `occurred_at` 이벤트가 저장되지 않고 프롬프트에도 들어가지 않는 회귀 테스트
- 3개 언어 프롬프트 블록 생성

온라인(네트워크 필요, 미실행 시 교부에 명시):

- 실제 로컬 World Monitor 스택 대상 3개 엔드포인트 동기화
- seeder 중단 상태에서 `unavailable` 판정 실증

## 15. 문서와 변경 기록

- `.env.example`, `.env.example.ko`: 신규 6개 설정
- `docs/intelligence-sources.md`: 이번 단계에서 이벤트가 시장 리뷰에만 들어가고
  종목 판단에는 쓰이지 않는다는 경계 갱신
- `docs/CHANGELOG.md` `[Unreleased]`: 扁平 형식 1줄
- 시장 리뷰 프롬프트 변경이므로 PR 설명에 영향받는 리포트 스크린샷 첨부

## 16. Rollback

- `WORLDMONITOR_EVENTS_ENABLED=false` → 프롬프트 주입과 수집이 즉시 멈춘다.
  테이블과 데이터는 남지만 읽지 않는다.
- 코드 rollback 시 모델·리포지토리·서비스·프롬프트 블록·설정·문서를 한 단위로
  되돌린다. `world_events` 테이블은 additive이므로 남아 있어도 기존 경로에
  영향이 없다.
- 기존 `WORLDMONITOR_ENABLED` 상태 확인 경계는 이번 변경과 독립적으로 유지된다.

## 17. 후속 단계

1. 산업·섹터 노출도 매핑
2. 미래정보 누출을 차단한 백테스트로 실제 개선 여부 측정
3. 유의미한 개선이 확인된 신호만 개별 종목 판단에 반영 검토
4. 자연재해·기후 등 나머지 World Monitor 도메인 확장

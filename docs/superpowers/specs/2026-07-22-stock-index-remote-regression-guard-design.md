# 원격 주식 인덱스 회귀 가드 + URL 설정화 — 설계 스펙

- 작성일: 2026-07-22
- 상태: 설계 확정 (그릴링 결정 3건 반영). **v2(advisor 사전 점검): 주 수정을 쓰기 가드→로더 측 완전성 게이트로 전환. 쓰기 가드만으로는 이미 오염된(그리고 반복 재생성되는) 캐시를 로더가 계속 우선해 라이브 버그가 안 고쳐지고 오히려 오염이 고착됨. 실측: 업스트림 vs 포크는 KR만 다름(KR 30 vs 2767), 나머지 시장 완전 동일 → 0.8 임계는 KR에서만 발동(타 시장 오탐 없음)하고, 원격 새로고침 dormancy는 현재 무비용(업스트림이 포크 대비 KR 외 추가분 없음).**
- 유형: fix (프로덕션 버그 — KR 인덱스 원격 덮어쓰기)
- 관련 영역: `src/services/stock_index_remote_service.py`, `src/data/stock_index_loader.py`, `src/config.py`, `src/core/config_registry.py`, `.env.example`(+.ko), `main.py`(리프레시 트리거), `tests/`

## 1. 문제와 목표

### 발견된 버그 (실측)
일일 분석은 원격 주식 인덱스를 TTL 48시간으로 새로고침한다(`main.py:524`, `:694`). 원격 URL이 **업스트림 원본 레포**(`raw.githubusercontent.com/ZhuLinsen/daily_stock_analysis/main/apps/dsa-web/public/stocks.index.json`)로 하드코딩돼 있는데, 업스트림 인덱스는 **KR 희소**(~60종)다. 이 포크는 KR 로드맵 A에서 KR을 **2,767종**으로 확장(`expand_kr_index.py`, 커밋된 `apps/dsa-web/public/stocks.index.json`)했다.

`validate_stock_index_payload`는 전체 항목 수(`min_items=100`)와 항목 형식만 검사하고 **시장별 완전성은 검증하지 않는다**. 따라서 KR 희소 업스트림 인덱스가 검증을 통과해 `data/cache/stocks.index.json`을 덮고, 로더는 이 캐시를 커밋된 완전 인덱스보다 **우선**한다(`stock_index_loader.py:33` 후보 경로 순서). 결과: 이틀마다 원격 새로고침이 배포된 일일 분석의 **KR 종목명·코드 해석을 60종 인덱스로 저하**시킨다 — 포크가 만든 KR 자동완성/이름해석 능력이 조용히 무력화된다. (로컬 테스트 `test_expanded_kr_map_reaches_loader` 반복 실패의 근본 원인이기도 하다.)

### 목표
원격 새로고침이 **어느 시장이든 회귀시키는 인덱스로 커밋 기준선을 덮지 못하게** 하고, 원격 URL을 배포가 포크로 가리킬 수 있게 설정화한다. 미설정 시에도 안전(회귀 가드가 URL과 무관하게 보호).

### 그릴링 확정 결정 (3건)
1. **수정 방향**: 회귀 가드(핵심) + URL env 설정화. 가드는 URL 오설정·업스트림 드리프트와 무관하게 견고.
2. **가드 범위**: 전 시장 회귀 거부(KR만이 아니라 어느 시장이든 기준선 대비 규정 이상 회귀하면 원격 전체 거부).
3. **거부 시 폴백**: 커밋 인덱스 유지 + WARNING 로그(기존 best-effort/fail-open 기조 일관, 단 이전의 조용한 클로버를 가시화).

## 2. 확정된 설계 결정

**설계 우선순위 (v2)**: 라이브 버그를 고치는 것은 **로더 측 완전성 게이트**(주). 쓰기 가드는 미래 오염을 막는 보조. 이유: 캐시가 이미(그리고 Docker 재기동마다 반복) KR 희소로 오염돼 있고 로더가 이를 커밋 인덱스보다 우선하므로, 쓰기만 막으면 오염이 고착돼 KR이 계속 60종으로 저하된다. 로더 게이트가 오염 캐시를 **읽기 시점에 자가 치유**(unusable 판정→커밋 인덱스로 폴백)한다.

| 결정 | 값 | 근거 |
|---|---|---|
| **(주) 로더 완전성 게이트** | 로더가 원격 캐시를 사용 가능으로 판정하는 지점에 **시장 회귀 검사** 추가 — 캐시의 시장별 카운트가 커밋 기준선 대비 `< ratio × baseline[market]`이면 그 캐시를 **unusable**로 보고 다음 후보(커밋 인덱스)로 폴백. **두 경로 모두 적용**: `_is_remote_stock_index_cache_usable`(`stock_index_loader.py:231`, `find_existing_stock_index_path`가 사용) **및** `get_stock_name_index_map`(`:271` 부근, 현재 usability 게이트를 안 거치고 첫 파싱 성공 후보를 로드하는 별도 경로 — 여기에도 동일 게이트 필요) | advisor. 이미 오염된/재생성되는 캐시를 읽기 시점에 자가 치유. 수동 삭제에 의존하지 않음 |
| (보조) 쓰기 회귀 가드 | 원격 payload 수용(`_atomic_write`) 전, **시장별 항목 수를 커밋 기준선과 비교**해 어느 한 시장이라도 `count < ratio × baseline[market]`이면 **원격 전체를 거부**(부분 병합 안 함), 캐시 미변경 | 그릴링 2 + 방어심층. 오염이 애초에 디스크에 안 닿게(로더 게이트가 실패해도 이중 방어) |
| 기준선(baseline) | 커밋된 `apps/dsa-web/public/stocks.index.json`의 시장별 항목 수. 로더 후보 경로 중 이 커밋 파일이 권위 있는 floor | 커밋 인덱스가 포크의 큐레이션된 완전 인덱스. 캐시가 아니라 커밋 파일 기준(캐시는 이미 오염됐을 수 있음) |
| 임계 비율 | `STOCK_INDEX_REMOTE_MIN_MARKET_RATIO`(신규 env, 기본 **0.8** = 시장별 20% 초과 회귀 거부). `(0,1]` 범위·유한 검증, 벗어나면 ERROR 로그+기본값 강제(기존 config 패턴) | 정상 종목 churn(상폐 등 소폭)은 허용, KR 60/2767(97.8% 회귀)은 확실히 차단. 튜너블 |
| 시장 부재 처리 | 기준선에 있는 시장이 원격에서 0/부재면 회귀로 간주(거부). 원격이 기준선에 없는 **신규 시장** 추가는 허용(회귀 아님) | 시장 통째 누락이 가장 심한 회귀 |
| URL 설정화 | 신규 env `STOCK_INDEX_REMOTE_URL`(기본 = 현행 업스트림 URL, 문서화). 배포는 이를 포크(`jeongsk/...`)로 설정 가능. 하드코딩 상수는 default로만 유지 | 그릴링 1. 리포지토리 규칙(env 차이 하드코딩 금지) + 가드가 오설정 방어 |
| 원격 새로고침 dormancy (수용) | 가드+전체거부 하에서, URL=업스트림이면 매 새로고침이 KR 회귀로 거부(캐시 미갱신), URL=포크면 원격 파일이 커밋 파일과 동일해 갱신이 무의미 → **원격 새로고침 서브시스템이 사실상 휴면**. **실측상 현재 무비용**(업스트림 vs 포크는 KR만 다르고 CN/HK/US/JP/BSE 완전 동일 — 업스트림이 제공할 추가 churn이 없음). 미래에 업스트림 CN/US가 갱신되면 포크는 빌드타임 `expand_kr_index.py` 재스플라이스로 반영(기존 흐름) | advisor #2. dormancy는 사고가 아니라 의도된 수용. 스플라이스/병합 방식은 비범위(그릴링서 미채택) |
| 거부 폴백 | 원격 payload 거부 시 캐시를 **덮지 않고**, 기존(커밋 또는 이전 유효 캐시) 인덱스 유지. `RemoteStockIndexResult.refreshed=False`+거부 사유. 로그: WARNING, 시장별 before/after 카운트 포함 | 그릴링 3. best-effort 유지, 가시화 |
| 검증 위치 | `validate_stock_index_payload`는 형식 검증 유지. 회귀 비교는 **수용 경로**(`refresh_remote_stock_index_cache`가 다운로드 후 `_atomic_write` 직전)에 신규 함수 `assert_no_market_regression(payload, baseline_counts, ratio)`로 추가 | 형식(무조건)과 회귀(기준선 상대) 관심사 분리 |
| 다운로드 후 원자성 | 회귀 가드는 `_atomic_write` **전**에 통과해야 하며, 실패 시 temp 파일도 남기지 않음 | 오염 캐시가 디스크에 안 닿음 |

## 3. 데이터 흐름

### (주) 로더 읽기 경로 — 라이브 버그를 고치는 곳
```
[로더가 인덱스 로드: find_existing_stock_index_path / get_stock_name_index_map]
  후보 순회(원격 캐시 우선, 그다음 커밋 apps/dsa-web/public/, static/):
    각 후보에 대해:
      - 형식 유효?(기존 is_valid_remote_stock_index_file)
      - **(신규) 시장 회귀 없음?** per_market_counts(후보) 대비
        baseline = per_market_counts(커밋 인덱스): 어느 시장이라도
        count < ratio × baseline[m] 이면 이 후보 unusable → 다음 후보로
    → 첫 usable 후보 사용
  결과: 오염(KR 희소) 원격 캐시는 unusable → 커밋 인덱스(KR 2767) 사용 = 자가 치유
  (baseline이 곧 커밋 인덱스일 때 자기 자신과 비교는 항상 통과 — 순환 없음)
```

### (보조) 쓰기 경로 — 미래 오염 예방
```
[일일 분석 시작 → refresh_remote_stock_index_cache(settings)]
  TTL(48h) 이내면 skip. 아니면:
  1. GET settings.url (STOCK_INDEX_REMOTE_URL, 기본 업스트림)
  2. validate_stock_index_payload(payload)  # 형식 — 무조건
  3. assert_no_market_regression(payload, baseline=커밋 인덱스, ratio)
     위반 → 캐시 미변경, WARNING(시장별 before/after), refreshed=False
  4. 통과 → _atomic_write(cache) → refreshed=True
```

## 4. 엣지 케이스 계약

- **원격 다운로드 실패(네트워크/타임아웃)**: 기존 fail-open 유지(캐시 미변경, 기존 실패 상태 로직). 회귀 가드와 독립.
- **커밋 기준선 파일 부재/파싱 실패**: 기준선을 구할 수 없으면 회귀 판정 불가 → **보수적으로 원격 거부**(커밋 인덱스가 없으면 어차피 로더가 다른 후보 사용) + ERROR 로그. 조용히 통과시키지 않음.
- **원격이 기준선보다 모든 시장에서 크거나 같음**(정상 포크 인덱스): 통과, 캐시 갱신.
- **정상 churn**(한 시장 5% 감소): ratio 0.8 이내 → 통과.
- **신규 시장**(원격에 기준선에 없는 시장): 회귀 아님, 통과에 영향 없음.
- **임계 env 오설정**(0/음수/>1/NaN): ERROR 로그+기본 0.8 강제.
- **캐시가 이미 오염(KR 희소) 상태에서 첫 정상 새로고침**: 포크 URL이면 통과해 캐시를 완전 인덱스로 교체(자기 치유). 업스트림 URL이면 거부돼 캐시는 그대로지만 로더가 커밋 인덱스 우선하도록 별도 확인(아래).
- **로더 순환 방지**: baseline이 커밋 인덱스이고 로더가 커밋 인덱스 후보를 검사할 때, 자기 자신과 비교하므로 항상 통과(회귀 아님). 오직 원격 캐시 후보만 회귀 판정 대상.
- **baseline 캐싱**: 커밋 인덱스 per-market 카운트는 로드마다 재계산하지 않고 파일 signature 기준 캐시(기존 `_REMOTE_INDEX_VALIDITY_CACHE` 패턴 참고) — 핫 경로 성능.
- **두 로더 경로 동기화**: `find_existing_stock_index_path`와 `get_stock_name_index_map`이 각각 후보를 순회하므로 회귀 게이트를 **양쪽에** 넣어야 함(한쪽만 고치면 다른 경로로 오염 캐시가 샘).

## 5. 검증 계획

- **(주) 로더 자가 치유 테스트**: KR 희소 원격 캐시를 디스크에 두고, `find_existing_stock_index_path`와 `get_stock_name_index_map` **양쪽**이 그 캐시를 건너뛰고 커밋 인덱스(KR 2767)를 쓰는지 실측. **이것이 라이브 버그 재현→치유의 핵심 테스트** — 오염 캐시가 있을 때 KR 맵이 2767이어야 함(60이면 실패).
- **가드/게이트 공통 판정 단위 테스트**: KR 희소(업스트림 형상) 회귀 판정, KR 완전(포크 형상) 통과, 정상 churn(−5%) 통과, 임의 시장 −30% 회귀, 시장 통째 누락 회귀, 신규 시장 추가 통과. baseline==자기자신이면 통과(순환 없음).
- **임계 env**: 정상값 반영, 오설정(0/음수/>1/NaN) 시 기본 0.8 강제.
- **URL 설정화**: env 미설정 시 기본 업스트림, 설정 시 그 URL로 GET(모킹).
- **(보조) 쓰기 폴백**: 거부 시 캐시 파일 미변경 + WARNING 로그 + `refreshed=False`, temp 파일 잔존 없음.
- **기존 `test_expanded_kr_map_reaches_loader`**: 이제 오염 캐시가 디스크에 있어도 통과해야 함(로더 게이트가 자가 치유) — 로컬 캐시를 수동으로 치우지 않고도 green.
- **기존**: `./scripts/ci_gate.sh` green, `pytest -m "not network"` 전체.

## 6. 리스크와 롤백

- **최대 리스크**: 가드가 너무 엄격해 정상 원격 업데이트를 false-reject → 인덱스가 오래된 채 고정. 완화 — 튜너블 ratio(기본 0.8), 신규 시장 허용, WARNING 로그로 가시화. false-reject해도 커밋 인덱스로 안전하게 폴백(기능 저하 없음, 최신성만 손해).
- **원격 새로고침 dormancy(수용)**: §2 참고 — 실측상 현재 무비용, 의도된 수용. 미래 업스트림 CN/US 갱신은 빌드타임 재스플라이스로.
- **롤백**: 신규 env 2종(전부 기본값 안전), 로더 게이트·쓰기 가드·테스트는 additive. 리버트로 즉시 원복(원복 시 버그 재현하므로 권장 안 함).
- **배포 조치(코드 외, 선택)**: 로더 게이트가 오염 캐시를 자가 치유하므로 **수동 캐시 삭제 불필요**. 원격 새로고침을 실제로 활용하려면(선택) 배포 env에 `STOCK_INDEX_REMOTE_URL`=포크. 로컬 정리용 `.kr-sparse-bak*` 잔재는 무해(gitignored).

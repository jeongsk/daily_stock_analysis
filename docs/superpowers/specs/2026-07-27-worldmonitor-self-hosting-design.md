# World Monitor 로컬 Self-hosting 연동 설계

## 1. 목적

`daily_stock_analysis`와 World Monitor를 소스 코드 수준에서 병합하지 않고,
한 번의 운영 명령으로 로컬에서 함께 실행할 수 있게 한다. 이번 단계의 목표는
World Monitor의 글로벌 정보가 투자 판단에 직접 영향을 주게 만드는 것이 아니라,
향후 데이터 연동을 위한 재현 가능한 실행 환경과 상태 확인 경계를 마련하는 것이다.

## 2. 범위

이번 단계에 포함한다.

- World Monitor를 Git submodule로 추적한다.
- World Monitor 앱과 지원 서비스를 고정된 submodule 소스에서 직접 빌드한다.
- World Monitor 전체 스택과 `daily_stock_analysis`를 공용 내부 네트워크로 연결한다.
- 최초 데이터 적재와 30분 주기 갱신을 컨테이너에서 실행한다.
- `daily_stock_analysis`에 World Monitor 연결 설정과 상태 확인 경계를 추가한다.
- World Monitor 장애가 기존 분석, 스케줄러, API 기동을 막지 않게 한다.
- 배포·설정 문서와 `[Unreleased]` changelog를 갱신한다.

이번 단계에 포함하지 않는다.

- World Monitor 이벤트의 저장 또는 재가공
- `AnalysisContextPack` 변경
- 프롬프트, 시장 리뷰, 종목 점수에 World Monitor 데이터를 주입하는 작업
- World Monitor UI를 `dsa-web` 안에 복제하거나 iframe으로 삽입하는 작업
- World Monitor 소스 수정
- 외부 데이터 공급자의 유료 API 키 발급 또는 자동 등록

## 3. 버전과 라이선스 경계

- submodule 경로는 `external/worldmonitor`로 한다.
- 최초 기준선은 self-hosting Docker 자산이 포함된 World Monitor commit
  `6c48a33c97cd643d87ee3a4ed2b54aacbb1cbc3b`으로 고정한다.
- 앱 이미지는 해당 commit의 루트 `Dockerfile`로 빌드한다.
- 최신 semver 릴리스인 `v2.5.23`은 self-hosting Compose와 지원 서비스
  Dockerfile이 추가되기 전의 코드이므로 이 통합의 기준선으로 사용할 수 없다.
- 외부의 `latest`, `main` 또는 움직이는 이미지 태그는 사용하지 않는다.
- 실행 전 검증은 submodule HEAD가 기대 commit인지 확인한다.
- World Monitor 버전 갱신은 submodule commit과 호환성 검증 결과를 하나의 변경으로
  함께 갱신해야 한다.
- 상위 저장소의 MIT 코드와 World Monitor의 AGPL-3.0-only 코드는 별도 Git 이력,
  별도 컨테이너와 별도 빌드 경계를 유지한다.
- 배포 문서에는 submodule이 AGPL-3.0-only 구성요소이며 원본 라이선스가 적용된다는
  사실을 명시한다.

## 4. 런타임 구성

최종 로컬 스택은 논리적으로 두 제품이지만 여러 컨테이너로 구성된다.

| 제품 | 서비스 | 역할 |
| --- | --- | --- |
| daily_stock_analysis | `server` | FastAPI 및 WebUI |
| daily_stock_analysis | `analyzer` | 선택적인 정기 분석 |
| World Monitor | `worldmonitor` | 고정 submodule에서 빌드한 대시보드와 로컬 API |
| World Monitor | `redis` | 영속 캐시 및 seed 데이터 |
| World Monitor | `redis-rest` | Redis REST 호환 프록시 |
| World Monitor | `ais-relay` | `AISSTREAM_API_KEY` 설정 시 활성화되는 선택적 AIS relay |
| World Monitor | `worldmonitor-seeder` | 초기 및 주기 데이터 적재 |

World Monitor 서비스 정의는 별도 Compose 파일에 둔다. 기존
`docker/docker-compose.yml`의 기본 동작은 유지하고, 통합 실행 래퍼가 두 Compose
파일과 전용 네트워크를 조합한다. 사용자는 기본적으로 다음 명령만 사용한다.

```bash
git submodule update --init --recursive
./scripts/worldmonitor-stack.sh up
```

동일한 래퍼가 `down`, `status`, `logs`, `seed`, `validate` 동작을 제공한다.
`down`은 기본적으로 영속 볼륨을 삭제하지 않으며, 데이터 삭제는 별도의 명시적
명령으로만 허용한다.

## 5. 네트워크와 포트

- 통합 전용 bridge network를 사용한다.
- `daily_stock_analysis`는 Docker DNS 서비스 이름을 통해 World Monitor에 접근한다.
- 컨테이너 내부 기본 주소는 `http://worldmonitor:8080`이다.
- 호스트에는 World Monitor 대시보드만 `${WORLDMONITOR_PORT:-3000}`으로 공개한다.
- Redis는 호스트에 공개하지 않는다.
- Redis REST 프록시는 seeder에 필요한 경우에도 내부 네트워크에서만 접근한다.
- AIS relay는 브라우저 프록시 경로로 소비하며 기본적으로 별도 호스트 포트를
  공개하지 않는다.
- 기존 DSA 호스트 포트 `${WEBUI_DOCKER_PORT:-8001}` 계약을 변경하지 않는다.

## 6. 설정과 비밀 값

DSA 공개 설정:

- `WORLDMONITOR_ENABLED=false`
- `WORLDMONITOR_BASE_URL`
- `WORLDMONITOR_CONNECT_TIMEOUT_SECONDS`
- `WORLDMONITOR_READ_TIMEOUT_SECONDS`

통합 Compose 설정:

- `WORLDMONITOR_PORT=3000`
- `WORLDMONITOR_SEED_INTERVAL_SECONDS=1800`
- `RELAY_SHARED_SECRET`
- `REDIS_PASSWORD`
- `REDIS_TOKEN`

세 필수 secret에는 저장소 기본값을 제공하지 않는다. 누락되면 Compose 검증 또는
실행 래퍼가 어떤 값이 필요한지 명확하게 표시하고 시작을 중단한다. 실제 secret은
`.env` 또는 사용자가 선택한 Docker secret 파일에만 존재하며 로그, API 응답,
진단 snapshot에 노출하지 않는다. 선택적인 데이터 공급자와 LLM 키는 기능 단위로
degrade하며 전체 스택 시작 조건으로 삼지 않는다.

`WORLDMONITOR_BASE_URL`은 실행 환경에 따라 다음처럼 설정한다.

- 통합 Compose 내부: `http://worldmonitor:8080`
- 호스트에서 DSA를 직접 실행: `http://127.0.0.1:3000`

새 설정은 `.env.example`, 설정 레지스트리 및 관련 배포 문서에 함께 반영한다.

## 7. Seeder

`worldmonitor-seeder`는 submodule의 공식 seed 스크립트를 컨테이너 안에서 실행하여
호스트 Node.js 의존성을 제거한다.

- 스택 시작 후 Redis REST가 준비되면 최초 seed를 실행한다.
- 이후 기본 1,800초 간격으로 반복한다.
- 간격은 양의 정수로 검증한다.
- 개별 upstream 실패는 해당 실행 결과에 기록하고 다음 수집 및 다음 주기를 계속한다.
- 동일 seeder가 중복 실행되지 않도록 단일 컨테이너만 구성한다.
- 컨테이너 재시작 시 즉시 seed한 뒤 주기 실행을 재개한다.
- Redis 볼륨은 `down`과 재시작 사이에 유지한다.
- 운영자가 `seed` 래퍼 명령으로 즉시 수동 실행할 수 있게 한다.

이번 단계에서는 upstream seeder 스크립트를 fork하거나 변경하지 않는다. upstream의
실행 계약이 릴리스 사이에서 바뀌면 버전 갱신 검증에서 이를 탐지하고 통합 래퍼만
조정한다.

## 8. DSA 연결 상태

독립적인 World Monitor 클라이언트/상태 서비스는 다음 역할만 담당한다.

- 설정 활성화 여부 확인
- 허용된 HTTP(S) base URL 정규화
- 짧은 connect/read timeout을 적용한 read-only health 요청
- 사용자에게 노출 가능한 낮은 민감도의 상태 결과 생성

상태 값은 다음으로 제한한다.

| 상태 | 의미 |
| --- | --- |
| `disabled` | 연동 설정이 꺼져 있음 |
| `healthy` | 로컬 World Monitor가 정상 응답함 |
| `degraded` | 응답은 하지만 일부 의존성 또는 데이터가 비정상임 |
| `unreachable` | timeout, DNS 또는 연결 실패 |
| `misconfigured` | 활성화됐지만 URL 등 설정이 유효하지 않음 |

상태 조회 실패는 예외를 분석 파이프라인으로 전파하지 않는다. 마지막 오류는
민감정보를 제거한 짧은 진단 문자열로만 유지한다. 이번 단계에서 `/api/health`의
기존 성공/실패 의미를 바꾸지 않는다. 상세 상태는 기존 진단 표면에 선택적 구성요소로
추가하며 World Monitor가 꺼져 있거나 중단되어도 DSA liveness는 성공할 수 있다.

## 9. 오류 처리

- submodule 미초기화: 빌드 전에 중단하고 정확한 초기화 명령을 출력한다.
- submodule commit 불일치: `validate`와 `up`에서 중단한다.
- 필수 secret 누락: World Monitor 스택을 시작하지 않는다.
- 선택 API 키 누락: 해당 World Monitor 기능만 비활성화한다.
- World Monitor 시작 지연: DSA는 독립적으로 시작하고 상태는 `unreachable` 또는
  `degraded`로 표시한다.
- World Monitor 런타임 장애: DSA 분석 및 스케줄러는 기존 입력만으로 계속 동작한다.
- seeder 부분 실패: 현재 성공 데이터와 기존 Redis 데이터를 유지하고 다음 주기에
  재시도한다.
- Redis 볼륨 삭제: 다음 기동에서 최초 seed를 다시 수행한다.

## 10. 검증

정적·단위 검증:

- Compose config 렌더링
- submodule 경로와 commit 일치 검사
- 설정 기본값, URL 및 timeout 검증
- `disabled`, `healthy`, `degraded`, `unreachable`, `misconfigured` 상태 테스트
- 오류와 로그의 secret redaction 테스트
- World Monitor 실패가 DSA health와 분석 경로를 실패시키지 않는 회귀 테스트

통합 검증:

- 깨끗한 checkout에서 submodule 초기화
- 필수 secret 누락 시 fail-fast 확인
- 전체 스택 기동과 각 컨테이너 상태 확인
- 호스트에서 DSA WebUI와 World Monitor 대시보드 접근
- DSA 컨테이너에서 World Monitor 서비스 DNS 및 health 접근
- 최초 seed와 수동 seed 실행
- 짧은 테스트 간격으로 두 번째 seed 주기 확인
- World Monitor 중단 중 DSA API와 분석 기능의 정상 동작 확인
- 재기동 후 Redis 데이터 유지 확인

Docker 이미지 pull과 외부 데이터 수집에는 네트워크가 필요하므로, 실행하지 못한
온라인 검증은 최종 교부에서 명시한다.

## 11. 문서와 변경 기록

- `.env.example`: 모든 신규 설정 및 환경별 URL 예시
- 배포 문서: submodule 초기화, 통합 스택 명령, 포트, secret 생성, 업데이트와 rollback
- intelligence 관련 문서: 이번 단계는 연결 상태만 제공하고 분석 입력에는 사용하지
  않는다는 경계
- `docs/CHANGELOG.md`: `[Unreleased]`에扁平 형식으로 사용자 가시 변경 추가
- README는 변경하지 않는다. 상세 운영 기능이므로 배포 문서가 적절한 위치다.
- 영문 배포 문서도 동기화한다. 한국어 전용 문서를 새로 만들기보다 기존 중·영문
  배포 문서 구조를 유지한다.

## 12. Rollback

- 통합 스택을 내리되 Redis volume은 유지한다.
- `WORLDMONITOR_ENABLED=false`로 DSA 연동 상태 확인을 끈다.
- 기존 `docker/docker-compose.yml`만 사용하면 이전 DSA 단독 운영으로 돌아간다.
- 코드 rollback 시 submodule pointer, Compose 통합 파일, 설정 및 문서를 같은 변경
  단위로 되돌린다.
- World Monitor 버전 rollback은 submodule commit을 이전 검증 버전으로 복원하고
  이미지를 다시 빌드한다.

## 13. 후속 단계

다음 단계는 별도 설계와 검증을 거친다.

1. World Monitor 이벤트의 최소 정규화 계약 정의
2. 원본 시각과 수집 시각을 보존하는 저장 모델
3. 시장·국가·산업 노출도 매핑
4. `DailyMarketContext` 또는 새 `AnalysisContextPack` 블록 후보 검토
5. 미래정보 누출을 차단한 백테스트
6. 유의미한 개선이 확인된 신호만 보고서와 투자 판단에 반영

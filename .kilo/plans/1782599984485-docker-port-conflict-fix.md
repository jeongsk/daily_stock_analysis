# Docker / 로컬 dev 서버 포트 충돌 분리 플랜

## 배경 및 근거

- `docker/docker-compose.yml`의 `server` 서비스는 호스트 발행 포트를 `WEBUI_PORT`에서 읽는다:
  - `command: ["python", "main.py", "--serve-only", "--host", "0.0.0.0", "--port", "8000"]`
  - `ports: - "${WEBUI_PORT:-8000}:8000"`
- 컨테이너 내 앱은 CLI `--port 8000`으로 고정 수신하므로, Docker에서 `WEBUI_PORT`는 **오직 호스트 발행 포트**로만 사용된다.
- 반면 로컬 dev(`python main.py --serve` / `uvicorn server:app`)는 `WEBUI_PORT`(기본 8000)를 **앱 수신 포트**로 사용한다 (`main.py:1298`, `src/config.py:1037`, `webui.py:32`).
- 양쪽 모두 동일 `./.env`의 `WEBUI_PORT=8000`을 읽으므로, 로컬 dev(8000) + Docker `server`(8000) 동시 구동 시 **호스트 8000 충돌** 발생.
- 근본 원인: 하나의 변수 `WEBUI_PORT`가 "앱 수신 포트(로컬 dev)"와 "Docker 호스트 발행 포트" 두 의미로 중복 사용됨.

## 결정 사항 (확정됨)

1. **Docker 호스트 발행 포트를 전용 변수 `WEBUI_DOCKER_PORT`로 분리** (compose 보간 전용).
2. **기본값 8001** (8000 + 1 오프셋). 로컬 dev(8000)와 Docker(8001)가 기본적으로 공존.
3. **`WEBUI_PORT`는 Docker compose에서 무시** (fallback 없음). 목표(기본 충돌 회피) 확실히 달성.
   - 클라우드 Docker 배포자(`WEBUI_PORT=8888` 등)는 동일 값을 `WEBUI_DOCKER_PORT`로 이전 (breaking, 마이그레이션 안내 필수).
   - 구형 동작(`:8000`)을 원하면 `WEBUI_DOCKER_PORT=8000` 설정으로 복원 가능 (escape hatch).
4. **앱 config registry / Web 설정 UI에서 제외**: 이 변수는 compose 보간 전용이며 컨테이너 내 앱은 `--port 8000` 고정이므로 앱 런타임 설정이 아님. `src/core/config_registry.py`의 `WEBUI_PORT` 엔트리는 앱 수신 포트 의미로 유지 (변경 불필요).

## 범위 외 (Out of scope)

- 포트와 별개: Docker(`../data:/app/data`)와 로컬 dev가 동일 `./data/stock_analysis.db`를 공유 → 동시 구동 시 SQLite 잠금 경합 가능. 본 플랜은 포트만 다룬다. 알려진 공존 위험으로 문서에 한 줄 참고 추가까지만.
- `analyzer` 서비스(포트 미발행), `Dockerfile`, `entrypoint.sh` 변경 없음.
- 앱 Python 소스 코드(`src/`, `main.py`, `webui.py`, `server.py`) 변경 없음.

## 작업 순서

### 1. `docker/docker-compose.yml`
- `server.ports` 라인 변경:
  ```yaml
  ports:
    - "${WEBUI_DOCKER_PORT:-8001}:8000"
  ```
- `x-common.environment`의 구형 주석 수정:
  - 기존: `# The app listens on 8000 inside the container; WEBUI_PORT controls the host-side published port.`
  - 신규: `WEBUI_PORT`는 더 이상 Docker 호스트 포트에 영향을 주지 않음. 호스트 발행 포트는 `WEBUI_DOCKER_PORT`(기본 8001)로 제어하며, 컨테이너 내 앱은 `--port 8000`으로 고정 수신. 로컬 dev와의 충돌 회피를 위해 기본 포트를 분리함.
- 파일 상단 사용 방식 주석 블록에 `WEBUI_DOCKER_PORT` 언급 추가 (선택, 일관성).

### 2. `.env.example`
- WebUI 설정 섹션(약 line 776-788 부근)에 주석 처리된 변수 추가:
  ```
  # Docker 전용 호스트 발행 포트. 로컬 dev(WEBUI_PORT)와 분리되어 동시 구동 시 충돌을 회피.
  # 기본 8001. 컨테이너 내 앱은 항상 8000으로 수신(--port 8000 고정)하므로 이 변수는 호스트 측 매핑만 제어.
  # 구형 동작(호스트 8000)이 필요하면 WEBUI_DOCKER_PORT=8000 설정.
  # WEBUI_DOCKER_PORT=8001
  ```
- AGENTS.md 규칙 준수: 신규 설정 → `.env.example` + 관련 문서 동기화.

### 3. 문서 동기화 (breaking change 대응)
- `docs/deploy-webui-cloud.md`:
  - line 72, 120 의 `WEBUI_PORT=8888` → Docker 컨텍스트는 `WEBUI_DOCKER_PORT=8888`로 변경.
  - line 240-241의 포트 설명: Docker 항목을 "기본 8001, `WEBUI_DOCKER_PORT=xxxx`로 변경"으로 수정.
  - 마이그레이션 안내 추가: 기존 `WEBUI_PORT`로 Docker 호스트 포트를 지정하던 클라우드 사용자는 `WEBUI_DOCKER_PORT`로 이전.
- `docs/full-guide.md` (line 562) 및 `docs/full-guide_EN.md` (line 493):
  - compose 스니펫 `"${WEBUI_PORT:-8000}:8000"` → `"${WEBUI_DOCKER_PORT:-8001}:8000"`로 업데이트.
- `docs/settings-help.md` (line 94):
  - `WEBUI_PORT` 설명에 "앱 수신 포트(로컬 dev). Docker 호스트 발행 포트는 `WEBUI_DOCKER_PORT`(docs 참고)" 명시.
- `docs/docker/zeabur-deployment.md` (line 151): 해당 문맥이 플랫폼 env 호환(`WEBUI_PORT` → API 서비스 포워딩)을 다루므로 영향 확인. Docker 호스트 발행 포트 언급이면 동일하게 수정, 아니면 유지.

### 4. `docs/CHANGELOG.md`
- `[Unreleased]` 섹션에 flat 항목 추가 (AGENTS.md 규칙: 한 줄, `- [类型] 描述`):
  - `- [改进] Docker Compose 호스트 발행 포트를 WEBUI_PORT에서 WEBUI_DOCKER_PORT(기본 8001)로 분리하여 로컬 dev 서버와의 기본 포트 충돌 회피. 기존 WEBUI_PORT 기반 Docker 호스트 포트 설정은 WEBUI_DOCKER_PORT로 이전 필요(breaking).`
  - 마이그레이션 escape hatch(`WEBUI_DOCKER_PORT=8000`) 명시.

## 검증 계획

1. **compose 보간 확인** (구현 agent 실행 권한 필요):
   - `docker compose -f docker/docker-compose.yml config` 실행 → `ports`가 `8001:8000`으로 풀리는지 확인.
   - `WEBUI_DOCKER_PORT=8888 docker compose -f docker/docker-compose.yml config` → `8888:8000` 확인.
2. **백엔드 게이트**: `./scripts/ci_gate.sh` (앱 코드 변경 없으나 회귀 확인).
3. **컴파일**: 변경된 Python 파일 없음 (docs/compose만). 필요시 `python -m py_compile`은 N/A.
4. **수동 동시 구동 시나리오** (가능한 경우):
   - 로컬 `python main.py --serve` → 8000 확인.
   - `docker compose -f docker/docker-compose.yml up server` → 호스트 8001에서 응답, 8000 충돌 없음 확인.
   - `curl -s localhost:8001/api/health` 정상 응답 확인.
5. **AI 자산 검증**: 본 변경이 AI 협업 자산에 해당하지 않으므로 `python scripts/check_ai_assets.py`는 불필요 (변경 없음 시).

## 리스크 및 롤백

- **Breaking change**: 기존 Docker 사용자의 기본 접속 URL이 `:8000` → `:8001`로 변경. 문서와 CHANGELOG로 명시, escape hatch(`WEBUI_DOCKER_PORT=8000`) 제공.
- **앱 런타임 영향**: 없음. 컨테이너 내 앱은 계속 `--port 8000` 수신, `Dockerfile` HEALTHCHECK `localhost:8000` 유지.
- **공유 DB 위험 (본 플랜 범위 외)**: Docker와 로컬 dev 동시 구동 시 동일 SQLite 파일 잠금 경합. 포트만 분리해도 DB는 공유됨. 문서에 참고 한 줄 추가.
- **롤백**: `docker/docker-compose.yml`의 ports를 `"${WEBUI_PORT:-8000}:8000"`로 되돌리고 문서/CHANGELOG 항목 제거.

## 산출물 체크리스트

- [ ] `docker/docker-compose.yml` ports + 주석 갱신
- [ ] `.env.example` `WEBUI_DOCKER_PORT` 주석 추가
- [ ] `docs/deploy-webui-cloud.md` 갱신 + 마이그레이션 안내
- [ ] `docs/full-guide.md` / `docs/full-guide_EN.md` 스니펫 갱신
- [ ] `docs/settings-help.md` WEBUI_PORT 설명 보충
- [ ] `docs/docker/zeabur-deployment.md` 해당 라인 재확인
- [ ] `docs/CHANGELOG.md` `[Unreleased]` flat 항목 추가
- [ ] `docker compose config` 보간 검증
- [ ] `./scripts/ci_gate.sh` 통과

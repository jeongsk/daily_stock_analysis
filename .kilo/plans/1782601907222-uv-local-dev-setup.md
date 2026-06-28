# 로컬 개발 uv 환경 도입 계획

## 목표

로컬 개발 환경을 `uv` 기반으로 전환하여 빠른 설치와 재현 가능한 환경(lockfile)을 확보한다.
의존성 단일 소스는 `pyproject.toml` + `uv.lock`이며, CI/Docker는 기존 `requirements.txt`를 pip로 계속 소비하므로 워크플로/Dockerfile 변경을 최소화한다.

## 핵심 결정 (확정됨)

1. **의존성 소스**: `pyproject.toml` `[project]` 단일 소스 + `uv.lock` 커밋
2. **requirements.txt 제공**: 의존성 변경 시 `uv export --no-dev -o requirements.txt` 재생성 후 같은 PR에 커밋 (uv가 자동 생성 파일 헤더 표시). CI/Docker pip 명령은 그대로 유지
3. **dev 의존성**: flake8/pytest를 `[dependency-groups] dev`로 분리. `.github/requirements-ci.txt`는 `uv export`(dev 포함)로 재생성
4. **Python 버전**: `.python-version` = `3.11` + `requires-python = ">=3.11"` (CI/Docker 3.11과 일치)
5. **문서 범위**: AGENTS.md + README + docs (full-guide/_EN + 신규 local-development.md + INDEX/_EN). 중영 동기화

## 변경 대상 파일

### 신규

- `.python-version` — 내용 `3.11`
- `docs/local-development.md` — uv 기반 로컬 개발 가이드(설치/동기화/실행/테스트/lint, pip 폴백 안내)

### 수정

- `pyproject.toml` — 다음 섹션 추가 (기존 `[tool.black]`/`[tool.isort]`/`[tool.bandit]` 유지):
  - `[project]`: `name`, `version`(문서용 임의/`0.0.0`), `requires-python = ">=3.11"`, `dependencies` = 현재 `requirements.txt`의 프로덕션 의존성 전체
  - `[dependency-groups] dev` = `["flake8", "pytest"]`
  - `[tool.uv.sources]`:
    ```toml
    alphasift = { git = "https://github.com/ZhuLinsen/alphasift.git", rev = "377049857cc04175dc3cca62121ee41adec6cdb8" }
    ```
- `requirements.txt` — `uv export --no-dev -o requirements.txt`로 재생성 (alphasift는 `@ git+https://...@<rev>` 형태로 export되어 pip 호환). uv가 헤더 코멘트 자동 삽입
- `.github/requirements-ci.txt` — `uv export -o .github/requirements-ci.txt`로 재생성 (기존 `-r ../requirements.txt` + flake8/pytest 참조 구조를 전체 export로 단순화). flake8/pytest는 dev 그룹에서 포함
- `uv.lock` — `uv lock`/`uv sync` 결과 커밋 (자동 생성)
- `AGENTS.md` — "4. 常用命令" 의 "运行应用"/"后端验证" 의 `pip install -r requirements.txt` 명령을 uv 기반(`uv sync`, `uv run python main.py`, `uv run ./scripts/ci_gate.sh`, `uv run pytest`)으로 교체. pip 폴백 한 줄 메모 병기 (사용자가 uv를 쓰지 않는 경우)
- `README.md` — "方式二"(140-167행) 설치 단계에 uv 우선 블록 추가:
  ```bash
  uv sync
  uv run python main.py
  ```
  기존 pip 블록은 유지하거나 "또는 pip"로 축소. Python 뱃지(3.10+)는 유지
- `docs/full-guide.md` / `docs/full-guide_EN.md` — 설치/환경 섹션에 uv 안내 추가 및 `docs/local-development.md` 링크
- `docs/INDEX.md` / `docs/INDEX_EN.md` — 로컬 개발 가이드 항목 추가
- `docs/CHANGELOG.md` — `[Unreleased]`에 `- [改进] 로컬 개발 환경 uv 도입 (pyproject.toml 단일 소스 + uv.lock)` 추가

### 변경 없음 (의도적)

- `docker/Dockerfile`, `docker/docker-compose.yml`, `docker/entrypoint.sh` — `requirements.txt`를 pip로 그대로 소비
- `.github/workflows/*.yml` — pip + requirements.txt 그대로
- `scripts/ci_gate.sh` — `python`/`flake8`/`pytest` 호출 그대로 (uv 환경에서는 `uv run ./scripts/ci_gate.sh`로 실행)
- `setup.cfg` — flake8/pytest/isort 설정 유지
- `.env.example` / `.env.example.ko` — uv 전용 환경변수 없음
- `.gitignore` — `.venv/` 이미 무시됨, `uv.lock`/`.python-version`은 무시 대상 아님

## 의존성 이관 매핑 (주의점)

- `httpx[socks]` → `[project.dependencies]`에 `"httpx[socks]"` 그대로 (extras 유지)
- `litellm>=1.80.10,!=1.82.7,!=1.82.8,<2.0.0` → 버전 지정자 그대로 이관
- `alphasift`(git) → `dependencies`에 `"alphasift"` + `[tool.uv.sources]`에 git/rev 지정
- 그 외 `>=` 하한선 의존성은 specifier 그대로 이관 (lock 시점 버전이 uv.lock에 고정되지만 requirements.txt export는 `>=` 하한선을 보존하는지 export 옵션에서 확인 필요)

## 구현 순서

1. `.python-version` 작성 (`3.11`)
2. `pyproject.toml`에 `[project]` / `[dependency-groups] dev` / `[tool.uv.sources]` 추가
3. `uv sync` 실행 → `uv.lock` 생성 확인
4. `uv run python -c "import alphasift.dsa_adapter"` 로 git 의존성 해결/임포트 확인
5. `uv export --no-dev -o requirements.txt` 재생성 → 기존 패키지 보존 diff 확인
6. `uv export -o .github/requirements-ci.txt` 재생성
7. 문서 수정 (AGENTS.md → README → docs/*)
8. `docs/CHANGELOG.md` `[Unreleased]` 추가
9. AI 자산 체크: `python scripts/check_ai_assets.py` (문서 변경 시)

## 검증 매트릭스

- **uv 환경 기능 검증**:
  - `uv sync` 성공 (alphasift git 의존성 포함)
  - `uv run python -c "import alphasift.dsa_adapter; from src.config import get_config; from api.app import app"` (Docker smoke import와 동일 세트)
  - `uv run ./scripts/ci_gate.sh syntax`
  - `uv run ./scripts/ci_gate.sh flake8`
  - `uv run pytest -m "not network"` (네트워크 외 테스트)
- **CI/Docker 호환성 검증**:
  - `uv export --no-dev -o requirements.txt` 결과가 pip로 설치 가능한지 확인 (`uv export` 기본 포맷 = pip 호환)
  - 기존 `requirements.txt` 대비 패키지 누락/추가 없는지 diff 검토 (주석 제외)
  - 가능하면 `docker build -f docker/Dockerfile .` smoke (옵션, 환경 허용 시)
- **문서 검증**: AGENTS.md/README/docs의 uv 명령어가 실제 동작하는지, 파일명/경로 일치 여부

## 위험과 완화

- **alphasift 메타데이터 충돌**: `requires-python`이나 빌드 시스템 미스매치로 `uv sync` 실패 가능 → rev 고정 상태이므로, 실패 시 `[tool.uv]`의 `override-dependencies` 또는 소스 메타데이터 확인
- **uv export 포맷 호환성**: git 의존성이 `package-name @ git+https://...@rev ; ...` 형태로 export되어야 pip가 이해 가능. export 후 `pip install --dry-run -r requirements.txt`로 사전 검증 권장
- **하한선 semantic 변화**: `>=` 지정자가 lock에서는 특정 버전으로 고정되지만, export는 원 지정자를 보존하는 게 기본 동작(`--no-emit-package` 등 옵션 사용 금지). diff로 확인
- **이중 유지 부담**: 단일 소스이므로 발생 안 함 — 의존성 추가/수정은 `pyproject.toml`에서만, 이후 export 2개 파일 갱신이 PR 체크리스트에 명시됨 (AGENTS.md에 메모)

## 롤백

- `pyproject.toml`/`uv.lock`/`.python-version`/`requirements.txt`/`requirements-ci.txt` 변경을 revert.
- requirements.txt는 이전 커밋 내용(핀된 `>=` 목록)으로 복원되면 pip/Docker/CI 즉시 정상 복귀.
- 문서 변경은 독립적이므로 코드 동작에 영향 없음.

## 미해결/참고

- `uv export`가 dev 그룹을 기본 포함하므로 `--no-dev`만으로 프로덕션 분리 가능 (추가 플래그 불필요, 구현 시 `uv export --help`로 최종 확인)
- README Python 뱃지를 3.11로 변경할지는 README 최소 변경 원칙상 유지(3.10+) 권장 — 구현 시 사용자 확인 가능

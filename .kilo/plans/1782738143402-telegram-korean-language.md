# 텔레그램 알림 언어를 한국어로 전환

## 배경 / 근본 원인

- 텔레그램 알림이 중국어로 발송되는 원인은 `REPORT_LANGUAGE` 설정.
- `.env:661` 이 `# REPORT_LANGUAGE=zh` 로 **주석 처리**되어 있어 시스템 기본값 `zh`(중국어)가 적용 중.
- 시스템은 **이미 한국어(`ko`)를 완벽 지원**함. `src/report_language.py` 의 모든 라벨·LLM 프롬프트·알림 템플릿에 한국어 번역이 존재하며, `report_language` 하나로 LLM 분석 내용과 알림 라벨이 모두 한국어로 전환됨.
- 설정 우선순위(`src/config.py` `_resolve_report_language_env_value`):
  1. 프로세스 환경변수(`os.getenv("REPORT_LANGUAGE")`) — 최우선
  2. `.env` 파일 값 — 현재 주석 처리됨
  3. 기본값 `zh`
- 모든 배포 방식이 동일한 `.env` 를 읽음:
  - 로컬 `main.py`: `.env` 직접 읽음
  - Docker: `docker/docker-compose.yml:21` `env_file: ../.env`, `REPORT_LANGUAGE` 재정의 없음
  - 웹 UI: `.env` 에 저장 후 런타임 리로드(`_reload_runtime_config`)
- 커밋된 `.env.example` 문서가 `ko` 옵션을 누락하고 있어 발견 가능성이 떨어짐(660/81줄).

## 수정 항목

### 1. `.env` (로컬, gitignored) — 실제 알림 언어 수정
- 661줄: `# REPORT_LANGUAGE=zh` → `REPORT_LANGUAGE=ko` (주석 해제 + 값 변경)
- (권장) 660줄 주석 보정: `# 보고서 출력 언어: zh(중국어, 기본값) / en(영어)` → `ko(한국어)` 추가
- (권장) 81줄 주석 보정: `REPORT_LANGUAGE(zh/en)` → `zh/en/ko`

### 2. `.env.example` (커밋됨) — 문서 갭 수정
- 660줄: `# 报告输出语言：zh(中文，默认) / en(英文)` → `ko(韩文)` 추가
- 81줄: `# Longbridge SDK 语言与 REPORT_LANGUAGE（zh/en）一致...` → `zh/en/ko` 보정
- 661줄 예시는 그대로 `# REPORT_LANGUAGE=zh` 유지(주석 예시이므로)

### 3. (선택) `docs/CHANGELOG.md` `[Unreleased]`
- 플랫 포맷으로 한 줄 추가: `- [文档] .env.example REPORT_LANGUAGE 옵션에 ko(한국어) 누락 보완`

## 적용 / 재시작

- 로컬: `main.py` / 서버 프로세스 재시작
- Docker: `docker compose -f docker/docker-compose.yml restart` (또는 `up -d`)
- 웹 UI에서 저장한 경우 자동 리로드됨

## 검증

1. 설정 확인:
   ```bash
   uv run python -c "from src.config import get_config; print(get_config().report_language)"
   ```
   → `ko` 출력 확인
2. 단일 종목 분석 또는 시장 리뷰 1회 실행 후, 알림/보고서가 한국어인지 확인:
   ```bash
   uv run python main.py --stocks 600519 --dry-run   # 또는 --market-review
   ```
3. (옵션) `.env.example` 문구가 `ko` 를 포함하는지 확인

## 엣지 케이스 / 주의

- **환경변수 우선**: 셸 `export`, systemd unit, 또는 Docker `environment:` 에 `REPORT_LANGUAGE` 가 명시적으로 설정되어 있으면 `.env` 보다 우선함. `.env` 변경 후에도 중국어가 유지되면 프로세스 환경변수 확인 필요.
- **히스토리 언어 매칭**: 기존 zh 기록과 새 ko 기록이 섞일 수 있으나, `src/services/daily_market_context.py` 의 `_record_report_language_matches` 가 언어 기반 매칭을 수행하므로 호환됨.
- **Longbridge SDK 언어**: SDK 언어가 `REPORT_LANGUAGE` 와 연동됨(`.env:81` 주석 참고). `ko` 설정 시 SDK 동작 영향은 제한적이나 필요시 확인.

## 위험 / 회귀

- 낮음. `ko` 는 이미 프로덕션 수준으로 지원됨.
- 회귀 가능성: 라벨/프롬프트 누락이 의심되면 `src/report_language.py` 의 `_REPORT_LABELS["ko"]` 와 `_LOCALIZED_TEXT` 항목 점검.

## 영향받지 않는 항목 (out of scope)

- 코드 로직 변경 없음 (`report_language` 파이프라인은 이미 ko 분기를 가짐: `src/core/pipeline.py:1242-1244`)
- `src/core/config_registry.py` 의 REPORT_LANGUAGE 스키마는 이미 `zh/en/ko` 를 올바르게 나열 중(수정 불필요)

---
name: data-source-smoke
description: 데이터 소스가 불안정해 보일 때 network-smoke 검사(pytest -m network + scripts/test.sh quick)를 한 번에 실행하고 결과를 요약한다. 실제 외부 네트워크를 호출하므로 사용자가 명시적으로 호출할 때만 실행한다.
disable-model-invocation: true
---

# Data Source Smoke

다중 데이터 소스(akshare / efinance / yfinance / finance-datareader / longbridge 등)나 검색·LLM 경로가 의심될 때, `.github/workflows/network-smoke.yml`이 CI에서 돌리는 것과 동일한 온라인 스모크를 로컬에서 실행하고 결과를 요약한다.

이 스킬은 **실제 외부 네트워크를 호출**한다. 규칙 진원은 저장소 루트 `AGENTS.md`(§6 네트워크 관련 검증)이며, 규칙을 복사하지 않고 실제 스크립트 동작에 따른다.

## Usage

```text
/data-source-smoke
```

## Instructions

### Step 1: 전제 확인
- 작업 디렉터리가 저장소 루트인지 확인한다.
- 의존성 동기화가 필요하면 `uv sync`를 먼저 안내한다(자동 실행하지 않음).

### Step 2: 온라인 스모크 실행
`network-smoke.yml`과 동일한 두 단계를 순서대로 실행한다. 둘 다 non-blocking 관찰용이다.

```bash
# 1) 네트워크 마커 테스트
uv run python -m pytest -m network -q | tee /tmp/dsa-network.log

# 2) 단일 종목 빠른 스모크 (알림 발송 금지)
./scripts/test.sh quick --no-notify | tee /tmp/dsa-quick.log
```

> `uv`가 없으면 `python -m pytest ...` / `python main.py` 계열로 안내한다. 명령·플래그가 실제 스크립트와 다르면 실제 스크립트를 신뢰한다.

### Step 3: 결과 요약
- 각 단계의 통과/실패와, 실패 시 어떤 데이터 소스·경로가 원인인지(`data_provider/*` fetcher, 검색, LLM) 요약한다.
- 단일 소스 실패인지 파이프라인 전체 실패인지 구분한다(AGENTS.md §7: 단일 소스 실패가 전체를 죽이면 안 됨).
- `--no-notify`로 실제 알림이 나가지 않았음을 명시한다.

### 안전 규칙
- 실제 통지 채널(텔레그램/봇 등)로 메시지를 보내지 않는다(`--no-notify` 유지).
- `.env`·`longbridge_tokens/` 등 시크릿을 출력/수정하지 않는다.
- 온라인 검증이므로 rate limit·일시적 소스 장애 가능성을 결과에 함께 적는다.

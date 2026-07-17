# 토스증권 OpenAPI는 자격증명 게이트 opt-in 소스로만 도입한다

토스증권 OpenAPI(공식 브로커 API, KRX·미국 시세/계좌/주문)를 도입하되,
`TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET`이 설정된 환경에서만 fetcher를
활성화한다. 설정 시 KR **실시간** 시세는 Toss가 1순위(yfinance fallback)다.
KR **일봉**은 KRX 공식 종가 기준인 yfinance가 1순위를 유지하며, Toss는
yfinance 실패 시의 fallback으로만 동작한다 — Toss 일봉은 KRX+NXT(대체거래소)
통합 체결가라 국내 표준 차트의 KRX 공식 종가와 어긋난다는 실측 결과
(2026-07-17, 최대 3.8% 괴리)에 따른 재결정이다. US는 기존 체인 뒤
최후순위다. **정기 일일 파이프라인(GitHub Actions)은 계속 공개 소스
(yfinance·Naver 등)만 사용한다.**

핵심 제약은 Toss의 **허용 IP 등록제**다: 사전 등록되지 않은 IP의 호출은
403으로 차단되며, GitHub-hosted runner(`ubuntu-latest`)는 동적 IP라서
등록이 불가능하다. 즉 "공식 API인데 왜 매일 자동 분석에는 안 쓰는가"의
답은 기술 선호가 아니라 네트워크 정책이다. Toss가 실질 가치를 내는 곳은
고정 IP를 등록할 수 있는 로컬·자가호스팅·데스크톱 실행이다.

이 결정은 ADR 0001의 "무설정 동작" 계약과 충돌하지 않는다 — 0001이
KIS OpenAPI를 기각한 이유는 키가 **필수**가 되어 무설정 사용자의 기능이
사라지기 때문이었고, 0001 스스로 "이후 공식 소스가 필요해지면 opt-in으로
fetcher 체인에 추가"라는 확장 경로를 열어뒀다. Toss 도입은 정확히 그
경로다: 미설정 시 기존 동작이 완전히 보존되고, 설정 시에만 강화된다
(Tushare `TUSHARE_TOKEN`·Longbridge 키와 같은 선례).

## Considered Options

- **Self-hosted runner 전환**: 고정 IP runner로 정기 파이프라인에서도
  Toss 호출. 인프라 운영 부담이 추가되고 업스트림 워크플로우와 괴리가
  생기며, fork 사용자 전체에 강제할 수 없다 — 기각.
- **고정 egress 프록시**: GH Actions에서 고정 IP 프록시 경유. 비용·운영
  복잡도 증가에 더해 브로커 자격증명이 제3자 프록시를 경유하는 보안
  우려 — 기각.
- **Toss를 KR 최후순위 fallback으로만**: 기존 동작 보존은 최대지만,
  설정한 사용자조차 지연 있는 yfinance 데이터를 계속 받게 되어 도입
  실익이 소멸 — 기각 (US에서만 이 위치를 취한다: US는 이미 4중 체인).

## Consequences

- 실행 환경에 따라 KR **실시간** 시세 품질이 이원화된다: 정기 파이프라인
  리포트는 yfinance 기준, 자격증명 있는 로컬 실행은 Toss(NXT 통합
  최신 체결가) 기준. KR **일봉**은 자격증명 여부와 무관하게 yfinance
  (KRX 공식 종가) 기준으로 통일돼 있어 이원화되지 않는다. 리포트
  재현성 비교 시 실시간 경로의 차이만 인지하면 된다.
- KR breadth(#11)·섹터 등 정기 파이프라인이 소비하는 데이터는 Toss로
  전환할 수 없다 — 기존 Naver/Daum 계획이 유지된다.
- 자격증명을 설정하는 사용자는 토스증권 WTS에서 허용 IP 등록을 직접
  관리해야 한다 (IP 변경 시 403 → yfinance 강등으로 나타남).
- 이후 정기 파이프라인에서 Toss가 꼭 필요해지면 self-hosted runner
  결정을 별도 ADR로 재검토한다.
- KR 일봉은 Toss가 아닌 yfinance가 계속 기준선이므로, Toss 도입만으로는
  KR 일봉의 지연·단일 소스 취약점이 해소되지 않는다 — 일봉 이중화가
  필요해지면 별도 검토가 필요하다(NXT 통합 시세를 세션 분리해 제공하는
  API가 나오지 않는 한 Toss로는 해결되지 않는다).

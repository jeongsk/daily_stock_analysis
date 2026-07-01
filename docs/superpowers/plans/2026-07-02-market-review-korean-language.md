# Market Review Korean Language Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make market-review notifications honor `REPORT_LANGUAGE=ko` end to end so Telegram receives Korean wrapper text and Korean market-review body text.

**Architecture:** Keep the fix inside the existing market-review language boundary. `src/core/market_review.py` already localizes the wrapper title and passes runtime config into `MarketAnalyzer`; `src/market_analyzer.py` must add Korean prompt/template/strategy branches instead of treating every non-English language as Chinese. Bot `/market` should pass the same config into `run_market_review()` that it used to build runtime services.

**Tech Stack:** Python 3.11, pytest, existing `MarketAnalyzer`, `MarketCommand`, and `REPORT_LANGUAGE` localization helpers.

---

## Files

- Modify: `src/market_analyzer.py`
  - Add Korean market-review title, prompt data labels, output template, fallback template, and Korean strategy blocks.
- Modify: `bot/commands/market.py`
  - Pass `config=config` into `run_market_review()` for Bot-triggered market reviews.
- Modify: `tests/test_market_analyzer_generate_text.py`
  - Add regression coverage for Korean LLM prompt generation and template fallback.
- Modify: `tests/test_bot_market_command.py`
  - Update Bot assertions to require config forwarding.
- Modify: `docs/CHANGELOG.md`
  - Add flat `[Unreleased]` entries for the user-visible notification/report-language fix and tests.

## Task 1: Add Failing Korean Market-Review Tests

**Files:**
- Modify: `tests/test_market_analyzer_generate_text.py`
- Modify: `tests/test_bot_market_command.py`

- [ ] **Step 1: Add a failing prompt-language test**

Insert this test near the existing market-review language tests in `tests/test_market_analyzer_generate_text.py`:

```python
    def test_build_review_prompt_uses_korean_shell_when_report_language_is_ko(self):
        from src.market_analyzer import MarketOverview, MarketIndex

        ma = self._make_market_analyzer_with_mock_generate_text(return_value="review")
        ma.config.report_language = "ko"
        overview = MarketOverview(
            date="2026-07-02",
            indices=[
                MarketIndex(
                    code="000001",
                    name="상하이종합지수",
                    current=3300.0,
                    change=12.0,
                    change_pct=0.36,
                    amount=145000000000.0,
                )
            ],
            up_count=3200,
            down_count=1800,
            flat_count=100,
            limit_up_count=85,
            limit_down_count=12,
            total_amount=9800,
            top_sectors=[{"name": "AI 인프라", "change_pct": 3.25}],
            bottom_sectors=[{"name": "석탄", "change_pct": -1.12}],
        )

        prompt = ma._build_review_prompt(overview, [])

        assert "한국어" in prompt
        assert "# 오늘의 시장 데이터" in prompt
        assert "## 주요 지수" in prompt
        assert "## 시장 폭" in prompt
        assert "## 섹터 동향" in prompt
        assert "## 2026-07-02 시장 리뷰" in prompt
        assert "### 1. 시장 요약" in prompt
        assert "### 7. 전략 계획" in prompt
        assert "今日市场数据" not in prompt
        assert "输出格式模板" not in prompt
        assert "大盘复盘" not in prompt
```

- [ ] **Step 2: Add a failing fallback-template test**

Insert this test near `test_generate_template_review_keeps_chinese_shell_for_us_when_report_language_is_default`:

```python
    def test_generate_template_review_uses_korean_shell_when_report_language_is_ko(self):
        from src.market_analyzer import MarketOverview, MarketIndex

        ma = self._make_market_analyzer_with_mock_generate_text(return_value=None)
        ma.config.report_language = "ko"
        overview = MarketOverview(
            date="2026-07-02",
            indices=[
                MarketIndex(
                    code="000001",
                    name="상하이종합지수",
                    current=3300.0,
                    change=12.0,
                    change_pct=0.36,
                )
            ],
            up_count=3200,
            down_count=1800,
            flat_count=100,
            limit_up_count=85,
            limit_down_count=12,
            total_amount=9800,
            top_sectors=[{"name": "AI 인프라", "change_pct": 3.25}],
            bottom_sectors=[{"name": "석탄", "change_pct": -1.12}],
        )

        result = ma.generate_market_review(overview, [])

        assert "## 2026-07-02 시장 리뷰" in result
        assert "오늘 A주 시장은" in result
        assert "### 1. 시장 요약" in result
        assert "### 6. 전략 프레임워크" in result
        assert "### 七、风险提示" not in result
        assert "大盘复盘" not in result
```

- [ ] **Step 3: Update Bot assertions to require config forwarding**

In every `market_review_module.run_market_review.assert_called_once_with(...)` in `tests/test_bot_market_command.py`, add:

```python
            config=config,
```

The first updated call should look like:

```python
        market_review_module.run_market_review.assert_called_once_with(
            notifier=notifier,
            analyzer=runtime_analyzer,
            search_service=runtime_search,
            config=config,
            send_notification=True,
            override_region="cn,us",
            trigger_source="bot",
        )
```

- [ ] **Step 4: Run tests and verify RED**

Run:

```bash
uv run pytest \
  tests/test_market_analyzer_generate_text.py::TestMarketAnalyzerGenerateText::test_build_review_prompt_uses_korean_shell_when_report_language_is_ko \
  tests/test_market_analyzer_generate_text.py::TestMarketAnalyzerGenerateText::test_generate_template_review_uses_korean_shell_when_report_language_is_ko \
  tests/test_bot_market_command.py::MarketCommandRegionFilterTestCase::test_both_with_cn_us_open_passes_override_region_cn_us \
  -q
```

Expected:
- The two new market analyzer tests fail because Korean paths currently contain Chinese shell text.
- The Bot test fails because `config` is not passed to `run_market_review()`.

## Task 2: Implement Korean Market-Review Prompt and Template Shells

**Files:**
- Modify: `src/market_analyzer.py`

- [ ] **Step 1: Add Korean title/index hint branches**

Change `_get_review_title()` and `_get_index_hint()` so `ko` is separate from Chinese:

```python
    def _get_review_title(self, date: str) -> str:
        language = self._get_review_language()
        if language == "en":
            market_names = {"us": "US Market Recap", "hk": "HK Market Recap"}
            market_name = market_names.get(self.region, "A-share Market Recap")
            return f"## {date} {market_name}"
        if language == "ko":
            market_names = {"us": "미국 시장 리뷰", "hk": "홍콩 시장 리뷰"}
            market_name = market_names.get(self.region, "A주 시장 리뷰")
            return f"## {date} {market_name}"
        return f"## {date} 大盘复盘"
```

```python
    def _get_index_hint(self) -> str:
        language = self._get_review_language()
        if language == "en":
            if self.region == "us":
                return "Analyze the key moves in the S&P 500, Nasdaq, Dow, and other major indices."
            if self.region == "hk":
                return "Analyze the key moves in the HSI, Hang Seng Tech, HSCEI, and other major indices."
            return "Analyze the price action in the SSE, SZSE, ChiNext, and other major indices."
        if language == "ko":
            if self.region == "us":
                return "S&P 500, 나스닥, 다우 등 주요 지수의 움직임을 분석하세요."
            if self.region == "hk":
                return "항셍지수, 항셍테크, HSCEI 등 주요 지수의 움직임을 분석하세요."
            return "상하이종합, 선전성분, 창업판 등 주요 지수의 흐름을 분석하세요."
        return self.profile.prompt_index_hint
```

- [ ] **Step 2: Add Korean strategy blocks**

Add `ko` branches to `_get_strategy_prompt_block()` and `_get_strategy_markdown_block()` before existing Chinese/default returns. The Korean prompt block should include:

```python
        if self._get_review_language() == "ko":
            return f"""## 전략 블루프린트: {self._get_market_scope_name('ko')} 3단계 리뷰 전략
지수 추세, 유동성, 섹터 로테이션을 중심으로 다음 거래일 대응 계획을 정리합니다.

### 전략 원칙
- 먼저 지수 방향을 보고, 거래대금과 시장 폭으로 확인한 뒤, 섹터 지속성을 점검합니다.
- 모든 결론은 포지션 크기, 매매 속도, 리스크 통제 행동으로 연결되어야 합니다.
- 당일 데이터와 최근 3일 뉴스 흐름에 근거하고 확인되지 않은 사실은 만들지 않습니다.

### 분석 차원
- 추세 구조: 시장이 상승, 박스권, 방어 국면 중 어디에 있는지 판단합니다.
  - 주요 지수가 같은 방향으로 움직이는지 확인합니다.
  - 상승에는 거래대금이 동반되는지, 하락은 축소 거래인지 점검합니다.
  - 핵심 지지와 저항이 회복되거나 이탈됐는지 확인합니다.
- 유동성과 심리: 단기 위험 선호와 시장 온도를 읽습니다.
  - 상승/하락 종목 수와 상한가/하한가 구조를 확인합니다.
  - 거래대금이 늘었는지 줄었는지 봅니다.
  - 고베타 주도주에 균열이 있는지 점검합니다.
- 주도 테마: 거래 가능한 주도 섹터와 피해야 할 영역을 구분합니다.
  - 주도 섹터에 명확한 이벤트 촉매가 있는지 확인합니다.
  - 섹터 내부에서 대표 종목이 동반 상승을 이끄는지 봅니다.
  - 약세 섹터의 부진이 확산되는지 점검합니다.

### 행동 프레임워크
- 공격: 지수가 동반 상승하고 거래대금이 늘며 핵심 테마가 강화될 때.
- 균형: 지수가 엇갈리거나 거래가 줄어들면 포지션을 통제하고 확인을 기다릴 때.
- 방어: 지수가 약해지고 약세 섹터가 확산되면 리스크 관리와 비중 축소를 우선할 때."""
```

The Korean markdown block should be:

```python
        if review_language == "ko":
            return """### 6. 전략 프레임워크
- **추세 구조**: 시장이 상승, 박스권, 방어 국면 중 어디에 있는지 판단합니다.
- **유동성과 심리**: 시장 폭, 거래대금, 주도주의 균열 여부로 위험 선호를 점검합니다.
- **주도 테마**: 촉매와 지속성이 있는 섹터를 찾고 약세가 확산되는 영역은 피합니다.
"""
```

- [ ] **Step 3: Add Korean branch in `_build_review_prompt()`**

After the existing `if review_language == "en": ... return ...` block and before the Chinese return, add a Korean return. It must use Korean labels and call `_get_review_title()`:

```python
        if review_language == "ko":
            report_title = self._get_review_title(overview.date).removeprefix("## ").strip()
            return f"""당신은 전문 A/H/미국 주식 시장 분석가입니다. 아래 데이터를 바탕으로 구조화된 {self._get_market_scope_name('ko')} 시장 리뷰를 작성하세요.

[중요] 출력 요구사항:
- 순수 Markdown 텍스트만 출력하세요
- JSON 형식은 금지합니다
- 코드 블록은 금지합니다
- 이모지는 제목에서만 제한적으로 사용하세요(제목당 최대 1개)
- 고정된 제목, 안내 문구, 결론은 모두 한국어로 작성하세요
- 보고서는 트레이더의 장마감 워크스테이션처럼 결론을 먼저 제시하고, 데이터 표, 주도 흐름, 촉매, 계획 순서로 전개하세요
- 시스템이 주입한 표 데이터를 반복 나열하지 말고, 본문은 그 데이터가 의미하는 바를 설명하세요

---

# 오늘의 시장 데이터

## 날짜
{overview.date}

## 주요 지수
{indices_placeholder}

{stats_block}

{sector_block}

## 시장 뉴스
{news_placeholder}

{data_no_indices_hint}

{self._get_strategy_prompt_block()}

---

# 출력 템플릿

## {report_title}

> 오늘 시장 상태, 핵심 모순, 다음 거래일 우선 관찰 방향을 한 문장으로 제시하세요.

### 1. 시장 요약
(지수, 시장 폭, 거래대금, 심리 온도를 2-3문장으로 요약하고 강세/온기/횡보/약세 판단을 명확히 하세요.)

### 2. 지수 구조
({self._get_index_hint()} 누가 지수를 지지했고 누가 부담이었는지, 핵심 지지/저항을 설명하세요.)

### 3. 유동성과 심리
(거래대금, 상승/하락 종목 수, 상한가/하한가 구조, 위험 선호를 해석하세요.)

### 4. 섹터 하이라이트
(상승/하락 섹터의 논리, 지속성, 주도 흐름 형성 여부를 분석하세요.)

### 5. 뉴스 촉매
(최근 3일 뉴스에서 다음 거래일에 실제로 영향을 줄 촉매나 교란 요인을 추려내세요.)

### 6. 리스크 알림
(주시해야 할 주요 리스크를 정리하세요.)

### 7. 전략 계획
(공격/균형/방어 결론, 포지션 범위, 관심 방향, 회피 방향, 무효화 조건 하나를 제시하고 “참고용이며 투자 조언이 아닙니다.”로 끝내세요.)

---

보고서 본문만 출력하고 추가 해설은 쓰지 마세요.
"""
```

- [ ] **Step 4: Localize Korean data placeholders in `_build_review_prompt()`**

Before the Korean return, ensure Korean stats and placeholder strings are selected:

```python
        elif review_language == "ko":
            if self.profile.has_market_stats:
                stats_block = f"""## 시장 폭
- 상승: {overview.up_count}개 | 하락: {overview.down_count}개 | 보합: {overview.flat_count}개
- 상한가: {overview.limit_up_count}개 | 하한가: {overview.limit_down_count}개
- 거래대금: {overview.total_amount:.0f} ({self._get_turnover_unit_label()})"""
            else:
                stats_block = "## 시장 폭\n(이 시장은 상승/하락 종목 수 통계를 사용할 수 없습니다.)"

            if self.profile.has_sector_rankings:
                sector_block = f"""## 섹터 동향
상승 주도: {top_sectors_text if top_sectors_text else "데이터 없음"}
하락 주도: {bottom_sectors_text if bottom_sectors_text else "데이터 없음"}"""
            else:
                sector_block = "## 섹터 동향\n(이 시장은 섹터 등락 데이터를 사용할 수 없습니다.)"
```

Also use:

```python
        elif review_language == "ko":
            data_no_indices_hint = (
                "참고: 시장 데이터 수집에 실패했습니다. [시장 뉴스]를 중심으로 정성 분석을 수행하고 구체적인 지수 레벨을 만들지 마세요."
                if not indices_text
                else ""
            )
            indices_placeholder = indices_text if indices_text else "지수 데이터 없음(API 오류)"
            news_placeholder = news_text if news_text else "관련 뉴스 없음"
```

- [ ] **Step 5: Add Korean fallback template**

In `_generate_template_review()`, add `if template_language == "ko":` before the final Chinese return. It should produce:

```python
        if template_language == "ko":
            market_labels = {"cn": "A주", "us": "미국", "hk": "홍콩"}
            market_label = market_labels.get(self.region, "A주")
            return f"""## {overview.date} {market_label} 시장 리뷰

> 오늘 {self._get_market_scope_name(template_language)}은 **{market_mood}** 흐름을 보였습니다. 다음 거래일에는 지수 지지력, 거래대금 변화, 섹터 지속성을 우선 확인해야 합니다.

### 1. 시장 요약
{self._build_stats_block(overview) or "시장 폭 데이터가 없습니다."}

### 2. 지수 구조
{self._build_indices_block(overview) or indices_text or "지수 데이터가 없습니다."}

### 3. 섹터 하이라이트
{self._build_sector_block(overview) or "- 섹터 등락 데이터가 없습니다."}

### 4. 유동성과 심리
- 거래대금과 상승/하락 종목 수를 함께 보면, 단일 테마 추격보다 확인 이후 대응이 적절합니다.

### 5. 뉴스 촉매
- 사용 가능한 뉴스가 부족하면 테마 지속성에 대한 확신을 낮춰야 합니다.

{self._get_strategy_markdown_block(template_language)}

### 7. 리스크 알림
- 시장에는 리스크가 있으며 투자에는 신중해야 합니다. 위 데이터는 참고용이며 투자 조언이 아닙니다.

---
*리뷰 시간: {datetime.now().strftime('%H:%M')}*
"""
```

## Task 3: Forward Bot Config Into Market Review

**Files:**
- Modify: `bot/commands/market.py`

- [ ] **Step 1: Pass config into `run_market_review()`**

Change the call in `_run_market_review()`:

```python
            review_report = run_market_review(
                notifier=notifier,
                analyzer=analyzer,
                search_service=search_service,
                config=config,
                send_notification=True,
                override_region=override_region,
                trigger_source="bot",
            )
```

## Task 4: Update Docs and Verify

**Files:**
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Add changelog entries**

Add these two flat entries near the top of `[Unreleased]`:

```markdown
- [修复] 修复 `REPORT_LANGUAGE=ko` 时大盘复盘 Prompt、模板兜底与 Telegram 等 report 路由通知正文仍使用中文壳子的问题。
- [测试] 为韩语大盘复盘 Prompt、模板兜底与 Bot `/market` 配置传递补充回归测试。
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
uv run pytest \
  tests/test_market_analyzer_generate_text.py::TestMarketAnalyzerGenerateText::test_build_review_prompt_uses_korean_shell_when_report_language_is_ko \
  tests/test_market_analyzer_generate_text.py::TestMarketAnalyzerGenerateText::test_generate_template_review_uses_korean_shell_when_report_language_is_ko \
  tests/test_bot_market_command.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run broader relevant tests**

Run:

```bash
uv run pytest tests/test_market_analyzer_generate_text.py tests/test_bot_market_command.py -q
```

Expected: all selected tests pass.

- [ ] **Step 4: Optional backend gate**

Run when time permits:

```bash
uv run ./scripts/ci_gate.sh
```

Expected: backend gate passes. If unrelated existing failures appear, record the exact failing tests and do not hide them.

## Notes

- Do not edit `.env` or `.env.example` for this fix; the reproduced bug occurs even when `report_language` is already `ko`.
- Do not modify the existing untracked `.kilo/plans/1782738143402-telegram-korean-language.md`; it appears to be a pre-existing user artifact with a different diagnosis.
- Do not commit; repository instructions require explicit confirmation before `git commit`.

# 市场支持与边界

## 日本/韩国个股 suffix-only MVP（Issue #1718，Refs #1718）

当前阶段支持手动输入日本、韩国股票的 Yahoo Finance 后缀代码，进入既有个股分析、历史保存和基础报告展示链路。Web 自动补全内置一批常用日股/韩股种子索引，支持按 suffix 代码、中英文名称或常用别名搜索。

支持格式：

- 日本：`7203.T`、`6758.T`
- 韩国 KOSPI：`005930.KS`
- 韩国 KOSDAQ：`035720.KQ`

约束与边界：

- 手动输入裸代码时会先检索本地/远程股票池；若 `005930`、`000660` 等裸码命中 `005930.KS`、`000660.KS` 等日韩条目，则按命中的市场提交分析；若股票池未命中，仍按既有 6 位数字代码规则默认落到 A 股语义，并保留为可追踪的跨市场歧义边界。
- 韩股索引保留中文基础名称以兼容中文界面，同时为常用韩股补充英文/韩文名称；Web 韩文界面、分析提交与 `REPORT_LANGUAGE=ko` 报告会优先使用韩文显示名，例如 `005930.KS` 显示为 `삼성전자`。
- 日股/韩股 suffix 识别已集中到共享市场代码工具，数据源路由、Prompt 市场识别、交易日历和股票索引裸码解析复用同一组规则，减少后续市场扩展时的规则漂移。
- 日股日线和基础实时/近实时行情只走 `YfinanceFetcher`，不尝试 AkShare、Tushare、Efinance、Pytdx、Baostock 等 A 股专属数据源；yfinance 报价会尽量带上 `market`、`currency`、`data_quality`、`missing_fields` 等质量元数据。
- 韩股基础实时/近实时行情：配置 `TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET`（Toss Invest OpenAPI，需先在 Toss WTS 登记允许 IP，详见 `docs/adr/0003-toss-openapi-credential-gated-source.md`）后，`TossFetcher` 优先，失败（含未登记允许 IP 的 403）时降级 `YfinanceFetcher`；未配置 Toss 凭据时行为与此前一致，仍只走 `YfinanceFetcher`。
- 韩股日线仍以 `YfinanceFetcher`（KRX 官方收盘价）为首选源，`TossFetcher` 仅作日线 fallback（yfinance 失败时兜底）：实测（2026-07-17）显示 Toss 日线 K 线返回的是 KRX+NXT（韩国另类交易所，交易至约 20:00）合并成交的最终价，而非 KRX 官方收盘价，例如 005930 07-15 官方收盘 279,500 vs Toss 273,500、07-14 官方收盘 263,000 vs Toss 268,000（双向偏离，最大 3.8%），会导致技术指标与国内标准图表不一致；接口未提供按盘段拆分的参数。韩股实时行情路由（Toss 优先）不受影响，因为 NXT 合并最新成交价正是实时场景希望呈现的行情（与 Toss App 展示一致）。美股日线在配置 Toss 凭据时，会在既有 Finnhub/AlphaVantage/YFinance/Longbridge 兜底链路末位追加 `TossFetcher` 作为最后兜底，不改变既有优先级。
- 基本面复用既有 offshore yfinance 轻量路径；A 股专属资金流、龙虎榜、板块等能力按 `not_supported` 降级，offshore 基本面上下文也会标记 provider、as_of、data_quality 和缺失块。
- **韩股个股投资者别买卖动向（수급，KR-only，不含日股）**：`KrInstitutionalFetcher`（`data_provider/kr_institutional_fetcher.py`）通过 Naver（主）/Daum（备援）**无认证**公开页面获取外国人/机构/个人逐日净买卖（单位：**股数**），基准日为最新已确认交易日。已接入分析上下文包全市场统一品质区块（`investor_flows`，权重 5，ADR 0002：非 KR 市场按 `NOT_SUPPORTED` 排除于正规化分母，评分行为中立）、LLM 分析 Prompt（辅助信号）以及报告/通知确定性摘要行（zh/en/ko：外国人/机构 5 日累计净买卖 + 最新确认日 + 来源，个人不计入摘要行）。接口失败/限流/字段缺失一律 **fail-open** 返回无数据，不中断分析；市场级（KOSPI/KOSDAQ）大盘复盘投资者别买卖动向另见下文「日本/韩国大盘复盘 v1」小节。
- 报告 Prompt 已增加日股/韩股市场语义，避免套用 A 股涨跌停、北向资金、龙虎榜、融资融券等概念。
- 交易日历注册 `jp: XTKS / Asia/Tokyo` 与 `kr: XKRX / Asia/Seoul`。日股常规阶段可识别盘前、盘中、午休、15:25-15:30 收盘集合竞价、盘后与非交易日；韩股常规阶段可识别盘前、盘中、15:20-15:30 收盘集合竞价、盘后与非交易日。若本地 `exchange-calendars` 版本缺少对应日历，既有 fail-open/fail-closed 语义保持不变。

兼容性与回退说明（针对结构化检测命中项）：

- `#1815` 本次仅新增 `yfinance` 报价/基本面上下文中的可选字段元数据（如 `market`、`currency`、`data_quality`、`missing_fields`、`provider`），未改动 LLM provider/model/base URL、配置 Schema、运行时环境变量、数据库字段、存量缓存序列化或消息协议版本。
- 与本条 PR 相关的配置语义上，未新增或替换 provider、model、base URL，未新增配置清理/迁移分支；已保存配置仍保持原样，回退方式为回退该提交。
- 外部 API 边界仍仅限既有 `yfinance` fetch 路径（含 `Ticker`/`history`/`fast_info`）与既有兜底逻辑；没有新增或迁移 API 网关/host，`YFINANCE_PRIORITY` 是唯一受影响的可见参数。JP/KR 主指数与 Yahoo symbol 对应如下（可核验）：
  - 日经225：`^N225`（<https://finance.yahoo.com/quote/%5EN225/>）
  - 东证指数：`^TOPX`（<https://finance.yahoo.com/quote/%5ETOPX/>）
  - KOSPI：`^KS11`（<https://finance.yahoo.com/quote/%5EKS11/>）
  - KOSDAQ：`^KQ11`（<https://finance.yahoo.com/quote/%5EKQ11/>）
  - 依赖版本：`requirements.txt` 中 `yfinance>=0.2.0`，回归覆盖路径见 `tests/test_yfinance_jp_kr_indices.py` 与 `tests/test_yfinance_hk_indices.py`。
- 兼容性与回退：`MARKET_REVIEW_REGION` 会保留合法逗号子集（如 `cn,us`）并保持 `both` 全量行为，非法值或空值回退到 `cn`，不会清空或迁移已保存配置。
- 运行时边界：JP/KR 指数按 market_review 的 fail-open 约定逐项抓取；单项失败不会阻断其余指数与其他市场；当两个市场均无可用主指数行情时返回本地可见 `None/空`，主流程继续可按其余市场输出或直接降级。
- 兼容性验证依据：行情/基本面上下文在 `data_provider/base.py` 与 `realtime_types.py` 中按现有 `getattr`/可选字段约定向下游透传，不强制读写新增字段；无配置迁移脚本，未观察到 provider/model/base URL fallback 路径变更。
- 回退方式：若新增元数据字段在某端产生兼容问题，可先忽略这些字段并按既有市场判定+行情展示链路运行；必要时回滚本次提交或通过移除 `jp/kr` `MarketSymbol` 及路由扩展恢复旧行为。

不承诺项：

- 不承诺实时行情；Yahoo Finance 数据可能延迟或字段缺失。
- 不承诺完整基本面、行业/板块、市场宽度或涨跌家数。JP/KR 大盘复盘 v1 提供主要指数、新闻线索与模板/LLM 复盘，`kr` 另提供市场整体外国人/机构资金流统计（`jp` 不提供），但均不提供日韩市场宽度或板块排行。
- 韩股 Web 自动补全已覆盖 KOSPI/KOSDAQ 全量上市股票（约 2,767 只）：构建期由 `scripts/expand_kr_index.py` 经 **FinanceDataReader**（`[dependency-groups]` 的 `scripts` 组，仅构建期依赖，`uv sync --group scripts` 后 `uv run python scripts/expand_kr_index.py`）拉取全量 KR 列表，并只将 KR 行拼接进既有 `stocks.index.json`（CN/HK/US/JP/BSE 行保持不变）；人工种子的多语言名/别名（如 `005930.KS` 的中文/英文名）在同代码上优先覆盖。后端 `resolve_name_to_code` 可按韩文名解析到 `.KS`/`.KQ`（歧义名排除、fail-open），裸 6 位代码命中唯一 KR 条目时按“池命中→KR，未命中→A 股默认”的既有契约解析。日股自动补全仍仅覆盖仓内种子索引（约 30 只头部标的），未命中时手动输入 suffix 代码。
- 不补齐 Portfolio 的 JPY 汇率、成本、市值完整口径；相关字段仅放开市场类型以避免前后端校验拒绝。
- **韩股（KR）Portfolio 已按 KRW 正规口径核算**：账户按 `base_currency`（KR=KRW）聚合，纯 KR 组合以 KRW 直接聚合（KRW→KRW identity 换算，无 KRW→CNY 往返），成本/市值/损益按原生 KRW 计算，`data_quality=ok`。混合币种组合仍回退 CNY 聚合（无回归）。KRW 金额按 0 位小数展示。剩余 `limitations`（`realtime_quote_best_effort`=行情尽力取源、`sector_and_risk_metrics_limited`=不提供板块/风险指标）为**信息性标签**，与损益准确度无关；`fx_and_cost_basis_partial` 已对 KR 移除。JP/TW 维持 partial 口径。不新增专用 KRW 汇率源（复用既有 yfinance 路径）。

回滚方式：移除 `jp/kr` 市场识别、交易日历注册、YFinance 路由扩展、Web/API 类型放行、`scripts/stock_index_seeds/` 日韩种子索引，并删除本文档中的能力声明。

## 日本/韩国大盘复盘 v1（Issue #1815 Phase 2）

大盘复盘 `MARKET_REVIEW_REGION` 新增 `jp` 与 `kr`，并纳入 `both` 的多市场顺序：`cn,hk,us,jp,kr`。

支持范围：

- `jp`：通过 Yahoo Finance 获取日经225 `^N225` 与东证指数 `^TOPX`，输出日股大盘复盘。可复核页面：
  - `^N225`：<https://finance.yahoo.com/quote/%5EN225/>
  - `^TOPX`：<https://finance.yahoo.com/quote/%5ETOPX/>
- `kr`：通过 Yahoo Finance 获取 KOSPI `^KS11` 与 KOSDAQ `^KQ11`，输出韩股大盘复盘。可复核页面：
  - `^KS11`：<https://finance.yahoo.com/quote/%5EKS11/>
  - `^KQ11`：<https://finance.yahoo.com/quote/%5EKQ11/>
- Web 设置页通过 `MARKET_REVIEW_REGION` 文本框输入逗号分隔子集（如 `cn,jp`、`cn,us,jp,kr`）；交易日检查会按 `XTKS / Asia/Tokyo` 与 `XKRX / Asia/Seoul` 过滤 `both` 中当日开市市场。
- 复盘策略、新闻搜索词、Prompt 市场语义和中英文通知标题均按 JP/KR 独立 profile 处理。
- **韩股市场大盘复盘投资者别买卖动向（수급，市场级，KR-only）**：`kr` 大盘复盘接入 KOSPI/KOSDAQ 市场整体外国人/机构日别净买卖（单位 **KRW**，`KrInstitutionalFetcher.get_market_investor_flows`，`data_provider/kr_institutional_fetcher.py`）。数据源为 Naver **无认证**公开页面（`finance.naver.com/sise/investorDealTrendDay.naver`，单一来源，无 Daum 备援），基准日为最新已确认交易日；接口失败/字段缺失一律 **fail-open** 返回无数据，不中断复盘主流程，`jp` 大盘复盘及非 `kr` 复盘不受影响。已接入复盘 LLM Prompt 辅助信号、报告正文确定性摘要行（zh/en/ko：外国人/机构 5 日累计净买卖，个人净买卖不计入摘要行）与结构化 payload `investor_flows` 键。

说明（兼容性与验收口径）：

- 线上数据可用性来自 Yahoo Finance 指数页面与接口契约，当前实现仅覆盖 `data_provider/yfinance_fetcher.py` 的指数路由与降级行为；不对实时行情连通性作稳定性承诺。
- 与该条目标相关的本地自动化验证默认使用离线回归：`tests/test_yfinance_jp_kr_indices.py`、`tests/test_yfinance_hk_indices.py`（共性映射/回退）与 `tests/test_trading_calendar.py`（交易日过滤）。如果要补充实时可用性复核，可在联网环境直接访问上述 Yahoo Finance 页面进行一次性抽检。

- 外部兼容性边界（当前实现默认假设）：
  - 数据源：`yfinance`（版本下限 `requirements.txt` 中的 `yfinance>=0.2.0`）
  - 长期约束：`^N225`、`^TOPX`、`^KS11`、`^KQ11` 必须在 Yahoo Finance 端有可检索 quote 页面；无法检索视为索引级不可用，由 `market_review` fail-open 机制退化到已有市场输出，不中断主流程。
- 兼容验证（可复核）：
  - <https://finance.yahoo.com/quote/%5EN225/>
  - <https://finance.yahoo.com/quote/%5ETOPX/>
  - <https://finance.yahoo.com/quote/%5EKS11/>
  - <https://finance.yahoo.com/quote/%5EKQ11/>
  - 可复现联机复核命令（选做）：
```bash
python - <<'PY'
from yfinance import Ticker
for symbol in ("^N225", "^TOPX", "^KS11", "^KQ11"):
    data = Ticker(symbol).history(period="5d")
    print(symbol, "rows", len(data))
PY
```

边界：

- **韩股大盘复盘市场宽度（시장 폭，KR-only）**：`kr` 大盘复盘现提供 KOSPI/KOSDAQ **各自独立**的上涨/下跌/平盘家数（数据源：NAVER 指数页 `sise_index.naver`，无认证公开页面，2026-07-16 实测；record 含 `as_of` 与 `intraday|close` 会话状态，最新抓取失败时仅允许**同一交易日**缓存以 `stale=true` 提供，开盘前（预估指数区间）不生成 record）。市场宽度（参与广度）与投资者动向（主体方向）为**不同维度信号**，prompt 中各自独立成节并仅做一致/背离交叉解读。数据挂载在结构化 payload 的 KR 专属可选键 `kr_market_context.breadth.{kospi,kosdaq}` 下 — 既有扁平 `breadth` 键（`has_market_stats` 市场专用）契约不变，KR 仍不提供涨跌停家数或成交额汇总（行业板块排行见下条）。JP 大盘复盘不提供市场宽度；`jp` 亦不提供资金流统计（KR 资金流见上文「韩股市场大盘复盘投资者别买卖动向」小节）。任一来源失败均 fail-open，仅省略对应区块，不影响其余复盘内容；Market Light 及买卖评分不消费该数据。
- **韩股大盘复盘行业排名（KR 업종 순위，KR-only）**：`kr` 大盘复盘现提供 KOSPI/KOSDAQ **各自独立**的日涨跌幅行业上/下位榜（数据源：DAUM `finance.daum.net/api/sectors?market=...&change={RISE|FALL}`，无认证公开 API，**必须带 `Referer: https://finance.daum.net/domestic/sectors`（否则 403）**，2026-07-16 实测；`changeRate`(比率)→`change_pct`(%)，每方向各取最多 5 个、去重；record 含 `as_of` 与由 KR 交易日历 `market_phase`(XKRX) 派生的 `intraday|close`，最新抓取失败时仅允许**同一交易日**缓存以 `stale=true` 提供）。行业排名（市场结构）与市场宽度（参与广度）、投资者动向（主体方向）为**三个独立维度**；KR **不提供** A 股式概念/主题（概念板块）排行（D5）。数据挂载在结构化 payload 的 `kr_market_context.sector_rankings.{kospi,kosdaq}` 下（与 `breadth` 子键独立，任一缺失不影响另一）。CN 既有 `get_sector_rankings()` 遍历路径不变；`KR_PROFILE.has_sector_rankings` 保持 `False`（避免污染 CN 公共路径）。任一来源/方向失败均 fail-open（按市场·方向独立的 circuit breaker，键 `daum_sector_{market}_{rise|fall}`），仅省略对应区块；Market Light 及买卖评分不消费该数据。
- 单一 JP/KR 指数拉取失败按既有 yfinance fail-open 逻辑跳过，不拖垮其它指数或其它市场。
- 如果 `exchange-calendars` 缺少对应交易所日历，继续沿用既有交易日 fail-open/fail-closed 语义。

回滚方式：从 `MARKET_REVIEW_REGION` 合法值、Web 设置枚举、MarketProfile/MarketStrategy、`_MARKET_REVIEW_MARKETS` 和本文档中移除 `jp` / `kr`。

## 台湾个股支持（suffix-only，Issue #1772 / #1777）

大盘复盘 `MARKET_REVIEW_REGION` 新增 `jp` 与 `kr`，并纳入 `both` 的多市场顺序：`cn,hk,us,jp,kr`。

支持范围：

- `jp`：通过 Yahoo Finance 获取日经225 `^N225` 与东证指数 `^TOPX`，输出日股大盘复盘。可复核页面：
  - `^N225`：<https://finance.yahoo.com/quote/%5EN225/>
  - `^TOPX`：<https://finance.yahoo.com/quote/%5ETOPX/>
- `kr`：通过 Yahoo Finance 获取 KOSPI `^KS11` 与 KOSDAQ `^KQ11`，输出韩股大盘复盘。可复核页面：
  - `^KS11`：<https://finance.yahoo.com/quote/%5EKS11/>
  - `^KQ11`：<https://finance.yahoo.com/quote/%5EKQ11/>
- Web 设置页通过 `MARKET_REVIEW_REGION` 文本框输入逗号分隔子集（如 `cn,jp`、`cn,us,jp,kr`）；交易日检查会按 `XTKS / Asia/Tokyo` 与 `XKRX / Asia/Seoul` 过滤 `both` 中当日开市市场。
- 复盘策略、新闻搜索词、Prompt 市场语义和中英文通知标题均按 JP/KR 独立 profile 处理。

说明（兼容性与验收口径）：

- 线上数据可用性来自 Yahoo Finance 指数页面与接口契约，当前实现仅覆盖 `data_provider/yfinance_fetcher.py` 的指数路由与降级行为；不对实时行情连通性作稳定性承诺。
- 与该条目标相关的本地自动化验证默认使用离线回归：`tests/test_yfinance_jp_kr_indices.py`、`tests/test_yfinance_hk_indices.py`（共性映射/回退）与 `tests/test_trading_calendar.py`（交易日过滤）。如果要补充实时可用性复核，可在联网环境直接访问上述 Yahoo Finance 页面进行一次性抽检。

- 外部兼容性边界（当前实现默认假设）：
  - 数据源：`yfinance`（版本下限 `requirements.txt` 中的 `yfinance>=0.2.0`）
  - 长期约束：`^N225`、`^TOPX`、`^KS11`、`^KQ11` 必须在 Yahoo Finance 端有可检索 quote 页面；无法检索视为索引级不可用，由 `market_review` fail-open 机制退化到已有市场输出，不中断主流程。
- 兼容验证（可复核）：
  - <https://finance.yahoo.com/quote/%5EN225/>
  - <https://finance.yahoo.com/quote/%5ETOPX/>
  - <https://finance.yahoo.com/quote/%5EKS11/>
  - <https://finance.yahoo.com/quote/%5EKQ11/>
  - 可复现联机复核命令（选做）：
```bash
python - <<'PY'
from yfinance import Ticker
for symbol in ("^N225", "^TOPX", "^KS11", "^KQ11"):
    data = Ticker(symbol).history(period="5d")
    print(symbol, "rows", len(data))
PY
```

边界：

- JP/KR 大盘复盘 v1 不提供涨跌家数、涨跌停、行业/板块排行或资金流统计；结构化 payload 中 `breadth` 仍只在有市场宽度数据时出现。
- 单一 JP/KR 指数拉取失败按既有 yfinance fail-open 逻辑跳过，不拖垮其它指数或其它市场。
- 如果 `exchange-calendars` 缺少对应交易所日历，继续沿用既有交易日 fail-open/fail-closed 语义。

回滚方式：从 `MARKET_REVIEW_REGION` 合法值、Web 设置枚举、MarketProfile/MarketStrategy、`_MARKET_REVIEW_MARKETS` 和本文档中移除 `jp` / `kr`。

## 台湾个股支持（suffix-only，Issue #1772 / #1777）

当前阶段支持手动输入台湾股票的 Yahoo Finance 后缀代码，进入既有个股分析、历史保存、报告渲染、DecisionSignal、Portfolio 和 Intelligence 链路。TWSE 上市股票使用 `.TW` 后缀，TPEx 上柜（柜买）股票使用 `.TWO` 后缀，二者折叠为同一 `tw` 市场标签。

近期台股链路已从早期 MVP 收敛为一等个股分析市场：市场识别、数据路由、交易日历/市场阶段、YFinance 日线与基础行情、主要指数、服务层/API/Web 市场枚举、TWD 币种标注、三大法人报告区块与 LLM prompt 消费均已接入。仍需保留的边界是：台股股票池种子/自动补全、大盘复盘 `MARKET_REVIEW_REGION=tw`、Market Light 大盘红绿灯告警和完整台股市场宽度/板块排行尚未纳入。

支持格式：

- 上市（TWSE）：`2330.TW`、`0050.TW`
- 上柜（TPEx / 柜买）：`6488.TWO`、`5483.TWO`
- 代码 base 为 4-6 位数字（普通股 4 位，ETF/其他至 6 位，如 `00878.TW`、`006208.TW`），较日股 `.T` 的 4-5 位更宽。

约束与边界：

- **严格 suffix-only**：裸 `2330`、`00878` 等不带后缀的代码不会进入台股语义（`detect_market` / `get_market_for_stock` 仅在显式 `.TW`/`.TWO` 后缀时返回 `tw`）。当前未内置台股股票索引/种子解析，Web 自动补全不承诺完整台股股票池；未命中时请手动输入完整 suffix 代码。
- 台股日线和基础实时/近实时行情只走 `YfinanceFetcher`，不尝试 AkShare、Tushare、Efinance、Pytdx、Baostock 等 A 股专属数据源。
- 基本面复用既有 offshore yfinance 轻量路径；`institution` 区块额外消费台股三大法人资料并渲染到报告，A 股专属资金流、龙虎榜、板块等能力按 `not_supported` 降级。
- 报告 Prompt 已增加台股市场语义（新台币、三大法人、TWSE/TPEx ±10% 涨跌停），并将三大法人净买卖超注入 LLM 分析上下文，避免套用 A 股北向资金、龙虎榜等概念。
- 交易日历注册 `tw: XTAI / Asia/Taipei`。TWSE 为 09:00–13:30 连续交易、无午休；收盘集合竞价 13:25–13:30 已按 5 分钟启发式窗口建模（`_CLOSING_AUCTION_WINDOW_MINUTES["tw"]=5`，`market_phase` 可返回 `closing_auction`）。JP/KR 也已按常规交易时段补齐收盘集合竞价窗口（JP 15:25-15:30、KR 15:20-15:30）。若本地 `exchange-calendars` 版本缺少对应日历，既有 fail-open/fail-closed 语义保持不变。
- 主要指数提供加权指数 `^TWII` 与柜买指数 `^TWOII`。
- 三大法人买卖超（institutional flows）资料层：`TwInstitutionalFetcher`（`data_provider/tw_institutional_fetcher.py`）提供上市（TWSE T86，legacy `rwd` 端点）/ 上柜（TPEx OpenAPI）每日外资·投信·自营商·三大法人买卖超（单位：**股数**；按日期+市场做单日全市场缓存再过滤个股，TPEx 民国年转西元有单测覆盖）。接口失败/限流/空响应/字段缺失一律 **fail-open** 返回无数据，不中断分析；仅对 `.TW`/`.TWO` 生效，不改动现有市场流程。资料来源为政府开放资料，采「政府资料开放授权条款第 1 版」(OGDL v1，允许商用与再散布，需标示来源)。
- 三大法人 fetcher 已具备并发缓存防击穿和按 TWSE/TPEx 分流的熔断保护；TPEx OpenAPI 仅服务最新交易日，传入与服务日期不符的明确日期会 fail-open 返回无数据，避免错日资料静默进入报告。
- 台股财务金额会使用 TWD -> 「新台币」标注，避免落入 A 股语境下的默认「元」。

不承诺项：

- 不承诺实时行情；Yahoo Finance 数据可能延迟或字段缺失。
- 不承诺完整基本面、行业/板块、市场宽度、涨跌家数或台股大盘复盘；`MARKET_REVIEW_REGION` 仍只接受 `cn/hk/us/jp/kr/both` 或这些市场的逗号子集。
- 台股股票索引/种子和 Web 自动补全仍未完整接入；告警 MarketRegion 与后端 Market Light 告警仍为 `cn/hk/us`，未含 `tw`。
- 不补齐 Portfolio 的 TWD 汇率、成本、市值完整口径；台股 Portfolio 当前属于 partial valuation 市场。

回滚方式：移除 `tw` 市场识别、交易日历注册、YFinance 路由扩展、三大法人资料层/报告消费、TWD 标注、服务层/API 市场枚举及前端市场类型放行，并删除本文档中的能力声明。

## 日本/韩国 Portfolio 与 Market Light 边界（Issue #1815 Phase 3）

Portfolio 允许 JP/KR 账户、交易和持仓快照进入现有链路，但会将账户/持仓快照标记为 `data_quality=partial`，并通过 `limitations` 明确 `realtime_quote_best_effort`、`fx_and_cost_basis_partial`、`sector_and_risk_metrics_limited`；不承诺 JPY/KRW 汇率、成本、市值、行业集中度或组合风险指标完整口径。

- JP/KR 账户、交易、现金流水和公司行动 API 保持可创建/查询；当前不新增 JPY/KRW 汇率源、税费模型、交易单位/最小变动价位校验或行业映射。
- Market Light 快照和 Market Light 告警仍只支持 `cn` / `hk` / `us`。
- Web 告警市场下拉不展示 `jp` / `kr`；后端 `normalize_market_region()` 对 `jp` / `kr` 返回显式 unsupported 错误。
- Web 设置页中 `MARKET_REVIEW_REGION` 从固定枚举下拉收敛为自由文本输入，用于保存 `cn,us,jp`、`cn,hk,us` 等逗号分隔子集；该 UI 变化只影响大盘复盘配置，不影响 Market Light 告警市场枚举。
- `MARKET_REVIEW_REGION` 既有 `cn`、`hk`、`us` 可原样保留；若用户希望维持 JP/KR 扩展前 `both` 对应的三市场复盘边界，应改为 `cn,hk,us`；只有希望纳入五市场复盘时才继续使用 `both` 或显式配置 `cn,hk,us,jp,kr`。
- 该轮边界收敛不改动 LLM Provider / Model / Base URL 的持久化语义，也不执行默认模型、运行时配置清理或回写；配置更新仍是**原子 upsert**（`ConfigManager.apply_updates`），保存/导入只写入提交的键，未提交的 `LITELLM_MODEL`、`LITELLM_FALLBACK_MODELS`、`AGENT_LITELLM_MODEL`、`VISION_MODEL`、`OPENAI_BASE_URL` 等旧值保留不清空。
- 可直接核验的配置兼容证据：本轮未新增或替换外部 provider/model/Base URL，仍沿用 LiteLLM OpenAI-compatible 路由（<https://docs.litellm.ai/docs/providers/openai_compatible>）、OpenAI Chat Completions 请求形状（<https://platform.openai.com/docs/api-reference/chat/create>），以及 [LLM 服务商配置指南](llm-providers.md#官方来源与兼容性) 中集中维护的各 provider 官方来源链接。当前运行时依赖窗口以 `requirements.txt` 的 `litellm>=1.80.10,!=1.82.7,!=1.82.8,<2.0.0` 为准；旧配置没有迁移脚本或清理分支，保存/导入仍只通过 `ConfigManager.apply_updates` 写入本次提交键。回退路径是恢复变更前 `.env`/配置备份中的 `MARKET_REVIEW_REGION`，或直接 revert 本 PR；未提交的 `LITELLM_CONFIG`、`LLM_CHANNELS`、`LLM_OPENAI_*`、`LITELLM_MODEL`、`AGENT_LITELLM_MODEL`、`LITELLM_FALLBACK_MODELS`、`VISION_MODEL`、`OPENAI_*` 等既有运行时配置不需要迁移。回归证据为 `tests/test_system_config_service.py::SystemConfigServiceTestCase::test_update_market_review_region_does_not_trigger_runtime_model_cleanup` 与 `tests/test_config_env_compat.py::test_market_review_region_updates_do_not_change_llm_provider_model_contract`。
- Web UI 可视证据口径：Market Light 告警目标范围切到“大盘市场”时，市场区域下拉只显示 A 股、港股、美股，不显示日股/韩股；设置页 `MARKET_REVIEW_REGION` 渲染为可输入逗号分隔值的文本框。当前仓库不保存一次性截图证据，可替代证据为 `apps/dsa-web/src/components/alerts/__tests__/AlertRuleForm.test.tsx`、`apps/dsa-web/src/components/settings/__tests__/SettingsField.test.tsx` 和 `apps/dsa-web/tests/system_config_i18n.test.ts` 的断言。

回滚方式：移除 Portfolio snapshot 的 `data_quality` / `limitations` 扩展，恢复告警前端/后端对市场枚举的旧边界说明；如需整体回滚，移除 `jp/kr` 市场识别、交易日历注册、YFinance 路由扩展、Web/API 类型放行、`scripts/stock_index_seeds/` 日韩种子索引，并删除本文档中的能力声明。

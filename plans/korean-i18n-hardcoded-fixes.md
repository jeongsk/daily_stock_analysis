# Web UI 硬编码中文文本国际化修复计划

## 背景

`uiText.ts` 和 `featureText.ts` 的三语翻译本身完整，但多个页面/组件存在**绕过 i18n 的硬编码中文**，导致切换到韩语/英语时仍显示中文。

## i18n 架构说明

- 共享 UI 文本：`useUiLanguage()` → `t('key')`（查 `src/i18n/uiText.ts`）
- 功能专属文本：在 `src/locales/featureText.ts` 定义 `Record<UiLanguage, {...}>` 对象，组件中用 `const { language } = useUiLanguage()` 取 `TEXT[language].xxx`
- 组件内嵌文本：report 组件用局部 `Record<ReportLanguage, {...}>` 对象（如 `AnalysisContextSummary.tsx`）

## 受影响文件与修复策略

### 优先级 1：小文件快速修复

#### 1. `src/components/tasks/TaskPanel.tsx`（2 处）
已使用 `t()`，仅需替换 2 个硬编码 aria-label：
- L207: `aria-label="进行中任务"` → `aria-label={t('taskPanel.processingAria')}`
- L213: `aria-label="等待中任务"` → `aria-label={t('taskPanel.pendingAria')}`

#### 1b. `src/pages/NotFoundPage.tsx`（4 处）
**无 i18n。** 但对应 key 已存在于 `uiText.ts`：
- L10: `document.title = '页面未找到 - DSA'` → `t('notFound.pageTitle')`
- L27: `页面未找到` → `{t('notFound.title')}`
- L28: `抱歉，您访问的页面不存在或已被移动` → `{t('notFound.description')}`
- L38: `返回首页` → `{t('notFound.backHome')}`
修复：添加 `useUiLanguage` import + `const { t } = useUiLanguage()`，替换 4 处硬编码。

#### 2. `src/components/alerts/AlertTriggerHistory.tsx`（~15 处）
**当前无 i18n。** 需要：
- 添加 `useUiLanguage` import 和 hook
- 在 `featureText.ts` 新增 `ALERT_TRIGGER_TEXT`：

```ts
export const ALERT_TRIGGER_TEXT = {
  zh: {
    cardTitle: '触发历史', cardSubtitle: '评估记录',
    loading: '正在加载触发历史',
    emptyTitle: '暂无触发历史',
    emptyDescription: '后台评估会记录 triggered、skipped、degraded 和 failed 状态；正常未触发不会写入历史。',
    quality: '质量',
    status: '状态', phaseQuality: '阶段 / 质量', target: '目标',
    observedValue: '观察值', threshold: '阈值', dataSource: '数据源',
    dataTime: '数据时间', reason: '原因',
    statusLabels: { triggered: '已触发', skipped: '已跳过', degraded: '降级', failed: '失败' },
  },
  en: {
    cardTitle: 'Trigger history', cardSubtitle: 'Evaluation records',
    loading: 'Loading trigger history',
    emptyTitle: 'No trigger history',
    emptyDescription: 'Background evaluation records triggered, skipped, degraded, and failed statuses; normal non-triggers are not written to history.',
    quality: 'Quality',
    status: 'Status', phaseQuality: 'Phase / Quality', target: 'Target',
    observedValue: 'Observed', threshold: 'Threshold', dataSource: 'Data source',
    dataTime: 'Data time', reason: 'Reason',
    statusLabels: { triggered: 'Triggered', skipped: 'Skipped', degraded: 'Degraded', failed: 'Failed' },
  },
  ko: {
    cardTitle: '트리거 이력', cardSubtitle: '평가 기록',
    loading: '트리거 이력 로딩 중',
    emptyTitle: '트리거 이력 없음',
    emptyDescription: '백그라운드 평가는 triggered, skipped, degraded, failed 상태를 기록합니다. 정상 미발생은 이력에 기록되지 않습니다.',
    quality: '품질',
    status: '상태', phaseQuality: '단계 / 품질', target: '대상',
    observedValue: '관측값', threshold: '임계값', dataSource: '데이터 소스',
    dataTime: '데이터 시간', reason: '사유',
    statusLabels: { triggered: '발동됨', skipped: '건너뜀', degraded: '저하', failed: '실패' },
  },
} as const;
```

- 组件中 `const { language } = useUiLanguage()`，替换硬编码字符串为 `ALERT_TRIGGER_TEXT[language].xxx`
- L28 `renderPhaseQuality` 需传入 language 或改为组件内联渲染

---

### 优先级 2：已有部分 i18n 的文件

#### 3. `src/pages/PortfolioPage.tsx`（~60 处硬编码中文）
**已使用 `useUiLanguage()` 和 `PORTFOLIO_TEXT`。** 但仍存在大量硬编码：

**placeholder（需加入 PORTFOLIO_TEXT）：**
- L1069: `placeholder="账户名称（必填）"` / L1075: `placeholder="券商（可选，如 Demo/华泰）"`
- L1081: `placeholder="基准币（如 CNY/USD/HKD）"`
- L1338: `placeholder="股票代码（例如 600519）"` / L1350: `placeholder="数量（必填）"`
- L1352: `placeholder="成交价（必填）"` / L1356: `placeholder="手续费（可选）"`
- L1358: `placeholder="税费（可选）"` / L1378: `placeholder="金额"`
- L1389: `placeholder="股票代码"` / L1401: `placeholder="每股分红"` / L1405: `placeholder="拆并股比例"`
- L1489: `placeholder="按股票代码筛选"`

**title 属性：** L1054 `"创建账户失败"` / L1062 `"创建账户成功"` / L1455 `"CSV 解析结果"` / L1463 `"CSV 预演结果"`/`"CSV 提交结果"` / L1584 `"暂无流水"` / L1608 `"删除错误流水"`

**JSX 文本节点（section 标题、option、button）：**
- L1033 `"新建账户"` / L1047 `"创建后自动切换到该账户"`
- L1090-1095 市场选项：`"市场：A 股（cn）"` / `"市场：港股（hk）"` 等 6 个
- L1336 `"手工录入：交易"` / L1345-1346 `"买入"`/`"卖出"`
- L1361 `"手续费和税费可留空..."` / L1362 `"提交交易"`
- L1367 `"手工录入：资金流水"` / L1374-1375 `"流入"`/`"流出"` / L1382 `"提交资金流水"`
- L1387 `"手工录入：公司行为"` / L1396-1397 `"现金分红"`/`"拆并股调整"` / L1409 `"提交企业行为"`
- L1416 `"券商 CSV 导入"` / L1441 `"仅预演（不写入）"`
- L1472 `"事件记录"` / L1476-1478 `"交易流水"`/`"资金流水"`/`"公司行为"`
- L1494-1512 筛选选项（买卖方向、资金方向、公司行为）
- L1591 `"第 X / Y 页"`

> 注意：部分 option label（买入/卖出、流入/流出等）已存在于 `PORTFOLIO_SIDE_LABELS` / `PORTFOLIO_CASH_DIRECTION_LABELS` / `PORTFOLIO_CORPORATE_ACTION_LABELS`（featureText.ts），可直接复用。

---

### 优先级 3：完全无 i18n 的页面

#### 4. `src/pages/ChatPage.tsx`（~47 处用户可见中文）
**完全无 i18n。** 需要：
- 在 `featureText.ts` 新增 `CHAT_TEXT`
- 添加 `useUiLanguage` import 和 hook

需要翻译的字符串（按类型分组）：

**页面标题与空状态：**
- `'问股 - DSA'`（document.title）
- `"开始问股"` / `"输入「分析 600519」或「茅台现在能买吗」，AI 将调用实时数据工具为您生成决策报告。"`
- `"加载对话中..."` / `"暂无历史对话"` / `"开始提问后，这里会保留会话记录。"`

**JSX 文本节点：**
- `"思考过程"` / `"上下文压缩"` / `"节省长会话 token"` / `"策略"`

**aria-label / title：**
- `"开启新对话"` / `"历史对话"` / `"导出会话为 Markdown 文件"` / `"发送到已配置的通知机器人/邮箱"`
- `"删除对话"` / `"导出此条消息为 Markdown"` / `"查看最新消息"`
- `"追问上下文加载中"` / `"上下文压缩设置未保存"`
- `"发送成功"` / `"发送失败"`

**输入与状态：**
- `placeholder="例如：分析 600519 / 茅台现在适合买入吗？ (Enter 发送, Shift+Enter 换行)"`
- `'正在连接...'` / `'AI 正在思考...'` / `'正在生成最终分析...'` / `'处理中...'`
- `'保存中...'` / `'已启用'` / `'未启用'` / `'通用分析'` / `'通用'`
- `'操作失败，请重试'` / `'无法读取上下文压缩配置'` / `'上下文压缩设置保存失败'`
- `'已发送到通知渠道'` / `'发送失败'`
- `'从自选删除'` / `'加入自选'`
- `'收起策略选择'` / `'展开策略选择'`

**技能预设标签（L34-39）：**
- `'用缠论分析茅台'` / `'波浪理论看宁德时代'` / `'分析比亚迪趋势'` 等

#### 5. `src/pages/StockScreeningPage.tsx`（~95 处用户可见中文）
**完全无 i18n。** 这是最大的文件。需要：
- 在 `featureText.ts` 新增 `SCREENING_TEXT`
- 添加 `useUiLanguage` import 和 hook

主要字符串类别：
- 错误/状态消息（~20条）：`'选股任务失败，请稍后重试。'`, `'请求超时'`, `'网络连接中断'` 等
- JSX 文本节点（~35条）：section 标题（`热点题材`, `选择策略`, `选股结果`），表头（`代码`/`名称`/`行业`/`价格`/`涨跌幅`/`评分`/`风险`/`详情`），label（`摘要`/`操作信号`/`风险标签`/`主要因子`/`成交额`/`催化因素`），空状态（`暂无结果`, `无`, `无因子明细`）
- 任务状态（~10条）：`'正在提交选股任务...'`, `'选股运行中'`, `'选股完成'`, `'等待运行'` 等
- LLM/AlphaSift 提示（~10条）：`'AlphaSift 提示'`, `'LLM 已降级'`, `'未返回（LLM 已降级）'` 等
- JSX title 属性（5条）：`"AlphaSift 未开启"`, `"实验功能与风险提示"` 等

#### 6. `src/components/settings/LLMChannelEditor.tsx`（~11 处 JSX 属性 + 多处字符串字面量）
**完全无 i18n。** 需要：
- 在 `featureText.ts` 新增 `LLM_CHANNEL_TEXT`
- 添加 `useUiLanguage` import 和 hook

主要硬编码：
- `label="渠道名称"` / `label="协议"` / `label="主模型"` / `label="Agent 主模型"`
- `label="备选模型"` / `label="Vision 模型"` / `label="可选模型（可多选）"`
- `placeholder="选择协议"` / `placeholder="选择服务商"`
- `title="运行时能力检测"` / `title="保存后提示"`
- L595: `placeholder={channel.protocol === 'ollama' ? '本地 Ollama 可留空' : '支持多个 Key 逗号分隔'}`
- L658: `label={discoveredModels.length > 0 ? '手动模型（逗号分隔）' : '模型（逗号分隔）'}`

#### 7. `src/pages/AlertsPage.tsx`（~16 处用户可见中文）
**完全无 i18n。** 已有 `ALERT_LIST_TEXT` / `ALERT_FORM_TEXT` 在 featureText.ts，但页面本身仍有硬编码：
- `title="告警中心"` / `description="管理事件告警..."`
- `title="创建成功"` / `title="测试结果"`
- `title="通知尝试记录"` / `subtitle="通知结果"` / `"正在加载通知尝试记录"`
- `"暂无通知尝试记录"` / `"当前没有可展示的通知尝试明细..."`
- 表头（6条）：`"渠道"` / `"状态"` / `"错误码"` / `"耗时"` / `"时间"` / `"诊断"`

需要新增 `ALERT_PAGE_TEXT` 并使用 `useUiLanguage`。

---

### 优先级 4：`en ? : zh` 二元判断反模式（韩语回退到中文）

以下文件使用 `language === 'en' ? 英文 : 中文` 模式，韩语会错误回退到中文：

#### 8. `src/components/settings/SettingsPanelErrorBoundary.tsx`（L128-140）
```ts
// 当前：language === 'en' ? {英文} : {中文}
// 韩语走到 else 分支显示中文
```
修复：改为三语 `language === 'en' ? {...en} : language === 'ko' ? {...ko} : {...zh}`

#### 9. `src/components/settings/IntelligentImport.tsx`（L25-33）
`getConfidenceMeta` 函数：
- `'高'` / `'Low'` / `'中'` → 韩语需显示 `'높음'` / `'낮음'` / `'중간'`

#### 10. `src/pages/TokenUsagePage.tsx`（L27）
```ts
return language === 'en' ? 'en-US' : 'zh-CN';
// 韩语应返回 'ko-KR'
```

#### 11. `src/pages/SettingsPage.tsx`（L479）
```ts
new Intl.DateTimeFormat(language === 'en' ? 'en-US' : 'zh-CN', {...})
// 韩语应使用 'ko-KR'
```

---

## 实施顺序

1. **TaskPanel.tsx** — 2 行，直接复用现有 `t()` key
2. **NotFoundPage.tsx** — 4 行，复用现有 `notFound.*` key
3. **SettingsPanelErrorBoundary.tsx** — 改三元为三语
4. **IntelligentImport.tsx** — confidence labels 三语
5. **TokenUsagePage.tsx** — locale 补 `ko-KR`
6. **SettingsPage.tsx** — date locale 补 `ko-KR`
7. **AlertTriggerHistory.tsx** — 新增 `ALERT_TRIGGER_TEXT` + hook
8. **PortfolioPage.tsx** — 补全 `PORTFOLIO_TEXT` key（~60 处）
9. **AlertsPage.tsx** — 新增 `ALERT_PAGE_TEXT` + hook
10. **LLMChannelEditor.tsx** — 新增 `LLM_CHANNEL_TEXT` + hook
11. **ChatPage.tsx** — 新增 `CHAT_TEXT` + hook
12. **StockScreeningPage.tsx** — 新增 `SCREENING_TEXT` + hook（最大，~95 处）

---

## 实施模式

每个文件遵循统一模式：
1. 在 `featureText.ts` 中添加 `XXX_TEXT = { zh: {...}, en: {...}, ko: {...} } as const`
2. 在组件中 `import { useUiLanguage } from '../../contexts/UiLanguageContext'`
3. `const { language, t } = useUiLanguage()`
4. `const tx = XXX_TEXT[language]`
5. 将所有硬编码中文替换为 `tx.xxx` 或 `t('xxx')`
6. 对 `renderPhaseQuality` 等纯函数，传入 `language` 参数

## 验证

```bash
cd apps/dsa-web && npm run lint && npm run build
```

切换 UI 语言到韩语（KO），逐页确认无残留中文。

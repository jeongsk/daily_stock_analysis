# Report Evidence

为报告格式 / 报告渲染效果 / Web UI 变更生成 PR 描述所需的前后对比可视证据。

## Usage

```text
/report-evidence
```

## Instructions

规则真源为仓库根目录 `AGENTS.md`（尤其是 §1 中"修改报告格式、报告渲染效果或 Web UI 界面时，PR 描述必须附受影响报告 / 页面截图"以及"Issue / PR 过程截图...不得作为仓库文件合入"两条硬规则）。本 Skill 只提供生成证据的操作步骤，不重复其规则内容；如与 `AGENTS.md` 冲突，以 `AGENTS.md` 为准。

### Step 1: 判别变更对象

```bash
git diff --name-only
```

按路径分类：

- 报告渲染路径：`src/services/report_renderer.py`（Jinja2 模板渲染入口，`templates/report_*.j2`）、`src/formatters.py`（`markdown_to_html_document`，HTML 文档转换）、`src/md2img.py`（Markdown 转 PNG）等。
- Web UI 路径：`apps/dsa-web/`。

若两类路径都命中，Step 2 与 Step 3 都要执行。若改动不落在以上任一路径，说明本次改动不触发可视证据要求，无需继续。

### Step 2: 报告渲染前后对比图

确认实际渲染入口：`src/services/report_renderer.py` 的 `render(platform, results, report_date=None, summary_only=False, extra_context=None)`，`platform` 取值为 `markdown` / `wechat` / `brief`，读取 `templates/report_{platform}.j2`。若改动的是 `src/md2img.py` 或 `src/formatters.py`，最终视觉效果通过 `markdown_to_image()`（内部调用 `markdown_to_html_document()` 生成 HTML 后转 PNG）体现。

前置依赖确认（若要生成 PNG 而非仅 HTML）：

- Python 包 `imgkit`（`pyproject.toml` 已声明 `imgkit>=1.2.0`）。
- 系统二进制 `wkhtmltoimage`（随 `wkhtmltopdf` 提供，`docker/Dockerfile` 中安装）。本地未安装时，`imgkit.from_string` 会抛出 `OSError`，`markdown_to_image()` 捕获后返回 `None`（仅回退到文本，不生成图片）。
- 若 `md2img_engine` 配置为 `markdown-to-file`，需要 `m2f` 命令（`npm i -g markdown-to-file`）。

生成步骤：

1. 记录当前分支 HEAD，切到基准分支（通常是 `main` 或 PR 目标分支）生成"变更前"产物；再切回当前分支生成"变更后"产物。也可用 `git worktree` 或 `git show <base>:<path>` 避免来回切分支，按实际情况选择更安全的方式。
2. 使用仓库内既有测试夹具（如 `tests/test_report_renderer.py` 中的 `_make_result()` 构造方式）或已有真实报告样本作为输入，不要临时编造与实际数据结构不符的字段。
3. 最小 Python 片段示例（按需调整 platform 与字段）：

```python
from src.analyzer import AnalysisResult
from src.services.report_renderer import render

result = AnalysisResult(
    code="600519",
    name="贵州茅台",
    trend_prediction="看多",
    sentiment_score=72,
    operation_advice="持有",
    analysis_summary="稳健",
    decision_type="hold",
    dashboard={
        "core_conclusion": {"one_sentence": "持有观望"},
        "intelligence": {"risk_alerts": []},
        "battle_plan": {"sniper_points": {"stop_loss": "110"}},
    },
    report_language="zh",
)

md_text = render("markdown", [result], summary_only=False)

# 如需图片而非纯 Markdown/HTML，再调用：
from src.md2img import markdown_to_image
png_bytes = markdown_to_image(md_text)  # None 表示依赖缺失或转换失败，回退用 HTML 预览
```

4. 若 `markdown_to_image` 返回 `None`（依赖缺失），改用 `src.formatters.markdown_to_html_document(md_text)` 生成 HTML，再用浏览器打开渲染截图，并在 PR 描述中说明未安装 `wkhtmltoimage` 的降级方式。
5. 将变更前、变更后的图片（PNG 或 HTML 截图）分别保存，文件名体现 before/after，便于 Step 4 归档。

### Step 3: Web UI 前后对比截图

```bash
cd apps/dsa-web
npm run dev
```

启动 Vite dev server 后：

1. 变更前：切到基准分支或使用 `git stash` / 单独 worktree 跑一次 dev server，截取受影响页面。
2. 变更后：当前分支下跑 dev server，截取同一页面、同一状态（尽量保持窗口尺寸、数据、主题一致）。
3. 截图方式：优先使用可用的浏览器自动化工具（如已连接的 Chrome DevTools / Preview 工具）访问对应路由并截图；若无自动化工具可用，改为手动在浏览器中截图并说明是人工采集。
4. 记录截图对应的路由 / 页面状态（如深色模式、移动端视口等），保证前后一致可比。

### Step 4: 产物存放位置

所有生成的图片文件必须保存在仓库外部，例如系统临时目录或本会话的 scratchpad 目录，禁止把截图 / 渲染图片写入仓库任何路径（对应 `AGENTS.md` 中"一次性截图不得作为仓库文件合入"的硬规则）。

只允许在 `.claude/reviews/` 下保存纯文本记录（例如 `.claude/reviews/report-evidence/<date>-<topic>.md`），内容仅为：变更对象、外部产物文件路径列表、验证方式简述，不放图片二进制本身。

### Step 5: 生成 PR 附件说明

汇总以下内容，供直接粘贴进 PR 描述的 `Visual Evidence` 部分：

- 变更前后图片的绝对路径列表（外部路径，非仓库路径）。
- 一段变更点摘要，例如："变更前 `report_markdown` 模板缺少筹码分布提示；变更后新增未启用提示文案。" 需具体到字段 / 区块级别，不要泛泛而谈。
- 若某一侧（前或后）无法采集（例如基准分支无法本地重建、依赖缺失、环境限制），按 `AGENTS.md` 要求写明"原因 + 替代可视证据"，例如："变更前版本因数据源字段已废弃无法在当前环境复现，附变更后截图 + 对应 diff 行号作为替代证据。"

## Allowed Auto-Actions (No Confirmation Needed)

- 读取代码、模板、测试夹具以理解渲染入口和数据结构
- 执行本地渲染 / 转换脚本、`npm run dev`
- 将生成的图片保存到仓库外部路径
- 在 `.claude/reviews/` 下写入纯文本记录（不含图片）

## Actions Requiring Confirmation

- 切换当前 git 分支、创建 `git worktree`、执行 `git stash`
- 安装系统级依赖（如 `wkhtmltopdf`）或全局 npm 包（如 `markdown-to-file`）

# Sync Bilingual Docs

检测本次改动涉及的双语（多语言）文档配对，判断是否需要同步，并按 `AGENTS.md` 第 1 条硬规则完成同步或生成未同步说明文案。

## Usage

```text
/sync-bilingual-docs
```

## Instructions

规则真源为仓库根目录 `AGENTS.md`（尤其是第 1 条"变更中英双语文档之一时，需评估另一份是否需要同步"及 README 最小化条款）。本 skill 只提供执行步骤，不复述规则内容，如与 `AGENTS.md` 冲突以 `AGENTS.md` 为准。

### Step 1: 收集变更的文档文件

```bash
git status --short
git diff --name-only -- docs '*.md' README.md
```

只关注 `docs/**` 与根目录 `README*.md`；其余改动不在本 skill 处理范围内。

### Step 2: 语言对照表

以下配对为本仓库当前实际存在的语言对，逐一核对变更文件是否命中：

| 中文 / 原文 | 对应语言版本 | 说明 |
| --- | --- | --- |
| `README.md`（根目录） | `docs/README_EN.md`（英文）、`docs/README_CHT.md`（繁体中文）、`docs/README_KO.md`（韩文） | README 系列共 4 个语言版本，跨根目录与 `docs/` 存放；任一版本变更都要评估其余三个 |
| `docs/FAQ.md` | `docs/FAQ_EN.md` | |
| `docs/CONTRIBUTING.md` | `docs/CONTRIBUTING_EN.md` | |
| `docs/DEPLOY.md` | `docs/DEPLOY_EN.md` | |
| `docs/LLM_CONFIG_GUIDE.md` | `docs/LLM_CONFIG_GUIDE_EN.md` | |
| `docs/INDEX.md` | `docs/INDEX_EN.md` | |
| `docs/bot-command.md` | `docs/bot-command_EN.md` | |
| `docs/full-guide.md` | `docs/full-guide_EN.md` | |

命名规律：同目录下 `<name>.md`（中文原文）与 `<name>_EN.md`（英文）互为一对；README 系列是唯一跨目录、且有 4 个语言版本的特例。

不在上表中的 `docs/*.md`（例如专题文档、诊断文档、机器人接入文档等）在仓库当前状态下没有对应的其他语言版本，属于**单独文档**，改动后无需在本 skill 下寻找配对文件，但仍需按 `AGENTS.md` 第 1 条判断是否要更新 `docs/CHANGELOG.md`。

如果 `docs/` 或根目录新增/删除了 `.md` 文件，先重新执行 `ls docs/*.md README*.md` 核实实际配对关系，不要直接照抄本表。

### Step 3: 逐对比较变更内容

对每个命中配对表的变更文件：

1. 用 `git diff -- <changed_file>` 查看本次改动的具体内容，摘出改动点（新增说明、修正步骤、字段变化、命令变化等）。
2. 用 `git show HEAD:<counterpart_file>` 或直接 `Read` 打开配对文件，定位对应章节。
3. 判断改动性质：
   - **实质内容变化**（新增/删除步骤、字段、命令、行为说明、链接、配置项等）→ 需要同步。
   - **纯格式/措辞/错别字修正**（不改变语义）→ 可视为低优先级，同步与否需在交付说明中说明判断依据。

### Step 4: 执行同步

- 对判定为"需要同步"的配对，在对应语言文件的相应章节做等价语义修改。
- 不做机器直译：保持该语言文件已有的行文风格、术语习惯和历史措辞，只对应改动的信息点。
- README 系列：确认 4 个语言版本（`README.md`、`docs/README_EN.md`、`docs/README_CHT.md`、`docs/README_KO.md`）中所有需要更新的版本都已处理，而不是只改其中一个。
- 同步完成后，重新执行 `git diff` 自查，确认没有引入与原改动无关的内容。

### Step 5: 未同步时的交付说明

如某配对文件确认不需要同步，或因故未能同步，按 `AGENTS.md` 第 1 条要求生成交付说明用的原因文案，格式：

```text
- <changed_file> 已变更；对应 <counterpart_file> 因 <原因> 未同步。
```

常见原因示例：改动仅涉及该语言特有的表达修正、改动内容与目标语言读者无关、目标语言版本本次改动前已存在等价内容、需要人工确认术语暂缓同步等。禁止使用空泛理由（如"暂不需要"）而不说明具体依据。

将该文案汇总放入最终交付说明的"未同步项"部分，供 PR 描述引用。

## Allowed Auto-Actions (No Confirmation Needed)

- 读取 `git status` / `git diff` / 文件内容
- 在判定需要同步的语言文件中进行文本编辑
- 生成未同步说明文案

## Actions Requiring Confirmation

- 提交（`git commit`）、打 tag、推送等一切改变仓库历史或远端状态的操作
- 对判断存在歧义的同步范围，需先向用户确认再落笔

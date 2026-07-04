# PR Precheck

在提交 PR 前，按变更面分类当前工作树的改动文件，只执行 `AGENTS.md` §6 验证矩阵中对应的检查，并汇报执行结果与未覆盖项。

## Usage

```text
/pr-precheck
/pr-precheck <base-branch>
```

- 不带参数：以当前工作树相对默认 base（`main`）的 diff 为准。
- 带参数：使用 `<base-branch>` 作为对比基线。

## Instructions

规则真源为仓库根目录 `AGENTS.md`，本 skill 只负责执行顺序与命令编排，不复制其规则内容；执行中如与 `AGENTS.md` 描述不一致，以 `AGENTS.md` 和实际脚本行为为准。

### Step 1: 收集变更文件

```bash
git status --short
git diff --name-only <base-branch>...HEAD   # 默认 base 为 main
```

- 两个命令的结果取并集，作为本次改动面分类的输入。
- 若 `git status --short` 显示未跟踪或冲突状态，如实记录，不要执行 `stash`、`reset` 或强制切分支。

### Step 2: 按改动面分类

对 Step 1 收集到的文件路径逐一匹配，允许命中多个改动面：

| 改动面 | 匹配路径 |
| --- | --- |
| backend | `main.py`、`src/**`、`data_provider/**`、`api/**`、`bot/**`、`tests/**` |
| web | `apps/dsa-web/**` |
| desktop | `apps/dsa-desktop/**`、`scripts/*desktop*`、`scripts/build-*` |
| API/Schema 联动 | `api/**`、`src/schemas/**`、`src/services/**`（且同时命中 `apps/dsa-web/**` 或 `apps/dsa-desktop/**`） |
| docs/治理 | `README.md`、`docs/**`、`AGENTS.md`、`.github/copilot-instructions.md`、`.github/instructions/**`、`.claude/skills/**` |
| workflow/脚本/Docker | `.github/**`、`scripts/**`、`docker/**` |

### Step 3: 按改动面执行验证

只执行命中改动面对应的检查，未命中的改动面不必执行：

- **backend**：
  ```bash
  uv run ./scripts/ci_gate.sh
  ```
  时间受限时可分阶段执行，阶段参数为 `syntax`、`flake8`、`deterministic`、`offline-tests`（例如 `uv run ./scripts/ci_gate.sh syntax`），但交付时需说明只跑了部分阶段。

- **web**：
  ```bash
  cd apps/dsa-web && npm ci && npm run lint && npm run build
  ```

- **desktop**：先确认 web 已构建通过，再按平台约束验证桌面端；如受限于本地平台无法完整跑通 Electron 构建或 `scripts/build-desktop*.sh`/`.ps1`，需在结果中明确写出：是否验证了 Web 构建产物、是否验证了 Electron 构建、是否验证了 Release 工作流影响。

- **API/Schema 联动**：至少覆盖上面 backend 对应后端验证，加上受影响客户端（web 和/或 desktop）的构建验证；如涉及登录、Cookie、会话、轮询状态、字段增删或枚举变化，需在结果中单独说明兼容性影响。

- **docs/治理**：不强制跑代码测试；核对 skill/文档中出现的命令、文件名、工作流名称与仓库实际是否一致。若命中 `.claude/skills/**`、`AGENTS.md`、`.github/copilot-instructions.md`、`.github/instructions/**`，执行：
  ```bash
  python scripts/check_ai_assets.py
  ```

- **workflow/脚本/Docker**：运行最接近改动面的本地验证（例如改了 `scripts/ci_gate.sh` 本身就本地跑一遍对应阶段）；若未执行 Docker 或 GitHub Actions 相关验证，需说明原因与潜在风险。

若某改动面已有对应 CI 结果（如 PR 已存在、`gh pr checks` 可查），可直接引用 CI 结论代替本地重跑，并在结果中注明引用来源。

### Step 4: 汇总输出

按 `AGENTS.md` §9 交付结构中的以下三项整理输出：

```markdown
## 验证情况

- <改动面>：<执行的命令> -> <结果，或引用的 CI 结论>

## 未验证项

- <改动面/命令> -> <原因>

## 风险点

- <未覆盖或部分覆盖可能带来的风险>
```

- 每个命中的改动面必须在“验证情况”或“未验证项”中至少出现一次，不得遗漏。
- 不在此步骤执行 `git commit`、`git push`、`git tag`、`gh pr create` 等改变远端或分支状态的操作；如需创建或更新 PR，转交用户确认后再执行。

## Allowed Auto-Actions (No Confirmation Needed)

- 读取 `git status`、`git diff` 等只读信息
- 执行本地非破坏性验证命令（`ci_gate.sh`、`npm run lint`/`build`、`check_ai_assets.py` 等）
- 引用已有 CI 结论

## Actions Requiring Confirmation

- `git commit` / `git push` / `git tag`
- 创建或更新 PR
- 任何会改变远端或当前分支状态的操作

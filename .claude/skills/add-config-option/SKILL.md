# Add Config Option

新增一个环境变量 / 配置项，并保持 `src/config.py`、`.env.example`、文档、契约测试之间的一致性。

## Usage

```text
/add-config-option <ENV_VAR_NAME>
```

## Instructions

执行本 skill 前，先确认本次改动遵循仓库根目录 `AGENTS.md`（唯一规则真源）；特别是 §1 硬规则中「新增配置项时必须同步更新 `.env.example` 和相关文档」，以及 §7 稳定性护栏「配置与运行入口」「新配置优先做到不配置也可运行、配置后增强能力」的要求。本 skill 只提供执行步骤，不复述 `AGENTS.md` 中的规则内容。

### Step 1: 定位相似的现有配置

在 `src/config.py` 中找一个语义相近的现有配置项作为参照，确认注册方式：

- `Config` 是一个 `@dataclass`（定义于 `src/config.py:660`），每个配置项是该 dataclass 上的一个字段，例如：

  ```python
  tickflow_api_key: Optional[str] = None
  ```

- 字段的默认值来自读取环境变量的类方法 `Config._load_from_env()`（`src/config.py:1138`），该方法在 `Config(...)` 构造调用里按字段名传参，例如：

  ```python
  tickflow_api_key=os.getenv('TICKFLOW_API_KEY'),
  ```

- 对于需要「优先读取已持久化 `.env`、但允许进程环境变量显式覆盖」的运行期可写配置（例如 WebUI 设置页可改的项），使用 `cls._resolve_env_value(KEY, default=..., prefer_env_file=True)`（定义于 `src/config.py:2300`），而不是直接 `os.getenv`。
- 找一个与新配置**用途相近**的字段（同属数据源 Token / LLM 渠道 / 通知 / 调度等分组），复用同一读取方式和默认值风格，不要发明新的注册模式。

### Step 2: 在 `config.py` 中新增字段

- 在 `Config` dataclass 中对应分组注释下（如 `# === 数据源 API Token ===`）新增字段，给出合理默认值（未设置时不应报错、不应导致功能崩溃）。
- 在 `_load_from_env()` 的 `Config(...)` 构造参数中，按 Step 1 确认的方式（`os.getenv(...)` 或 `cls._resolve_env_value(...)`）读取该环境变量并赋值给新字段。
- 确认「不配置也能运行，配置后增强能力」：新配置缺省时，功能应保持当前行为（如某数据源不启用、某增强分析跳过），而不是抛异常或使主流程失败，除非该配置项本身语义就是显式的强约束（需在文档中说明）。
- 如涉及配置校验，检查是否需要在 `Config.validate_structured()`（`src/config.py:2575`）中补充告警/错误项（沿用现有 `ConfigIssue` 的 `severity` 分级：`error` / `warning` / `info`）。

### Step 3: 更新 `.env.example`

- 在 `.env.example` 中找到语义相近的分组区块，按现有格式追加新变量：
  - 注释先说明用途、是否可选、获取方式（如有官方链接）；
  - 默认注释掉的可选变量使用 `# VAR_NAME=` 形式（参考 `.env.example` 第 26 行 `# TICKFLOW_API_KEY=`）；
  - 必填或已有安全默认值的变量可直接写 `VAR_NAME=` 或 `VAR_NAME=<default>`。
- 检查仓库是否存在 `.env.example.ko`（当前存在，与 `.env.example` 保持相同 key 集合）。若存在双语模板，需要同步在 `.env.example.ko` 中追加对应中文/韩文注释条目，保持 key 完全一致（`tests/test_env_example_ko.py` 会校验两者 key 集合相同）。

### Step 4: 更新相关文档

- 判断该配置项属于哪个专题文档（如 `docs/llm-providers.md`、`docs/notifications.md`、`docs/market-support.md` 等），在对应 `docs/*.md` 中补充字段语义、默认值、影响范围、边界条件。
- 若该专题文档存在中英文双份（如 `docs/full-guide.md` / `docs/full-guide_EN.md`），评估是否需要同步更新另一份；若本次未同步，在交付说明中写明原因。
- 不要为此类配置细节改动 `README.md`（`README.md` 只承载首页级信息）。
- 在 `docs/CHANGELOG.md` 的 `[Unreleased]` 段追加一行扁平格式条目，例如：

  ```text
  - [新功能] 新增 `<ENV_VAR_NAME>` 配置项，用于 <一句话说明用途与默认行为>。
  ```

  类型从 `新功能`/`改进`/`修复`/`文档`/`测试`/`chore` 中按实际改动选择；不要在 `[Unreleased]` 内新增 `### 类目标题`。

### Step 5: 运行契约测试

- 使用仓库中实际存在的契约测试验证：

  ```bash
  uv run pytest tests/test_config_env_compat.py tests/test_env_example_ko.py
  ```

  - `tests/test_config_env_compat.py` 覆盖 `Config._load_from_env()` 对新增/兼容环境变量的读取行为与默认值。
  - `tests/test_env_example_ko.py` 校验 `.env.example` 与 `.env.example.ko` 的 key 集合一致，以及韩文模板保留必要的使用说明头部。
- 如果新配置项有明确的加载语义（默认值、优先级、多值解析等），在 `tests/test_config_env_compat.py` 中补充对应的最小回归用例，参照文件内现有用例的写法（`patch.dict(os.environ, {...}, clear=True)` + 断言 `Config._load_from_env()` 结果）。
- 如涉及 `scripts/check_env.py` 展示的配置项分类（该脚本用于本地校验 `.env` 加载、数据源、LLM、通知），评估是否需要在其输出中补充新配置的展示，非必需但有助于本地排障。

### Step 6: 影响面检查清单

对照 `AGENTS.md` §7「配置与运行入口」护栏，逐项确认新配置在以下场景中的传递与消费方式：

- 本地运行：`uv run python main.py` 是否能在未设置该变量时正常跑通（缺省行为符合预期）。
- Docker：如该变量需要在容器中生效，确认 `docker/` 下 compose / Dockerfile 是否需要透传该变量（多数情况下 `.env` 挂载即可，无需改动镜像）。
- GitHub Actions：如该变量会被 `.github/workflows/` 中的每日任务或 CI 使用，确认是否需要在对应 workflow 中新增/传递 secret 或 env（若无关则明确说明不涉及）。
- API：如该配置会影响 `api/` 暴露的行为或响应字段，确认是否需要新增/调整对应端点或 Schema（`src/schemas/`）。
- Web：如 `apps/dsa-web/` 的设置页需要读取或写入该配置，确认前端表单与后端 API 的字段契约是否已经或需要同步更新。
- Desktop：如 `apps/dsa-desktop/` 依赖该配置（如通过内嵌后端读取 `.env`），确认桌面端启动链路是否受影响。

若某一项确认不涉及，直接在交付说明中写明「不涉及」及原因，不要跳过该检查项。

## Allowed Auto-Actions (No Confirmation Needed)

- 阅读 `src/config.py`、`.env.example`、`.env.example.ko`、相关 `docs/*.md`、`tests/test_config_env_compat.py`、`tests/test_env_example_ko.py`
- 在 `src/config.py`、`.env.example`、`.env.example.ko`、对应 `docs/*.md`、`docs/CHANGELOG.md` 中新增与本次配置项直接相关的最小改动
- 运行 `uv run pytest tests/test_config_env_compat.py tests/test_env_example_ko.py`
- 运行 `uv run python -m py_compile src/config.py`

## Actions Requiring Confirmation

1. `git commit` / `git push` / 建分支
2. 修改 `.github/workflows/` 中的 secret 或 env 传递
3. 修改 `docker/` 下的 Dockerfile / compose 变量透传
4. 修改 `apps/dsa-web/` 或 `apps/dsa-desktop/` 中与该配置相关的表单、API 调用逻辑

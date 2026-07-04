# New Notification Channel

按照现有 13 个静态通知渠道的一致模式，新增一个通知渠道（sender），并把它接入检测、路由、降噪、诊断、Web/API、配置和文档全部实际注册点。

## Usage

```text
/new-notification-channel <channel-name>
```

`<channel-name>` 使用小写短名（例如 `bark`、`lark_bot`），需要与后续所有注册点保持一致的 `channel value` 字符串。

## Instructions

规则真源是仓库根目录 `AGENTS.md`（尤其是 §1 硬规则、§7 稳定性护栏中关于报告 / Prompt / 通知的部分）；本文件只描述执行步骤，不复制其规则内容。分析和实现时使用简洁中文。

### Step 0：确认现状基线

先读取以下文件，确认当前 13 个渠道枚举与实际注册点（不要凭记忆假设）：

```bash
rg -n "class NotificationChannel" -A 20 src/notification.py
rg -n "ROUTABLE_NOTIFICATION_CHANNELS" src/notification_routing.py
rg -n "CHANNEL_PROFILES" src/notification_capabilities.py
```

当前渠道值集合（`src/notification.py` 的 `NotificationChannel` 枚举）：`wechat`、`feishu`、`telegram`、`email`、`pushover`、`ntfy`、`gotify`、`pushplus`、`serverchan3`、`custom`、`discord`、`slack`、`astrbot`。新渠道的 value 字符串不能与这些冲突。

### Step 1：选定参考实现

在 `src/notification_sender/` 中选择一个最相似的现有 sender 作为参考：

- 纯 Webhook / Bearer token（无图片）：参考 `astrbot_sender.py` 或 `pushplus_sender.py`。
- Webhook + Bot API 双模式（同时支持图片上传）：参考 `slack_sender.py`。
- Bot Token + Chat/Channel ID，支持长文本分片与图片：参考 `telegram_sender.py`。

阅读参考实现的类结构（构造函数接收 `config: Config`、`_is_xxx_configured()` 私有方法、`send_to_xxx()` 公开方法签名、超时参数 `timeout_seconds`、异常捕获与日志），确保新 sender 遵循同一形状，便于 `NotificationService` 多重继承。

### Step 2：编写 `src/notification_sender/<name>_sender.py`

新建 `<Name>Sender` 类，至少包含：

- `__init__(self, config: Config)`：从 `config` 读取该渠道专属属性（`getattr(config, 'xxx', None)`），不要直接读环境变量。
- `_is_<name>_configured(self) -> bool`：与 `detect_configured_channels()`（Step 3）判断逻辑保持等价。
- `send_to_<name>(self, content: str, *, title: Optional[str] = None, timeout_seconds: Optional[float] = None) -> bool`：公开发送入口，返回是否发送成功；网络异常必须捕获并记录日志后返回 `False`，不得向上抛出（保证单渠道失败不拖垮 `_send_notifications`/`send_with_results` 主流程）。
- 如需支持图片投递，参考 `slack_sender.py` 的 `_send_slack_image` 或 `telegram_sender.py` 的 `_send_telegram_photo` 命名模式：`_send_<name>_image`。
- 使用 `requests` 时显式传入 `timeout=timeout_seconds or <默认秒数>`，避免请求无限阻塞。

### Step 3：接入实际注册点

新渠道必须同时出现在以下文件（均为已确认的真实注册点，不是推测）：

1. `src/notification_sender/__init__.py`：新增 `from .<name>_sender import <Name>Sender` 导出。
2. `src/notification.py`：
   - `NotificationChannel` 枚举（约第 101 行 `class NotificationChannel(Enum)`）新增 `<NAME> = "<name>"` 成员。
   - `ChannelDetector.get_channel_name()`（约第 150 行）新增中文渠道名映射。
   - `NotificationService` 类的多重继承列表（约第 171 行 `class NotificationService(...)`）与 `__init__` 中的 `XxxSender.__init__(self, config)` 调用列表（约第 230 行起）都要加入 `<Name>Sender`。
   - `detect_configured_channels()`（静态方法，约第 390 行）新增该渠道的最小配置判断分支，追加 `NotificationChannel.<NAME>`。
   - `_send_to_static_channel()`（约第 2128 行）新增 `if channel == NotificationChannel.<NAME>: return self.send_to_<name>(content)` 分支（如支持图片，先判断 `use_image`）。
3. `src/notification_routing.py`：`ROUTABLE_NOTIFICATION_CHANNELS` 元组新增 `"<name>"`，用于 `NOTIFICATION_REPORT_CHANNELS` / `NOTIFICATION_ALERT_CHANNELS` / `NOTIFICATION_SYSTEM_ERROR_CHANNELS` 路由过滤。
4. `src/notification_capabilities.py`：`CHANNEL_PROFILES` 新增该渠道的 `ChannelProfile`（`markdown` 格式、`default_mode`、长度限制、`supports_image`/`supports_card`/`supports_file`）。是否需要同步在 `CHANNEL_RENDERER_PRESETS` 中加预留 preset，视是否已有专属 renderer 计划而定，非强制。
5. `src/services/notification_diagnostics.py`：如新渠道是成对 key（例如 token + channel id），使用 `_require_pair(...)` 补充校验分支，参考现有 Slack Bot 分支（约第 405 行）；如是单一 minimal key，参考 `pushplus`/`gotify` 等已有的 `_has(...)` 判断模式。
6. `src/services/system_config_service.py`：
   - `_NOTIFICATION_TEST_CHANNELS` 元组（约第 130 行）新增 `"<name>"`。
   - `_NOTIFICATION_TEST_KEY_MAP`（约第 145 行）新增该渠道每个 env key 到 `(config_attr, type)` 的映射。
   - `_NOTIFICATION_REQUIRED_KEY_GROUPS`（约第 189 行）新增该渠道的必填 key 组合（支持多组 OR 关系，参考 `discord`/`slack` 的双模式写法）。
   - `_NOTIFICATION_TEST_TARGET_KEYS`（约第 204 行）新增测试时用于展示 target 的 key。
   - `dispatch` 字典（约第 2247 行，在 `test_notification_channel` 方法内）新增 `"<name>": lambda: <Name>Sender(config).send_to_<name>(titled_content, timeout_seconds=timeout_seconds)`。
7. `api/v1/schemas/system_config.py`：`NotificationTestChannel` Literal（约第 11 行）新增 `"<name>"`。
8. `apps/dsa-web/src/types/systemConfig.ts`：对应的前端渠道联合类型新增 `'<name>'`（约第 224 行附近）。
9. `apps/dsa-web/src/components/settings/NotificationTestPanel.tsx`：渠道下拉选项数组新增 `{ value: '<name>', label: '<展示名>' }`（约第 34 行附近）。

以上 9 个文件、约 15 处具体位置缺一处都会导致新渠道在检测、路由、诊断、Web 测试或前端展示某一环节不一致，务必逐条核对，不要只改运行时 `send` 路径。

### Step 4：新增配置项

- `src/config.py`：在 `Config` dataclass 中新增该渠道字段（参考 `slack_webhook_url`/`slack_bot_token`/`slack_channel_id` 三个字段的写法），并在 `from_env()`（约第 1755 行附近的 `os.getenv(...)` 组）中新增对应读取。
- 同步更新 `.env.example`：新增该渠道的注释掉的示例 key（参考约第 604 行 `SLACK_*` 区块的写法），并在文件顶部或渠道区块附近补充一行简短说明。
- 确认"不配置也可运行，配置后增强能力"：新渠道 minimal key 全部为空时，`detect_configured_channels()` 不应把该渠道加入 `available_channels`，也不能让 `NotificationService.__init__` 抛异常。

### Step 5：更新文档

- `docs/notifications.md`：
  - "渠道基线"表格新增一行（渠道名、类型、Minimal key、Advanced key、说明）。
  - "通知路由策略"章节的渠道枚举逗号列表补充新渠道 value。
  - 如涉及 GitHub Actions 默认 workflow 映射，运行 `python scripts/generate_notification_actions_env_table.py` 重新生成 "GitHub Actions 映射" 表格（该表由脚本从 `.github/workflows/00-daily-analysis.yml` 的 `env:` 和诊断元数据生成，不要手工编辑表格内容）。
  - 若新渠道需要在默认 workflow 中生效，同步在 `.github/workflows/00-daily-analysis.yml` 的 `env:` 中新增对应 Secret/Variable 引用。
- 评估是否存在对应英文文档需要同步；若未同步，在交付说明中写明原因。
- `docs/CHANGELOG.md` 的 `[Unreleased]` 段追加一行，使用扁平格式：`- [新功能] 新增 <渠道名> 通知渠道支持`，禁止新增 `### 类目标题`。

### Step 6：测试

在 `tests/test_notification_sender.py` 中按现有 `TestSlackSender`（约第 1196 行）等测试类的模式新增 `Test<Name>Sender`，至少覆盖：

- `_is_<name>_configured()` 在配置完整 / 部分 / 缺失三种情况下的返回值。
- `send_to_<name>()` 成功路径（mock `requests.post` 返回 200/预期成功 body）。
- `send_to_<name>()` 失败路径（非 200、超时、连接异常），确认返回 `False` 而不是抛异常。

如涉及 `detect_configured_channels()`、路由、降噪或诊断的分支变化，检查 `tests/test_notification.py`、`tests/test_notification_routing.py`、`tests/test_notification_capabilities.py`（如存在同名测试文件，优先复用；不确定时先 `ls tests/ | rg notification` 核实实际文件名）是否需要补充对应用例。

运行验证：

```bash
uv run pytest -m "not network" tests/test_notification_sender.py
uv run pytest -m "not network" tests/test_notification.py
uv run ./scripts/ci_gate.sh
```

如涉及 `apps/dsa-web/` 改动：

```bash
cd apps/dsa-web && npm ci && npm run lint && npm run build
```

### Step 7：稳定性护栏自查

在交付前逐项确认（对应 `AGENTS.md` §7 通知护栏要求）：

- 单一渠道发送异常已在 sender 内部捕获并转换为 `False` 返回值，不会在 `_send_to_static_channel()` 或 `send_with_results()` 中向上抛出，不会中断其他渠道的发送循环。
- 新渠道未配置时，`NotificationService.__init__` 与 `is_available()` 行为与其他 12 个渠道一致（不产生新增的强制依赖或启动失败）。
- 新渠道加入 `ROUTABLE_NOTIFICATION_CHANNELS` 后，`NOTIFICATION_REPORT_CHANNELS`/`NOTIFICATION_ALERT_CHANNELS`/`NOTIFICATION_SYSTEM_ERROR_CHANNELS` 留空时的默认行为（发送到所有已配置渠道）不受影响。
- Web 一键测试只使用页面草稿值合成临时配置，不会因新增渠道而意外写入或修改 `.env`。
- 若新渠道支持图片投递，确认接入 `MARKDOWN_TO_IMAGE_CHANNELS` 判断分支时不会影响 `ntfy`/`gotify` 等已排除渠道的既有排除逻辑（参考 `src/notification.py` 中 `channels_needing_image` 的排除集合写法）。

## Output

完成后，在交付说明中列出：改了哪些文件、新增的渠道 value 与 env key、执行的验证命令与结果、未覆盖的验证项、以及回滚方式（通常是移除该渠道在上述 9 个文件中的新增分支并回退 `.env.example`/`docs/CHANGELOG.md`）。

# 本地开发指南 (uv)

本项目推荐使用 [uv](https://docs.astral.sh/uv/) 进行本地开发，以获得快速的依赖安装和可复现的环境。

## 前置条件

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)（安装方式：`curl -LsSf https://astral.sh/uv/install.sh | sh` 或 `brew install uv`）

## 快速开始

```bash
# 克隆仓库
git clone https://github.com/ZhuLinsen/daily_stock_analysis.git
cd daily_stock_analysis

# 同步依赖（自动创建 .venv 虚拟环境）
uv sync

# 运行分析
uv run python main.py
```

## 安装依赖

```bash
# 同步所有依赖（含 dev 依赖如 flake8/pytest）
uv sync

# 仅同步生产依赖
uv sync --no-dev
```

`uv sync` 会自动创建 `.venv` 虚拟环境，安装所有依赖，并锁定版本到 `uv.lock`。

## 运行应用

```bash
# 使用 uv run 执行所有 Python 命令
uv run python main.py
uv run python main.py --debug
uv run python main.py --stocks 600519,hk00700,AAPL
uv run python main.py --market-review
uv run python main.py --schedule
uv run uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

也可以手动激活虚拟环境后直接运行：

```bash
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows
python main.py
```

## 运行测试与 Lint

```bash
# CI gate 完整检查
uv run ./scripts/ci_gate.sh

# 仅语法检查
uv run ./scripts/ci_gate.sh syntax

# 仅 flake8
uv run ./scripts/ci_gate.sh flake8

# 离线测试（跳过网络依赖测试）
uv run pytest -m "not network"
```

## 添加新依赖

```bash
# 添加运行时依赖
uv add <package-name>

# 添加开发依赖
uv add --dev <package-name>
```

依赖变更后，需要重新导出 `requirements.txt` 以保证 CI/Docker 兼容：

```bash
# 导出生产依赖
uv export --no-dev -o requirements.txt

# 导出 CI 依赖（含 dev）
uv export -o .github/requirements-ci.txt
```

## pip 回退

如果未安装 uv，仍可使用传统 pip 方式：

```bash
# 创建虚拟环境
python -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
pip install flake8 pytest

# 运行应用
python main.py
```

## 目录说明

| 文件 | 说明 |
|------|------|
| `pyproject.toml` | 项目元数据与依赖单源定义 |
| `uv.lock` | 锁定所有依赖的确切版本（提交到仓库） |
| `.python-version` | 指定 Python 版本（3.11） |
| `requirements.txt` | uv export 生成的生产依赖（CI/Docker 使用） |
| `.github/requirements-ci.txt` | uv export 生成的 CI 依赖 |
| `.venv/` | 虚拟环境目录（已在 .gitignore 中忽略） |

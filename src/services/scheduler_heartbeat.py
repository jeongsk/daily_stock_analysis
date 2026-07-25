# -*- coding: utf-8 -*-
"""调度循环存活标记（liveness heartbeat）。

背景（2026-07-24 事故）：调度线程阻塞在数据源 socket 的死循环里，整整 14.5 小时
没有任何输出、没有产出当日报告，但容器健康检查一直显示 healthy —— 因为它最终会落到
``python -c "import sys; sys.exit(0)"`` 这个永远成功的兜底上，而且它探测的 HTTP 服务
活在另一个线程里，本来就反映不了调度线程的死活。

这里给调度循环加一个**进程本地**的时间戳文件，容器健康检查据此判断调度循环是否卡死。

约定：

- 时间戳文件写在临时目录（容器本地），**不能**写在 ``data/`` 这类跨容器共享的
  bind mount 上：否则 analyzer 卡死会把只跑 Web/API 的 server 容器一起判成不健康，
  把一个正常服务拖进重启循环。
- 文件不存在 = 该容器没有调度循环（例如 ``--serve-only``），健康检查直接通过。
- 调度循环执行分析任务时是**同步阻塞**的，正常一轮分析也会让心跳停顿数分钟，
  所以阈值必须显著大于一次正常分析的耗时（默认 1 小时）。

用作容器健康检查：``python -m src.services.scheduler_heartbeat``
（退出码 0 = 健康或本容器无调度循环，1 = 调度循环已停摆）。
"""

import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

HEARTBEAT_FILENAME = "dsa-scheduler-heartbeat"

#: 默认阈值：远大于一次正常分析的耗时，同时把「永久卡死」的暴露时间从「无限」压到 1 小时。
DEFAULT_MAX_AGE_SECONDS = 3600.0

MAX_AGE_ENV = "SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS"


def heartbeat_path() -> Path:
    """返回心跳文件路径（容器/进程本地的临时目录）。"""
    return Path(tempfile.gettempdir()) / HEARTBEAT_FILENAME


def touch_heartbeat() -> bool:
    """刷新心跳时间戳。

    尽力而为：任何写入失败都只记 debug 日志，绝不能让存活标记反过来打断调度循环。

    Returns:
        是否成功写入。
    """
    path = heartbeat_path()
    try:
        path.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        return True
    except Exception as exc:
        logger.debug(f"调度心跳写入失败（忽略）: {path}: {exc}")
        return False


def heartbeat_age_seconds() -> Optional[float]:
    """返回心跳距今的秒数；文件不存在或不可读时返回 None。

    以 mtime 为准而非文件内容，避免读到写了一半的内容时误判。
    """
    path = heartbeat_path()
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug(f"调度心跳读取失败（按未知处理）: {path}: {exc}")
        return None


def resolve_max_age_seconds() -> float:
    """解析阈值配置；非法值回退到默认值。"""
    raw = os.getenv(MAX_AGE_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_AGE_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_SECONDS
    if value <= 0:
        return DEFAULT_MAX_AGE_SECONDS
    return value


def check_liveness(max_age_seconds: Optional[float] = None) -> Tuple[bool, str]:
    """判断本容器的调度循环是否仍在心跳。

    Args:
        max_age_seconds: 允许的最大心跳停顿；省略时读取环境变量或使用默认值。

    Returns:
        ``(是否健康, 说明)``。没有心跳文件时判为健康——代表本容器不跑调度循环。
    """
    threshold = resolve_max_age_seconds() if max_age_seconds is None else max_age_seconds
    age = heartbeat_age_seconds()

    if age is None:
        return True, "no scheduler heartbeat in this container; liveness check skipped"

    if age > threshold:
        return False, (
            f"scheduler loop stalled: last tick {age:.0f}s ago "
            f"(threshold {threshold:.0f}s, {MAX_AGE_ENV})"
        )

    return True, f"scheduler loop alive: last tick {age:.0f}s ago"


def main() -> int:
    healthy, reason = check_liveness()
    print(reason)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())

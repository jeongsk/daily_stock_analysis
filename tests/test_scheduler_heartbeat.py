# -*- coding: utf-8 -*-
"""Regression tests for the scheduler liveness heartbeat.

2026-07-24 事故：调度线程阻塞在数据源死循环里 14.5 小时，容器健康检查却始终 healthy。
这些测试锁定两件事：调度循环确实在打心跳，以及健康检查在「停摆」和「本容器没有调度
循环」这两种情况下给出不同的结论。
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services import scheduler_heartbeat  # noqa: E402
from src.services.scheduler_heartbeat import (  # noqa: E402
    DEFAULT_MAX_AGE_SECONDS,
    MAX_AGE_ENV,
    check_liveness,
    heartbeat_age_seconds,
    heartbeat_path,
    resolve_max_age_seconds,
    touch_heartbeat,
)


class HeartbeatPathTestCase(unittest.TestCase):
    def test_heartbeat_lives_in_container_local_tempdir(self):
        """必须是容器本地路径。

        写到 data/ 这类跨容器共享的 bind mount 上，analyzer 卡死会把只跑 Web/API 的
        server 容器一并判成不健康，把正常服务拖进重启循环——这是本方案最危险的失误模式。
        """
        path = heartbeat_path()

        self.assertEqual(path.parent, Path(tempfile.gettempdir()))
        self.assertNotIn("/app/data", str(path))


class HeartbeatLivenessTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "heartbeat"
        patcher = patch.object(scheduler_heartbeat, "heartbeat_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_heartbeat_is_treated_as_no_scheduler(self):
        self.assertIsNone(heartbeat_age_seconds())

        healthy, reason = check_liveness()

        self.assertTrue(healthy)
        self.assertIn("no scheduler heartbeat", reason)

    def test_fresh_heartbeat_is_healthy(self):
        self.assertTrue(touch_heartbeat())

        healthy, reason = check_liveness(max_age_seconds=60)

        self.assertTrue(healthy)
        self.assertIn("alive", reason)

    def test_stalled_heartbeat_is_unhealthy(self):
        touch_heartbeat()
        stale = time.time() - 7200
        os.utime(self.path, (stale, stale))

        healthy, reason = check_liveness(max_age_seconds=3600)

        self.assertFalse(healthy)
        self.assertIn("stalled", reason)

    def test_main_exit_code_reflects_liveness(self):
        touch_heartbeat()
        self.assertEqual(scheduler_heartbeat.main(), 0)

        stale = time.time() - 7200
        os.utime(self.path, (stale, stale))
        self.assertEqual(scheduler_heartbeat.main(), 1)

    def test_touch_heartbeat_never_raises_on_write_failure(self):
        with patch.object(Path, "write_text", side_effect=OSError("read-only fs")):
            self.assertFalse(touch_heartbeat())


class HeartbeatThresholdTestCase(unittest.TestCase):
    def test_threshold_defaults_when_unset_or_invalid(self):
        for raw in (None, "", "   ", "not-a-number", "0", "-5"):
            with self.subTest(raw=raw):
                env = {} if raw is None else {MAX_AGE_ENV: raw}
                with patch.dict(os.environ, env, clear=False):
                    if raw is None:
                        os.environ.pop(MAX_AGE_ENV, None)
                    self.assertEqual(resolve_max_age_seconds(), DEFAULT_MAX_AGE_SECONDS)

    def test_threshold_reads_env_override(self):
        with patch.dict(os.environ, {MAX_AGE_ENV: "120"}, clear=False):
            self.assertEqual(resolve_max_age_seconds(), 120.0)


class SchedulerLoopHeartbeatTestCase(unittest.TestCase):
    def test_scheduler_loop_touches_heartbeat_each_tick(self):
        from src.scheduler import Scheduler

        scheduler = Scheduler(schedule_time="18:00", register_signals=False)
        scheduler.schedule = MagicMock()
        scheduler._refresh_daily_schedule_if_needed = MagicMock()
        scheduler._run_background_tasks = MagicMock()
        scheduler._get_next_run_time = MagicMock(return_value="2026-07-26 18:00:00")

        ticks = {"count": 0}

        def _stop_after_two_ticks(_seconds):
            ticks["count"] += 1
            if ticks["count"] >= 2:
                scheduler._running = False

        with patch("src.scheduler.touch_heartbeat") as touch, \
             patch("src.scheduler.time.sleep", side_effect=_stop_after_two_ticks):
            scheduler.run()

        # 启动时 1 次 + 每轮循环各 1 次。
        self.assertEqual(touch.call_count, 3)


if __name__ == "__main__":
    unittest.main()

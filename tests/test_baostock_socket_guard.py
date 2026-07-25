# -*- coding: utf-8 -*-
"""
Regression tests for the Baostock socket EOF guard.

背景（2026-07-24 事故）：baostock 的 socketutil.send_msg 以「收到结束分隔符」为唯一
循环出口，对端断开时 recv() 永远返回 b""，于是退化成 100% CPU 死循环，把调度线程
整整 wedge 了 14 小时。这里直接驱动 baostock 真实的 send_msg 循环来锁定修复，
而不是 mock 掉真正出问题的那一层。
"""

import os
import sys
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.baostock_fetcher import (  # noqa: E402
    BaostockFetcher,
    _EofRaisingSocket,
    _SOCKET_GUARD_FLAG,
    _SOCKET_RECV_TIMEOUT_SECONDS,
    _install_baostock_socket_guard,
)


class _EofSocket:
    """已被对端关闭的 socket：recv() 不抛异常，永远返回 b""。"""

    #: 无保护时 send_msg 会无限空转；设上限让测试失败而不是挂死。
    MAX_RECV_CALLS = 5000

    def __init__(self):
        self.recv_calls = 0
        self.sent = []
        self.timeout = None
        self.closed = False

    def send(self, payload):
        self.sent.append(payload)
        return len(payload)

    def recv(self, _bufsize):
        self.recv_calls += 1
        if self.recv_calls > self.MAX_RECV_CALLS:
            raise AssertionError("send_msg 在 EOF 上空转")
        return b""

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


@contextmanager
def _temporary_default_socket(context_module, sock):
    had_previous = hasattr(context_module, "default_socket")
    previous = getattr(context_module, "default_socket", None)
    setattr(context_module, "default_socket", sock)
    try:
        yield
    finally:
        if had_previous:
            setattr(context_module, "default_socket", previous)
        else:
            delattr(context_module, "default_socket")


def _call_with_deadline(func, timeout=10.0):
    """在独立线程里跑 func，超时即判定为「又挂住了」。"""
    box = {}

    def _runner():
        box["result"] = func()

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError(f"调用在 {timeout}s 内未返回，疑似死循环")
    return box.get("result")


class TestBaostockSocketGuard(unittest.TestCase):
    def test_real_send_msg_terminates_on_peer_eof(self):
        """核心回归：用 baostock 真实的 send_msg 循环验证 EOF 不再空转。"""
        from baostock.common import context
        from baostock.util import socketutil

        raw = _EofSocket()
        with _temporary_default_socket(context, _EofRaisingSocket(raw)):
            result = _call_with_deadline(lambda: socketutil.send_msg("login"))

        # send_msg 内部 `except Exception` 捕获后返回 None —— 这正是 login()
        # 判定 BSERR_RECVSOCK_FAIL 所依赖的信号。
        self.assertIsNone(result)
        # 判别性断言：有保护时 recv 只会被调用一次；没有保护则会调到上限。
        self.assertEqual(raw.recv_calls, 1)

    def test_real_send_msg_spins_without_guard(self):
        """反证：不加保护时同一条路径确实会空转（锁定测试的判别力）。"""
        from baostock.common import context
        from baostock.util import socketutil

        raw = _EofSocket()
        with _temporary_default_socket(context, raw):
            _call_with_deadline(lambda: socketutil.send_msg("login"))

        self.assertGreater(raw.recv_calls, 1)

    def test_eof_raising_socket_raises_on_empty_read(self):
        raw = _EofSocket()
        guarded = _EofRaisingSocket(raw)

        with self.assertRaises(ConnectionError):
            guarded.recv(8192)

    def test_eof_raising_socket_passes_through_payload_and_attributes(self):
        raw = MagicMock()
        raw.recv.return_value = b"payload"
        raw.fileno.return_value = 42
        guarded = _EofRaisingSocket(raw)

        self.assertEqual(guarded.recv(8192), b"payload")
        # 未覆写的属性必须透传，避免改变 baostock 对连接对象的其他用法。
        self.assertEqual(guarded.fileno(), 42)
        guarded.close()
        raw.close.assert_called_once()

    def test_install_guard_wraps_socket_sets_timeout_and_is_idempotent(self):
        fake_context = SimpleNamespace()
        raw = _EofSocket()

        class _FakeSocketUtil:
            connect_calls = 0

            def connect(self):
                type(self).connect_calls += 1
                fake_context.default_socket = raw

        fake_socketutil = SimpleNamespace(SocketUtil=_FakeSocketUtil)

        self.assertTrue(_install_baostock_socket_guard(fake_socketutil, fake_context))
        self.assertTrue(_install_baostock_socket_guard(fake_socketutil, fake_context))
        self.assertTrue(getattr(fake_socketutil, _SOCKET_GUARD_FLAG))

        _FakeSocketUtil().connect()

        self.assertEqual(_FakeSocketUtil.connect_calls, 1)
        guarded = fake_context.default_socket
        self.assertIsInstance(guarded, _EofRaisingSocket)
        # 重复安装不得叠加包装层。
        self.assertIs(guarded._sock, raw)
        self.assertEqual(raw.timeout, _SOCKET_RECV_TIMEOUT_SECONDS)

    def test_install_guard_rejects_partial_injection(self):
        with self.assertRaises(ValueError):
            _install_baostock_socket_guard(SimpleNamespace(), None)

    def test_get_stock_name_returns_none_when_login_fails(self):
        """终点验证：登录失败沿链收敛成 None，既不抛穿也不挂住。"""
        fetcher = BaostockFetcher()
        bs = MagicMock()
        bs.login.return_value = SimpleNamespace(error_code="10001", error_msg="网络接收错误。")
        bs.logout.return_value = SimpleNamespace(error_code="0", error_msg="")

        with patch.object(fetcher, "_get_baostock", return_value=bs):
            name = _call_with_deadline(lambda: fetcher.get_stock_name("600519"))

        self.assertIsNone(name)
        bs.query_stock_basic.assert_not_called()
        bs.logout.assert_called_once()


if __name__ == "__main__":
    unittest.main()

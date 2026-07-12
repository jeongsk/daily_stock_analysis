# -*- coding: utf-8 -*-
"""Tests for KR wiring in scripts/refresh_stock_index.py."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import refresh_stock_index as rsi  # noqa: E402


def test_kr_fetch_is_invoked_on_skip_fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(rsi, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(rsi, "_sync_static_index", lambda: None)
    assert rsi.main(["--skip-fetch"]) == 0
    joined = [" ".join(c) for c in calls]
    assert any("fetch_kr_stock_list.py" in c for c in joined)
    assert any("generate_index_from_csv.py" in c for c in joined)


def test_skip_kr_omits_kr_fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(rsi, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(rsi, "_sync_static_index", lambda: None)
    assert rsi.main(["--skip-fetch", "--skip-kr"]) == 0
    joined = [" ".join(c) for c in calls]
    assert not any("fetch_kr_stock_list.py" in c for c in joined)


def test_kr_fetch_failure_is_fail_open(monkeypatch):
    def fake_run(cmd):
        if "fetch_kr_stock_list.py" in " ".join(cmd):
            raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(rsi, "_run", fake_run)
    monkeypatch.setattr(rsi, "_sync_static_index", lambda: None)
    # KR failure must NOT abort the overall refresh.
    assert rsi.main(["--skip-fetch"]) == 0

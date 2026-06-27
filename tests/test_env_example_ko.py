# -*- coding: utf-8 -*-
"""Regression checks for the Korean environment example."""

import re
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT_DIR / ".env.example"
ENV_EXAMPLE_KO = ROOT_DIR / ".env.example.ko"
ENV_KEY_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")


def _env_keys(path: Path) -> set[str]:
    return {
        match.group(1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := ENV_KEY_RE.match(line))
    }


def test_korean_env_example_keeps_same_config_keys() -> None:
    assert ENV_EXAMPLE_KO.exists()
    assert _env_keys(ENV_EXAMPLE_KO) == _env_keys(ENV_EXAMPLE)


def test_korean_env_example_has_korean_usage_header() -> None:
    content = ENV_EXAMPLE_KO.read_text(encoding="utf-8")

    assert "환경변수 설정 템플릿" in content
    assert "이 파일을 .env로 복사한 뒤 실제 설정값을 입력하세요." in content

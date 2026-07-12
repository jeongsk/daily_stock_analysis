# -*- coding: utf-8 -*-
"""Integrity checks on the committed stocks.index.json after the KR expansion."""
import json
from collections import Counter
from pathlib import Path

import pytest

INDEX_PATH = Path(__file__).parent.parent / "apps" / "dsa-web" / "public" / "stocks.index.json"


@pytest.fixture(scope="module")
def index():
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def test_kr_universe_is_expanded(index):
    markets = Counter(row[6] for row in index)
    assert markets["KR"] >= 2000  # full KOSPI/KOSDAQ, not the 30-seed set


def test_non_kr_markets_present(index):
    # The KR-only splice must not drop any other market.
    markets = Counter(row[6] for row in index)
    for market in ("CN", "HK", "US"):
        assert markets[market] > 0


def test_no_duplicate_canonical_codes(index):
    codes = [row[0] for row in index]
    assert len(codes) == len(set(codes))


def test_curated_seed_names_preserved(index):
    by_code = {row[0]: row for row in index}
    samsung = by_code["005930.KS"]
    assert samsung[2] == "三星电子"   # nameZh from the curated seed (Chinese, not Korean)
    assert samsung[11] == "삼성전자"  # nameKo preserved

# -*- coding: utf-8 -*-
"""KR name->code map (index loader) + resolver integration tests."""
import pytest  # noqa: F401  # used by tests appended in later tasks

from src.data.stock_index_loader import _build_kr_name_to_code_map


def _kr_item(canonical, name_ko, market="KR"):
    # [canon, disp, nameZh, pyFull, pyAbbr, aliases, market, assetType, active, pop, nameEn, nameKo]
    return [canonical, canonical, name_ko, None, None, [], market, "stock", True, 100, "", name_ko]


def test_build_kr_map_basic():
    items = [_kr_item("005930.KS", "삼성전자"), _kr_item("035720.KQ", "카카오")]
    result = _build_kr_name_to_code_map(items)
    assert result == {"삼성전자": "005930.KS", "카카오": "035720.KQ"}


def test_build_kr_map_excludes_ambiguous_names():
    items = [_kr_item("000001.KS", "동명"), _kr_item("000002.KQ", "동명")]
    assert "동명" not in _build_kr_name_to_code_map(items)


def test_build_kr_map_ignores_non_kr_and_short_rows():
    cn_row = ["000001.SZ", "000001", "平安银行", "pa", "pa", [], "CN", "stock", True, 100]
    items = [cn_row, _kr_item("005930.KS", "삼성전자")]
    assert _build_kr_name_to_code_map(items) == {"삼성전자": "005930.KS"}

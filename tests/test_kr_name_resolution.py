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


from src.services import name_to_code_resolver as nr


def test_resolver_resolves_korean_name(monkeypatch):
    monkeypatch.setattr(nr, "_get_kr_name_to_code_map_safe", lambda: {"삼성전자": "005930.KS"})
    assert nr.resolve_name_to_code("삼성전자") == "005930.KS"


def test_resolver_kr_map_failure_is_fail_open(monkeypatch):
    # The wrapper lazily imports get_kr_name_to_code_map from the loader module,
    # so patch it THERE (not on nr) to exercise the fail-open path.
    import src.data.stock_index_loader as loader

    def boom():
        raise RuntimeError("index unavailable")
    monkeypatch.setattr(loader, "get_kr_name_to_code_map", boom)
    # Unknown Hangul name returns None without raising.
    assert nr.resolve_name_to_code("존재하지않는종목명") is None


def test_resolver_cn_local_map_unaffected():
    # The KR addition must not perturb an existing unambiguous local-map name.
    # Pick a name guaranteed to be an unambiguous entry so the assertion is
    # deterministic and needs no network/AkShare.
    if not nr._LOCAL_REVERSE_MAP:
        pytest.skip("no local reverse names available")
    name, code = next(iter(nr._LOCAL_REVERSE_MAP.items()))
    assert nr.resolve_name_to_code(name) == code

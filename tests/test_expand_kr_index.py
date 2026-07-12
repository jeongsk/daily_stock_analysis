# -*- coding: utf-8 -*-
"""Offline tests for scripts/expand_kr_index.py (pykrx/network never used)."""
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from expand_kr_index import (  # noqa: E402
    build_kr_compact_rows,
    splice_kr,
    assert_index_ok,
    load_index,
    write_index,
    main,
)


def _cn_row(code):
    return [code, code.split(".")[0], "平安银行", "pa", "pa", [], "CN", "stock", True, 100]


def _kr_row(code, name_ko):
    return [code, code, name_ko, name_ko, name_ko, [], "KR", "stock", True, 100, "", name_ko]


def test_splice_replaces_only_kr():
    existing = [_cn_row("000001.SZ"), _kr_row("005930.KS", "삼성전자(old)"), _cn_row("600519.SH")]
    new_kr = [_kr_row("005930.KS", "삼성전자"), _kr_row("035720.KQ", "카카오")]
    result = splice_kr(existing, new_kr)
    markets = [r[6] for r in result]
    assert markets.count("CN") == 2  # CN untouched
    kr = [r for r in result if r[6] == "KR"]
    assert {r[0] for r in kr} == {"005930.KS", "035720.KQ"}  # old KR gone, new KR in
    assert all(r[6] != "KR" for r in result[:2])  # non-KR kept first, KR appended


def test_build_kr_compact_rows_from_csv(tmp_path):
    csv_path = tmp_path / "stock_list_kr.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ts_code", "symbol", "name", "enname", "name_ko", "aliases"])
        w.writeheader()
        w.writerow({"ts_code": "005930.KS", "symbol": "005930.KS", "name": "三星电子",
                    "enname": "Samsung Electronics Co. Ltd.", "name_ko": "삼성전자", "aliases": "Samsung|삼성전자"})
        w.writerow({"ts_code": "035720.KQ", "symbol": "035720.KQ", "name": "카카오",
                    "enname": "", "name_ko": "카카오", "aliases": ""})
    rows = build_kr_compact_rows(csv_path)
    by_code = {r[0]: r for r in rows}
    assert set(by_code) == {"005930.KS", "035720.KQ"}
    assert all(r[6] == "KR" for r in rows)
    assert by_code["005930.KS"][2] == "三星电子"   # nameZh from seed
    assert by_code["005930.KS"][11] == "삼성전자"  # nameKo
    assert by_code["035720.KQ"][2] == "카카오"     # new row Korean-primary nameZh
    assert by_code["035720.KQ"][11] == "카카오"


def test_assert_index_ok_rejects_low_kr_count():
    idx = [_cn_row("000001.SZ"), _kr_row("005930.KS", "삼성전자")]
    with pytest.raises(ValueError, match="KR count"):
        assert_index_ok(idx)


def test_assert_index_ok_rejects_duplicate_codes(monkeypatch):
    import expand_kr_index
    monkeypatch.setattr(expand_kr_index, "MIN_EXPECTED_KR_STOCKS", 1)
    idx = [_kr_row("005930.KS", "삼성전자"), _kr_row("005930.KS", "dup")]
    with pytest.raises(ValueError, match="duplicate"):
        assert_index_ok(idx)


def test_write_index_round_trips(tmp_path):
    out = tmp_path / "stocks.index.json"
    idx = [_cn_row("000001.SZ"), _kr_row("005930.KS", "삼성전자")]
    write_index(idx, out)
    assert load_index(out) == idx


def test_main_skip_fetch_splices_existing_csv(tmp_path, monkeypatch):
    import expand_kr_index
    kr_csv = tmp_path / "stock_list_kr.csv"
    with kr_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ts_code", "symbol", "name", "enname", "name_ko", "aliases"])
        w.writeheader()
        w.writerow({"ts_code": "035720.KQ", "symbol": "035720.KQ", "name": "카카오",
                    "enname": "", "name_ko": "카카오", "aliases": ""})
    web = tmp_path / "stocks.index.json"
    write_index([_cn_row("000001.SZ"), _kr_row("005930.KS", "old")], web)
    monkeypatch.setattr(expand_kr_index, "KR_CSV_PATH", kr_csv)
    monkeypatch.setattr(expand_kr_index, "WEB_INDEX_PATH", web)
    monkeypatch.setattr(expand_kr_index, "STATIC_INDEX_PATH", tmp_path / "static.json")
    monkeypatch.setattr(expand_kr_index, "MIN_EXPECTED_KR_STOCKS", 1)
    rc = main(["--skip-fetch"])
    assert rc == 0
    result = load_index(web)
    kr = [r for r in result if r[6] == "KR"]
    assert {r[0] for r in kr} == {"035720.KQ"}      # old KR replaced
    assert any(r[6] == "CN" for r in result)         # CN preserved

# -*- coding: utf-8 -*-
"""Offline tests for scripts/fetch_kr_stock_list.py (FinanceDataReader is never imported)."""
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from fetch_kr_stock_list import (  # noqa: E402
    FIELDNAMES,
    build_kr_row,
    collect_kr_rows,
    load_seed_rows,
    merge_rows,
    write_csv,
    main,
)


def test_build_kr_row_is_korean_primary():
    row = build_kr_row("005930.KS", "삼성전자")
    assert row == {
        "ts_code": "005930.KS",
        "symbol": "005930.KS",
        "name": "삼성전자",
        "enname": "",
        "name_ko": "삼성전자",
        "aliases": "",
    }


def test_collect_maps_market_suffix_and_skips_others():
    listing = [
        ("005930", "삼성전자", "KOSPI"),
        ("035720", "카카오", "KOSDAQ"),
        ("900000", "글로벌종목", "KOSDAQ GLOBAL"),   # KOSDAQ segment -> .KQ
        ("000001", "코넥스종목", "KONEX"),           # excluded (no .KS/.KQ)
        ("111111", "", "KOSPI"),                     # blank name skipped
        ("035720", "중복이름", "KOSDAQ"),            # duplicate code skipped
    ]
    rows = collect_kr_rows(listing)
    by_code = {r["ts_code"]: r for r in rows}
    assert set(by_code) == {"005930.KS", "035720.KQ", "900000.KQ"}
    assert by_code["005930.KS"]["name_ko"] == "삼성전자"
    assert by_code["005930.KS"]["name"] == by_code["005930.KS"]["name_ko"]  # Korean-primary


def test_merge_seeds_override_fetched_and_sorted():
    fetched = [build_kr_row("005930.KS", "삼성전자(원본)"), build_kr_row("035720.KQ", "카카오")]
    seeds = [{
        "ts_code": "005930.KS", "symbol": "005930.KS", "name": "三星电子",
        "enname": "Samsung Electronics Co. Ltd.", "name_ko": "삼성전자", "aliases": "Samsung|삼성전자",
    }]
    merged = merge_rows(fetched, seeds)
    by_code = {r["ts_code"]: r for r in merged}
    assert [r["ts_code"] for r in merged] == ["005930.KS", "035720.KQ"]  # sorted
    assert by_code["005930.KS"]["name"] == "三星电子"          # seed wins
    assert by_code["005930.KS"]["enname"] == "Samsung Electronics Co. Ltd."


def test_write_csv_round_trips_schema(tmp_path):
    out = tmp_path / "stock_list_kr.csv"
    write_csv([build_kr_row("035720.KQ", "카카오")], out)
    with out.open("r", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == FIELDNAMES
        rows = list(reader)
    assert rows[0]["ts_code"] == "035720.KQ" and rows[0]["name_ko"] == "카카오"


def test_write_csv_atomic_preserves_existing_on_failure(tmp_path, monkeypatch):
    out = tmp_path / "stock_list_kr.csv"
    write_csv([build_kr_row("005930.KS", "삼성전자")], out)  # existing good file
    original = out.read_bytes()

    def boom(self, row):
        raise RuntimeError("simulated disk failure")
    monkeypatch.setattr("csv.DictWriter.writerow", boom)

    with pytest.raises(RuntimeError):
        write_csv([build_kr_row("035720.KQ", "카카오")], out)

    assert out.read_bytes() == original  # original untouched
    assert list(out.parent.glob(".stock_list_kr.*.tmp")) == []  # no leftover temp


def test_load_seed_rows_reads_curated_seed_file():
    seeds = load_seed_rows()
    by_code = {r["ts_code"]: r for r in seeds}
    assert "005930.KS" in by_code and by_code["005930.KS"]["name_ko"] == "삼성전자"


def test_main_refuses_overwrite_below_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "fetch_kr_stock_list._fetch_from_fdr",
        lambda: [build_kr_row("005930.KS", "삼성전자")],  # 1 row < threshold
    )
    out = tmp_path / "stock_list_kr.csv"
    rc = main(["--output", str(out)])
    assert rc == 1
    assert not out.exists()  # existing CSV never overwritten on sanity failure

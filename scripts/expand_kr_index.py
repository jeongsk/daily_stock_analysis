#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Expand ONLY the KR entries of the committed stock index.

Unlike refresh_stock_index.py (which regenerates the whole index from Tushare +
KR and needs a TUSHARE_TOKEN), this runs scripts/fetch_kr_stock_list.py
(FinanceDataReader) to get the full KOSPI/KOSDAQ listing and splices it into the
existing committed stocks.index.json, replacing the KR rows only. CN/HK/US/JP/BSE
rows are left byte-for-byte unchanged.

Build-time only. Requires the `scripts` dependency group (finance-datareader):
    uv sync --group scripts
    uv run python scripts/expand_kr_index.py
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ for sibling import

from generate_index_from_csv import (  # noqa: E402
    build_stock_index,
    compress_index,
    parse_stock_row,
)

KR_CSV_PATH = REPO_ROOT / "data" / "stock_list_kr.csv"
WEB_INDEX_PATH = REPO_ROOT / "apps" / "dsa-web" / "public" / "stocks.index.json"
STATIC_INDEX_PATH = REPO_ROOT / "static" / "stocks.index.json"
MIN_EXPECTED_KR_STOCKS = 2000
KR_MARKET_INDEX = 6  # market position in a compact index row


def build_kr_compact_rows(kr_csv_path: Path = KR_CSV_PATH) -> List[list]:
    """Build compact KR index rows from the KR CSV, reusing the generator logic."""
    stocks: List[Dict[str, Any]] = []
    with kr_csv_path.open("r", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            parsed = parse_stock_row(row, "KR")
            if parsed:
                stocks.append(parsed)
    return compress_index(build_stock_index(stocks))


def _is_kr(row: Any) -> bool:
    return isinstance(row, list) and len(row) > KR_MARKET_INDEX and row[KR_MARKET_INDEX] == "KR"


def splice_kr(existing_index: List[list], kr_rows: List[list]) -> List[list]:
    """Return the index with all KR rows replaced by kr_rows (others unchanged)."""
    non_kr = [row for row in existing_index if not _is_kr(row)]
    return non_kr + list(kr_rows)


def assert_index_ok(index: List[list]) -> None:
    kr_count = sum(1 for row in index if _is_kr(row))
    if kr_count < MIN_EXPECTED_KR_STOCKS:
        raise ValueError(f"KR count {kr_count} < {MIN_EXPECTED_KR_STOCKS}; refusing to write")
    codes = [row[0] for row in index if isinstance(row, list) and row]
    if len(codes) != len(set(codes)):
        raise ValueError(f"index has {len(codes) - len(set(codes))} duplicate canonical codes; refusing to write")


def load_index(index_path: Path) -> List[list]:
    with index_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"unexpected index payload type: {type(data).__name__}")
    return data


def write_index(index: List[list], output_path: Path) -> None:
    """Atomically write the compact index (one row per line), matching the generator format."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write("[\n")
            for i, row in enumerate(index):
                json.dump(row, fh, ensure_ascii=False, separators=(",", ":"))
                fh.write(",\n" if i < len(index) - 1 else "\n")
            fh.write("]\n")
        os.replace(tmp, output_path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Expand KR entries of the stock index via pykrx")
    parser.add_argument(
        "--skip-fetch", action="store_true",
        help="use existing data/stock_list_kr.csv instead of fetching",
    )
    args = parser.parse_args(argv)

    if not args.skip_fetch:
        try:
            subprocess.run([sys.executable, "scripts/fetch_kr_stock_list.py"], cwd=REPO_ROOT, check=True)
        except subprocess.CalledProcessError as exc:
            print(f"[expand_kr_index] ERROR: KR fetch failed (exit {exc.returncode})", file=sys.stderr)
            return exc.returncode or 1

    if not KR_CSV_PATH.exists():
        print(f"[expand_kr_index] ERROR: {KR_CSV_PATH} not found (run without --skip-fetch)", file=sys.stderr)
        return 1

    kr_rows = build_kr_compact_rows(KR_CSV_PATH)
    spliced = splice_kr(load_index(WEB_INDEX_PATH), kr_rows)
    try:
        assert_index_ok(spliced)
    except ValueError as exc:
        print(f"[expand_kr_index] ERROR: {exc}", file=sys.stderr)
        return 1

    write_index(spliced, WEB_INDEX_PATH)
    if STATIC_INDEX_PATH.exists():
        write_index(spliced, STATIC_INDEX_PATH)

    kr_total = sum(1 for r in spliced if _is_kr(r))
    print(f"[expand_kr_index] wrote {len(spliced)} rows ({kr_total} KR) -> {WEB_INDEX_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

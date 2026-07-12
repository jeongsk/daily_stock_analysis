#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch the full KOSPI/KOSDAQ listing via pykrx and write data/stock_list_kr.csv.

Build-time only. pykrx is a `[dependency-groups]` script dependency and is never
imported at runtime. Curated seeds in scripts/stock_index_seeds/stock_list_kr.csv
override fetched rows so multilingual names/aliases are preserved.

Usage:
    uv sync --group scripts
    uv run python scripts/fetch_kr_stock_list.py
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "scripts" / "stock_index_seeds" / "stock_list_kr.csv"
OUTPUT_PATH = REPO_ROOT / "data" / "stock_list_kr.csv"
FIELDNAMES = ["ts_code", "symbol", "name", "enname", "name_ko", "aliases"]
MARKET_SUFFIX = (("KOSPI", "KS"), ("KOSDAQ", "KQ"))
MIN_EXPECTED_KR_STOCKS = 2000


def build_kr_row(ts_code: str, name_ko: str) -> Dict[str, str]:
    """Return a seed-schema CSV row for a fetched KR stock (Korean name primary)."""
    return {
        "ts_code": ts_code,
        "symbol": ts_code,
        "name": name_ko,
        "enname": "",
        "name_ko": name_ko,
        "aliases": "",
    }


def collect_kr_rows(
    ticker_list_fn: Callable[[str], List[str]],
    ticker_name_fn: Callable[[str], str],
    excluded_tickers: set,
) -> List[Dict[str, str]]:
    """Collect common-stock rows for KOSPI+KOSDAQ, skipping excluded tickers."""
    rows: List[Dict[str, str]] = []
    seen: set = set()
    for market, suffix in MARKET_SUFFIX:
        for ticker in ticker_list_fn(market):
            ticker = str(ticker).strip()
            if not ticker or ticker in excluded_tickers or ticker in seen:
                continue
            name = str(ticker_name_fn(ticker) or "").strip()
            if not name:
                continue
            seen.add(ticker)
            rows.append(build_kr_row(f"{ticker}.{suffix}", name))
    return rows


def load_seed_rows(seed_path: Path = SEED_PATH) -> List[Dict[str, str]]:
    """Load curated KR seed rows normalized to FIELDNAMES."""
    if not seed_path.is_file():
        return []
    rows: List[Dict[str, str]] = []
    with seed_path.open("r", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            ts_code = (row.get("ts_code") or "").strip()
            if not ts_code:
                continue
            rows.append({key: (row.get(key) or "").strip() for key in FIELDNAMES})
    return rows


def merge_rows(fetched: List[Dict[str, str]], seeds: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Merge fetched + seed rows; seeds override on same ts_code. Sorted by ts_code."""
    by_code: Dict[str, Dict[str, str]] = {r["ts_code"]: r for r in fetched}
    for seed in seeds:
        by_code[seed["ts_code"]] = seed
    return [by_code[code] for code in sorted(by_code)]


def write_csv(rows: List[Dict[str, str]], output_path: Path = OUTPUT_PATH) -> None:
    """Atomically write rows to output_path in the seed CSV schema.

    Writes to a temp file in the same directory, then os.replace() into place,
    so a mid-write failure can never truncate an existing good CSV.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(output_path.parent), prefix=".stock_list_kr.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in FIELDNAMES})
        os.replace(tmp_name, output_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _fetch_from_pykrx() -> List[Dict[str, str]]:
    """Collect KR rows using live pykrx (network). Imported lazily on purpose."""
    from pykrx import stock

    business_date = stock.get_nearest_business_day_in_a_week()

    def ticker_list_fn(market: str) -> List[str]:
        return list(stock.get_market_ticker_list(business_date, market=market))

    excluded = set(stock.get_etf_ticker_list(business_date))
    try:
        excluded |= set(stock.get_etn_ticker_list(business_date))
    except Exception:  # noqa: BLE001 - ETN endpoint is optional
        pass

    return collect_kr_rows(ticker_list_fn, stock.get_market_ticker_name, excluded)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fetch full KOSPI/KOSDAQ list via pykrx")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    args = parser.parse_args(argv)

    try:
        fetched = _fetch_from_pykrx()
    except ImportError:
        print(
            "[fetch_kr_stock_list] ERROR: pykrx not installed. "
            "Install with: uv sync --group scripts",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - fail-open at build time
        print(f"[fetch_kr_stock_list] ERROR: KRX fetch failed: {exc}", file=sys.stderr)
        return 1

    if len(fetched) < MIN_EXPECTED_KR_STOCKS:
        print(
            f"[fetch_kr_stock_list] ERROR: fetched only {len(fetched)} stocks "
            f"(< {MIN_EXPECTED_KR_STOCKS}); refusing to overwrite existing CSV.",
            file=sys.stderr,
        )
        return 1

    merged = merge_rows(fetched, load_seed_rows())
    write_csv(merged, Path(args.output))
    print(f"[fetch_kr_stock_list] wrote {len(merged)} rows -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

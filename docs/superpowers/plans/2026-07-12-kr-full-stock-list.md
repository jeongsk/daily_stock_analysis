# KR Full Stock List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand KR stock coverage from 30 curated seeds to the full KOSPI/KOSDAQ listing (~2,700) in the autocomplete index and enable backend Korean-name→code resolution.

**Architecture:** A build-time pykrx script writes the full KR list (with curated seeds merged as overrides) to `data/stock_list_kr.csv`; the existing index generator consumes it and produces the committed `stocks.index.json`. Backend Korean-name resolution reuses the existing `stock_index_loader` (single source of truth = the index) via a new KR reverse map, consulted by `name_to_code_resolver`.

**Tech Stack:** Python 3.11+, pykrx (build-time only), existing pypinyin-based generator, pytest.

## Global Constraints

- pykrx MUST be build-time only — never imported by runtime/Docker/CI. Runtime consumes generated artifacts only. (Add it to `[dependency-groups]`, NOT `[project].dependencies`.)
- All fetch/load failures are fail-open: never overwrite an existing good `data/stock_list_kr.csv` or index; log and continue.
- New KR rows are Korean-name-primary: `name == name_ko == <Hangul>`, `enname`/`aliases` empty.
- Curated seeds in `scripts/stock_index_seeds/stock_list_kr.csv` always override fetched rows on the same `ts_code`.
- CSV schema (verbatim, order matters): `ts_code,symbol,name,enname,name_ko,aliases`.
- Compact index element order (verbatim): `[canonicalCode, displayCode, nameZh, pinyinFull, pinyinAbbr, aliases, market, assetType, active, popularity, nameEn, nameKo]`.
- Do NOT modify `scripts/generate_index_from_csv.py` or the frontend — both already handle KR (`name_ko` field + `nameKo` search).
- No commit of `git tag`/`git push`; commit messages in English, no `Co-Authored-By`.
- `docs/CHANGELOG.md` `[Unreleased]` entries are flat one-liners `- [类型] 描述` — no `###` subheaders.

---

### Task 1: pykrx fetch script

Build-time script that fetches the full KOSPI/KOSDAQ listing, merges curated seeds, and writes `data/stock_list_kr.csv`. All pure transforms are unit-tested offline; pykrx is imported lazily only inside the live fetch path.

**Files:**
- Create: `scripts/fetch_kr_stock_list.py`
- Test: `tests/test_fetch_kr_stock_list.py`

**Interfaces:**
- Produces: `build_kr_row(ts_code: str, name_ko: str) -> dict`, `collect_kr_rows(ticker_list_fn, ticker_name_fn, excluded_tickers: set[str]) -> list[dict]`, `load_seed_rows(seed_path: Path = SEED_PATH) -> list[dict]`, `merge_rows(fetched: list[dict], seeds: list[dict]) -> list[dict]`, `write_csv(rows: list[dict], output_path: Path = OUTPUT_PATH) -> None`, `main(argv=None) -> int`. Module constants: `FIELDNAMES`, `MIN_EXPECTED_KR_STOCKS = 2000`, `MARKET_SUFFIX = (("KOSPI","KS"),("KOSDAQ","KQ"))`.

- [ ] **Step 1: Write failing tests for the pure transforms**

Create `tests/test_fetch_kr_stock_list.py`:

```python
# -*- coding: utf-8 -*-
"""Offline tests for scripts/fetch_kr_stock_list.py (pykrx is never imported)."""
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


def _fake_market(kospi, kosdaq):
    def ticker_list_fn(market):
        return {"KOSPI": kospi, "KOSDAQ": kosdaq}[market]
    return ticker_list_fn


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


def test_collect_maps_suffix_and_excludes_etf_and_blank():
    names = {"005930": "삼성전자", "035720": "카카오", "069500": "KODEX 200", "900000": ""}
    rows = collect_kr_rows(
        _fake_market(["005930", "069500", "900000"], ["035720"]),
        lambda t: names.get(t, ""),
        excluded_tickers={"069500"},
    )
    codes = {r["ts_code"] for r in rows}
    assert codes == {"005930.KS", "035720.KQ"}  # ETF excluded, blank-name skipped


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


def test_load_seed_rows_reads_curated_seed_file():
    seeds = load_seed_rows()
    by_code = {r["ts_code"]: r for r in seeds}
    assert "005930.KS" in by_code and by_code["005930.KS"]["name_ko"] == "삼성전자"


def test_main_refuses_overwrite_below_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "fetch_kr_stock_list._fetch_from_pykrx",
        lambda: [build_kr_row("005930.KS", "삼성전자")],  # 1 row < threshold
    )
    out = tmp_path / "stock_list_kr.csv"
    rc = main(["--output", str(out)])
    assert rc == 1
    assert not out.exists()  # existing CSV never overwritten on sanity failure
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fetch_kr_stock_list.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fetch_kr_stock_list'`

- [ ] **Step 3: Create the fetch script**

Create `scripts/fetch_kr_stock_list.py`:

```python
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
import sys
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
    """Write rows to output_path in the seed CSV schema."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in FIELDNAMES})


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fetch_kr_stock_list.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 scripts/fetch_kr_stock_list.py tests/test_fetch_kr_stock_list.py
git add scripts/fetch_kr_stock_list.py tests/test_fetch_kr_stock_list.py
git commit -m "feat: add pykrx KR full stock list fetch script"
```

---

### Task 2: Backend KR name→code map in the index loader

Add a KR `nameKo → canonicalCode` reverse map to `stock_index_loader.py`, reusing its existing lazy-load/cache/multi-path infrastructure. Ambiguous Korean names (one name → multiple codes) are excluded.

**Files:**
- Modify: `src/data/stock_index_loader.py`
- Test: `tests/test_kr_name_resolution.py` (create)

**Interfaces:**
- Consumes: existing `_STOCK_INDEX_CACHE_LOCK`, `get_remote_stock_index_cache_path`, `_get_fresh_stock_index_candidates`, `get_stock_index_candidate_paths`, `_load_stock_index_payload`, `_same_path`, `validate_stock_index_payload`.
- Produces: `_build_kr_name_to_code_map(raw_items: list) -> Dict[str, str]`, `get_kr_name_to_code_map() -> Dict[str, str]`. `clear_stock_index_cache()` also resets the new cache.

- [ ] **Step 1: Write failing tests**

Create `tests/test_kr_name_resolution.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_kr_name_resolution.py -q`
Expected: FAIL with `ImportError: cannot import name '_build_kr_name_to_code_map'`

- [ ] **Step 3: Add the map builder, accessor, and cache**

In `src/data/stock_index_loader.py`, add a cache global next to the existing caches (after `_STOCK_CODE_LOOKUP_CACHE`):

```python
_KR_NAME_TO_CODE_CACHE: Dict[str, str] | None = None
```

Add these functions (place near `get_stock_code_index_map`):

```python
def _build_kr_name_to_code_map(raw_items: list) -> Dict[str, str]:
    """Build a KR Hangul name -> canonical code map; drop ambiguous names."""
    name_to_codes: dict[str, set] = {}
    for item in raw_items:
        if not isinstance(item, list) or len(item) < 12:
            continue
        if str(item[6] or "").strip().upper() != "KR":
            continue
        canonical = str(item[0] or "").strip()
        name_ko = str(item[11] or "").strip()
        if not canonical or not name_ko:
            continue
        name_to_codes.setdefault(name_ko, set()).add(canonical)
    return {name: next(iter(codes)) for name, codes in name_to_codes.items() if len(codes) == 1}


def get_kr_name_to_code_map() -> Dict[str, str]:
    """Lazily load and cache the KR name->code map from the generated index."""
    global _KR_NAME_TO_CODE_CACHE

    if _KR_NAME_TO_CODE_CACHE is not None:
        return _KR_NAME_TO_CODE_CACHE

    with _STOCK_INDEX_CACHE_LOCK:
        if _KR_NAME_TO_CODE_CACHE is not None:
            return _KR_NAME_TO_CODE_CACHE

        remote_path = get_remote_stock_index_cache_path()
        for index_path in _get_fresh_stock_index_candidates(get_stock_index_candidate_paths(), remote_path):
            try:
                raw_items = _load_stock_index_payload(index_path)
                if _same_path(index_path, remote_path):
                    validate_stock_index_payload(raw_items)
                _KR_NAME_TO_CODE_CACHE = _build_kr_name_to_code_map(raw_items)
                return _KR_NAME_TO_CODE_CACHE
            except (OSError, TypeError, ValueError) as exc:
                logger.debug("[股票名称] 加载 KR 名称映射失败 %s: %s", index_path, exc)

        _KR_NAME_TO_CODE_CACHE = {}
        return _KR_NAME_TO_CODE_CACHE
```

Update `clear_stock_index_cache()` to reset the new cache — change its body to:

```python
def clear_stock_index_cache() -> None:
    """Clear the in-process stock index lookup cache."""
    global _REMOTE_INDEX_VALIDITY_CACHE, _STOCK_INDEX_CACHE, _STOCK_INDEX_RECORD_CACHE
    global _STOCK_CODE_LOOKUP_CACHE, _KR_NAME_TO_CODE_CACHE
    with _STOCK_INDEX_CACHE_LOCK:
        _STOCK_INDEX_CACHE = None
        _STOCK_INDEX_RECORD_CACHE = None
        _STOCK_CODE_LOOKUP_CACHE = None
        _KR_NAME_TO_CODE_CACHE = None
        _REMOTE_INDEX_VALIDITY_CACHE = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_kr_name_resolution.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/data/stock_index_loader.py tests/test_kr_name_resolution.py
git add src/data/stock_index_loader.py tests/test_kr_name_resolution.py
git commit -m "feat: add KR name-to-code map to stock index loader"
```

---

### Task 3: Resolver consults the KR name map

Wire `resolve_name_to_code` to consult the KR map before the CJK-only early return, so Hangul names (which are outside the Han-ideograph range checked by `_contains_cjk`) resolve.

**Files:**
- Modify: `src/services/name_to_code_resolver.py`
- Test: append to `tests/test_kr_name_resolution.py`

**Interfaces:**
- Consumes: `get_kr_name_to_code_map()` from Task 2.
- Produces: `_get_kr_name_to_code_map_safe() -> Dict[str, str]` (fail-open wrapper).

- [ ] **Step 1: Write failing tests (append to `tests/test_kr_name_resolution.py`)**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_kr_name_resolution.py -q`
Expected: FAIL with `AttributeError: ... has no attribute '_get_kr_name_to_code_map_safe'`

- [ ] **Step 3: Add the safe wrapper and consult it in `resolve_name_to_code`**

In `src/services/name_to_code_resolver.py`, add after `_is_single_char_typo` (before `resolve_name_to_code`):

```python
def _get_kr_name_to_code_map_safe() -> Dict[str, str]:
    """Load KR name->code map from the generated index; fail-open to {}."""
    try:
        from src.data.stock_index_loader import get_kr_name_to_code_map

        return get_kr_name_to_code_map()
    except Exception as exc:  # noqa: BLE001 - KR resolution is additive/optional
        logger.debug(f"[NameResolver] KR 名称映射加载失败: {exc}")
        return {}
```

Then, inside `resolve_name_to_code`, insert a KR lookup immediately AFTER the local-ambiguous check and BEFORE the pinyin block. Locate:

```python
    if s in _LOCAL_AMBIGUOUS_NAMES:
        logger.debug(f"[NameResolver] 命中本地歧义名称，快速返回 None: {s}")
        return None

    # 3. Pinyin match (exact)
```

and change it to:

```python
    if s in _LOCAL_AMBIGUOUS_NAMES:
        logger.debug(f"[NameResolver] 命中本地歧义名称，快速返回 None: {s}")
        return None

    # 2.5 KR localized (Hangul) name from the generated index.
    # Hangul is outside the Han-ideograph range checked by _contains_cjk, so it
    # must be resolved before the non-CJK early return below.
    kr_reverse = _get_kr_name_to_code_map_safe()
    if s in kr_reverse:
        logger.debug(f"[NameResolver] 命中 KR 索引名称映射: {s} -> {kr_reverse[s]}")
        return kr_reverse[s]

    # 3. Pinyin match (exact)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_kr_name_resolution.py -q`
Expected: PASS (6 tests total in the file)

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/services/name_to_code_resolver.py tests/test_kr_name_resolution.py
git add src/services/name_to_code_resolver.py tests/test_kr_name_resolution.py
git commit -m "feat: resolve KR Hangul stock names via index map"
```

---

### Task 4: Wire KR fetch into refresh + declare pykrx script dependency

Add a fail-open KR fetch step to `refresh_stock_index.py` (with `--skip-kr` for offline regeneration) and declare pykrx in `[dependency-groups]`.

**Files:**
- Modify: `scripts/refresh_stock_index.py`
- Modify: `pyproject.toml`
- Test: `tests/test_refresh_stock_index_kr.py` (create)

**Interfaces:**
- Consumes: `scripts/fetch_kr_stock_list.py` (Task 1) via subprocess.
- Produces: `_run_kr_fetch() -> None` (fail-open), new `--skip-kr` argparse flag.

- [ ] **Step 1: Write failing tests**

Create `tests/test_refresh_stock_index_kr.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_refresh_stock_index_kr.py -q`
Expected: FAIL (`--skip-kr` unrecognized / KR fetch not invoked)

- [ ] **Step 3: Add `_run_kr_fetch`, the `--skip-kr` flag, and the fetch step**

In `scripts/refresh_stock_index.py`, add after `_sync_static_index`:

```python
def _run_kr_fetch() -> None:
    """Fetch the full KR list; fail-open so KR issues never abort the refresh."""
    try:
        _run([sys.executable, "scripts/fetch_kr_stock_list.py"])
    except subprocess.CalledProcessError as exc:
        print(
            f"[refresh_stock_index] WARNING: KR fetch failed (exit {exc.returncode}); "
            "keeping existing data/stock_list_kr.csv",
            file=sys.stderr,
        )
```

Add the flag in `main`, after the `--skip-fetch` argument:

```python
    parser.add_argument(
        "--skip-kr",
        action="store_true",
        help="跳过 KR (pykrx) 股票列表抓取，仅用现有 data/stock_list_kr.csv",
    )
```

Add the fetch step in `main`, immediately BEFORE the `generate_index_from_csv.py` call. Locate:

```python
        _run([sys.executable, "scripts/generate_index_from_csv.py", "--source", "tushare"])
        _sync_static_index()
```

and change it to:

```python
        if args.skip_kr:
            print("[refresh_stock_index] skip KR fetch; using existing data/stock_list_kr.csv")
        else:
            _run_kr_fetch()

        _run([sys.executable, "scripts/generate_index_from_csv.py", "--source", "tushare"])
        _sync_static_index()
```

- [ ] **Step 4: Add pykrx to `[dependency-groups]` in `pyproject.toml`**

Locate:

```toml
[dependency-groups]
dev = [
    "flake8",
    "pytest",
]
```

and change it to:

```toml
[dependency-groups]
dev = [
    "flake8",
    "pytest",
]
scripts = [
    "pykrx>=1.0.45",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_refresh_stock_index_kr.py -q`
Expected: PASS (3 tests)

- [ ] **Step 6: Lint and commit**

```bash
uv run flake8 scripts/refresh_stock_index.py tests/test_refresh_stock_index_kr.py
git add scripts/refresh_stock_index.py tests/test_refresh_stock_index_kr.py pyproject.toml
git commit -m "feat: wire KR fetch into refresh_stock_index with --skip-kr and pykrx group"
```

---

### Task 5: Regenerate the committed index + docs (MAINTAINER step)

**Prerequisites (cannot run in offline CI):** a `TUSHARE_TOKEN` with A/HK/US permissions AND pykrx installed (`uv sync --group scripts`) AND network. Regenerating the index re-fetches all markets, so the diff will include CN/HK/US refresh churn in addition to KR — this is normal for an index refresh. If you only have pykrx (no Tushare), you cannot produce a complete index; hand this task to a maintainer.

**Files:**
- Modify (regenerate, commit): `apps/dsa-web/public/stocks.index.json`
- Modify: `docs/market-support.md`, `docs/CHANGELOG.md`
- Test: `tests/test_kr_index_integrity.py` (create)

- [ ] **Step 1: Regenerate the index**

```bash
uv sync --group scripts
uv run python scripts/refresh_stock_index.py   # fetches Tushare + KR, regenerates index
```

Expected tail: `生成完成！市场分布:` with `KR: ~2700` (up from 30).

- [ ] **Step 2: Write the offline integrity test (runs against the committed index)**

Create `tests/test_kr_index_integrity.py`:

```python
# -*- coding: utf-8 -*-
"""Integrity checks on the committed stocks.index.json after KR expansion."""
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


def test_no_duplicate_canonical_codes(index):
    codes = [row[0] for row in index]
    assert len(codes) == len(set(codes))


def test_curated_seed_names_preserved(index):
    by_code = {row[0]: row for row in index}
    samsung = by_code["005930.KS"]
    assert samsung[2] == "三星电子"   # nameZh from curated seed (not Korean)
    assert samsung[11] == "삼성전자"  # nameKo preserved
```

- [ ] **Step 3: Write the resolver/bare-code integration test**

Append to `tests/test_kr_name_resolution.py`:

```python
def test_expanded_kr_map_reaches_loader():
    # After regeneration, the loader's KR map must reflect the full listing,
    # proving the expanded index flows through to backend resolution.
    from src.data.stock_index_loader import clear_stock_index_cache, get_kr_name_to_code_map

    clear_stock_index_cache()
    kr_map = get_kr_name_to_code_map()
    assert len(kr_map) >= 2000
    assert kr_map.get("삼성전자") == "005930.KS"  # seed still resolves


def test_representative_a_share_bare_code_stays_cn():
    # Expanding KR must not shadow a real A-share bare code via resolve_index_stock_code.
    from src.services.stock_code_utils import resolve_index_stock_code_for_analysis

    # 000001 is Ping An Bank (000001.SZ); it must not resolve to a KR suffix form.
    resolved = resolve_index_stock_code_for_analysis("000001")
    assert not resolved.endswith(".KS") and not resolved.endswith(".KQ")
```

> Before committing, confirm the regenerated index has no unambiguous KR `000001.*` entry (so the bare-code regression is meaningful):
> `python -c "import json;d=json.load(open('apps/dsa-web/public/stocks.index.json'));print([r[0] for r in d if r[1]=='000001' or r[0].startswith('000001.')])"`

- [ ] **Step 4: Run the full offline gate**

Run: `uv run ./scripts/ci_gate.sh`
Expected: PASS (flake8 + `pytest -m "not network"`), including the new KR tests.

- [ ] **Step 5: Update docs**

In `docs/market-support.md`, under the KR suffix-only MVP section, update the autocomplete coverage note: KR autocomplete now covers the full KOSPI/KOSDAQ listing (regenerated from pykrx, curated seeds override multilingual names); backend resolves Korean names to `.KS`/`.KQ` via the index; bare 6-digit codes matching a unique KR index entry resolve to KR per the documented pool-hit contract, else default to A-share. Note the regeneration command (`uv sync --group scripts && uv run python scripts/refresh_stock_index.py`) and that pykrx is build-time only.

Add to `docs/CHANGELOG.md` under `[Unreleased]` (flat one-liner):

```
- [新功能] KR 종목 자동완성·백엔드 한글명 해석을 KOSPI/KOSDAQ 전체 상장(~2,700)으로 확장: pykrx(빌드타임 전용)로 전체 리스트를 취득해 큐레이션 시드(다국어명/별칭)를 override 병합, 생성 인덱스에서 한글명→코드(.KS/.KQ) 해석 — 런타임 pykrx 미의존, fail-open, 나코드 KR 해석은 문서화된 풀 계약대로 자연 확장.
```

- [ ] **Step 6: Commit the regenerated index, tests, and docs**

```bash
git add apps/dsa-web/public/stocks.index.json tests/test_kr_index_integrity.py \
        tests/test_kr_name_resolution.py docs/market-support.md docs/CHANGELOG.md
git commit -m "feat: regenerate stock index with full KR listing and docs"
```

---

## Self-Review Notes

- **Spec coverage:** fetch script (Task 1), backend resolution via loader reuse (Tasks 2–3), refresh wiring + pykrx group (Task 4), regeneration + docs + integrity/bare-code regression (Task 5). All success criteria and §5 edge cases map to a task.
- **No generator/frontend change:** confirmed against current code (`generate_index_from_csv.py` already reads `data/stock_list_kr.csv` + `name_ko`; `searchStocks.ts` already searches `nameKo`).
- **CI safety:** Tasks 1–4 are fully offline (mocks/fixtures, no pykrx import). Only Task 5 needs network + Tushare + pykrx; its committed test (`test_kr_index_integrity.py`) then runs offline in CI against the committed index.
- **Verification caveat:** the Task 5 bare-code regression uses `000001` (Ping An Bank, 000001.SZ); confirm no unambiguous KR `000001.*` entry exists in the regenerated index before commit — flagged inline in Step 3.

# Korean Stock Name Localization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 한국 주식이 한국어 UI/한국어 보고서에서 중국어 종목명으로 표시되는 문제를 해결하고, 기존 중국어 표시 계약은 유지한다.

**Architecture:** `stocks.index.json`의 기존 0-9번 압축 필드는 그대로 두고, 뒤쪽에 선택 필드 `nameEn`, `nameKo`를 추가한다. 프런트는 UI 언어에 따라 표시명/제출명을 선택하고, 백엔드는 보고서 언어에 따라 인덱스 이름을 선택하되 기존 호출자는 기본 중국어 동작을 유지한다.

**Tech Stack:** Python, pytest, TypeScript, React, Vitest, existing `stocks.index.json` compressed tuple format.

---

## Background

현재 한국 종목명은 seed 단계에서 이미 중국어로 들어간다.

- `scripts/stock_index_seeds/stock_list_kr.csv`
  - `005930.KS -> 三星电子`
  - `000660.KS -> SK海力士`
  - `005380.KS -> 现代汽车`
- `scripts/generate_index_from_csv.py`는 US 외 시장에서 `name` 필드를 그대로 `nameZh`로 저장한다.
- `apps/dsa-web/src/components/StockAutocomplete/SuggestionsList.tsx`는 `suggestion.nameZh`를 그대로 렌더링한다.
- `data_provider/base.py::DataFetcherManager.get_stock_name()`도 `src.data.stock_index_loader.get_index_stock_name()` 결과를 그대로 사용한다.

따라서 단순히 프런트 문구만 바꾸면 해결되지 않는다. 데이터 생성, 프런트 표시/제출, 백엔드 보고서 이름 선택이 함께 맞아야 한다.

## File Structure

- Modify: `scripts/stock_index_seeds/stock_list_kr.csv`
  - 한국 seed에 `name_ko` 컬럼을 추가하고 한국어 표시명을 채운다.
- Modify: `scripts/generate_index_from_csv.py`
  - CSV의 `enname`, `name_ko`를 읽어 인덱스 항목에 `nameEn`, `nameKo`를 보존한다.
  - 압축 tuple 뒤쪽에 선택 필드 2개를 추가한다.
- Modify: `apps/dsa-web/src/types/stockIndex.ts`
  - `StockIndexItem`, `StockSuggestion`, `StockIndexTuple`에 `nameEn`, `nameKo`, `displayName`을 반영한다.
- Modify: `apps/dsa-web/src/utils/stockIndexFields.ts`
  - `NAME_EN`, `NAME_KO` 인덱스 상수를 추가한다.
- Create: `apps/dsa-web/src/utils/stockDisplayName.ts`
  - UI 언어별 종목 표시명 선택을 한 곳에 모은다.
- Modify: `apps/dsa-web/src/utils/stockIndexLoader.ts`
  - 압축 tuple의 10, 11번 선택 필드를 unpack한다.
- Modify: `apps/dsa-web/src/utils/searchStocks.ts`
  - 검색 대상에 `nameEn`, `nameKo`를 추가하고 suggestion에 `displayName`을 담는다.
- Modify: `apps/dsa-web/src/hooks/useAutocomplete.ts`
  - UI 언어를 받아 검색 결과의 표시명을 결정할 수 있게 한다.
- Modify: `apps/dsa-web/src/components/StockAutocomplete/StockAutocomplete.tsx`
  - `useUiLanguage()`를 사용하고, 선택/제출 시 `displayName`을 전달한다.
- Modify: `apps/dsa-web/src/components/StockAutocomplete/SuggestionsList.tsx`
  - `suggestion.displayName`을 표시한다.
- Modify: `src/data/stock_index_loader.py`
  - `get_index_stock_name(stock_code, language=None)`에서 언어별 이름을 선택한다.
- Modify: `data_provider/base.py`
  - `get_stock_name(..., report_language=None)`을 추가하고 기존 기본 동작은 유지한다.
- Modify: `src/core/pipeline.py`
  - `report_language`를 확정한 뒤 `get_stock_name(..., report_language=report_language)`로 호출한다.
- Modify: `src/report_language.py`
  - 기존 저장 이력/결과가 중국어명을 갖고 있어도 한국어 보고서에서는 index의 `nameKo`로 보정한다.
- Modify: `docs/market-support.md`, `docs/CHANGELOG.md`
  - 한국 종목 표시명 정책과 변경 내역을 기록한다.
- Test: `tests/test_generate_index_from_csv.py`
- Test: `tests/test_stock_index_loader.py`
- Test: `tests/test_report_language.py`
- Test: `apps/dsa-web/src/utils/__tests__/stockIndexLoader.test.ts`
- Test: `apps/dsa-web/src/utils/__tests__/searchStocks.test.ts`
- Test: `apps/dsa-web/src/components/StockAutocomplete/__tests__/StockAutocomplete.test.tsx`

## Compatibility Rules

- 기존 tuple 0-9번 필드는 절대 재배열하지 않는다.
- 오래된 `stocks.index.json`에는 10, 11번 필드가 없을 수 있으므로 모든 reader는 optional로 처리한다.
- `zh` 또는 언어 미지정 경로는 기존 `nameZh` 표시를 유지한다.
- `ko`는 `nameKo -> nameEn -> nameZh -> code` 순서로 선택한다.
- `en`은 `nameEn -> nameZh -> code` 순서로 선택한다.
- 검색은 중국어명, 영어명, 한국어명, alias, code를 모두 대상으로 유지한다.

---

### Task 1: Extend Korean Seed Data

**Files:**
- Modify: `scripts/stock_index_seeds/stock_list_kr.csv`
- Test: `tests/test_generate_index_from_csv.py`

- [ ] **Step 1: Write the failing seed parsing test**

Add this test to `tests/test_generate_index_from_csv.py` inside `TestDataCleaning`:

```python
    def test_valid_kr_stock_preserves_localized_names(self):
        """测试韩股种子记录保留英文名和韩文名"""
        row = {
            'ts_code': '005930.KS',
            'name': '三星电子',
            'enname': 'Samsung Electronics Co. Ltd.',
            'name_ko': '삼성전자',
            'aliases': 'Samsung|Samsung Electronics|三星|삼성전자',
        }
        result = parse_stock_row(row, 'KR')
        assert result is not None
        assert result['ts_code'] == '005930.KS'
        assert result['symbol'] == '005930.KS'
        assert result['name'] == '三星电子'
        assert result['name_en'] == 'Samsung Electronics Co. Ltd.'
        assert result['name_ko'] == '삼성전자'
        assert result['market'] == 'KR'
        assert result['aliases'] == ['Samsung', 'Samsung Electronics', '三星', '삼성전자']
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/test_generate_index_from_csv.py::TestDataCleaning::test_valid_kr_stock_preserves_localized_names -q
```

Expected: fail with `KeyError: 'name_en'` or `KeyError: 'name_ko'`.

- [ ] **Step 3: Update Korean seed CSV**

Replace the header and rows in `scripts/stock_index_seeds/stock_list_kr.csv` with:

```csv
ts_code,symbol,name,enname,name_ko,aliases
005930.KS,005930.KS,三星电子,Samsung Electronics Co. Ltd.,삼성전자,Samsung|Samsung Electronics|三星|삼성전자
000660.KS,000660.KS,SK海力士,SK hynix Inc.,SK하이닉스,SK Hynix|海力士|하이닉스
373220.KS,373220.KS,LG新能源,LG Energy Solution Ltd.,LG에너지솔루션,LG Energy Solution|LGES|LG新能源
005380.KS,005380.KS,现代汽车,Hyundai Motor Company,현대차,Hyundai|Hyundai Motor|现代|현대차
035420.KS,035420.KS,NAVER,NAVER Corporation,NAVER,Naver|NAVER|네이버
051910.KS,051910.KS,LG化学,LG Chem Ltd.,LG화학,LG Chem|LG化学
006400.KS,006400.KS,三星SDI,Samsung SDI Co. Ltd.,삼성SDI,Samsung SDI|三星SDI
035720.KQ,035720.KQ,Kakao,Kakao Corp.,카카오,Kakao|可可|카카오
247540.KQ,247540.KQ,Ecopro BM,Ecopro BM Co. Ltd.,에코프로비엠,Ecopro BM|ECOPRO BM|에코프로비엠
091990.KQ,091990.KQ,赛尔群医疗,Celltrion Healthcare Co. Ltd.,셀트리온헬스케어,Celltrion Healthcare|Celltrion|셀트리온헬스케어
```

- [ ] **Step 4: Parse optional localized fields**

In `scripts/generate_index_from_csv.py`, add this helper near `get_stock_name`:

```python
def get_optional_localized_name(row: Dict[str, str], field_name: str) -> Optional[str]:
    """Return a normalized optional localized stock name from a CSV row."""
    value = unicodedata.normalize('NFKC', str(row.get(field_name, '') or '')).strip()
    return value if value else None
```

Then in `parse_stock_row`, before the return dict, add:

```python
    name_en = get_optional_localized_name(row, 'enname')
    name_ko = get_optional_localized_name(row, 'name_ko')
```

Return these fields:

```python
    return {
        'ts_code': ts_code,
        'symbol': display_code,
        'name': name,
        'name_en': name_en,
        'name_ko': name_ko,
        'market': market,
        'aliases': parse_aliases(row),
    }
```

- [ ] **Step 5: Run the seed parsing test**

Run:

```bash
uv run pytest tests/test_generate_index_from_csv.py::TestDataCleaning::test_valid_kr_stock_preserves_localized_names -q
```

Expected: pass.

- [ ] **Step 6: Commit**

Do not commit unless the user explicitly approves. If approved, use:

```bash
git add scripts/stock_index_seeds/stock_list_kr.csv scripts/generate_index_from_csv.py tests/test_generate_index_from_csv.py
git commit -m "fix: preserve localized Korean stock names"
```

---

### Task 2: Extend Stock Index Generation Without Breaking Tuple Compatibility

**Files:**
- Modify: `scripts/generate_index_from_csv.py`
- Test: `tests/test_generate_index_from_csv.py`

- [ ] **Step 1: Write failing compressed format tests**

Add these tests inside `TestOutputFormat`:

```python
    def test_compress_index_appends_optional_localized_names(self):
        """测试压缩格式在尾部追加可选英文名和韩文名"""
        index = [{
            "canonicalCode": "005930.KS",
            "displayCode": "005930.KS",
            "nameZh": "三星电子",
            "nameEn": "Samsung Electronics Co. Ltd.",
            "nameKo": "삼성전자",
            "pinyinFull": "sanxingdianzi",
            "pinyinAbbr": "sxdz",
            "aliases": ["Samsung", "삼성전자"],
            "market": "KR",
            "assetType": "stock",
            "active": True,
            "popularity": 100,
        }]

        compressed = compress_index(index)

        assert compressed[0][0] == "005930.KS"
        assert compressed[0][2] == "三星电子"
        assert compressed[0][10] == "Samsung Electronics Co. Ltd."
        assert compressed[0][11] == "삼성전자"
```

Update `TestIntegration.test_full_workflow_tushare` to write `name_ko` in the KR CSV row and assert:

```python
        samsung = next(item for item in index if item['canonicalCode'] == '005930.KS')
        assert samsung['nameZh'] == '三星电子'
        assert samsung['nameEn'] == 'Samsung Electronics'
        assert samsung['nameKo'] == '삼성전자'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run pytest tests/test_generate_index_from_csv.py::TestOutputFormat::test_compress_index_appends_optional_localized_names tests/test_generate_index_from_csv.py::TestIntegration::test_full_workflow_tushare -q
```

Expected: fail because `nameEn` and `nameKo` are not written to index/compressed output.

- [ ] **Step 3: Add localized fields to index entries**

In `build_stock_index`, add:

```python
        name_en = stock.get('name_en')
        name_ko = stock.get('name_ko')
```

Then add fields to the `index.append({...})` object:

```python
            "nameEn": name_en,
            "nameKo": name_ko,
```

Keep `nameZh` as `name`.

- [ ] **Step 4: Append optional fields in compressed output**

In `compress_index`, append two fields after popularity:

```python
            item.get("nameEn"),
            item.get("nameKo"),
```

The final tuple order must be:

```python
[
    item["canonicalCode"],
    item["displayCode"],
    item["nameZh"],
    item.get("pinyinFull"),
    item.get("pinyinAbbr"),
    item.get("aliases", []),
    item["market"],
    item["assetType"],
    item["active"],
    item.get("popularity", 0),
    item.get("nameEn"),
    item.get("nameKo"),
]
```

- [ ] **Step 5: Run generation tests**

Run:

```bash
uv run pytest tests/test_generate_index_from_csv.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

Do not commit unless the user explicitly approves. If approved, use:

```bash
git add scripts/generate_index_from_csv.py tests/test_generate_index_from_csv.py
git commit -m "fix: append localized names to stock index"
```

---

### Task 3: Localize Backend Stock Name Resolution

**Files:**
- Modify: `src/data/stock_index_loader.py`
- Modify: `data_provider/base.py`
- Modify: `src/core/pipeline.py`
- Modify: `src/report_language.py`
- Test: `tests/test_stock_index_loader.py`
- Test: `tests/test_report_language.py`

- [ ] **Step 1: Write failing stock index loader test**

Add this test to `tests/test_stock_index_loader.py`:

```python
    def test_get_index_stock_name_selects_localized_name_by_language(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "stocks.index.json"
            index_path.write_text(
                json.dumps(
                    [[
                        "005930.KS",
                        "005930.KS",
                        "三星电子",
                        "sanxingdianzi",
                        "sxdz",
                        ["Samsung", "삼성전자"],
                        "KR",
                        "stock",
                        True,
                        100,
                        "Samsung Electronics Co. Ltd.",
                        "삼성전자",
                    ]],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.object(stock_index_loader, "get_stock_index_candidate_paths", return_value=(index_path,)):
                self.assertEqual(stock_index_loader.get_index_stock_name("005930.KS"), "三星电子")
                self.assertEqual(stock_index_loader.get_index_stock_name("005930.KS", language="zh"), "三星电子")
                self.assertEqual(stock_index_loader.get_index_stock_name("005930.KS", language="en"), "Samsung Electronics Co. Ltd.")
                self.assertEqual(stock_index_loader.get_index_stock_name("005930.KS", language="ko"), "삼성전자")
```

- [ ] **Step 2: Run the loader test to verify it fails**

Run:

```bash
uv run pytest tests/test_stock_index_loader.py::TestStockIndexLoader::test_get_index_stock_name_selects_localized_name_by_language -q
```

Expected: fail because `get_index_stock_name()` does not accept `language`.

- [ ] **Step 3: Store localized index records in backend loader**

In `src/data/stock_index_loader.py`, add:

```python
_STOCK_INDEX_RECORD_CACHE: Dict[str, dict[str, str]] | None = None
```

Add helper:

```python
def _select_localized_name(record: dict[str, str], language: Optional[str]) -> Optional[str]:
    normalized_language = str(language or "zh").strip().lower()
    if normalized_language.startswith("ko"):
        return record.get("nameKo") or record.get("nameEn") or record.get("nameZh")
    if normalized_language.startswith("en"):
        return record.get("nameEn") or record.get("nameZh")
    return record.get("nameZh")
```

Add builder:

```python
def _build_stock_name_record_map(raw_items: list) -> Dict[str, dict[str, str]]:
    record_map: Dict[str, dict[str, str]] = {}
    for item in raw_items:
        if not isinstance(item, list) or len(item) < 3:
            continue

        canonical_code, display_code, name_zh = item[0], item[1], item[2]
        if not is_meaningful_stock_name(name_zh, str(display_code or canonical_code or "")):
            continue

        record = {
            "nameZh": str(name_zh).strip(),
            "nameEn": str(item[10]).strip() if len(item) > 10 and item[10] else "",
            "nameKo": str(item[11]).strip() if len(item) > 11 and item[11] else "",
        }
        for key in _build_lookup_keys(str(canonical_code or ""), str(display_code or "")):
            record_map[key] = record
    return record_map
```

Add `get_stock_name_index_record_map()` using the same candidate iteration as `get_stock_name_index_map()`. It should call `_build_stock_name_record_map(raw_items)` and cache the result in `_STOCK_INDEX_RECORD_CACHE`.

Update `clear_stock_index_cache()` to reset `_STOCK_INDEX_RECORD_CACHE`.

- [ ] **Step 4: Update `get_index_stock_name` signature**

Replace:

```python
def get_index_stock_name(stock_code: str) -> str | None:
```

with:

```python
def get_index_stock_name(stock_code: str, language: Optional[str] = None) -> str | None:
```

Use the record map when `language` is set:

```python
    if language:
        record_map = get_stock_name_index_record_map()
        for key in _build_lookup_keys(code, code):
            record = record_map.get(key)
            if not record:
                continue
            name = _select_localized_name(record, language)
            if is_meaningful_stock_name(name, code):
                return name
        return None
```

Keep the existing `stock_name_map` fallback for `language is None`.

- [ ] **Step 5: Pass report language through backend name lookup**

In `data_provider/base.py`, change:

```python
    def get_stock_name(self, stock_code: str, allow_realtime: bool = True) -> Optional[str]:
```

to:

```python
    def get_stock_name(
        self,
        stock_code: str,
        allow_realtime: bool = True,
        report_language: Optional[str] = None,
    ) -> Optional[str]:
```

Change:

```python
        index_name = get_index_stock_name(stock_code)
```

to:

```python
        index_name = get_index_stock_name(stock_code, language=report_language)
```

In `src/core/pipeline.py`, change:

```python
            stock_name = self.fetcher_manager.get_stock_name(code, allow_realtime=False)
```

to:

```python
            stock_name = self.fetcher_manager.get_stock_name(
                code,
                allow_realtime=False,
                report_language=report_language,
            )
```

Keep `prefetch_stock_names()` unchanged or pass no language so existing cache behavior remains compatible.

- [ ] **Step 6: Add report language fallback for stored Chinese names**

Add this test to `tests/test_report_language.py`:

```python
    def test_get_localized_stock_name_uses_korean_index_name_for_korean_report(self) -> None:
        with unittest.mock.patch(
            "src.report_language.get_index_stock_name",
            side_effect=lambda code, language=None: "삼성전자" if code == "005930.KS" and language == "ko" else None,
        ):
            self.assertEqual(
                get_localized_stock_name("三星电子", "005930.KS", "ko"),
                "삼성전자",
            )
```

Add import at the top of `tests/test_report_language.py`:

```python
from unittest import mock
```

Or use `unittest.mock.patch` after importing `unittest` as already present.

In `src/report_language.py`, import:

```python
from src.data.stock_index_loader import get_index_stock_name
```

Then update `get_localized_stock_name`:

```python
def get_localized_stock_name(value: Any, code: Any, language: Optional[str]) -> str:
    """Return a localized stock name when the index has one, else keep existing value."""
    raw_text = str(value or "").strip()
    normalized_language = normalize_report_language(language)
    localized_index_name = get_index_stock_name(str(code or ""), language=normalized_language)

    if localized_index_name and normalized_language != "zh":
        zh_index_name = get_index_stock_name(str(code or ""), language="zh")
        if not raw_text or raw_text == zh_index_name or _is_placeholder_stock_name(raw_text, code):
            return localized_index_name

    if not _is_placeholder_stock_name(raw_text, code):
        return raw_text
    return _GENERIC_STOCK_NAME_BY_LANGUAGE[normalized_language]
```

- [ ] **Step 7: Run backend tests**

Run:

```bash
uv run pytest tests/test_stock_index_loader.py tests/test_report_language.py -q
```

Expected: pass.

- [ ] **Step 8: Commit**

Do not commit unless the user explicitly approves. If approved, use:

```bash
git add src/data/stock_index_loader.py data_provider/base.py src/core/pipeline.py src/report_language.py tests/test_stock_index_loader.py tests/test_report_language.py
git commit -m "fix: localize stock names by report language"
```

---

### Task 4: Localize Frontend Autocomplete Display and Submitted Stock Name

**Files:**
- Modify: `apps/dsa-web/src/types/stockIndex.ts`
- Modify: `apps/dsa-web/src/utils/stockIndexFields.ts`
- Modify: `apps/dsa-web/src/utils/stockIndexLoader.ts`
- Create: `apps/dsa-web/src/utils/stockDisplayName.ts`
- Modify: `apps/dsa-web/src/utils/searchStocks.ts`
- Modify: `apps/dsa-web/src/hooks/useAutocomplete.ts`
- Modify: `apps/dsa-web/src/components/StockAutocomplete/StockAutocomplete.tsx`
- Modify: `apps/dsa-web/src/components/StockAutocomplete/SuggestionsList.tsx`
- Test: `apps/dsa-web/src/utils/__tests__/stockIndexLoader.test.ts`
- Test: `apps/dsa-web/src/utils/__tests__/searchStocks.test.ts`
- Test: `apps/dsa-web/src/components/StockAutocomplete/__tests__/StockAutocomplete.test.tsx`

- [ ] **Step 1: Write failing stock index loader test**

In `apps/dsa-web/src/utils/__tests__/stockIndexLoader.test.ts`, add a compressed tuple case:

```ts
it('unpacks optional localized stock names from compressed tuples', async () => {
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => [[
      '005930.KS',
      '005930.KS',
      '三星电子',
      'sanxingdianzi',
      'sxdz',
      ['Samsung', '삼성전자'],
      'KR',
      'stock',
      true,
      100,
      'Samsung Electronics Co. Ltd.',
      '삼성전자',
    ]],
  });

  const result = await loadStockIndex();

  expect(result.loaded).toBe(true);
  expect(result.data[0].nameZh).toBe('三星电子');
  expect(result.data[0].nameEn).toBe('Samsung Electronics Co. Ltd.');
  expect(result.data[0].nameKo).toBe('삼성전자');
});
```

- [ ] **Step 2: Write failing search display test**

In `apps/dsa-web/src/utils/__tests__/searchStocks.test.ts`, add:

```ts
it('uses Korean display name when requested while still matching Chinese and Korean aliases', () => {
  const index = [{
    canonicalCode: '005930.KS',
    displayCode: '005930.KS',
    nameZh: '三星电子',
    nameEn: 'Samsung Electronics Co. Ltd.',
    nameKo: '삼성전자',
    pinyinFull: 'sanxingdianzi',
    pinyinAbbr: 'sxdz',
    aliases: ['Samsung', '삼성전자'],
    market: 'KR' as const,
    assetType: 'stock' as const,
    active: true,
    popularity: 100,
  }];

  expect(searchStocks('三星', index, { language: 'ko' })[0].displayName).toBe('삼성전자');
  expect(searchStocks('삼성', index, { language: 'ko' })[0].displayName).toBe('삼성전자');
  expect(searchStocks('Samsung', index, { language: 'en' })[0].displayName).toBe('Samsung Electronics Co. Ltd.');
});
```

This requires extending `SearchOptions` with `language?: UiLanguage | ReportLanguage | string`.

- [ ] **Step 3: Update TypeScript types and tuple fields**

In `apps/dsa-web/src/types/stockIndex.ts`, update `StockSuggestion`:

```ts
export interface StockSuggestion {
  canonicalCode: string;
  displayCode: string;
  nameZh: string;
  nameEn?: string;
  nameKo?: string;
  displayName: string;
  market: Market;
  matchType: 'exact' | 'prefix' | 'contains' | 'fuzzy';
  matchField: 'code' | 'name' | 'pinyin' | 'alias';
  score: number;
}
```

Update `StockIndexTuple` to append optional fields:

```ts
export type StockIndexTuple = [
  string,
  string,
  string,
  string | undefined,
  string | undefined,
  string[],
  Market,
  AssetType,
  boolean,
  number | undefined,
  string | undefined,
  string | undefined,
];
```

In `apps/dsa-web/src/utils/stockIndexFields.ts`, append field names and indices:

```ts
  'nameEn',
  'nameKo',
```

```ts
  NAME_EN: 10,
  NAME_KO: 11,
```

- [ ] **Step 4: Add display-name selector**

Create `apps/dsa-web/src/utils/stockDisplayName.ts`:

```ts
import type { StockIndexItem, StockSuggestion } from '../types/stockIndex';

export type StockNameLanguage = 'zh' | 'en' | 'ko' | string | undefined;

function nonEmpty(value: string | undefined | null): string | undefined {
  const normalized = value?.trim();
  return normalized || undefined;
}

export function getStockIndexDisplayName(
  item: Pick<StockIndexItem | StockSuggestion, 'nameZh' | 'nameEn' | 'nameKo' | 'displayCode'>,
  language: StockNameLanguage,
): string {
  const normalizedLanguage = (language || 'zh').toLowerCase();
  if (normalizedLanguage.startsWith('ko')) {
    return nonEmpty(item.nameKo) || nonEmpty(item.nameEn) || nonEmpty(item.nameZh) || item.displayCode;
  }
  if (normalizedLanguage.startsWith('en')) {
    return nonEmpty(item.nameEn) || nonEmpty(item.nameZh) || item.displayCode;
  }
  return nonEmpty(item.nameZh) || nonEmpty(item.nameEn) || nonEmpty(item.nameKo) || item.displayCode;
}
```

- [ ] **Step 5: Unpack optional fields**

In `apps/dsa-web/src/utils/stockIndexLoader.ts`, update `unpackTuples`:

```ts
    nameEn: tuple[INDEX_FIELD.NAME_EN],
    nameKo: tuple[INDEX_FIELD.NAME_KO],
```

This must be optional-safe because older tuples may not include these positions.

- [ ] **Step 6: Search all localized names**

In `apps/dsa-web/src/utils/searchStocks.ts`:

```ts
import { getStockIndexDisplayName, type StockNameLanguage } from './stockDisplayName';
```

Update `SearchOptions`:

```ts
  language?: StockNameLanguage;
```

When returning suggestions:

```ts
    nameEn: s.item.nameEn,
    nameKo: s.item.nameKo,
    displayName: getStockIndexDisplayName(s.item, options.language),
```

In `calculateMatchScore`, add:

```ts
  const normalizedNameEn = normalizeQuery(item.nameEn || '');
  const normalizedNameKo = normalizeQuery(item.nameKo || '');
```

Then add exact/prefix/contains checks:

```ts
  if (q === normalizedNameEn || q === normalizedNameKo) return 98;
  if (normalizedNameEn.startsWith(q) || normalizedNameKo.startsWith(q)) score = Math.max(score, 79);
  if (normalizedNameEn.includes(q) || normalizedNameKo.includes(q)) score = Math.max(score, 59);
```

In `determineMatchField`, treat `nameEn` and `nameKo` as `name`.

- [ ] **Step 7: Pass UI language through autocomplete**

In `apps/dsa-web/src/hooks/useAutocomplete.ts`, extend options:

```ts
  language?: string;
```

Pass it to search:

```ts
      const results = searchStocks(q, index, { limit, language: options.language });
```

Include `options.language` or destructured `language` in the `useCallback` dependency list.

In `apps/dsa-web/src/components/StockAutocomplete/StockAutocomplete.tsx`, import:

```ts
import { useUiLanguage } from '../../contexts/UiLanguageContext';
```

Inside `StockAutocompleteInner`:

```ts
  const { language } = useUiLanguage();
```

Call:

```ts
  } = useAutocomplete(index, { language });
```

Replace both `onSubmit(..., selected.nameZh, ...)` and `onSubmit(..., s.nameZh, ...)` with:

```ts
onSubmit(selected.canonicalCode, selected.displayName, 'autocomplete');
```

and:

```ts
onSubmit(s.canonicalCode, s.displayName, 'autocomplete');
```

- [ ] **Step 8: Render display name in suggestions**

In `apps/dsa-web/src/components/StockAutocomplete/SuggestionsList.tsx`, replace:

```tsx
{suggestion.nameZh}
```

with:

```tsx
{suggestion.displayName}
```

- [ ] **Step 9: Run frontend focused tests**

Run:

```bash
cd apps/dsa-web
npm run test -- stockIndexLoader searchStocks StockAutocomplete
```

If this repo does not support filtered `npm run test -- ...`, run:

```bash
cd apps/dsa-web
npm run test
```

Expected: pass.

- [ ] **Step 10: Commit**

Do not commit unless the user explicitly approves. If approved, use:

```bash
git add apps/dsa-web/src/types/stockIndex.ts apps/dsa-web/src/utils/stockIndexFields.ts apps/dsa-web/src/utils/stockIndexLoader.ts apps/dsa-web/src/utils/stockDisplayName.ts apps/dsa-web/src/utils/searchStocks.ts apps/dsa-web/src/hooks/useAutocomplete.ts apps/dsa-web/src/components/StockAutocomplete/StockAutocomplete.tsx apps/dsa-web/src/components/StockAutocomplete/SuggestionsList.tsx apps/dsa-web/src/utils/__tests__/stockIndexLoader.test.ts apps/dsa-web/src/utils/__tests__/searchStocks.test.ts apps/dsa-web/src/components/StockAutocomplete/__tests__/StockAutocomplete.test.tsx
git commit -m "fix: localize stock autocomplete names"
```

---

### Task 5: Regenerate Index Assets and Update Fixtures

**Files:**
- Modify: `apps/dsa-web/public/stocks.index.json`
- Modify: `static/stocks.index.json`
- Modify: `data/cache/stocks.index.json` only if it is intentionally tracked in this repo
- Modify: tests that hard-code Korean Chinese names

- [ ] **Step 1: Regenerate index assets**

Run:

```bash
uv run python scripts/generate_index_from_csv.py --source tushare
cp apps/dsa-web/public/stocks.index.json static/stocks.index.json
```

Expected:

```text
生成完成
```

- [ ] **Step 2: Verify generated Korean rows**

Run:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

for path in [Path("apps/dsa-web/public/stocks.index.json"), Path("static/stocks.index.json")]:
    data = json.loads(path.read_text(encoding="utf-8"))
    row = next(item for item in data if item[0] == "005930.KS")
    print(path, row[:12])
    assert row[2] == "三星电子"
    assert row[10] == "Samsung Electronics Co. Ltd."
    assert row[11] == "삼성전자"
PY
```

Expected: both files print a 12-field Samsung row and assertions pass.

- [ ] **Step 3: Update tests that expected Chinese display for Korean UI paths**

Use:

```bash
rg -n "三星电子|SK海力士|LG新能源|现代汽车|005930\\.KS|000660\\.KS" tests apps/dsa-web/src -g '!node_modules'
```

Only update assertions that represent Korean/English localized display. Keep tests that explicitly validate `nameZh` as Chinese.

Examples:

```ts
expect(screen.getByText('삼성전자')).toBeInTheDocument();
expect(onSubmit).toHaveBeenCalledWith('000660.KS', 'SK하이닉스', 'autocomplete');
```

Backend examples:

```python
self.assertEqual(report.meta.stock_name, "삼성전자")
```

- [ ] **Step 4: Run asset consistency checks**

Run:

```bash
uv run pytest tests/test_static_assets_consistency.py tests/test_stock_index_loader.py tests/test_generate_index_from_csv.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

Do not commit unless the user explicitly approves. If approved, use:

```bash
git add apps/dsa-web/public/stocks.index.json static/stocks.index.json tests apps/dsa-web/src
git commit -m "chore: regenerate localized stock index"
```

---

### Task 6: Documentation, Changelog, and Final Verification

**Files:**
- Modify: `docs/market-support.md`
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Update market support docs**

In `docs/market-support.md`, add a note near the Korean stock support section:

```markdown
- 韩国股票索引保留中文、英文、韩文名称。中文界面继续显示中文名；英文界面优先显示英文名；韩文界面和韩文报告优先显示韩文名。例如 `005930.KS` 在韩文界面显示为 `삼성전자`。
```

If the English or Korean docs also mention JP/KR stock support, add equivalent notes:

```markdown
- Korean stock index entries keep Chinese, English, and Korean names. Korean UI and Korean reports prefer the Korean name, for example `005930.KS` displays as `삼성전자`.
```

```markdown
- 한국 주식 인덱스는 중국어, 영어, 한국어 종목명을 함께 보관합니다. 한국어 UI와 한국어 보고서는 한국어 종목명을 우선 사용하며, 예를 들어 `005930.KS`는 `삼성전자`로 표시됩니다.
```

- [ ] **Step 2: Update changelog**

Add one flat `[Unreleased]` entry to `docs/CHANGELOG.md`:

```markdown
- [修复] 韩股自动补全、分析提交和韩文报告优先使用韩文股票名称，避免 `005930.KS` 等韩国股票显示为中文名称。
```

Do not add a new heading inside `[Unreleased]`.

- [ ] **Step 3: Run backend verification**

Run:

```bash
uv run pytest tests/test_generate_index_from_csv.py tests/test_stock_index_loader.py tests/test_report_language.py tests/test_static_assets_consistency.py -q
```

Expected: pass.

- [ ] **Step 4: Run frontend verification**

Run:

```bash
cd apps/dsa-web
npm run lint
npm run build
```

Expected: both commands pass.

- [ ] **Step 5: Optional browser evidence for PR**

If this goes into a PR, start the web app and capture autocomplete before/after evidence because this is a user-visible UI change.

Run:

```bash
cd apps/dsa-web
npm run dev
```

Then in Korean UI mode:

1. Type `005930.KS` into stock autocomplete.
2. Confirm suggestion shows `삼성전자`, not `三星电子`.
3. Select it.
4. Confirm submitted/running task title uses `삼성전자`.

Do not commit screenshots. Attach them to PR description or PR comment.

- [ ] **Step 6: Final PR description checklist**

Include:

```markdown
## Changed
- Added optional English/Korean stock names to stock index generation.
- Localized Korean stock autocomplete display and submitted stock name.
- Localized backend stock name lookup for Korean reports.

## Why
- Korean seed data stored Chinese names in `nameZh`, and both frontend and backend used that field as the display name.

## Verification
- `uv run pytest tests/test_generate_index_from_csv.py tests/test_stock_index_loader.py tests/test_report_language.py tests/test_static_assets_consistency.py -q`
- `cd apps/dsa-web && npm run lint && npm run build`

## Risk
- Low-to-medium. Tuple format is extended only at the tail, so old 10-field readers remain compatible if they ignore extra fields.

## Rollback
- Revert the index generator, frontend display selector, backend language-aware lookup, and regenerated `stocks.index.json` files.
```

- [ ] **Step 7: Commit**

Do not commit unless the user explicitly approves. If approved, use:

```bash
git add docs/market-support.md docs/CHANGELOG.md
git commit -m "docs: document localized Korean stock names"
```

---

## Verification Matrix

Minimum local verification before marking the fix ready:

```bash
uv run pytest tests/test_generate_index_from_csv.py tests/test_stock_index_loader.py tests/test_report_language.py tests/test_static_assets_consistency.py -q
cd apps/dsa-web && npm run lint && npm run build
```

Additional useful checks:

```bash
uv run python - <<'PY'
from src.data.stock_index_loader import clear_stock_index_cache, get_index_stock_name

clear_stock_index_cache()
assert get_index_stock_name("005930.KS", language="zh") == "三星电子"
assert get_index_stock_name("005930.KS", language="ko") == "삼성전자"
print("localized stock names ok")
PY
```

```bash
rg -n "\"005930.KS\".*\"三星电子\"|\"000660.KS\".*\"SK海力士\"" apps/dsa-web/src tests
```

Expected: remaining hits should be tests that intentionally validate `nameZh`, not Korean UI display.

## Rollback Plan

Revert these groups together:

1. `scripts/generate_index_from_csv.py` and `scripts/stock_index_seeds/stock_list_kr.csv`
2. `src/data/stock_index_loader.py`, `data_provider/base.py`, `src/core/pipeline.py`, `src/report_language.py`
3. `apps/dsa-web/src/types/stockIndex.ts`, `stockIndexFields.ts`, `stockIndexLoader.ts`, `searchStocks.ts`, `useAutocomplete.ts`, `StockAutocomplete.tsx`, `SuggestionsList.tsx`, `stockDisplayName.ts`
4. Regenerated `apps/dsa-web/public/stocks.index.json` and `static/stocks.index.json`
5. Test and docs updates

Because tuple fields are appended, a partial rollback of only regenerated index files is usually safe, but do not leave backend/frontend expecting `nameKo` while generated assets omit it unless tests confirm optional fallback still passes.

## Open Decisions

- English UI for Korean stocks should prefer `enname`; this plan implements that. If maintainers want native Korean names for both English and Korean UI, change `getStockIndexDisplayName()` and `_select_localized_name()` fallback order before implementation.
- Japanese stock names currently have a similar Chinese/native-name ambiguity. This plan only fixes Korean stocks because that is the reported bug. Apply the same `name_ja` pattern later if needed.

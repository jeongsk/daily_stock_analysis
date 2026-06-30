/**
 * useAutocomplete hook tests.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useAutocomplete } from '../useAutocomplete';
import type { StockIndexItem } from '../../types/stockIndex';

const searchStocksMock = vi.fn();

vi.mock('../../utils/searchStocks', () => ({
  searchStocks: (...args: unknown[]) => searchStocksMock(...args),
}));

const mockIndex: StockIndexItem[] = [
  {
    canonicalCode: '600519.SH',
    displayCode: '600519',
    nameZh: '贵州茅台',
    pinyinFull: 'guizhoumaotai',
    pinyinAbbr: 'gzmt',
    aliases: ['茅台'],
    market: 'CN',
    assetType: 'stock',
    active: true,
    popularity: 100,
  },
];

describe('useAutocomplete', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('activates runtime fallback when search throws', () => {
    searchStocksMock.mockImplementation(() => {
      throw new Error('Search exploded');
    });

    const { result } = renderHook(() => useAutocomplete(mockIndex, { debounceMs: 10 }));

    act(() => {
      result.current.setQuery('600519');
    });

    act(() => {
      vi.advanceTimersByTime(10);
    });

    expect(result.current.runtimeFallback).toBe(true);
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.isOpen).toBe(false);
    expect(result.current.suggestions).toEqual([]);
  });

  it('keeps suggestions open without auto-highlighting the first result', () => {
    searchStocksMock.mockReturnValue([
      {
        canonicalCode: '600519.SH',
        displayCode: '600519',
        nameZh: '贵州茅台',
        displayName: '贵州茅台',
        market: 'CN',
        matchType: 'exact',
        matchField: 'code',
        score: 100,
      },
    ]);

    const { result } = renderHook(() => useAutocomplete(mockIndex, { debounceMs: 10 }));

    act(() => {
      result.current.setQuery('600519');
    });

    act(() => {
      vi.advanceTimersByTime(10);
    });

    expect(result.current.isOpen).toBe(true);
    expect(result.current.suggestions).toHaveLength(1);
    expect(result.current.highlightedIndex).toBe(-1);
  });

  it('updates suggestion display names immediately when language changes', () => {
    searchStocksMock.mockImplementation((_query, _index, options) => [
      {
        canonicalCode: '005930.KS',
        displayCode: '005930.KS',
        nameZh: '三星电子',
        nameKo: '삼성전자',
        displayName: options.language === 'ko' ? '삼성전자' : '三星电子',
        market: 'KR',
        matchType: 'exact',
        matchField: 'code',
        score: 99,
      },
    ]);

    const { result, rerender } = renderHook(
      ({ language }) => useAutocomplete(mockIndex, { debounceMs: 10, language }),
      { initialProps: { language: 'zh' } },
    );

    act(() => {
      result.current.setQuery('005930');
    });

    act(() => {
      vi.advanceTimersByTime(10);
    });

    expect(result.current.suggestions[0].displayName).toBe('三星电子');

    act(() => {
      rerender({ language: 'ko' });
    });

    expect(result.current.suggestions[0].displayName).toBe('삼성전자');
    expect(searchStocksMock).toHaveBeenCalledTimes(1);
  });
});

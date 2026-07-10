import { useCallback, useEffect, useRef, useState } from 'react';
import { systemConfigApi } from '../api/systemConfig';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiLanguage } from '../i18n/uiText';
import { findMatchingStockCode, includesStockCode } from '../utils/stockCode';

const WATCHLIST_TEXT: Record<UiLanguage, {
  added: string;
  removed: string;
  actionFailed: string;
}> = {
  zh: { added: '已加入自选 {stockCode}', removed: '已从自选移除 {stockCode}', actionFailed: '操作失败' },
  en: { added: 'Added {stockCode} to watchlist', removed: 'Removed {stockCode} from watchlist', actionFailed: 'Operation failed' },
  ko: { added: '관심 종목에 {stockCode}을(를) 추가했습니다', removed: '관심 종목에서 {stockCode}을(를) 제거했습니다', actionFailed: '작업에 실패했습니다' },
};

export interface UseWatchlistReturn {
  watchlistCodes: string[];
  isLoading: boolean;
  isActioning: boolean;
  actionMessage: string | null;
  isInWatchlist: (stockCode: string) => boolean;
  addToWatchlist: (stockCode: string) => Promise<void>;
  removeFromWatchlist: (stockCode: string) => Promise<void>;
  toggleWatchlist: (stockCode: string) => Promise<void>;
  refresh: () => Promise<void>;
}

export function useWatchlist(): UseWatchlistReturn {
  const { language } = useUiLanguage();
  const textRef = useRef(WATCHLIST_TEXT[language] ?? WATCHLIST_TEXT.zh);
  textRef.current = WATCHLIST_TEXT[language] ?? WATCHLIST_TEXT.zh;
  const [codes, setCodes] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isActioning, setIsActioning] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const messageTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (messageTimerRef.current !== null) {
        window.clearTimeout(messageTimerRef.current);
      }
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const result = await systemConfigApi.getWatchlist();
      if (mountedRef.current) {
        setCodes(result);
      }
    } catch {
      // keep existing codes
    }
  }, []);

  useEffect(() => {
    setIsLoading(true);
    void refresh().finally(() => {
      if (mountedRef.current) {
        setIsLoading(false);
      }
    });
  }, [refresh]);

  const showMessage = useCallback((msg: string) => {
    if (messageTimerRef.current !== null) {
      window.clearTimeout(messageTimerRef.current);
    }
    setActionMessage(msg);
    messageTimerRef.current = window.setTimeout(() => {
      if (mountedRef.current) {
        setActionMessage(null);
      }
    }, 3000);
  }, []);

  const isInWatchlist = useCallback(
    (stockCode: string) => includesStockCode(codes, stockCode),
    [codes],
  );

  const addToWatchlist = useCallback(async (stockCode: string) => {
    if (!stockCode || isActioning) return;
    setIsActioning(true);
    try {
      const result = await systemConfigApi.addToWatchlist(stockCode);
      if (mountedRef.current) {
        setCodes(result);
        showMessage(textRef.current.added.replace('{stockCode}', stockCode));
      }
    } catch {
      if (mountedRef.current) showMessage(textRef.current.actionFailed);
    } finally {
      if (mountedRef.current) setIsActioning(false);
    }
  }, [isActioning, showMessage]);

  const removeFromWatchlist = useCallback(async (stockCode: string) => {
    if (!stockCode || isActioning) return;
    setIsActioning(true);
    try {
      const result = await systemConfigApi.removeFromWatchlist(stockCode);
      if (mountedRef.current) {
        setCodes(result);
        showMessage(textRef.current.removed.replace('{stockCode}', stockCode));
      }
    } catch {
      if (mountedRef.current) showMessage(textRef.current.actionFailed);
    } finally {
      if (mountedRef.current) setIsActioning(false);
    }
  }, [isActioning, showMessage]);

  const toggleWatchlist = useCallback(async (stockCode: string) => {
    const existingStockCode = findMatchingStockCode(codes, stockCode);
    if (existingStockCode) {
      await removeFromWatchlist(existingStockCode);
    } else {
      await addToWatchlist(stockCode);
    }
  }, [codes, removeFromWatchlist, addToWatchlist]);

  return {
    watchlistCodes: codes,
    isLoading,
    isActioning,
    actionMessage,
    isInWatchlist,
    addToWatchlist,
    removeFromWatchlist,
    toggleWatchlist,
    refresh,
  };
}

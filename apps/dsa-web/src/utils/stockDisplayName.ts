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

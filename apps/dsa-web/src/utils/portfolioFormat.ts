import type {
  PortfolioCashDirection,
  PortfolioCorporateActionType,
  PortfolioFxRefreshResponse,
  PortfolioImportCommitResponse,
  PortfolioImportParseResponse,
  PortfolioPositionItem,
  PortfolioSide,
} from '../types/portfolio';
import type { UiLanguage } from '../i18n/uiText';
import { toDateInputValue } from './format';

const POSITION_PRICE_TEXT: Record<UiLanguage, {
  missing: string;
  realtime: string;
  close: string;
  unknownSource: string;
}> = {
  zh: { missing: '缺价', realtime: '实时价', close: '收盘价', unknownSource: '未知来源' },
  en: { missing: 'No price', realtime: 'Realtime', close: 'Close', unknownSource: 'Unknown source' },
  ko: { missing: '가격 없음', realtime: '실시간가', close: '종가', unknownSource: '알 수 없는 출처' },
};

const FX_FEEDBACK_TEXT: Record<UiLanguage, {
  refreshDisabled: string;
  noPairs: string;
  refreshed: string;
  summary: string;
  partialStale: string;
  partialFailed: string;
}> = {
  zh: {
    refreshDisabled: '汇率在线刷新已被禁用。',
    noPairs: '当前范围无可刷新的汇率对。',
    refreshed: '汇率已刷新，共更新 {updated} 对。',
    summary: '更新 {updated} 对，仍过期 {stale} 对，失败 {error} 对。',
    partialStale: '已尝试刷新，但仍有部分货币对使用 stale/fallback 汇率。{summary}',
    partialFailed: '在线刷新未完全成功。{summary}',
  },
  en: {
    refreshDisabled: 'Online FX refresh is disabled.',
    noPairs: 'No FX pairs to refresh in the current scope.',
    refreshed: 'FX rates refreshed; {updated} pairs updated.',
    summary: '{updated} updated, {stale} still stale, {error} failed.',
    partialStale: 'Refresh attempted, but some currency pairs still use stale/fallback rates. {summary}',
    partialFailed: 'Online refresh did not fully succeed. {summary}',
  },
  ko: {
    refreshDisabled: '환율 온라인 새로고침이 비활성화되어 있습니다.',
    noPairs: '현재 범위에 새로고침할 환율 쌍이 없습니다.',
    refreshed: '환율이 새로고침되었습니다. 총 {updated}쌍 갱신.',
    summary: '갱신 {updated}쌍, 만료 유지 {stale}쌍, 실패 {error}쌍.',
    partialStale: '새로고침을 시도했지만 일부 통화쌍은 여전히 stale/fallback 환율을 사용합니다. {summary}',
    partialFailed: '온라인 새로고침이 완전히 성공하지 못했습니다. {summary}',
  },
};

export type FxRefreshFeedback = {
  tone: 'neutral' | 'success' | 'warning';
  text: string;
};

export type PortfolioAlertVariant = 'info' | 'success' | 'warning' | 'danger';

export function getTodayIso(): string {
  return toDateInputValue(new Date());
}

// Currency -> money display fraction digits. KRW is a zero-decimal currency; others
// use 2. Mirror of the Python table (report/notification) documented in
// docs/superpowers/specs/2026-07-12-kr-portfolio-krw-design.md — keep both in sync.
const CURRENCY_FRACTION_DIGITS: Record<string, number> = {
  KRW: 0,
};
const DEFAULT_FRACTION_DIGITS = 2;

export function getCurrencyFractionDigits(currency: string | undefined | null): number {
  const key = (currency ?? '').toUpperCase();
  return CURRENCY_FRACTION_DIGITS[key] ?? DEFAULT_FRACTION_DIGITS;
}

export function formatMoney(value: number | undefined | null, currency = 'CNY'): string {
  if (value == null || Number.isNaN(value)) return '--';
  const digits = getCurrencyFractionDigits(currency);
  return `${currency} ${Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function formatPct(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  return `${value.toFixed(2)}%`;
}

export function formatSignedPct(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}%`;
}

export function hasPositionPrice(row: PortfolioPositionItem): boolean {
  return row.priceAvailable !== false && row.priceSource !== 'missing';
}

export function formatPositionPrice(row: PortfolioPositionItem): string {
  if (!hasPositionPrice(row)) return '--';
  return row.lastPrice.toFixed(4);
}

export function formatPositionMoney(value: number, row: PortfolioPositionItem): string {
  if (!hasPositionPrice(row)) return '--';
  return formatMoney(value, row.valuationCurrency);
}

export function getPositionPriceLabel(row: PortfolioPositionItem, language: UiLanguage = 'zh'): string {
  const text = POSITION_PRICE_TEXT[language] ?? POSITION_PRICE_TEXT.zh;
  if (!hasPositionPrice(row)) return text.missing;
  if (row.priceSource === 'realtime_quote') {
    return row.priceProvider ? `${text.realtime} · ${row.priceProvider}` : text.realtime;
  }
  if (row.priceSource === 'history_close') {
    return row.priceStale && row.priceDate ? `${text.close} · ${row.priceDate}` : text.close;
  }
  return row.priceSource || text.unknownSource;
}

export function formatSideLabel(value: PortfolioSide): string {
  return value === 'buy' ? '买入' : '卖出';
}

export function formatCashDirectionLabel(value: PortfolioCashDirection): string {
  return value === 'in' ? '流入' : '流出';
}

export function formatCorporateActionLabel(value: PortfolioCorporateActionType): string {
  return value === 'cash_dividend' ? '现金分红' : '拆并股调整';
}

export function formatBrokerLabel(value: string, displayName?: string): string {
  if (displayName && displayName.trim()) return `${value}（${displayName.trim()}）`;
  if (value === 'huatai') return 'huatai（华泰）';
  if (value === 'citic') return 'citic（中信）';
  if (value === 'cmb') return 'cmb（招商）';
  return value;
}

export function buildFxRefreshFeedback(data: PortfolioFxRefreshResponse, language: UiLanguage = 'zh'): FxRefreshFeedback {
  const text = FX_FEEDBACK_TEXT[language] ?? FX_FEEDBACK_TEXT.zh;
  if (data.refreshEnabled === false) {
    return {
      tone: 'neutral',
      text: text.refreshDisabled,
    };
  }

  if (data.pairCount === 0) {
    return {
      tone: 'neutral',
      text: text.noPairs,
    };
  }

  if (data.updatedCount > 0 && data.staleCount === 0 && data.errorCount === 0) {
    return {
      tone: 'success',
      text: text.refreshed.replace('{updated}', String(data.updatedCount)),
    };
  }

  const summary = text.summary
    .replace('{updated}', String(data.updatedCount))
    .replace('{stale}', String(data.staleCount))
    .replace('{error}', String(data.errorCount));
  if (data.staleCount > 0) {
    return {
      tone: 'warning',
      text: text.partialStale.replace('{summary}', summary),
    };
  }

  return {
    tone: 'warning',
    text: text.partialFailed.replace('{summary}', summary),
  };
}

export function getFxRefreshFeedbackVariant(tone: FxRefreshFeedback['tone']): PortfolioAlertVariant {
  if (tone === 'success') return 'success';
  if (tone === 'warning') return 'warning';
  return 'info';
}

export function getCsvParseVariant(result: PortfolioImportParseResponse): PortfolioAlertVariant {
  return result.errorCount > 0 || result.skippedCount > 0 ? 'warning' : 'info';
}

export function getCsvCommitVariant(result: PortfolioImportCommitResponse, isDryRun: boolean): PortfolioAlertVariant {
  if (isDryRun) return 'info';
  return result.failedCount > 0 || result.duplicateCount > 0 ? 'warning' : 'success';
}

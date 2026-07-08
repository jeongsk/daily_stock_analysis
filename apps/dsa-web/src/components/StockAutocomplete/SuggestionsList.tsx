/**
 * Stock search suggestion list.
 */

import type { CSSProperties } from 'react';
import type { StockSuggestion } from '../../types/stockIndex';
import { Badge } from '../common';
import { cn } from '../../utils/cn';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { SUGGESTION_BADGE_TEXT } from '../../locales/featureText';

export interface SuggestionsListProps {
  /** Suggestion list */
  suggestions: StockSuggestion[];
  /** Highlighted index */
  highlightedIndex: number;
  /** Selection callback */
  onSelect: (suggestion: StockSuggestion) => void;
  /** Mouse hover callback */
  onMouseEnter: (index: number) => void;
  /** Custom style (for Portal fixed positioning) */
  style?: CSSProperties;
}

export function SuggestionsList({
  suggestions,
  highlightedIndex,
  onSelect,
  onMouseEnter,
  style,
}: SuggestionsListProps) {
  if (suggestions.length === 0) {
    return null;
  }

  return (
    <ul
      id="suggestions-list"
      className="z-[100] border-x border-b rounded-b-lg rounded-t-none max-h-60 overflow-auto"
      style={{
        ...style,
        backgroundColor: 'hsl(var(--card) / 0.85)',
        borderColor: 'var(--border-accent)',
        boxShadow: '0 10px 25px -5px rgba(0, 0, 0, 0.3), 0 8px 10px -6px rgba(0, 0, 0, 0.3), -4px 0 15px -3px rgba(0, 0, 0, 0.2), 4px 0 15px -3px rgba(0, 0, 0, 0.2)',
      }}
      role="listbox"
    >
      {suggestions.map((suggestion, index) => (
        <li
          key={suggestion.canonicalCode}
          role="option"
          aria-selected={index === highlightedIndex}
          className={cn(
            'px-4 py-1 cursor-pointer flex items-center justify-between',
            'hover:bg-[var(--autocomplete-hover-bg)]/25',
            index === highlightedIndex && 'bg-[var(--autocomplete-hover-bg)]/25',
          )}
          onClick={() => onSelect(suggestion)}
          onMouseEnter={() => onMouseEnter(index)}
        >
          <div className="flex items-center gap-3">
            <MarketBadge market={suggestion.market} />

            <div className="flex flex-col">
              <span className="text-sm font-medium text-primary-text">
                {suggestion.displayName}
              </span>
              <span className="text-sm text-secondary-text">
                {suggestion.displayCode}
              </span>
            </div>
          </div>

          <MatchTypeBadge matchType={suggestion.matchType} />
        </li>
      ))}
    </ul>
  );
}

const MARKET_BADGE_CONFIG = {
  CN: { labelKey: 'cnMarket' as const, className: 'border-danger/25 bg-danger/10 text-danger' },
  HK: { labelKey: 'hkMarket' as const, className: 'border-success/25 bg-success/10 text-success' },
  US: { labelKey: 'usMarket' as const, className: 'border-cyan/25 bg-cyan/10 text-cyan' },
  JP: { labelKey: 'jpMarket' as const, className: 'border-indigo-500/25 bg-indigo-500/10 text-indigo-500' },
  KR: { labelKey: 'krMarket' as const, className: 'border-rose-500/25 bg-rose-500/10 text-rose-500' },
  INDEX: { labelKey: 'indexMarket' as const, className: 'border-purple/25 bg-purple/10 text-purple' },
  ETF: { labelKey: 'etfMarket' as const, className: 'border-warning/25 bg-warning/10 text-warning' },
  BSE: { labelKey: 'bseMarket' as const, className: 'border-orange-500/25 bg-orange-500/10 text-orange-500' },
} as const;

function MarketBadge({ market }: { market: string }) {
  const { language } = useUiLanguage();
  const badgeText = SUGGESTION_BADGE_TEXT[language];
  const config = MARKET_BADGE_CONFIG[market as keyof typeof MARKET_BADGE_CONFIG];

  if (!config) {
    throw new Error(`Unsupported market in stock suggestion: ${market}`);
  }

  return (
    <Badge variant="default" size="sm" className={cn('min-w-[3rem] justify-center shadow-none', config.className)}>
      {badgeText[config.labelKey]}
    </Badge>
  );
}

function MatchTypeBadge({ matchType }: { matchType: string }) {
  const { language } = useUiLanguage();
  const badgeText = SUGGESTION_BADGE_TEXT[language];
  const configMap = {
    exact: { labelKey: 'exact' as const, className: 'border-cyan/25 bg-cyan/10 text-cyan' },
    prefix: { labelKey: 'prefix' as const, className: 'border-purple/25 bg-purple/10 text-purple' },
    contains: { labelKey: 'contains' as const, className: 'border-warning/25 bg-warning/10 text-warning' },
    fuzzy: { labelKey: 'fuzzy' as const, className: 'border-border/55 bg-elevated/75 text-muted-text' },
  };

  const config = configMap[matchType as keyof typeof configMap] || configMap.fuzzy;

  return (
    <Badge variant="default" size="sm" className={cn('shrink-0 shadow-none', config.className)}>
      {badgeText[config.labelKey]}
    </Badge>
  );
}

export default SuggestionsList;

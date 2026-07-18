import type React from 'react';
import { useState, useEffect, useCallback } from 'react';
import type { ParsedApiError } from '../../api/error';
import { getParsedApiError } from '../../api/error';
import { ApiErrorAlert, Card } from '../common';
import { DashboardPanelHeader, DashboardStateBlock } from '../dashboard';
import { historyApi } from '../../api/history';
import type { NewsIntelItem, ReportLanguage } from '../../types/analysis';
import { getReportText, normalizeReportLanguage } from '../../utils/reportLanguage';

interface ReportNewsProps {
  recordId?: number;  // 分析历史记录主键 ID
  limit?: number;
  language?: ReportLanguage;
}

const NEWS_SOURCE_TEXT = {
  zh: {
    sourceLabel: '相关资讯/后续检索',
    sourceHint: '来源：报告页补充资讯；是否用于分析以输入数据块为准。',
    originalLabel: '原文',
    unavailableLabel: '翻译不可用',
    sourcePrefix: '来源',
    directSource: '检索',
    rssSource: 'RSS',
    newsnowSource: 'NewsNow',
    originalAria: '新闻原文',
    provenanceAria: '新闻来源',
  },
  en: {
    sourceLabel: 'Related news / follow-up retrieval',
    sourceHint: 'Source: supplemental report-page news; analysis input is shown in Input Blocks.',
    originalLabel: 'Original',
    unavailableLabel: 'Translation unavailable',
    sourcePrefix: 'Source',
    directSource: 'Search',
    rssSource: 'RSS',
    newsnowSource: 'NewsNow',
    originalAria: 'Original news text',
    provenanceAria: 'News source',
  },
  ko: {
    sourceLabel: '관련 뉴스 / 후속 검색',
    sourceHint: '출처: 보고서 페이지 보충 뉴스; 분석 입력은 입력 블록에 표시됩니다.',
    originalLabel: '원문',
    unavailableLabel: '번역 불가',
    sourcePrefix: '출처',
    directSource: '검색',
    rssSource: 'RSS',
    newsnowSource: 'NewsNow',
    originalAria: '뉴스 원문',
    provenanceAria: '뉴스 출처',
  },
} as const;

function displaySourceType(item: NewsIntelItem, sourceText: typeof NEWS_SOURCE_TEXT[ReportLanguage]): string {
  const raw = (item.sourceType || '').toLowerCase();
  if (raw === 'rss') return sourceText.rssSource;
  if (raw === 'newsnow') return sourceText.newsnowSource;
  if (raw === 'search') return sourceText.directSource;
  return raw.toUpperCase() || sourceText.sourcePrefix;
}

function provenanceLabel(item: NewsIntelItem, sourceText: typeof NEWS_SOURCE_TEXT[ReportLanguage]): string | null {
  const sourceName = (item.source || '').trim();
  if (item.provenance === 'pool') {
    const typeLabel = displaySourceType(item, sourceText);
    return sourceName ? `${typeLabel} · ${sourceName}` : typeLabel;
  }
  if (item.provenance === 'direct') {
    return sourceName && sourceName !== 'search'
      ? `${sourceText.directSource} · ${sourceName}`
      : sourceText.directSource;
  }
  return null;
}

/**
 * 资讯区组件 - 终端风格
 */
export const ReportNews: React.FC<ReportNewsProps> = ({ recordId, limit = 8, language = 'zh' }) => {
  const reportLanguage = normalizeReportLanguage(language);
  const text = getReportText(reportLanguage);
  const sourceText = NEWS_SOURCE_TEXT[reportLanguage];
  const [isLoading, setIsLoading] = useState(false);
  const [items, setItems] = useState<NewsIntelItem[]>([]);
  const [error, setError] = useState<ParsedApiError | null>(null);

  const fetchNews = useCallback(async () => {
    if (!recordId) return;
    setIsLoading(true);
    setError(null);

    try {
      const response = await historyApi.getNews(recordId, limit);
      setItems(response.items || []);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setIsLoading(false);
    }
  }, [recordId, limit]);

  useEffect(() => {
    setItems([]);
    setError(null);

    if (recordId) {
      fetchNews();
    }
  }, [recordId, fetchNews]);

  if (!recordId) {
    return null;
  }

  return (
    <Card variant="bordered" padding="md" className="home-panel-card">
      <DashboardPanelHeader
        eyebrow={text.newsFeed}
        title={text.relatedNews}
        actions={(
          <div className="flex items-center gap-2">
            {isLoading ? (
              <div className="home-spinner h-3.5 w-3.5 animate-spin border-2" aria-hidden="true" />
            ) : null}
            <span className="home-accent-chip px-2 py-0.5 text-xs text-muted-text">
              {sourceText.sourceLabel}
            </span>
            <button
              type="button"
              onClick={() => void fetchNews()}
              className="home-accent-link text-xs"
              aria-label={text.refresh}
            >
              {text.refresh}
            </button>
          </div>
        )}
      />
      <p className="mb-3 text-xs leading-5 text-muted-text">
        {sourceText.sourceHint}
      </p>

      {error && !isLoading && (
        <ApiErrorAlert
          error={error}
          actionLabel={text.retry}
          onAction={() => void fetchNews()}
          dismissLabel={text.dismiss}
        />
      )}

      {isLoading && !error && (
        <DashboardStateBlock
          compact
          loading
          title={text.loadingNews}
        />
      )}

      {!isLoading && !error && items.length === 0 && (
        <DashboardStateBlock
          compact
          title={text.noNews}
          description={text.noNewsDescription}
          icon={(
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 14l-7-7m0 0l-7 7m7-7v18" />
            </svg>
          )}
        />
      )}

      {!isLoading && !error && items.length > 0 && (
        <div className="space-y-3 text-left">
          {items.map((item, index) => {
            const isTranslated = item.translationStatus === 'translated';
            const isUnavailable = item.translationStatus === 'unavailable';
            const label = provenanceLabel(item, sourceText);
            return (
              <div
                key={`${item.title}-${index}`}
                className="home-subpanel home-news-item group p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0 text-left">
                    <div className="mb-2 flex flex-wrap items-center gap-2">
                      {label ? (
                        <span
                          className="home-accent-chip px-2 py-0.5 text-xs text-muted-text"
                          aria-label={`${sourceText.provenanceAria}: ${label}`}
                        >
                          {label}
                        </span>
                      ) : null}
                      {isUnavailable ? (
                        <span className="home-accent-chip px-2 py-0.5 text-xs text-muted-text">
                          {sourceText.unavailableLabel}
                        </span>
                      ) : null}
                    </div>
                    <p className="home-news-title text-sm font-medium leading-6 text-foreground text-left">
                      {item.title}
                    </p>
                    {item.snippet && (
                      <p className="home-news-snippet mt-2 text-sm leading-6 text-secondary-text text-left overflow-hidden [display:-webkit-box] [-webkit-line-clamp:3] [-webkit-box-orient:vertical]">
                        {item.snippet}
                      </p>
                    )}
                    {isTranslated && (item.originalTitle || item.originalSnippet) ? (
                      <div
                        className="mt-3 rounded-lg border border-dashed border-subtle-border/70 bg-surface/40 p-3 text-left text-xs leading-5 text-muted-text"
                        aria-label={sourceText.originalAria}
                      >
                        <p className="mb-1 font-medium text-secondary-text">{sourceText.originalLabel}</p>
                        {item.originalTitle ? <p>{item.originalTitle}</p> : null}
                        {item.originalSnippet ? <p className="mt-1">{item.originalSnippet}</p> : null}
                      </div>
                    ) : null}
                  </div>
                  {item.url && (
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="home-accent-pill-link shrink-0 whitespace-nowrap px-2.5 py-1 text-xs"
                      aria-label={text.openLink}
                    >
                      {text.openLink}
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={2}
                          d="M14 3h7m0 0v7m0-7L10 14"
                        />
                      </svg>
                    </a>
                  )}
                </div>
              </div>
            );
          })}

        </div>
      )}
    </Card>
  );
};

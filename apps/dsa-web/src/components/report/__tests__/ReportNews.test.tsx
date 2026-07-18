import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { historyApi } from '../../../api/history';
import { ReportNews } from '../ReportNews';

vi.mock('../../../api/history', () => ({
  historyApi: {
    getNews: vi.fn(),
  },
}));

describe('ReportNews', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders news items and refreshes with preserved subpanel styling', async () => {
    vi.mocked(historyApi.getNews).mockResolvedValue({
      total: 1,
      items: [
        {
          title: '茅台发布最新经营数据',
          snippet: '公司披露季度经营情况，市场关注度提升。',
          url: 'https://example.com/news',
        },
      ],
    });

    const { container } = render(<ReportNews recordId={1} />);

    expect(await screen.findByText('茅台发布最新经营数据')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '跳转' })).toHaveAttribute('href', 'https://example.com/news');
    expect(screen.getByText('相关资讯/后续检索')).toBeVisible();
    expect(screen.getByText('来源：报告页补充资讯；是否用于分析以输入数据块为准。')).toBeVisible();
    expect(container.querySelector('.home-panel-card')).toBeTruthy();
    expect(container.querySelector('.home-subpanel')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '刷新' }));

    await waitFor(() => {
      expect(historyApi.getNews).toHaveBeenCalledTimes(2);
    });
  });

  it('renders the empty state when no news exists', async () => {
    vi.mocked(historyApi.getNews).mockResolvedValue({
      total: 0,
      items: [],
    });

    render(<ReportNews recordId={1} />);

    expect(await screen.findByText('暂无相关资讯')).toBeInTheDocument();
    expect(screen.getByText('可稍后刷新以获取最新资讯。')).toBeInTheDocument();
  });

  it('localizes the empty state description for english reports', async () => {
    vi.mocked(historyApi.getNews).mockResolvedValue({
      total: 0,
      items: [],
    });

    render(<ReportNews recordId={1} language="en" />);

    expect(await screen.findByText('No related news')).toBeInTheDocument();
    expect(screen.getByText('Refresh later to check for the latest updates.')).toBeInTheDocument();
    expect(screen.getByText('Related news / follow-up retrieval')).toBeVisible();
  });

  it('renders the error state and supports retry', async () => {
    vi.mocked(historyApi.getNews)
      .mockRejectedValueOnce(new Error('network failed'))
      .mockResolvedValueOnce({
        total: 1,
        items: [
          {
            title: '重试成功',
            snippet: '第二次请求成功返回。',
            url: 'https://example.com/retry',
          },
        ],
      });

    render(<ReportNews recordId={1} />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '重试' }));

    expect(await screen.findByText('重试成功')).toBeInTheDocument();
  });

  it('renders Korean translated news first with original text and pool provenance', async () => {
    vi.mocked(historyApi.getNews).mockResolvedValue({
      total: 1,
      items: [
        {
          title: '연준 금리 인하',
          snippet: '시장이 상승했습니다.',
          url: 'https://example.com/fed',
          originalTitle: 'Fed cuts rates',
          originalSnippet: 'Markets rally.',
          translationStatus: 'translated',
          sourceLanguage: 'en',
          provenance: 'pool',
          sourceType: 'rss',
          source: 'Federal Reserve All Press Releases',
        },
      ],
    });

    render(<ReportNews recordId={1} language="ko" />);

    expect(await screen.findByText('연준 금리 인하')).toBeInTheDocument();
    expect(screen.getByText('Fed cuts rates')).toBeInTheDocument();
    expect(screen.getByText('RSS · Federal Reserve All Press Releases')).toBeVisible();
    expect(screen.getByLabelText('뉴스 원문')).toBeInTheDocument();
  });

  it('renders unavailable badge and keeps original text', async () => {
    vi.mocked(historyApi.getNews).mockResolvedValue({
      total: 1,
      items: [
        {
          title: 'Fed cuts rates',
          snippet: 'Markets rally.',
          url: 'https://example.com/fed',
          translationStatus: 'unavailable',
          sourceLanguage: 'en',
          provenance: 'pool',
          sourceType: 'newsnow',
          source: 'cls-hot',
        },
      ],
    });

    render(<ReportNews recordId={1} language="ko" />);

    expect(await screen.findByText('Fed cuts rates')).toBeInTheDocument();
    expect(screen.getByText('번역 불가')).toBeVisible();
    expect(screen.getByText('NewsNow · cls-hot')).toBeVisible();
    expect(screen.queryByText('원문')).not.toBeInTheDocument();
  });

  it('keeps skipped or legacy responses as a single block', async () => {
    vi.mocked(historyApi.getNews).mockResolvedValue({
      total: 1,
      items: [
        {
          title: 'Legacy title',
          snippet: 'Legacy snippet',
          url: 'https://example.com/legacy',
          translationStatus: 'skipped',
          provenance: 'direct',
          sourceType: 'search',
        },
      ],
    });

    render(<ReportNews recordId={1} language="en" />);

    expect(await screen.findByText('Legacy title')).toBeInTheDocument();
    expect(screen.getByText('Search')).toBeVisible();
    expect(screen.queryByText('Original')).not.toBeInTheDocument();
  });
});

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { useUiLanguage, UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';
import { NotificationTestPanel } from '../NotificationTestPanel';

const testNotificationChannel = vi.hoisted(() => vi.fn());

vi.mock('../../../api/systemConfig', () => ({
  systemConfigApi: {
    testNotificationChannel,
  },
}));

describe('NotificationTestPanel', () => {
  beforeEach(() => {
    localStorage.removeItem(UI_LANGUAGE_STORAGE_KEY);
    testNotificationChannel.mockReset();
    testNotificationChannel.mockResolvedValue({
      success: true,
      message: 'ok',
      errorCode: null,
      stage: 'notification_send',
      retryable: false,
      latencyMs: 12,
      attempts: [
        {
          channel: 'custom',
          success: true,
          message: 'sent',
          target: 'https://example.com/hook?token=***',
          errorCode: null,
          stage: 'notification_send',
          retryable: false,
          latencyMs: 12,
          httpStatus: 200,
        },
      ],
    });
  });

  it('submits draft notification items and renders attempt details', async () => {
    render(
      <NotificationTestPanel
        items={[{ key: 'CUSTOM_WEBHOOK_URLS', value: 'https://example.com/hook?token=secret' }]}
        maskToken="******"
      />,
    );

    expect(screen.getByRole('option', { name: 'ntfy' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Gotify' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('渠道'), { target: { value: 'custom' } });
    fireEvent.click(screen.getByRole('button', { name: /发送测试/ }));

    await waitFor(() => expect(testNotificationChannel).toHaveBeenCalledWith(expect.objectContaining({
      channel: 'custom',
      items: [{ key: 'CUSTOM_WEBHOOK_URLS', value: 'https://example.com/hook?token=secret' }],
      maskToken: '******',
      timeoutSeconds: 20,
    })));
    expect(await screen.findByText('测试成功')).toBeInTheDocument();
    expect(screen.getByText('HTTP 200')).toBeInTheDocument();
    expect(screen.getByText('https://example.com/hook?token=***')).toBeInTheDocument();
  });

  it('localizes backend notification failure messages in Korean UI mode', async () => {
    localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'ko');
    testNotificationChannel.mockResolvedValueOnce({
      success: false,
      message: 'telegram 通知测试失败',
      errorCode: 'send_failed',
      stage: 'notification_send',
      retryable: false,
      latencyMs: 4792,
      attempts: [
        {
          channel: 'telegram',
          success: false,
          message: '通知测试发送失败',
          target: '***',
          errorCode: 'send_failed',
          stage: 'notification_send',
          retryable: false,
          latencyMs: 4792,
          httpStatus: null,
        },
      ],
    });

    render(
      <UiLanguageProvider>
        <NotificationTestPanel
          items={[{ key: 'TELEGRAM_CHAT_ID', value: '462899939' }]}
          maskToken="******"
        />
      </UiLanguageProvider>
    );

    fireEvent.change(screen.getByLabelText('채널'), { target: { value: 'telegram' } });
    fireEvent.click(screen.getByRole('button', { name: /테스트 전송/ }));

    expect(await screen.findByText('테스트 실패')).toBeInTheDocument();
    expect(screen.getByText(/Telegram 알림 테스트 실패 · 4792 ms · send_failed/)).toBeInTheDocument();
    expect(screen.getByText('시도 1')).toBeInTheDocument();
    expect(screen.getByText('알림 테스트 전송 실패')).toBeInTheDocument();
    expect(screen.queryByText(/通知测试/)).not.toBeInTheDocument();
  });

  it('uses translated defaults when UI language changes and user has not edited fields', async () => {
    localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');

    const SwitchHarness = ({ children }: { children: ReactNode }) => {
      const { setLanguage } = useUiLanguage();
      return (
        <div>
          <button type="button" onClick={() => setLanguage('en')}>
            switch-en
          </button>
          {children}
        </div>
      );
    };

    render(
      <UiLanguageProvider>
        <SwitchHarness>
          <NotificationTestPanel
            items={[{ key: 'CUSTOM_WEBHOOK_URLS', value: 'https://example.com/hook?token=secret' }]}
            maskToken="******"
          />
        </SwitchHarness>
      </UiLanguageProvider>
    );

    const titleInput = screen.getByLabelText('标题');
    const contentInput = screen.getByLabelText('正文');

    expect(titleInput).toHaveValue('DSA 通知测试');
    expect(contentInput).toHaveValue('这是一条来自 DSA Web 设置页的通知测试消息。');

    fireEvent.click(screen.getByRole('button', { name: 'switch-en' }));

    await waitFor(() => {
      expect(titleInput).toHaveValue('DSA notification test');
      expect(contentInput).toHaveValue('This is a test notification from the DSA Web settings page.');
    });

    fireEvent.click(screen.getByRole('button', { name: /发送测试|Send test/ }));
    await waitFor(() => expect(testNotificationChannel).toHaveBeenCalledWith(expect.objectContaining({
      title: 'DSA notification test',
      content: 'This is a test notification from the DSA Web settings page.',
      timeoutSeconds: 20,
    })));
  });

  it('preserves user-edited notification defaults when language switches', async () => {
    localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');

    const SwitchHarness = ({ children }: { children: ReactNode }) => {
      const { setLanguage } = useUiLanguage();
      return (
        <div>
          <button type="button" onClick={() => setLanguage('en')}>
            switch-en
          </button>
          {children}
        </div>
      );
    };

    render(
      <UiLanguageProvider>
        <SwitchHarness>
          <NotificationTestPanel
            items={[{ key: 'CUSTOM_WEBHOOK_URLS', value: 'https://example.com/hook?token=secret' }]}
            maskToken="******"
          />
        </SwitchHarness>
      </UiLanguageProvider>
    );

    const titleInput = screen.getByLabelText('标题');
    const contentInput = screen.getByLabelText('正文');

    fireEvent.change(titleInput, { target: { value: '自定义标题' } });
    fireEvent.change(contentInput, { target: { value: '自定义正文' } });

    fireEvent.click(screen.getByRole('button', { name: 'switch-en' }));
    expect(titleInput).toHaveValue('自定义标题');
    expect(contentInput).toHaveValue('自定义正文');
  });

  it('renders custom webhook partial failure attempts', async () => {
    testNotificationChannel.mockResolvedValueOnce({
      success: true,
      message: '自定义 Webhook 通知测试部分成功（1/2）',
      errorCode: null,
      stage: 'notification_send',
      retryable: true,
      latencyMs: 35,
      attempts: [
        {
          channel: 'custom',
          success: false,
          message: 'HTTP 500',
          target: 'https://example.com/hook?token=***',
          errorCode: 'http_500',
          stage: 'notification_send',
          retryable: true,
          latencyMs: 12,
          httpStatus: 500,
        },
        {
          channel: 'custom',
          success: true,
          message: 'sent',
          target: 'https://example.com/second/***',
          errorCode: null,
          stage: 'notification_send',
          retryable: false,
          latencyMs: 23,
          httpStatus: 200,
        },
      ],
    });

    render(
      <NotificationTestPanel
        items={[{ key: 'CUSTOM_WEBHOOK_URLS', value: 'https://example.com/hook?token=secret' }]}
        maskToken="******"
      />,
    );

    fireEvent.change(screen.getByLabelText('渠道'), { target: { value: 'custom' } });
    fireEvent.click(screen.getByRole('button', { name: /发送测试/ }));

    expect(await screen.findByText('测试成功')).toBeInTheDocument();
    expect(screen.getByText(/部分成功/)).toBeInTheDocument();
    expect(screen.getAllByText('HTTP 500').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('HTTP 200')).toBeInTheDocument();
    expect(screen.getByText('http_500')).toHaveClass('text-warning');
    expect(screen.getByText('https://example.com/hook?token=***')).toBeInTheDocument();
  });

  it('renders retryable timeout diagnostics', async () => {
    testNotificationChannel.mockResolvedValueOnce({
      success: false,
      message: '通知测试异常: timeout',
      errorCode: 'timeout',
      stage: 'notification_send',
      retryable: true,
      latencyMs: null,
      attempts: [
        {
          channel: 'wechat',
          success: false,
          message: 'timeout',
          target: 'https://qyapi.example.com/cgi-bin/webhook/send?key=***',
          errorCode: 'timeout',
          stage: 'notification_send',
          retryable: true,
          latencyMs: null,
          httpStatus: null,
        },
      ],
    });

    render(
      <NotificationTestPanel
        items={[{ key: 'WECHAT_WEBHOOK_URL', value: 'https://qyapi.example.com/cgi-bin/webhook/send?key=secret' }]}
        maskToken="******"
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /发送测试/ }));

    expect(await screen.findByText('测试失败')).toBeInTheDocument();
    const timeoutEntries = screen.getAllByText('timeout');
    expect(timeoutEntries[0]).toBeInTheDocument();
    expect(screen.getByText('https://qyapi.example.com/cgi-bin/webhook/send?key=***')).toBeInTheDocument();
    expect(timeoutEntries[0]).toHaveClass('text-warning');
  });
});

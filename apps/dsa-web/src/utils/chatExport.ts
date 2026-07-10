import type { Message } from '../stores/agentChatStore';
import type { UiLanguage } from '../i18n/uiText';

const CHAT_EXPORT_TEXT: Record<UiLanguage, {
  locale: string;
  title: string;
  generatedAt: string;
  user: string;
  assistant: string;
  filenamePrefix: string;
  skillSeparator: string;
}> = {
  zh: {
    locale: 'zh-CN',
    title: '问股会话',
    generatedAt: '生成时间',
    user: '用户',
    assistant: 'AI',
    filenamePrefix: '问股会话',
    skillSeparator: '、',
  },
  en: {
    locale: 'en-US',
    title: 'Stock chat session',
    generatedAt: 'Generated at',
    user: 'User',
    assistant: 'AI',
    filenamePrefix: 'stock_chat_session',
    skillSeparator: ', ',
  },
  ko: {
    locale: 'ko-KR',
    title: '종목 문의 세션',
    generatedAt: '생성 시간',
    user: '사용자',
    assistant: 'AI',
    filenamePrefix: '종목문의세션',
    skillSeparator: ', ',
  },
};

/**
 * Format chat messages as Markdown for export.
 */
export function formatSessionAsMarkdown(messages: Message[], language: UiLanguage = 'zh'): string {
  const text = CHAT_EXPORT_TEXT[language];
  const now = new Date();
  const timeStr = now.toLocaleString(text.locale, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });

  const lines: string[] = [
    `# ${text.title}`,
    '',
    `${text.generatedAt}: ${timeStr}`,
    '',
  ];

  for (const msg of messages) {
    const heading = msg.role === 'user' ? `## ${text.user}` : `## ${text.assistant}`;
    if (msg.role === 'assistant') {
      const skillLabel = msg.skillNames?.length
        ? msg.skillNames.join(text.skillSeparator)
        : msg.skillName;
      if (skillLabel) {
        lines.push(`${heading} (${skillLabel})`);
      } else {
        lines.push(heading);
      }
    } else {
      lines.push(heading);
    }
    lines.push('');
    lines.push(msg.content);
    lines.push('');
  }

  return lines.join('\n');
}

/**
 * Trigger browser download of session as .md file.
 * Revokes object URL after download to prevent memory leak.
 */
export function downloadSession(messages: Message[], language: UiLanguage = 'zh'): void {
  const text = CHAT_EXPORT_TEXT[language];
  const content = formatSessionAsMarkdown(messages, language);
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
  const now = new Date();
  const dateStr = now.toISOString().slice(0, 10).replace(/-/g, '');
  const pad = (n: number) => n.toString().padStart(2, '0');
  const timeStr = pad(now.getHours()) + pad(now.getMinutes());
  const filename = `${text.filenamePrefix}_${dateStr}_${timeStr}.md`;

  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

import { describe, expect, it } from 'vitest';
import { formatSessionAsMarkdown } from '../chatExport';
import type { Message } from '../../stores/agentChatStore';

const baseMessages: Message[] = [
  { id: '1', role: 'user', content: '분석 600519' },
  {
    id: '2',
    role: 'assistant',
    content: '趋势偏强',
    skillNames: ['趋势分析', '均线金叉'],
    skillName: '趋势分析、均线金叉',
  },
];

describe('formatSessionAsMarkdown', () => {
  it('joins multiple skill names with the Chinese separator by default', () => {
    const markdown = formatSessionAsMarkdown(baseMessages);

    expect(markdown).toContain('# 问股会话');
    expect(markdown).toContain('## 用户');
    expect(markdown).toContain('## AI (趋势分析、均线金叉)');
  });

  it('joins multiple skill names with the Korean separator for ko exports', () => {
    const messages: Message[] = [
      { id: '1', role: 'user', content: '삼성전자 분석' },
      {
        id: '2',
        role: 'assistant',
        content: '상승 추세',
        skillNames: ['추세 분석', '골든크로스'],
        skillName: '추세 분석, 골든크로스',
      },
    ];

    const markdown = formatSessionAsMarkdown(messages, 'ko');

    expect(markdown).toContain('# 종목 문의 세션');
    expect(markdown).toContain('## 사용자');
    expect(markdown).toContain('## AI (추세 분석, 골든크로스)');
  });

  it('falls back to skillName when skillNames is absent', () => {
    const messages: Message[] = [
      { id: '2', role: 'assistant', content: '결과', skillName: '추세 분석' },
    ];

    const markdown = formatSessionAsMarkdown(messages, 'ko');

    expect(markdown).toContain('## AI (추세 분석)');
  });

  it('renders a plain assistant heading when no skill label exists', () => {
    const messages: Message[] = [
      { id: '2', role: 'assistant', content: '결과' },
    ];

    const markdown = formatSessionAsMarkdown(messages, 'ko');

    expect(markdown).toContain('## AI\n');
    expect(markdown).not.toContain('## AI (');
  });
});

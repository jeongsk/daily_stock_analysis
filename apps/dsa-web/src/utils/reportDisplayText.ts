import type { DecisionAction, ReportLanguage } from '../types/analysis';
import { normalizeReportLanguage } from './reportLanguage';

const KO_EXACT_LABELS: Record<string, string> = {
  买入: '매수',
  加仓: '비중 확대',
  持有: '보유',
  减仓: '비중 축소',
  卖出: '매도',
  观望: '관망',
  等待: '대기',
  回避: '회피',
  预警: '경고',
  震荡: '박스권',
  行业: '업종',
  概念: '테마',
};

const KO_PHRASE_REPLACEMENTS: Array<[RegExp, string]> = [
  [/处于中长期多头趋势中/g, '는 중장기 상승 추세에 있고'],
  [/均线多头排列/g, '이동평균선은 상승 배열입니다'],
  [/今日放量下跌/g, '금일 거래량 증가와 함께 하락'],
  [/跌破/g, ' 하향 이탈'],
  [/显示短线抛压/g, '단기 매도 압력을 보여줍니다'],
  [/基本面优秀/g, '펀더멘털은 우수합니다'],
  [/业绩超预期/g, '실적이 예상치를 상회'],
  [/AI业务高增长/g, 'AI 사업 고성장'],
  [/新闻面偏正面/g, '뉴스 흐름은 대체로 긍정적입니다'],
  [/摩根士丹利看好/g, '모건스탠리가 긍정적으로 평가한'],
  [/但技术面/g, '다만 기술적 측면에서는'],
  [/放量下跌信号需警惕/g, '거래량 증가 하락 신호에 유의해야 합니다'],
  [/尚未出现企稳迹象/g, '아직 안정화 신호는 나타나지 않았습니다'],
  [/建议持有者继续持有并设好/g, '보유자는 계속 보유하되 '],
  [/空仓者等待/g, '미보유자는 '],
  [/后再介入/g, ' 후 진입을 기다리십시오'],
  [/理想入场位/g, '이상적 진입가'],
  [/次优入场位/g, '차선 진입가'],
  [/理想买入点/g, '이상적 매수 구간'],
  [/次优买入点/g, '차선 매수 구간'],
  [/止损位/g, '손절가'],
  [/止损/g, '손절'],
  [/目标位/g, '목표가'],
  [/区间/g, '구간'],
  [/缩量回踩/g, '거래량 감소 후 되돌림'],
  [/放量突破/g, '거래량 동반 돌파'],
  [/确认多头延续/g, '상승 추세 지속 확인'],
  [/且企稳/g, '및 안정화'],
  [/企稳/g, '안정화'],
  [/今日盘中低点下方/g, '금일 장중 저점 하단'],
  [/前期平台高点/g, '이전 박스권 고점'],
  [/先看/g, '1차 목표'],
  [/再看/g, '2차 목표'],
  [/多头趋势/g, '상승 추세'],
  [/多头/g, '상승'],
  [/短线/g, '단기'],
  [/抛压/g, '매도 압력'],
  [/正面/g, '긍정적'],
  [/负面/g, '부정적'],
  [/中性/g, '중립'],
  [/观望/g, '관망'],
  [/震荡/g, '박스권'],
  [/行业/g, '업종'],
  [/概念/g, '테마'],
  [/或/g, '또는'],
  [/但/g, '다만'],
  [/今日/g, '금일'],
  [/继续持有/g, '계속 보유'],
  [/持有/g, '보유'],
  [/买入/g, '매수'],
  [/卖出/g, '매도'],
  [/等待/g, '대기'],
  [/设置/g, '설정'],
  [/设好/g, '설정'],
  [/([0-9]+(?:\.[0-9]+)?)元/g, '$1위안'],
  [/([0-9]+(?:\.[0-9]+)?위안)(손절|목표가)/g, '$1 $2'],
  [/한([A-Z])/g, '한 $1'],
  [/：/g, ': '],
  [/，/g, ', '],
  [/。/g, '. '],
  [/（/g, '('],
  [/）/g, ')'],
];

export const formatReportDisplayText = (
  value: string | null | undefined,
  language?: ReportLanguage | null,
): string => {
  const text = (value || '').trim();
  if (!text) return '';

  const reportLanguage = normalizeReportLanguage(language);
  if (reportLanguage !== 'ko') {
    return text;
  }

  const exact = KO_EXACT_LABELS[text];
  if (exact) {
    return exact;
  }

  return KO_PHRASE_REPLACEMENTS.reduce(
    (current, [pattern, replacement]) => current.replace(pattern, replacement),
    text,
  ).replace(/\s{2,}/g, ' ').trim();
};

const DECISION_ACTION_LABELS: Record<ReportLanguage, Record<DecisionAction, string>> = {
  zh: {
    buy: '买入',
    add: '加仓',
    hold: '持有',
    reduce: '减仓',
    sell: '卖出',
    watch: '观望',
    avoid: '回避',
    alert: '预警',
  },
  en: {
    buy: 'Buy',
    add: 'Add',
    hold: 'Hold',
    reduce: 'Reduce',
    sell: 'Sell',
    watch: 'Watch',
    avoid: 'Avoid',
    alert: 'Alert',
  },
  ko: {
    buy: '매수',
    add: '비중 확대',
    hold: '보유',
    reduce: '비중 축소',
    sell: '매도',
    watch: '관망',
    avoid: '회피',
    alert: '경고',
  },
};

export const getDecisionActionDisplayLabel = (
  action: DecisionAction | null | undefined,
  language?: ReportLanguage | null,
): string | null => {
  if (!action) return null;
  return DECISION_ACTION_LABELS[normalizeReportLanguage(language)][action] || null;
};

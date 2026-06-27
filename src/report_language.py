# -*- coding: utf-8 -*-
"""Helpers for report output language selection and localization."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

SUPPORTED_REPORT_LANGUAGES = ("zh", "en", "ko")

_REPORT_LANGUAGE_ALIASES = {
    "zh-cn": "zh",
    "zh_cn": "zh",
    "zh-hans": "zh",
    "zh_hans": "zh",
    "zh-tw": "zh",
    "zh_tw": "zh",
    "cn": "zh",
    "chinese": "zh",
    "english": "en",
    "en-us": "en",
    "en_us": "en",
    "en-gb": "en",
    "en_gb": "en",
    "ko-kr": "ko",
    "ko_kr": "ko",
    "korean": "ko",
    "한국어": "ko",
}

_OPERATION_ADVICE_CANONICAL_MAP = {
    "强烈买入": "strong_buy",
    "strong buy": "strong_buy",
    "strong_buy": "strong_buy",
    "买入": "buy",
    "buy": "buy",
    "加仓": "buy",
    "accumulate": "buy",
    "add position": "buy",
    "持有": "hold",
    "洗盘观察": "hold",
    "观察": "hold",
    "hold": "hold",
    "观望": "watch",
    "watch": "watch",
    "wait": "watch",
    "wait and see": "watch",
    "减仓": "reduce",
    "reduce": "reduce",
    "trim": "reduce",
    "卖出": "sell",
    "sell": "sell",
    "强烈卖出": "strong_sell",
    "strong sell": "strong_sell",
    "strong_sell": "strong_sell",
}

_OPERATION_ADVICE_TRANSLATIONS = {
    "strong_buy": {"zh": "强烈买入", "en": "Strong Buy", "ko": "강력 매수"},
    "buy": {"zh": "买入", "en": "Buy", "ko": "매수"},
    "hold": {"zh": "持有", "en": "Hold", "ko": "보유"},
    "watch": {"zh": "观望", "en": "Watch", "ko": "관망"},
    "reduce": {"zh": "减仓", "en": "Reduce", "ko": "비중 축소"},
    "sell": {"zh": "卖出", "en": "Sell", "ko": "매도"},
    "strong_sell": {"zh": "强烈卖出", "en": "Strong Sell", "ko": "강력 매도"},
}

_TREND_PREDICTION_CANONICAL_MAP = {
    "强势空头": "strong_bearish",
    "强烈看多": "strong_bullish",
    "strong bullish": "strong_bullish",
    "very bullish": "strong_bullish",
    "强势多头": "strong_bullish",
    "多头排列": "bullish",
    "空头排列": "bearish",
    "弱势多头": "bullish",
    "弱势空头": "bearish",
    "看多": "bullish",
    "盘整": "sideways",
    "bullish": "bullish",
    "uptrend": "bullish",
    "震荡": "sideways",
    "neutral": "sideways",
    "sideways": "sideways",
    "range-bound": "sideways",
    "看空": "bearish",
    "bearish": "bearish",
    "downtrend": "bearish",
    "强烈看空": "strong_bearish",
    "strong bearish": "strong_bearish",
    "very bearish": "strong_bearish",
}

_TREND_PREDICTION_TRANSLATIONS = {
    "strong_bullish": {"zh": "强烈看多", "en": "Strong Bullish", "ko": "강한 상승 전망"},
    "bullish": {"zh": "看多", "en": "Bullish", "ko": "상승 전망"},
    "sideways": {"zh": "震荡", "en": "Sideways", "ko": "횡보"},
    "bearish": {"zh": "看空", "en": "Bearish", "ko": "하락 전망"},
    "strong_bearish": {"zh": "强烈看空", "en": "Strong Bearish", "ko": "강한 하락 전망"},
}

_CONFIDENCE_LEVEL_CANONICAL_MAP = {
    "高": "high",
    "high": "high",
    "中": "medium",
    "medium": "medium",
    "med": "medium",
    "低": "low",
    "low": "low",
}

_CONFIDENCE_LEVEL_TRANSLATIONS = {
    "high": {"zh": "高", "en": "High", "ko": "높음"},
    "medium": {"zh": "中", "en": "Medium", "ko": "보통"},
    "low": {"zh": "低", "en": "Low", "ko": "낮음"},
}

_CHIP_HEALTH_CANONICAL_MAP = {
    "健康": "healthy",
    "healthy": "healthy",
    "一般": "average",
    "average": "average",
    "警惕": "caution",
    "caution": "caution",
}

_CHIP_HEALTH_TRANSLATIONS = {
    "healthy": {"zh": "健康", "en": "Healthy", "ko": "양호"},
    "average": {"zh": "一般", "en": "Average", "ko": "보통"},
    "caution": {"zh": "警惕", "en": "Caution", "ko": "주의"},
}

_BIAS_STATUS_CANONICAL_MAP = {
    "安全": "safe",
    "safe": "safe",
    "警戒": "caution",
    "警惕": "caution",
    "caution": "caution",
    "危险": "danger",
    "risk": "danger",
    "danger": "danger",
}

_BIAS_STATUS_TRANSLATIONS = {
    "safe": {"zh": "安全", "en": "Safe", "ko": "안전"},
    "caution": {"zh": "警戒", "en": "Caution", "ko": "경고"},
    "danger": {"zh": "危险", "en": "Danger", "ko": "위험"},
}

_PLACEHOLDER_BY_LANGUAGE = {
    "zh": "待补充",
    "en": "TBD",
    "ko": "추가 예정",
}

_UNKNOWN_BY_LANGUAGE = {
    "zh": "未知",
    "en": "Unknown",
    "ko": "알 수 없음",
}

_NO_DATA_BY_LANGUAGE = {
    "zh": "数据缺失",
    "en": "Data unavailable",
    "ko": "데이터 없음",
}

_CHIP_UNAVAILABLE_BY_LANGUAGE = {
    "zh": "筹码分布未启用或数据源暂不可用，未纳入筹码判断。",
    "en": "Chip distribution is disabled or temporarily unavailable; chip signals were not used.",
    "ko": "수급 분포가 비활성화되었거나 일시적으로 사용할 수 없어 수급 신호를 사용하지 않았습니다.",
}

_CHIP_PLACEHOLDER_EXACT = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "tbd",
    "数据缺失",
    "未知",
    "暂无",
    "待补充",
}

_CHIP_PLACEHOLDER_HINTS = (
    "数据缺失",
    "无法判断",
    "data unavailable",
    "unavailable",
    "not available",
    "missing",
    "not supported",
)

_CHIP_METRIC_KEYS = ("profit_ratio", "avg_cost", "concentration")
_CHIP_UNAVAILABLE_REASON_KEYS = (
    "chip_unavailable_reason",
    "unavailable_reason",
    "chip_unavailable",
)

_GENERIC_STOCK_NAME_BY_LANGUAGE = {
    "zh": "待确认股票",
    "en": "Unnamed Stock",
    "ko": "미확인 종목",
}

_REPORT_LABELS: Dict[str, Dict[str, str]] = {
    "zh": {
        "dashboard_title": "决策仪表盘",
        "brief_title": "决策简报",
        "analyzed_prefix": "共分析",
        "stock_unit": "只股票",
        "stock_unit_compact": "只",
        "buy_label": "买入",
        "watch_label": "观望",
        "sell_label": "卖出",
        "summary_heading": "分析结果摘要",
        "info_heading": "重要信息速览",
        "sentiment_summary_label": "舆情情绪",
        "earnings_outlook_label": "业绩预期",
        "risk_alerts_label": "风险警报",
        "positive_catalysts_label": "利好催化",
        "latest_news_label": "最新动态",
        "core_conclusion_heading": "核心结论",
        "one_sentence_label": "一句话决策",
        "time_sensitivity_label": "时效性",
        "default_time_sensitivity": "本周内",
        "position_status_label": "持仓情况",
        "action_advice_label": "操作建议",
        "no_position_label": "空仓者",
        "has_position_label": "持仓者",
        "continue_holding": "继续持有",
        "market_snapshot_heading": "当日行情",
        "close_label": "收盘",
        "prev_close_label": "昨收",
        "open_label": "开盘",
        "high_label": "最高",
        "low_label": "最低",
        "change_pct_label": "涨跌幅",
        "change_amount_label": "涨跌额",
        "amplitude_label": "振幅",
        "volume_label": "成交量",
        "amount_label": "成交额",
        "current_price_label": "当前价",
        "volume_ratio_label": "量比",
        "turnover_rate_label": "换手率",
        "source_label": "行情来源",
        "data_perspective_heading": "数据透视",
        "ma_alignment_label": "均线排列",
        "bullish_alignment_label": "多头排列",
        "yes_label": "是",
        "no_label": "否",
        "trend_strength_label": "趋势强度",
        "price_metrics_label": "价格指标",
        "ma5_label": "MA5",
        "ma10_label": "MA10",
        "ma20_label": "MA20",
        "bias_ma5_label": "乖离率(MA5)",
        "support_level_label": "支撑位",
        "resistance_level_label": "压力位",
        "chip_label": "筹码",
        "phase_decision_heading": "盘中决策护栏",
        "action_window_label": "行动窗口",
        "immediate_action_label": "当前动作",
        "watch_conditions_label": "观察条件",
        "next_check_time_label": "下次检查",
        "confidence_reason_label": "置信度理由",
        "data_limitations_label": "数据限制",
        "battle_plan_heading": "作战计划",
        "ideal_buy_label": "理想买入点",
        "secondary_buy_label": "次优买入点",
        "stop_loss_label": "止损位",
        "take_profit_label": "目标位",
        "suggested_position_label": "仓位建议",
        "entry_plan_label": "建仓策略",
        "risk_control_label": "风控策略",
        "checklist_heading": "检查清单",
        "failed_checks_heading": "检查未通过项",
        "history_compare_heading": "历史信号对比",
        "time_label": "时间",
        "score_label": "评分",
        "advice_label": "建议",
        "trend_label": "趋势",
        "generated_at_label": "报告生成时间",
        "report_time_label": "生成时间",
        "no_results": "无分析结果",
        "report_title": "股票分析报告",
        "avg_score_label": "均分",
        "action_points_heading": "操作点位",
        "position_advice_heading": "持仓建议",
        "analysis_model_label": "分析模型",
        "not_investment_advice": "AI生成，仅供参考，不构成投资建议",
        "details_report_hint": "详细报告见",
        "financial_summary_heading": "财务摘要",
        "report_date_label": "报告期",
        "revenue_label": "营业收入",
        "net_profit_label": "归母净利润",
        "operating_cash_flow_label": "经营现金流",
        "roe_label": "ROE",
        "revenue_yoy_label": "营收同比",
        "net_profit_yoy_label": "净利同比",
        "gross_margin_label": "毛利率",
        "shareholder_return_heading": "股东回报",
        "ttm_cash_dividend_label": "近12月每股现金分红(税前)",
        "ttm_event_count_label": "近12月分红次数",
        "ttm_dividend_yield_label": "TTM 股息率",
        "latest_ex_dividend_label": "最近除息日",
        "related_boards_heading": "关联板块",
        "board_name_label": "板块",
        "board_type_label": "类型",
        "board_status_label": "板块表现",
        "board_change_pct_label": "板块涨跌幅",
        "leading_board_label": "领涨",
        "lagging_board_label": "领跌",
    },
    "en": {
        "dashboard_title": "Decision Dashboard",
        "brief_title": "Decision Brief",
        "analyzed_prefix": "Analyzed",
        "stock_unit": "stocks",
        "stock_unit_compact": "stocks",
        "buy_label": "Buy",
        "watch_label": "Watch",
        "sell_label": "Sell",
        "summary_heading": "Summary",
        "info_heading": "Key Updates",
        "sentiment_summary_label": "Sentiment",
        "earnings_outlook_label": "Earnings Outlook",
        "risk_alerts_label": "Risk Alerts",
        "positive_catalysts_label": "Positive Catalysts",
        "latest_news_label": "Latest News",
        "core_conclusion_heading": "Core Conclusion",
        "one_sentence_label": "One-line Decision",
        "time_sensitivity_label": "Time Sensitivity",
        "default_time_sensitivity": "This week",
        "position_status_label": "Position",
        "action_advice_label": "Action",
        "no_position_label": "No Position",
        "has_position_label": "Holding",
        "continue_holding": "Continue holding",
        "market_snapshot_heading": "Market Snapshot",
        "close_label": "Close",
        "prev_close_label": "Prev Close",
        "open_label": "Open",
        "high_label": "High",
        "low_label": "Low",
        "change_pct_label": "Change %",
        "change_amount_label": "Change",
        "amplitude_label": "Amplitude",
        "volume_label": "Volume",
        "amount_label": "Turnover",
        "current_price_label": "Price",
        "volume_ratio_label": "Volume Ratio",
        "turnover_rate_label": "Turnover Rate",
        "source_label": "Source",
        "data_perspective_heading": "Data View",
        "ma_alignment_label": "MA Alignment",
        "bullish_alignment_label": "Bullish Alignment",
        "yes_label": "Yes",
        "no_label": "No",
        "trend_strength_label": "Trend Strength",
        "price_metrics_label": "Price Metrics",
        "ma5_label": "MA5",
        "ma10_label": "MA10",
        "ma20_label": "MA20",
        "bias_ma5_label": "Bias (MA5)",
        "support_level_label": "Support",
        "resistance_level_label": "Resistance",
        "chip_label": "Chip Structure",
        "phase_decision_heading": "Phase Decision Guardrail",
        "action_window_label": "Action Window",
        "immediate_action_label": "Current Action",
        "watch_conditions_label": "Watch Conditions",
        "next_check_time_label": "Next Check",
        "confidence_reason_label": "Confidence Reason",
        "data_limitations_label": "Data Limitations",
        "battle_plan_heading": "Battle Plan",
        "ideal_buy_label": "Ideal Entry",
        "secondary_buy_label": "Secondary Entry",
        "stop_loss_label": "Stop Loss",
        "take_profit_label": "Target",
        "suggested_position_label": "Position Size",
        "entry_plan_label": "Entry Plan",
        "risk_control_label": "Risk Control",
        "checklist_heading": "Checklist",
        "failed_checks_heading": "Failed Checks",
        "history_compare_heading": "Historical Signal Comparison",
        "time_label": "Time",
        "score_label": "Score",
        "advice_label": "Advice",
        "trend_label": "Trend",
        "generated_at_label": "Generated At",
        "report_time_label": "Generated",
        "no_results": "No analysis results",
        "report_title": "Stock Analysis Report",
        "avg_score_label": "Avg Score",
        "action_points_heading": "Action Levels",
        "position_advice_heading": "Position Advice",
        "analysis_model_label": "Model",
        "not_investment_advice": "AI-generated content for reference only. Not investment advice.",
        "details_report_hint": "See detailed report:",
        "financial_summary_heading": "Financial Summary",
        "report_date_label": "Report Date",
        "revenue_label": "Revenue",
        "net_profit_label": "Net Profit (Parent)",
        "operating_cash_flow_label": "Operating Cash Flow",
        "roe_label": "ROE",
        "revenue_yoy_label": "Revenue YoY",
        "net_profit_yoy_label": "Net Profit YoY",
        "gross_margin_label": "Gross Margin",
        "shareholder_return_heading": "Shareholder Return",
        "ttm_cash_dividend_label": "TTM Cash Dividend / Share (Pre-tax)",
        "ttm_event_count_label": "TTM Dividend Events",
        "ttm_dividend_yield_label": "TTM Dividend Yield",
        "latest_ex_dividend_label": "Latest Ex-dividend Date",
        "related_boards_heading": "Related Boards",
        "board_name_label": "Board",
        "board_type_label": "Type",
        "board_status_label": "Status",
        "board_change_pct_label": "Change %",
        "leading_board_label": "Leading",
        "lagging_board_label": "Lagging",
    },
    "ko": {
        "dashboard_title": "의사결정 대시보드",
        "brief_title": "의사결정 요약",
        "analyzed_prefix": "총 분석",
        "stock_unit": "종목",
        "stock_unit_compact": "종목",
        "buy_label": "매수",
        "watch_label": "관망",
        "sell_label": "매도",
        "summary_heading": "분석 결과 요약",
        "info_heading": "주요 정보 요약",
        "sentiment_summary_label": "시장 심리",
        "earnings_outlook_label": "실적 전망",
        "risk_alerts_label": "리스크 경보",
        "positive_catalysts_label": "호재 촉매",
        "latest_news_label": "최신 동향",
        "core_conclusion_heading": "핵심 결론",
        "one_sentence_label": "한 줄 결론",
        "time_sensitivity_label": "시의성",
        "default_time_sensitivity": "금주 내",
        "position_status_label": "보유 현황",
        "action_advice_label": "매매 제안",
        "no_position_label": "미보유자",
        "has_position_label": "보유자",
        "continue_holding": "계속 보유",
        "market_snapshot_heading": "당일 시세",
        "close_label": "종가",
        "prev_close_label": "전일 종가",
        "open_label": "시가",
        "high_label": "고가",
        "low_label": "저가",
        "change_pct_label": "등락률",
        "change_amount_label": "등락폭",
        "amplitude_label": "진폭",
        "volume_label": "거래량",
        "amount_label": "거래대금",
        "current_price_label": "현재가",
        "volume_ratio_label": "거래량비",
        "turnover_rate_label": "회전율",
        "source_label": "시세 출처",
        "data_perspective_heading": "데이터 분석",
        "ma_alignment_label": "이동평균선 배열",
        "bullish_alignment_label": "정배열",
        "yes_label": "예",
        "no_label": "아니요",
        "trend_strength_label": "추세 강도",
        "price_metrics_label": "가격 지표",
        "ma5_label": "MA5",
        "ma10_label": "MA10",
        "ma20_label": "MA20",
        "bias_ma5_label": "괴리율(MA5)",
        "support_level_label": "지지선",
        "resistance_level_label": "저항선",
        "chip_label": "수급 분포",
        "phase_decision_heading": "장중 의사결정 가드레일",
        "action_window_label": "매매 구간",
        "immediate_action_label": "현재 동작",
        "watch_conditions_label": "관찰 조건",
        "next_check_time_label": "다음 확인",
        "confidence_reason_label": "신뢰도 근거",
        "data_limitations_label": "데이터 제한사항",
        "battle_plan_heading": "매매 계획",
        "ideal_buy_label": "이상적 매수 구간",
        "secondary_buy_label": "차선 매수 구간",
        "stop_loss_label": "손절가",
        "take_profit_label": "목표가",
        "suggested_position_label": "비중 제안",
        "entry_plan_label": "진입 전략",
        "risk_control_label": "리스크 관리",
        "checklist_heading": "체크리스트",
        "failed_checks_heading": "미통과 항목",
        "history_compare_heading": "과거 신호 비교",
        "time_label": "시간",
        "score_label": "점수",
        "advice_label": "제안",
        "trend_label": "추세",
        "generated_at_label": "보고서 생성 시간",
        "report_time_label": "생성 시간",
        "no_results": "분석 결과 없음",
        "report_title": "종목 분석 보고서",
        "avg_score_label": "평균 점수",
        "action_points_heading": "매매 구간",
        "position_advice_heading": "보유 제안",
        "analysis_model_label": "분석 모델",
        "not_investment_advice": "AI 생성, 참고용이며 투자 권유가 아닙니다",
        "details_report_hint": "상세 보고서 보기",
        "financial_summary_heading": "재무 요약",
        "report_date_label": "보고 기간",
        "revenue_label": "매출액",
        "net_profit_label": "귀속 순이익",
        "operating_cash_flow_label": "영업현금흐름",
        "roe_label": "ROE",
        "revenue_yoy_label": "매출 YoY",
        "net_profit_yoy_label": "순이익 YoY",
        "gross_margin_label": "매출총이익률",
        "shareholder_return_heading": "주주환원",
        "ttm_cash_dividend_label": "최근 12개월 주당 현금 배당(세전)",
        "ttm_event_count_label": "최근 12개월 배당 횟수",
        "ttm_dividend_yield_label": "TTM 배당수익률",
        "latest_ex_dividend_label": "최근 배당락일",
        "related_boards_heading": "관련 테마",
        "board_name_label": "테마",
        "board_type_label": "유형",
        "board_status_label": "테마 동향",
        "board_change_pct_label": "테마 등락률",
        "leading_board_label": "상승 주도",
        "lagging_board_label": "하락 주도",
    },
}

_DECISION_INTENT_NEGATIONS = (
    "不",
    "并非",
    "并未",
    "未",
    "没有",
    "无",
    "不是",
    "no ",
    "not ",
    " never",
)

_DECISION_INTENT_NEGATION_SCOPE_BREAK_CHARS = "，,。；;:!?！？"
_DECISION_INTENT_NEGATION_CONNECTORS = (
    "建议",
    "应",
    "应当",
    "宜",
    "先",
    "再",
    "暂",
    "暂时",
    "可",
    "可以",
    "需要",
    "需",
    "继续",
)


def _strip_decision_negation_connectors(text: str) -> str:
    """Remove common advisory connectors between a negation token and decision word."""
    suffix = text.strip()
    changed = True
    while changed:
        changed = False
        for connector in _DECISION_INTENT_NEGATION_CONNECTORS:
            if suffix.startswith(connector):
                suffix = suffix[len(connector):].strip()
                changed = True
                break
    return suffix


def normalize_report_language(value: Optional[str], default: str = "zh") -> str:
    """Normalize report language to a supported short code."""
    candidate = (value or default).strip().lower().replace(" ", "_")
    candidate = _REPORT_LANGUAGE_ALIASES.get(candidate, candidate)
    if candidate in SUPPORTED_REPORT_LANGUAGES:
        return candidate
    return default


def is_supported_report_language_value(value: Optional[str]) -> bool:
    """Return whether the raw value is a supported language code or alias."""
    candidate = (value or "").strip().lower().replace(" ", "_")
    if not candidate:
        return False
    return candidate in SUPPORTED_REPORT_LANGUAGES or candidate in _REPORT_LANGUAGE_ALIASES


def get_report_labels(language: Optional[str]) -> Dict[str, str]:
    """Return UI copy for the selected report language."""
    normalized = normalize_report_language(language)
    return _REPORT_LABELS[normalized]


def get_placeholder_text(language: Optional[str]) -> str:
    """Return placeholder text for missing localized content."""
    return _PLACEHOLDER_BY_LANGUAGE[normalize_report_language(language)]


def get_unknown_text(language: Optional[str]) -> str:
    """Return localized unknown text."""
    return _UNKNOWN_BY_LANGUAGE[normalize_report_language(language)]


def get_no_data_text(language: Optional[str]) -> str:
    """Return localized data unavailable text."""
    return _NO_DATA_BY_LANGUAGE[normalize_report_language(language)]


def get_chip_unavailable_text(language: Optional[str]) -> str:
    """Return the localized one-line chip distribution fallback text."""
    return _CHIP_UNAVAILABLE_BY_LANGUAGE[normalize_report_language(language)]


def _normalize_lookup_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", " ").replace("-", " ")


def _iter_lookup_candidates(value: Any) -> list[str]:
    raw_text = str(value or "").strip()
    if not raw_text:
        return []

    candidates = [raw_text]
    for part in re.split(r"[/|,，、]+", raw_text):
        normalized = part.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _canonicalize_lookup_value(value: Any, canonical_map: Dict[str, str]) -> Optional[str]:
    for candidate in _iter_lookup_candidates(value):
        canonical = canonical_map.get(_normalize_lookup_key(candidate))
        if canonical:
            return canonical
    return None


def _first_non_negated_position(text: str, token: str) -> Optional[int]:
    if not text or not token:
        return None

    normalized_text = text.lower().strip()
    if any(ch in normalized_text for ch in "abcdefghijklmnopqrstuvwxyz"):
        matches = list(re.finditer(rf"(?<![a-z0-9_]){re.escape(token)}(?![a-z0-9_])", normalized_text))
    else:
        matches = list(re.finditer(re.escape(token), normalized_text))

    for match in matches:
        prefix = normalized_text[: match.start()]
        if any(prefix.rstrip().endswith(neg) for neg in _DECISION_INTENT_NEGATIONS):
            continue
        lookback = prefix[-12:]
        negated = False
        for neg in _DECISION_INTENT_NEGATIONS:
            if not neg:
                continue
            neg_idx = lookback.rfind(neg)
            if neg_idx < 0:
                continue
            suffix = lookback[neg_idx + len(neg):]
            if not suffix:
                negated = True
                break
            if any(ch in suffix for ch in _DECISION_INTENT_NEGATION_SCOPE_BREAK_CHARS):
                continue
            normalized_suffix = _strip_decision_negation_connectors(suffix)
            if not normalized_suffix:
                negated = True
                break
            if any(ch in normalized_suffix for ch in _DECISION_INTENT_NEGATION_SCOPE_BREAK_CHARS):
                continue
            if len(normalized_suffix) > 6 and token not in normalized_suffix:
                continue
            if normalized_suffix.startswith(token):
                negated = True
                break
        if negated:
            continue
        else:
            return match.start()
    return None


def _is_placeholder_stock_name(value: Any, code: Any = None) -> bool:
    text = str(value or "").strip()
    if not text:
        return True

    lowered = text.lower()
    if lowered in {"n/a", "na", "none", "null", "unknown"}:
        return True
    if text in {"-", "—", "未知", "待补充"}:
        return True

    code_text = str(code or "").strip()
    if code_text and lowered == code_text.lower():
        return True

    return text.startswith("股票")


def _translate_from_map(
    value: Any,
    language: Optional[str],
    *,
    canonical_map: Dict[str, str],
    translations: Dict[str, Dict[str, str]],
) -> str:
    normalized_language = normalize_report_language(language)
    raw_text = str(value or "").strip()
    if not raw_text:
        return raw_text

    canonical = _canonicalize_lookup_value(raw_text, canonical_map)
    if canonical:
        return translations[canonical][normalized_language]
    return raw_text


def localize_operation_advice(value: Any, language: Optional[str]) -> str:
    """Translate operation advice between Chinese and English when recognized."""
    return _translate_from_map(
        value,
        language,
        canonical_map=_OPERATION_ADVICE_CANONICAL_MAP,
        translations=_OPERATION_ADVICE_TRANSLATIONS,
    )


def localize_trend_prediction(value: Any, language: Optional[str]) -> str:
    """Translate trend prediction between Chinese and English when recognized."""
    normalized_language = normalize_report_language(language)
    raw_text = str(value or "").strip()
    if not raw_text:
        return raw_text
    if normalized_language == "zh":
        if re.search(r"[\u4e00-\u9fff]", raw_text):
            return raw_text
    return _translate_from_map(
        value,
        normalized_language,
        canonical_map=_TREND_PREDICTION_CANONICAL_MAP,
        translations=_TREND_PREDICTION_TRANSLATIONS,
    )


def localize_confidence_level(value: Any, language: Optional[str]) -> str:
    """Translate confidence level between Chinese and English when recognized."""
    return _translate_from_map(
        value,
        language,
        canonical_map=_CONFIDENCE_LEVEL_CANONICAL_MAP,
        translations=_CONFIDENCE_LEVEL_TRANSLATIONS,
    )


def localize_chip_health(value: Any, language: Optional[str]) -> str:
    """Translate chip health labels between Chinese and English when recognized."""
    return _translate_from_map(
        value,
        language,
        canonical_map=_CHIP_HEALTH_CANONICAL_MAP,
        translations=_CHIP_HEALTH_TRANSLATIONS,
    )


def is_chip_placeholder_value(value: Any) -> bool:
    """Return True for chip fields filled with empty or no-data placeholders."""
    if value is None:
        return True
    if isinstance(value, (int, float)) and value == 0:
        return True
    text = str(value).strip()
    lowered = text.lower()
    if lowered in _CHIP_PLACEHOLDER_EXACT:
        return True
    return any(hint in lowered for hint in _CHIP_PLACEHOLDER_HINTS)


def is_chip_structure_unavailable(chip_data: Any) -> bool:
    """Detect chip_structure blocks that contain only unavailable placeholders."""
    if not isinstance(chip_data, dict) or not chip_data:
        return False
    for key in _CHIP_UNAVAILABLE_REASON_KEYS:
        raw = chip_data.get(key)
        if isinstance(raw, bool):
            if raw:
                return True
            continue
        if str(raw or "").strip():
            return True
    if any(key in chip_data for key in _CHIP_METRIC_KEYS):
        return all(is_chip_placeholder_value(chip_data.get(key)) for key in _CHIP_METRIC_KEYS)
    return all(is_chip_placeholder_value(value) for value in chip_data.values())


def get_chip_unavailable_reason(value: Any, language: Optional[str]) -> str:
    """Return the explicit or default chip unavailable reason for rendering."""
    if not isinstance(value, dict) or not value:
        return ""
    for key in _CHIP_UNAVAILABLE_REASON_KEYS:
        raw = value.get(key)
        if isinstance(raw, bool):
            if raw:
                return get_chip_unavailable_text(language)
            continue
        text = str(raw or "").strip()
        if text:
            return text
    if is_chip_structure_unavailable(value):
        return get_chip_unavailable_text(language)
    return ""


def localize_bias_status(value: Any, language: Optional[str]) -> str:
    """Translate price bias status labels between Chinese and English when recognized."""
    return _translate_from_map(
        value,
        language,
        canonical_map=_BIAS_STATUS_CANONICAL_MAP,
        translations=_BIAS_STATUS_TRANSLATIONS,
    )


def get_bias_status_emoji(value: Any) -> str:
    """Return the stable alert emoji for a localized or canonical bias status."""
    canonical = _canonicalize_lookup_value(value, _BIAS_STATUS_CANONICAL_MAP)
    if canonical == "safe":
        return "✅"
    if canonical == "caution":
        return "⚠️"
    return "🚨"


def infer_decision_type_from_advice(value: Any, default: str = "hold") -> str:
    """Infer buy/hold/sell from human-readable operation advice."""
    canonical = _canonicalize_lookup_value(value, _OPERATION_ADVICE_CANONICAL_MAP)
    if canonical in {"strong_buy", "buy"}:
        return "buy"
    if canonical in {"reduce", "sell", "strong_sell"}:
        return "sell"
    if canonical in {"hold", "watch"}:
        return "hold"

    normalized_text = _normalize_lookup_key(value)
    best_position: Optional[int] = None
    best_canonical: Optional[str] = None
    for option, canonical in _OPERATION_ADVICE_CANONICAL_MAP.items():
        option_norm = _normalize_lookup_key(option)
        pos = _first_non_negated_position(normalized_text, option_norm)
        if pos is None:
            continue
        if best_position is None or pos < best_position:
            best_position = pos
            best_canonical = canonical

    if best_canonical in {"strong_buy", "buy"}:
        return "buy"
    if best_canonical in {"reduce", "sell", "strong_sell"}:
        return "sell"
    if best_canonical in {"hold", "watch"}:
        return "hold"

    return default


def get_signal_level(advice: Any, score: Any, language: Optional[str]) -> tuple[str, str, str]:
    """Return localized signal text, emoji, and stable color tag."""
    normalized_language = normalize_report_language(language)
    canonical = _canonicalize_lookup_value(advice, _OPERATION_ADVICE_CANONICAL_MAP)
    if canonical == "strong_buy":
        return (_OPERATION_ADVICE_TRANSLATIONS["strong_buy"][normalized_language], "💚", "strong_buy")
    if canonical == "buy":
        return (_OPERATION_ADVICE_TRANSLATIONS["buy"][normalized_language], "🟢", "buy")
    if canonical == "hold":
        return (_OPERATION_ADVICE_TRANSLATIONS["hold"][normalized_language], "🟡", "hold")
    if canonical == "watch":
        return (_OPERATION_ADVICE_TRANSLATIONS["watch"][normalized_language], "⚪", "watch")
    if canonical == "reduce":
        return (_OPERATION_ADVICE_TRANSLATIONS["reduce"][normalized_language], "🟠", "reduce")
    if canonical in {"sell", "strong_sell"}:
        return (_OPERATION_ADVICE_TRANSLATIONS["sell"][normalized_language], "🔴", "sell")

    try:
        numeric_score = int(float(score))
    except (TypeError, ValueError):
        numeric_score = 50

    if numeric_score >= 80:
        return (_OPERATION_ADVICE_TRANSLATIONS["strong_buy"][normalized_language], "💚", "strong_buy")
    if numeric_score >= 65:
        return (_OPERATION_ADVICE_TRANSLATIONS["buy"][normalized_language], "🟢", "buy")
    if numeric_score >= 55:
        return (_OPERATION_ADVICE_TRANSLATIONS["hold"][normalized_language], "🟡", "hold")
    if numeric_score >= 45:
        return (_OPERATION_ADVICE_TRANSLATIONS["watch"][normalized_language], "⚪", "watch")
    if numeric_score >= 35:
        return (_OPERATION_ADVICE_TRANSLATIONS["reduce"][normalized_language], "🟠", "reduce")
    return (_OPERATION_ADVICE_TRANSLATIONS["sell"][normalized_language], "🔴", "sell")


def get_localized_stock_name(value: Any, code: Any, language: Optional[str]) -> str:
    """Return a localized stock name placeholder when the original name is missing."""
    raw_text = str(value or "").strip()
    if not _is_placeholder_stock_name(raw_text, code):
        return raw_text
    return _GENERIC_STOCK_NAME_BY_LANGUAGE[normalize_report_language(language)]


def get_sentiment_label(score: int, language: Optional[str]) -> str:
    """Return localized sentiment label by score band."""
    normalized = normalize_report_language(language)
    if normalized == "en":
        if score >= 80:
            return "Very Bullish"
        if score >= 60:
            return "Bullish"
        if score >= 40:
            return "Neutral"
        if score >= 20:
            return "Bearish"
        return "Very Bearish"

    if normalized == "ko":
        if score >= 80:
            return "매우 낙관"
        if score >= 60:
            return "낙관"
        if score >= 40:
            return "중립"
        if score >= 20:
            return "비관"
        return "매우 비관"

    if score >= 80:
        return "极度乐观"
    if score >= 60:
        return "乐观"
    if score >= 40:
        return "中性"
    if score >= 20:
        return "悲观"
    return "极度悲观"


_LOCALIZED_TEXT: Dict[str, Dict[str, str]] = {
    "trend_unknown": {"zh": "未知", "en": "Unknown", "ko": "알 수 없음"},
    "advice_watch": {"zh": "观望", "en": "Watch", "ko": "관망"},
    "advice_hold": {"zh": "持有", "en": "Hold", "ko": "보유"},
    "advice_buy": {"zh": "买入", "en": "Buy", "ko": "매수"},
    "advice_sell": {"zh": "卖出", "en": "Sell", "ko": "매도"},
    "confidence_medium": {"zh": "中", "en": "Medium", "ko": "보통"},
    "confidence_low": {"zh": "低", "en": "Low", "ko": "낮음"},
    "confidence_high": {"zh": "高", "en": "High", "ko": "높음"},
    "agent_failed_dashboard": {
        "zh": "Agent 未能生成有效的决策仪表盘",
        "en": "Agent failed to generate a valid decision dashboard",
        "ko": "Agent가 유효한 의사결정 대시보드를 생성하지 못했습니다",
    },
    "analysis_pending": {
        "zh": "分析待补充",
        "en": "Analysis pending",
        "ko": "분석 대기 중",
    },
    "to_be_completed": {
        "zh": "待补充",
        "en": "To be completed",
        "ko": "추가 예정",
    },
    "analysis_date": {
        "zh": "分析日期",
        "en": "Analysis Date",
        "ko": "분석 날짜",
    },
    "report_time": {
        "zh": "报告生成时间",
        "en": "Report Time",
        "ko": "보고서 생성 시간",
    },
    "rationale_label": {
        "zh": "操作理由",
        "en": "Rationale",
        "ko": "매매 근거",
    },
    "risk_warning_label": {
        "zh": "风险提示",
        "en": "Risk Warning",
        "ko": "리스크 경고",
    },
    "technicals_label": {
        "zh": "技术面",
        "en": "Technicals",
        "ko": "기술적 분석",
    },
    "moving_averages_label": {
        "zh": "均线",
        "en": "Moving Averages",
        "ko": "이동평균선",
    },
    "volume_label_short": {
        "zh": "量能",
        "en": "Volume",
        "ko": "거래량",
    },
    "news_flow_label": {
        "zh": "消息面",
        "en": "News Flow",
        "ko": "뉴스 동향",
    },
    "market_review_title": {
        "zh": "大盘复盘",
        "en": "Market Review",
        "ko": "시장 리뷰",
    },
    "market_review_generated": {
        "zh": "大盘复盘报告已生成。",
        "en": "Market review report generated.",
        "ko": "시장 리뷰 보고서가 생성되었습니다.",
    },
    "trend_conclusion_fmt": {
        "zh": "趋势结论：{trend}；操作建议：{advice}。",
        "en": "Trend view: {trend}; action advice: {advice}.",
        "ko": "추세 결론: {trend}; 매매 제안: {advice}.",
    },
    "not_investment_advice_short": {
        "zh": "（AI生成，仅供参考，不构成投资建议）",
        "en": "(AI-generated, for reference only, not investment advice)",
        "ko": "(AI 생성, 참고용이며 투자 권유가 아닙니다)",
    },
}


def get_localized_text(key: str, language: Optional[str] = "zh", **kwargs: Any) -> str:
    """Return localized text by key, with optional format parameters."""
    normalized = normalize_report_language(language)
    entry = _LOCALIZED_TEXT.get(key)
    if entry is None:
        return f"???{key}???"
    text = entry.get(normalized, entry.get("zh", f"???{key}???"))
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, ValueError):
            pass
    return text


def localize_market_label(
    market: str,
    language: Optional[str] = "zh",
) -> str:
    """Return localized market name for cn/hk/us."""
    normalized = normalize_report_language(language)
    labels: Dict[str, Dict[str, str]] = {
        "cn": {"zh": "A股", "en": "A-shares", "ko": "A주"},
        "hk": {"zh": "港股", "en": "Hong Kong", "ko": "홍콩"},
        "us": {"zh": "美股", "en": "US", "ko": "미국"},
        "tw": {"zh": "台股", "en": "Taiwan", "ko": "대만"},
    }
    entry = labels.get(market, labels.get("cn", {"zh": market, "en": market, "ko": market}))
    return entry.get(normalized, entry.get("zh", market))


def localize_phase_label(
    phase: str,
    language: Optional[str] = "zh",
) -> str:
    """Return localized market phase label."""
    normalized = normalize_report_language(language)
    labels: Dict[str, Dict[str, str]] = {
        "premarket": {"zh": "盘前", "en": "Pre-market", "ko": "장전"},
        "intraday": {"zh": "盘中", "en": "Intraday", "ko": "장중"},
        "closing_auction": {"zh": "尾盘竞价", "en": "Closing Auction", "ko": "마감 경매"},
        "after_hours": {"zh": "盘后", "en": "After Hours", "ko": "장후"},
        "closed": {"zh": "已收盘", "en": "Closed", "ko": "장 마감"},
    }
    return labels.get(phase, {}).get(normalized, phase)


_MARKET_STATUS_TEXT: Dict[str, Dict[str, str]] = {
    "zh": {"prefix": "市场状态"},
    "en": {"prefix": "Market status"},
    "ko": {"prefix": "시장 상태"},
}


def get_market_status_prefix(language: Optional[str] = "zh") -> str:
    """Return localized market status prefix."""
    normalized = normalize_report_language(language)
    return _MARKET_STATUS_TEXT.get(normalized, _MARKET_STATUS_TEXT["zh"])["prefix"]

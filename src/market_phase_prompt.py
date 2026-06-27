# -*- coding: utf-8 -*-
"""Prompt rendering for Issue #1386 runtime market phase context."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_PHASE_LABELS_ZH = {
    "premarket": "盘前",
    "intraday": "盘中",
    "lunch_break": "午间休市",
    "closing_auction": "临近收盘",
    "postmarket": "盘后",
    "non_trading": "非交易日",
    "unknown": "未知阶段",
}

_PHASE_LABELS_EN = {
    "premarket": "pre-market",
    "intraday": "intraday",
    "lunch_break": "lunch break",
    "closing_auction": "near close",
    "postmarket": "post-market",
    "non_trading": "non-trading day",
    "unknown": "unknown phase",
}

_PHASE_LABELS_KO = {
    "premarket": "장전",
    "intraday": "장중",
    "lunch_break": "점심 휴장",
    "closing_auction": "마감 임박",
    "postmarket": "장후",
    "non_trading": "휴장일",
    "unknown": "알 수 없는 단계",
}

_KNOWN_PHASES = set(_PHASE_LABELS_ZH)

_WARNING_LABELS_ZH = {
    "unknown_market": "未知市场",
    "calendar_unavailable": "交易日历不可用",
    "calendar_error": "交易日历异常",
}

_WARNING_LABELS_EN = {
    "unknown_market": "unknown market",
    "calendar_unavailable": "trading calendar unavailable",
    "calendar_error": "trading calendar error",
}

_WARNING_LABELS_KO = {
    "unknown_market": "알 수 없는 시장",
    "calendar_unavailable": "거래일 캘린더 사용 불가",
    "calendar_error": "거래일 캘린더 오류",
}


def format_market_phase_prompt_section(
    market_phase_context: Optional[Dict[str, Any]],
    *,
    report_language: str = "zh",
) -> str:
    """Return a human-readable prompt section for a P1a market phase payload.

    The helper is intentionally narrow: callers pass the runtime dict produced
    by ``MarketPhaseContext.to_dict()`` when available. Missing optional fields
    are omitted, unknown phases use the conservative ``unknown`` template, and
    raw runtime keys such as ``market_phase_context`` are never rendered.
    """
    if not isinstance(market_phase_context, dict) or not market_phase_context:
        return ""

    lang = str(report_language or "").lower()
    raw_phase = market_phase_context.get("phase")
    phase = raw_phase if isinstance(raw_phase, str) and raw_phase in _KNOWN_PHASES else "unknown"

    if lang == "en":
        return _format_en(market_phase_context, phase)
    if lang == "ko":
        return _format_ko(market_phase_context, phase)
    return _format_zh(market_phase_context, phase)


def _format_zh(ctx: Dict[str, Any], phase: str) -> str:
    label = _PHASE_LABELS_ZH[phase]
    lines = ["", "## 市场阶段上下文", f"- 当前市场阶段：{label}"]
    lines.extend(_metadata_lines_zh(ctx))
    lines.append(f"- 阶段约束：{_phase_rule_zh(ctx, phase)}")

    warning_text = _warning_text(ctx.get("warnings"), lang="zh")
    if warning_text:
        lines.append(f"- 降级说明：{warning_text}，请保持保守表述。")

    return "\n".join(lines) + "\n"


def _format_en(ctx: Dict[str, Any], phase: str) -> str:
    label = _PHASE_LABELS_EN[phase]
    lines = ["", "## Market Phase Context", f"- Current market phase: {label}"]
    lines.extend(_metadata_lines_en(ctx))
    lines.append(f"- Phase constraint: {_phase_rule_en(ctx, phase)}")

    warning_text = _warning_text(ctx.get("warnings"), lang="en")
    if warning_text:
        lines.append(f"- Degradation note: {warning_text}; keep the analysis conservative.")

    return "\n".join(lines) + "\n"


def _metadata_lines_zh(ctx: Dict[str, Any]) -> List[str]:
    items: List[str] = []
    market = _string_value(ctx.get("market"))
    market_time = _string_value(ctx.get("market_local_time"))
    effective_date = _string_value(ctx.get("effective_daily_bar_date"))
    minutes_to_open = _int_like(ctx.get("minutes_to_open"))
    minutes_to_close = _int_like(ctx.get("minutes_to_close"))

    if market:
        items.append(f"- 市场：{market}")
    if market_time:
        items.append(f"- 市场本地时间：{market_time}")
    if effective_date:
        items.append(f"- 最新可复用完整日线日期：{effective_date}")
    if minutes_to_open is not None:
        items.append(f"- 距常规开盘约 {minutes_to_open} 分钟。")
    if minutes_to_close is not None:
        items.append(f"- 距常规收盘约 {minutes_to_close} 分钟。")
    return items


def _metadata_lines_en(ctx: Dict[str, Any]) -> List[str]:
    items: List[str] = []
    market = _string_value(ctx.get("market"))
    market_time = _string_value(ctx.get("market_local_time"))
    effective_date = _string_value(ctx.get("effective_daily_bar_date"))
    minutes_to_open = _int_like(ctx.get("minutes_to_open"))
    minutes_to_close = _int_like(ctx.get("minutes_to_close"))

    if market:
        items.append(f"- Market: {market}")
    if market_time:
        items.append(f"- Market-local time: {market_time}")
    if effective_date:
        items.append(f"- Latest reusable complete daily bar date: {effective_date}")
    if minutes_to_open is not None:
        items.append(f"- About {minutes_to_open} minutes until the regular session opens.")
    if minutes_to_close is not None:
        items.append(f"- About {minutes_to_close} minutes until the regular session closes.")
    return items


def _phase_rule_zh(ctx: Dict[str, Any], phase: str) -> str:
    effective_date = _string_value(ctx.get("effective_daily_bar_date"))
    date_hint = f"（{effective_date}）" if effective_date else ""

    if phase == "premarket":
        return (
            f"当前尚未开盘，不得描述“今日走势已经发生”；只能基于上一完整交易日{date_hint}"
            "和盘前信息生成开盘计划、观察价位与风险预案。"
        )
    if phase in {"intraday", "lunch_break", "closing_auction"}:
        base = "当前不是盘后复盘，应聚焦当前盘中状态、观察条件与下一次检查点。"
        if ctx.get("is_partial_bar") is True:
            base += " 今日最后一根日线可能尚未完成，不得当作完整日线复盘。"
        if phase == "lunch_break":
            base += " 午间休市期间应说明后续复盘仍需下午交易确认。"
        if phase == "closing_auction":
            base += " 临近收盘时应更偏向收盘前风险控制和是否隔夜持仓。"
        return base
    if phase == "postmarket":
        return "常规交易时段已结束，可以保留完整交易日复盘语义。"
    if phase == "non_trading":
        return f"当前不是交易日或属于强制运行，只能基于上一完整交易日{date_hint}和已知事件分析，不得伪造今日盘中走势。"
    return "当前市场阶段不可可靠推断，不要补全不存在的盘中或盘前事实，结论需保持保守。"


def _phase_rule_en(ctx: Dict[str, Any], phase: str) -> str:
    effective_date = _string_value(ctx.get("effective_daily_bar_date"))
    date_hint = f" ({effective_date})" if effective_date else ""

    if phase == "premarket":
        return (
            f"The regular session has not opened. Do not describe today's price action as already happened; "
            f"use only the latest complete daily bar{date_hint} and pre-market information for the opening plan."
        )
    if phase in {"intraday", "lunch_break", "closing_auction"}:
        base = "This is not a post-market recap. Focus on the current intraday state, watch conditions, and next check point."
        if ctx.get("is_partial_bar") is True:
            base += " The latest daily bar may be unfinished; do not treat it as a complete daily candle."
        if phase == "lunch_break":
            base += " During the lunch break, later confirmation depends on the afternoon session."
        if phase == "closing_auction":
            base += " Near the close, emphasize end-of-day risk control and overnight-position decisions."
        return base
    if phase == "postmarket":
        return "The regular session has ended, so a complete-session recap style is acceptable."
    if phase == "non_trading":
        return (
            f"This is a non-trading day or forced run. Use the latest complete daily bar{date_hint} and known events; "
            "do not invent today's intraday movement."
        )
    return "The market phase cannot be inferred reliably. Do not invent pre-market or intraday facts, and keep conclusions conservative."


def _format_ko(ctx: Dict[str, Any], phase: str) -> str:
    label = _PHASE_LABELS_KO[phase]
    lines = ["", "## 시장 단계 컨텍스트", f"- 현재 시장 단계: {label}"]
    lines.extend(_metadata_lines_ko(ctx))
    lines.append(f"- 단계 제약: {_phase_rule_ko(ctx, phase)}")

    warning_text = _warning_text(ctx.get("warnings"), lang="ko")
    if warning_text:
        lines.append(f"- 저하 안내: {warning_text}; 보수적인 분석을 유지하세요.")

    return "\n".join(lines) + "\n"


def _metadata_lines_ko(ctx: Dict[str, Any]) -> List[str]:
    items: List[str] = []
    market = _string_value(ctx.get("market"))
    market_time = _string_value(ctx.get("market_local_time"))
    effective_date = _string_value(ctx.get("effective_daily_bar_date"))
    minutes_to_open = _int_like(ctx.get("minutes_to_open"))
    minutes_to_close = _int_like(ctx.get("minutes_to_close"))

    if market:
        items.append(f"- 시장: {market}")
    if market_time:
        items.append(f"- 시장 현지 시간: {market_time}")
    if effective_date:
        items.append(f"- 최신 재사용 가능 완전 일봉 일자: {effective_date}")
    if minutes_to_open is not None:
        items.append(f"- 정규장 개장까지 약 {minutes_to_open}분.")
    if minutes_to_close is not None:
        items.append(f"- 정규장 마감까지 약 {minutes_to_close}분.")
    return items


def _phase_rule_ko(ctx: Dict[str, Any], phase: str) -> str:
    effective_date = _string_value(ctx.get("effective_daily_bar_date"))
    date_hint = f" ({effective_date})" if effective_date else ""

    if phase == "premarket":
        return (
            f"정규장이 아직 개장하지 않았습니다. 오늘의 가격 움직임이 이미 발생했다고 설명하지 마세요; "
            f"최신 완전 일봉{date_hint}과 장전 정보만을 사용하여 개장 계획을 수립하세요."
        )
    if phase in {"intraday", "lunch_break", "closing_auction"}:
        base = "현재는 장후 리캡이 아닙니다. 현재 장중 상태, 관찰 조건 및 다음 체크 포인트에 집중하세요."
        if ctx.get("is_partial_bar") is True:
            base += " 최신 일봉이 아직 완성되지 않았을 수 있으므로 완전한 일봉으로 취급하지 마세요."
        if phase == "lunch_break":
            base += " 점심 휴장 중이므로 이후 확정은 오후 세션 확인이 필요합니다."
        if phase == "closing_auction":
            base += " 마감 임박 시에는 장 마감 리스크 관리와 익일 포지션 결정에 중점을 두세요."
        return base
    if phase == "postmarket":
        return "정규 거래 세션이 종료되었으므로 완전 세션 리캡 스타일이 허용됩니다."
    if phase == "non_trading":
        return (
            f"오늘은 거래일이 아니거나 강제 실행입니다. 최신 완전 일봉{date_hint}과 알려진 이벤트를 사용하세요; "
            "오늘의 장중 움직임을 만들어내지 마세요."
        )
    return "시장 단계를 신뢰성 있게 추론할 수 없습니다. 장전 또는 장중 사실을 만들어내지 말고 보수적인 결론을 유지하세요."


def _warning_text(value: Any, *, lang: str) -> str:
    if not isinstance(value, list):
        return ""
    if lang == "en":
        labels = _WARNING_LABELS_EN
    elif lang == "ko":
        labels = _WARNING_LABELS_KO
    else:
        labels = _WARNING_LABELS_ZH
    rendered = [labels[item] for item in value if isinstance(item, str) and item in labels]
    if not rendered:
        return ""
    if lang == "en":
        return ", ".join(rendered)
    return "、".join(rendered)


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _int_like(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None

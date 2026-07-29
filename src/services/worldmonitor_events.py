# -*- coding: utf-8 -*-
"""把 World Monitor 上游载荷归一化成 DSA 事件。

设计: docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md §3/§8/§9

端点与字段是读取 submodule `6c48a33c` 下真实的 handler 与生成的 service_server
契约后确定的，而不是按名字挑选。三个端点的可靠性并不一致，详见设计 §3.1。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

FreshnessState = Literal["fresh", "stale", "unavailable", "unverified"]


@dataclass(frozen=True)
class CategoryFreshness:
    """某个类别在提示词里可以声称到什么程度。

    ``can_claim_no_events`` 与 ``state`` 分开，是因为"同步成功"并不等于"这个类别
    真的能产出事件"（设计 §7.1）。
    """

    category: str
    state: FreshnessState
    last_success_at: Optional[datetime] = None
    last_nonempty_at: Optional[datetime] = None
    can_claim_no_events: bool = False
    detail: Optional[str] = None

CATEGORY_CONFLICT = "geopolitical_conflict"
CATEGORY_OUTAGE = "infrastructure_outage"
CATEGORY_ENERGY = "supply_chain_energy"

CATEGORIES: Tuple[str, ...] = (CATEGORY_CONFLICT, CATEGORY_OUTAGE, CATEGORY_ENERGY)

ENDPOINT_CONFLICT = "/api/conflict/v1/list-acled-events"
ENDPOINT_OUTAGE = "/api/infrastructure/v1/list-internet-outages"
ENDPOINT_ENERGY = "/api/supply-chain/v1/list-energy-disruptions"

CATEGORY_ENDPOINTS: Dict[str, str] = {
    CATEGORY_CONFLICT: ENDPOINT_CONFLICT,
    CATEGORY_OUTAGE: ENDPOINT_OUTAGE,
    CATEGORY_ENERGY: ENDPOINT_ENERGY,
}

# ISO2 国家码到 DSA 市场的 1:1 映射（设计 §8）。
# 刻意不编码跨国传导关系（例如中国事件对港股的影响）：那属于未经验证的因果，
# 写进映射表等于把它当成事实注入提示词，应该留给 LLM 判断。
_COUNTRY_TO_MARKET: Dict[str, str] = {
    "CN": "cn",
    "HK": "hk",
    "US": "us",
    "KR": "kr",
    "JP": "jp",
}

# OutageSeverity 枚举取自生成的 infrastructure/v1 service_server 契约。
_OUTAGE_SEVERITY_RANK: Dict[str, int] = {
    "OUTAGE_SEVERITY_TOTAL": 3,
    "OUTAGE_SEVERITY_MAJOR": 2,
    "OUTAGE_SEVERITY_PARTIAL": 1,
    "OUTAGE_SEVERITY_UNSPECIFIED": 0,
}

# BCM/yr -> Mbd 的近似换算系数，仅用于把两种容量单位放到同一个排序刻度上。
# 这个值只服务于类别内部排序，不用于任何对外披露的数值。
_BCM_YR_TO_MBD = 0.172


def normalize_acled_event(raw: Any) -> Optional[Dict[str, Any]]:
    """ACLED 冲突事件 -> 归一化事件。

    上游没有标题字段，因此由 eventType 与 country 合成。
    """
    if not isinstance(raw, dict):
        return None

    external_id = _clean_str(raw.get("id"))
    if not external_id:
        return None

    occurred_at = _from_epoch_millis(raw.get("occurredAt"))
    if occurred_at is None:
        return None

    country = _clean_str(raw.get("country"))
    event_type = _clean_str(raw.get("eventType")) or "Conflict event"
    title = f"{event_type} - {country}" if country else event_type
    countries = [country] if country else []

    summary_parts = [part for part in (_clean_str(raw.get("admin1")),) if part]
    actors = [a for a in (raw.get("actors") or []) if isinstance(a, str) and a.strip()]
    if actors:
        summary_parts.append(", ".join(actors))

    markets, scope = _resolve_scope(countries)
    return {
        "category": CATEGORY_CONFLICT,
        "source_endpoint": ENDPOINT_CONFLICT,
        "external_id": external_id,
        "title": _truncate(title, 300),
        "summary": _truncate(" | ".join(summary_parts), 2000) or None,
        "url": None,  # 上游 AcledConflictEvent 不提供链接
        "occurred_at": occurred_at,
        # 上游投影没有结束字段，冲突事件一律视为进行中。
        "ended_at": None,
        "countries": countries,
        "markets": markets,
        "scope": scope,
        "severity_rank": _coerce_int(raw.get("fatalities")),
        "raw_payload": raw,
    }


def normalize_internet_outage(raw: Any) -> Optional[Dict[str, Any]]:
    """Cloudflare Radar 派生的网络中断 -> 归一化事件。"""
    if not isinstance(raw, dict):
        return None

    external_id = _clean_str(raw.get("id"))
    if not external_id:
        return None

    occurred_at = _from_epoch_millis(raw.get("detectedAt"))
    if occurred_at is None:
        return None

    # endedAt 是 proto number 字段，"尚未结束"以 0 而不是 null 到达；
    # 直接当 epoch 解析会把事件错标到 1970 年。
    ended_at = _from_epoch_millis(raw.get("endedAt"))

    country = _clean_str(raw.get("country"))
    region = _clean_str(raw.get("region"))
    countries = [country] if country else ([region] if region else [])

    markets, scope = _resolve_scope(countries)
    summary_parts = [
        part
        for part in (
            _clean_str(raw.get("description")),
            _clean_str(raw.get("cause")),
            _clean_str(raw.get("outageType")),
        )
        if part
    ]
    return {
        "category": CATEGORY_OUTAGE,
        "source_endpoint": ENDPOINT_OUTAGE,
        "external_id": external_id,
        "title": _truncate(_clean_str(raw.get("title")) or "Internet outage", 300),
        "summary": _truncate(" | ".join(summary_parts), 2000) or None,
        "url": _clean_str(raw.get("link")) or None,
        "occurred_at": occurred_at,
        "ended_at": ended_at,
        "countries": countries,
        "markets": markets,
        "scope": scope,
        "severity_rank": _OUTAGE_SEVERITY_RANK.get(_clean_str(raw.get("severity")), 0),
        "raw_payload": raw,
    }


def normalize_energy_disruption(raw: Any) -> Optional[Dict[str, Any]]:
    """能源/供应链中断 -> 归一化事件。"""
    if not isinstance(raw, dict):
        return None

    external_id = _clean_str(raw.get("id"))
    if not external_id:
        return None

    occurred_at = _from_iso(raw.get("startAt"))
    if occurred_at is None:
        return None

    # 上游把 `endAt: null` 投影成空字符串，空串即"仍在进行中"。
    ended_at = _from_iso(raw.get("endAt"))

    countries = [
        c.strip().upper()
        for c in (raw.get("countries") or [])
        if isinstance(c, str) and c.strip()
    ]
    markets, scope = _resolve_scope(countries)

    sources = raw.get("sources") or []
    url = None
    if isinstance(sources, list) and sources:
        first = sources[0]
        if isinstance(first, dict):
            url = _clean_str(first.get("url")) or None

    cause_chain = [c for c in (raw.get("causeChain") or []) if isinstance(c, str) and c.strip()]
    summary_parts = [part for part in (_clean_str(raw.get("assetType")),) if part]
    if cause_chain:
        summary_parts.append(" -> ".join(cause_chain))

    return {
        "category": CATEGORY_ENERGY,
        "source_endpoint": ENDPOINT_ENERGY,
        "external_id": external_id,
        "title": _truncate(
            _clean_str(raw.get("shortDescription"))
            or _clean_str(raw.get("eventType"))
            or "Energy disruption",
            300,
        ),
        "summary": _truncate(" | ".join(summary_parts), 2000) or None,
        "url": url,
        "occurred_at": occurred_at,
        "ended_at": ended_at,
        "countries": countries,
        "markets": markets,
        "scope": scope,
        "severity_rank": _energy_severity_rank(raw),
        "raw_payload": raw,
    }


def _energy_severity_rank(raw: Dict[str, Any]) -> int:
    """按设计 §9 的表把两种容量单位折算到同一排序刻度。

    上游 projector 在字段缺失时统一降级为 0，因此 rank 为 0 的并列是正常现象，
    此时由发生时刻的次级排序决定顺序 —— 这是预期行为，不是缺陷。
    """
    mbd = _coerce_float(raw.get("capacityOfflineMbd"))
    if mbd > 0:
        return round(mbd * 10)
    bcm_yr = _coerce_float(raw.get("capacityOfflineBcmYr"))
    if bcm_yr > 0:
        return round(bcm_yr * _BCM_YR_TO_MBD * 10)
    return 0


def _resolve_scope(countries: Sequence[str]) -> Tuple[List[str], str]:
    """把国家列表折算成 (markets, scope)。

    设计 §8.1: `markets` 为空有两条来源且语义相反 —— 知道发生地但不在 DSA 市场内
    (`global`)，以及根本不知道发生地 (`unmapped`)。上游明确提醒 denorm 之前写入的
    历史行会带空 countries，把两者合并会让"发生地未知"被悄悄升格成"影响所有市场"。
    """
    if not countries:
        return [], "unmapped"

    markets: List[str] = []
    for country in countries:
        market = _COUNTRY_TO_MARKET.get(str(country).strip().upper())
        if market and market not in markets:
            markets.append(market)

    if markets:
        return markets, "market"
    return [], "global"


def _from_epoch_millis(value: Any) -> Optional[datetime]:
    """epoch 毫秒 -> datetime。0 / None / 非有限值一律视为"无时刻"。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        millis = float(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0 or millis != millis:  # NaN != NaN
        return None
    try:
        return datetime.fromtimestamp(millis / 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _from_iso(value: Any) -> Optional[datetime]:
    """ISO 8601 -> naive 本地时刻。空串表示"仍在进行中"。

    必须是**本地**时刻，不是 UTC 挂钟时刻。``_from_epoch_millis`` 用
    ``datetime.fromtimestamp`` 得到的是本地时刻，两者写进同一个 ``occurred_at``
    列并与本地 ``datetime.now()`` 比较；若这里对带时区的输入直接 ``replace(tzinfo=None)``
    保留 UTC 挂钟值，KST 下就会产生 9 小时偏差 —— 跨类别排序错乱，提示词里显示的
    日期还可能整整差一天。
    """
    text = _clean_str(value)
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


# ---------------------------------------------------------------------------
# 提示词渲染（设计 §9）
# ---------------------------------------------------------------------------

# 每种语言的固定文案。刻意把"无事件"与"无法确认"写成完全不同的句子：
# 两者若共用措辞，采集管线挂掉就会被读成"一切正常"（设计 §3.1）。
_PROMPT_TEXT: Dict[str, Dict[str, str]] = {
    "ko": {
        "heading": "## 글로벌 리스크 이벤트",
        "intro": "아래는 외부 모니터링에서 수집한 이벤트입니다. 각 항목의 확인 상태를 함께 확인하세요.",
        CATEGORY_CONFLICT: "지정학 분쟁",
        CATEGORY_OUTAGE: "인프라 장애",
        CATEGORY_ENERGY: "공급망·에너지",
        "no_events": "해당 없음",
        "unverified": "확인할 수 없습니다. 이 표기는 이상 없음을 뜻하지 않습니다.",
        "stale": "최신 확인 실패. 아래는 마지막으로 확인된 시점의 데이터입니다.",
        "fresh_label": "최신",
        "ongoing": "진행 중",
        "ended": "종료",
        "guidance": (
            "- 확인할 수 없는 항목을 위험이 없다는 근거로 사용하지 마세요.\n"
            "- 위 이벤트는 참고 맥락이며, 개별 종목 판단의 직접 근거로 삼지 마세요."
        ),
    },
    "en": {
        "heading": "## Global Risk Events",
        "intro": "Events collected from external monitoring. Check the verification state of each section.",
        CATEGORY_CONFLICT: "Geopolitical conflict",
        CATEGORY_OUTAGE: "Infrastructure outage",
        CATEGORY_ENERGY: "Supply chain and energy",
        "no_events": "None in this window",
        "unverified": "Could not be verified. This does not mean nothing happened.",
        "stale": "Not freshly confirmed. The entries below are from the last confirmed check.",
        "fresh_label": "current",
        "ongoing": "ongoing",
        "ended": "ended",
        "guidance": (
            "- Do not treat an unverified section as evidence that no risk exists.\n"
            "- These events are background context; do not use them as direct grounds for single-stock calls."
        ),
    },
    "zh": {
        "heading": "## 全球风险事件",
        "intro": "以下事件来自外部监测，请同时关注每个板块的确认状态。",
        CATEGORY_CONFLICT: "地缘冲突",
        CATEGORY_OUTAGE: "基础设施中断",
        CATEGORY_ENERGY: "供应链与能源",
        "no_events": "本窗口内无事件",
        "unverified": "无法确认。该标记不代表没有发生事件。",
        "stale": "未能刷新确认，以下为最后一次确认时的数据。",
        "fresh_label": "最新",
        "ongoing": "进行中",
        "ended": "已结束",
        "guidance": (
            "- 不要把无法确认的板块当作没有风险的依据。\n"
            "- 上述事件仅作背景参考，不要作为个股判断的直接依据。"
        ),
    },
}


def render_worldmonitor_prompt_block(
    *,
    events_by_category: Dict[str, Sequence[Any]],
    freshness: Dict[str, CategoryFreshness],
    language: str,
    now: datetime,
) -> str:
    """渲染注入市场复盘提示词的全球风险事件区块。

    每个类别都会出现，即使为空 —— 悄悄省略一个类别会让模型默认它已被检查过。
    """
    text = _PROMPT_TEXT.get(language) or _PROMPT_TEXT["en"]

    sections: List[str] = [text["heading"], "", text["intro"], ""]
    for category in CATEGORIES:
        state = freshness.get(category)
        rows = list(events_by_category.get(category) or [])
        sections.append(f"### {text[category]} ({_state_label(state, text)})")

        if rows:
            if state is not None and state.state == "stale":
                sections.append(text["stale"])
            for row in rows:
                sections.append(_render_row(row, text))
        elif state is not None and state.can_claim_no_events:
            sections.append(text["no_events"])
        else:
            sections.append(text["unverified"])
        sections.append("")

    sections.append(text["guidance"])
    return "\n".join(sections).strip() + "\n"


def _state_label(state: Optional[CategoryFreshness], text: Dict[str, str]) -> str:
    if state is None:
        return text["unverified"].split(".")[0]
    if state.state == "fresh":
        return text["fresh_label"]
    return {
        "stale": text["stale"].split(".")[0],
        "unavailable": text["unverified"].split(".")[0],
        "unverified": text["unverified"].split(".")[0],
    }.get(state.state, text["fresh_label"])


def _render_row(row: Any, text: Dict[str, str]) -> str:
    occurred = getattr(row, "occurred_at", None)
    occurred_label = occurred.strftime("%Y-%m-%d") if isinstance(occurred, datetime) else "-"
    status = text["ongoing"] if getattr(row, "is_ongoing", False) else text["ended"]
    title = getattr(row, "title", "") or ""

    parts = [f"- [{status}] {title} ({occurred_label}"]
    countries = getattr(row, "country_list", None) or []
    if countries:
        parts.append(f", {'/'.join(str(c) for c in countries[:5])}")
    parts.append(")")

    summary = getattr(row, "summary", None)
    if summary:
        parts.append(f" {_truncate(str(summary), 160)}")
    return "".join(parts)


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _truncate(value: str, limit: int) -> str:
    return value[:limit] if len(value) > limit else value


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result else 0.0  # NaN -> 0

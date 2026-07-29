# -*- coding: utf-8 -*-
"""把 World Monitor 上游载荷归一化成 DSA 事件。

设计: docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md §3/§8/§9

端点与字段是读取 submodule `6c48a33c` 下真实的 handler 与生成的 service_server
契约后确定的，而不是按名字挑选。三个端点的可靠性并不一致，详见设计 §3.1。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

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
    """ISO 8601 -> naive datetime。空串表示"仍在进行中"。

    统一返回 naive 本地时刻，与库内其它时间列（`datetime.now()` 写入）保持同一
    参照系；混用 aware/naive 会让比较直接抛异常。
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
        parsed = parsed.replace(tzinfo=None)
    return parsed


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

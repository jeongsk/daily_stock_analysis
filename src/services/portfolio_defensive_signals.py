# -*- coding: utf-8 -*-
"""Shared held-position -> defensive decision-signal reverse mapping.

Extracted from ``PortfolioRiskService._build_decision_signal_risk`` (Phase 5
design spec
docs/superpowers/specs/2026-07-20-toss-auto-proposal-phase5-design.md §2
"입력 계약": "이 계산을 재사용한다(평행 구현 금지)") so the Phase 5
auto-proposal generator (``src/services/auto_proposal_service.py``) can reuse
the exact same held-position -> latest-active-defensive-signal matching logic
instead of a parallel reimplementation. ``PortfolioRiskService`` itself is
refactored to call this module; its own external output contract
(``get_risk_report()["decision_signal_risk"]``, a summarized/low-sensitivity
signal shape) is unchanged.

Two functions matter to callers:

- ``held_position_identities`` turns a portfolio snapshot into
  ``(account_id, symbol, market, signal_stock_code)`` rows for whichever
  markets the caller cares about.
- ``resolve_defensive_signal_matches`` batches the identity list through
  ``DecisionSignalService.list_signals`` and returns, for each held position
  that has a latest *active* ``sell``/``reduce``/``alert`` signal, the **raw**
  (unsummarized) signal dict — callers that only need a low-sensitivity
  public view (``PortfolioRiskService``) run the result through
  ``summarize_decision_signal`` themselves; callers that need structured
  fields (confidence/stop_loss/target_price/plan_quality/id — Phase 5) use
  the raw dict directly instead of re-fetching.

Market scope note: ``PortfolioRiskService`` has always scanned only
``cn``/``hk``/``us`` held positions for decision-signal risk (predating this
system's ``kr`` market support) — that is preserved as the default so its
external behavior does not change. Phase 5 explicitly widens this to
``kr``/``us`` (the only two markets Toss Invest trades — see
``portfolio_order_service._resolve_order_symbol``) by passing an explicit
``markets`` argument; without that widening, every KR holding (Toss's
primary use case) would be silently excluded from auto-proposal generation.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from src.services.decision_signal_service import DecisionSignalService

DEFENSIVE_DECISION_SIGNAL_ACTIONS = ("sell", "reduce", "alert")

# Markets PortfolioRiskService has always scanned for decision-signal risk
# (unchanged external behavior — see module docstring "Market scope note").
DEFAULT_DECISION_SIGNAL_MARKETS: FrozenSet[str] = frozenset({"cn", "hk", "us"})


def held_position_identities(
    snapshot: Dict[str, Any],
    *,
    markets: Optional[FrozenSet[str]] = None,
    account_ids: Optional[FrozenSet[int]] = None,
) -> List[Dict[str, Any]]:
    """Return one row per held (non-zero) position in ``snapshot`` whose
    market is in ``markets`` (default: the historical cn/hk/us scope) and
    whose account is in ``account_ids`` (default: every account in the
    snapshot)."""
    allowed_markets = markets if markets is not None else DEFAULT_DECISION_SIGNAL_MARKETS
    positions: List[Dict[str, Any]] = []
    for account in snapshot.get("accounts", []) or []:
        account_id = account.get("account_id")
        if account_ids is not None and account_id not in account_ids:
            continue
        for pos in account.get("positions", []) or []:
            symbol = str(pos.get("symbol") or "").strip().upper()
            market = str(pos.get("market") or "").strip().lower()
            if not symbol or market not in allowed_markets:
                continue
            signal_stock_code = DecisionSignalService.normalize_stock_code_for_signal(symbol, market=market)
            positions.append({
                "account_id": account_id,
                "symbol": symbol,
                "market": market,
                "signal_stock_code": signal_stock_code,
            })
    return positions


def resolve_defensive_signal_matches(
    held_positions: List[Dict[str, Any]],
    *,
    decision_signal_service: DecisionSignalService,
) -> List[Dict[str, Any]]:
    """Match ``held_positions`` to the latest *active* defensive decision
    signal for their ``(market, signal_stock_code)`` identity.

    Returns a list of ``{account_id, symbol, market, signal_stock_code,
    signal}`` dicts where ``signal`` is the raw serialized
    ``DecisionSignalRecord`` dict (id/action/confidence/stop_loss/
    target_price/plan_quality/...), deduped by
    ``(account_id, market, signal_stock_code, signal["id"])`` — mirrors the
    original ``PortfolioRiskService._build_decision_signal_risk`` matching
    loop verbatim, minus the summarization step (moved to the caller)."""
    if not held_positions:
        return []
    stock_identities = sorted({(p["market"], p["signal_stock_code"]) for p in held_positions})

    defensive_actions = set(DEFENSIVE_DECISION_SIGNAL_ACTIONS)
    latest_by_identity: Dict[Tuple[str, str], Dict[str, Any]] = {}
    page = 1
    while True:
        response = decision_signal_service.list_signals(
            stock_identities=stock_identities,
            status="active",
            page=page,
            page_size=100,
        )
        items = response.get("items", []) if isinstance(response, dict) else []
        for item in items:
            if str(item.get("action") or "") not in defensive_actions:
                continue
            key = (
                str(item.get("market") or "").strip().lower(),
                str(item.get("stock_code") or "").strip().upper(),
            )
            if key[0] and key[1] and key not in latest_by_identity:
                latest_by_identity[key] = item
        total = int(response.get("total", 0) or 0) if isinstance(response, dict) else 0
        if page * 100 >= total or not items:
            break
        page += 1

    matches: List[Dict[str, Any]] = []
    seen: set = set()
    for position in held_positions:
        signal = latest_by_identity.get((position["market"], position["signal_stock_code"]))
        if not isinstance(signal, dict) or not signal:
            continue
        action = str(signal.get("action") or "")
        if action not in defensive_actions:
            continue
        signal_id = int(signal.get("id") or 0)
        dedupe_key = (position["account_id"], position["market"], position["signal_stock_code"], signal_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        matches.append({
            "account_id": position["account_id"],
            "symbol": position["symbol"],
            "market": position["market"],
            "signal_stock_code": position["signal_stock_code"],
            "signal": signal,
        })
    return matches

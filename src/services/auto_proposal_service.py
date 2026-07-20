# -*- coding: utf-8 -*-
"""Toss defensive-signal auto-proposal batch generator (Phase 5 — design spec
docs/superpowers/specs/2026-07-20-toss-auto-proposal-phase5-design.md).

Converts a held Toss position's latest active defensive decision signal
(``sell``/``reduce``/``alert``) into a manual-approval order proposal draft.
Approval stays 100% manual: this module only ever calls
``PortfolioOrderService.create_proposal`` / ``PortfolioConditionalOrderService
.create_proposal`` (both write a ``pending`` draft) — never ``execute_proposal``
/``approve_proposal`` (the calls that actually reach Toss). That split is the
load-bearing safety property of this feature; see
``AutoProposalService.run_batch``'s docstring and
``tests/test_auto_proposal_service.py``'s safety-invariant test.

Data flow (design spec §4):

1. ``portfolio_defensive_signals.held_position_identities`` +
   ``resolve_defensive_signal_matches`` (reused, not reimplemented) give the
   held-position -> latest-active-defensive-signal reverse mapping, scoped to
   Toss-linked accounts and Toss's two tradeable markets (kr/us — see
   ``_TOSS_MARKETS`` below; this is a deliberate widening of
   ``PortfolioRiskService``'s historical cn/hk/us scope, documented in
   ``portfolio_defensive_signals``'s own module docstring).
2. Each match is filtered on confidence/plan_quality/price-field validity
   and de-duplicated against any already-active proposal for the same
   (account, symbol, side).
3. Surviving ``sell``/``reduce`` signals are sized off the ledger's cached
   ``PortfolioPosition.quantity`` (design spec v1.1 "보유수량 소스 정정" — the
   reverse mapping itself carries no quantity) and routed to either a Phase 4
   conditional STOP proposal (when the signal has a ``stop_loss``) or a
   Phase 3 plain LIMIT sell proposal (using a live quote otherwise, or
   skipped fail-closed if no quote is available). ``alert`` signals are
   counted but never produce a proposal.
4. Every ``create_proposal`` call failure (validation, cap, FX, sellable
   quantity, or a ``source_signal_id`` uniqueness violation on same-day
   re-run) is caught per-signal and logged — a single signal's failure never
   aborts the batch (design spec §5 edge cases).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from src.config import Config, get_config
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.decision_signal_service import DecisionSignalService
from src.services.portfolio_broker_sync_service import PortfolioBrokerSyncService
from src.services.portfolio_conditional_order_service import PortfolioConditionalOrderService
from src.services.portfolio_defensive_signals import (
    held_position_identities,
    resolve_defensive_signal_matches,
)
from src.services.portfolio_order_service import (
    PortfolioOrderService,
    _fetch_reference_price,
    _now_kst_naive,
    _resolve_order_symbol,
)
from src.services.portfolio_service import PortfolioService

logger = logging.getLogger(__name__)

# Toss Invest only ever trades these two markets
# (portfolio_order_service._resolve_order_symbol accepts nothing else) —
# widened from PortfolioRiskService's historical cn/hk/us decision-signal
# scope so kr holdings (Toss's primary use case) are not silently excluded.
_TOSS_MARKETS: FrozenSet[str] = frozenset({"kr", "us"})

# design spec §3 "필터": plan_quality values that must never produce an
# auto-generated order proposal. The spec's literal wording is "unknown/poor
# 제외", but `DecisionSignalRecord.plan_quality`'s actual domain
# (decision_signal_service.PLAN_QUALITIES) is
# {"complete", "partial", "minimal", "unknown"} — there is no "poor" value
# for this field ("poor" only exists in the unrelated data_quality_level
# vocabulary elsewhere in this codebase). Filtering on a literal "poor"
# string that can never occur would silently do nothing, defeating the
# spec's evident intent (exclude low-quality plans); "minimal" is this
# field's actual lowest-quality-but-present value, so it is used here in
# "poor"'s place. Flagged explicitly as a spec-vs-code deviation.
_INVALID_PLAN_QUALITIES = frozenset({"unknown", "minimal"})

_CONDITIONAL_EXPIRE_DAYS = 7


@dataclass
class AutoProposalBatchResult:
    """Outcome of one ``AutoProposalService.run_batch()`` call.

    ``refused``/``refused_reason`` (Codex 2nd-round review minor) distinguish
    "the batch ran and legitimately found nothing to do" (every count is 0,
    ``refused`` is False) from "the batch never ran at all because the
    idempotency index was missing/invalid" (``refused`` is True) — both
    would otherwise look identical to a caller that only inspects the
    generated/skipped/alert counts, which is exactly the ambiguity an
    operator needs to be able to resolve from the batch-summary
    notification alone."""

    generated: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    alert_count: int = 0
    refused: bool = False
    refused_reason: str = ""

    @property
    def generated_count(self) -> int:
        return len(self.generated)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _resolve_stop_loss(signal: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """Validate the signal's ``stop_loss`` field (design spec §3 "필수
    가격필드 존재"). Returns ``(stop_loss, invalid_reason)``: absent/blank is
    a legitimate route selector (falls through to the immediate-sell quote
    path) and returns ``(None, None)``; present-but-malformed (non-numeric or
    non-positive) is a data-quality problem and is reported as an explicit
    skip reason rather than silently mis-routed to the immediate-sell path."""
    raw = signal.get("stop_loss")
    if raw is None or raw == "":
        return None, None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None, f"stop_loss field is not numeric: {raw!r}"
    if not math.isfinite(parsed) or parsed <= 0:
        return None, f"stop_loss field is not a finite positive number: {raw!r}"
    return parsed, None


class AutoProposalService:
    """Generates Phase 3/4 order-proposal drafts from held-position
    defensive decision signals. Never executes/approves anything it creates.
    """

    def __init__(
        self,
        *,
        repo: Optional[PortfolioRepository] = None,
        portfolio_service: Optional[PortfolioService] = None,
        decision_signal_service: Optional[DecisionSignalService] = None,
        order_service: Optional[PortfolioOrderService] = None,
        conditional_order_service: Optional[PortfolioConditionalOrderService] = None,
        broker_sync_service: Optional[PortfolioBrokerSyncService] = None,
        config: Optional[Config] = None,
    ):
        self.repo = repo or PortfolioRepository()
        self.portfolio_service = portfolio_service or PortfolioService(repo=self.repo)
        self.decision_signal_service = decision_signal_service or DecisionSignalService(portfolio_repo=self.repo)
        self.order_service = order_service or PortfolioOrderService(
            portfolio_service=self.portfolio_service, repo=self.repo
        )
        self.conditional_order_service = conditional_order_service or PortfolioConditionalOrderService(
            portfolio_service=self.portfolio_service, repo=self.repo
        )
        # Phase 2 sync (design spec §3/§4 "포트폴리오 sync + 신호 생성 완료 후" —
        # Codex review M2: the batch must attempt a fresh sync for every
        # Toss-linked account before reading held positions, not just run
        # after signal generation with a potentially-stale ledger).
        self.broker_sync_service = broker_sync_service or PortfolioBrokerSyncService(
            portfolio_service=self.portfolio_service, repo=self.repo
        )
        self.config = config or get_config()

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------
    def run_batch(self) -> AutoProposalBatchResult:
        """Run one Phase 5 batch pass. Safe to call repeatedly (same-day
        re-runs are idempotent via the ``source_signal_id`` partial unique
        index + the active-proposal dedup check — design spec §5 "배치
        재실행"). Never raises for an individual signal's failure; only
        raises for something that prevents reading input at all (e.g. the
        portfolio snapshot itself failing), matching every other
        ``PortfolioService``/``PortfolioRiskService`` read path's
        fail-loud-on-input-error convention."""
        result = AutoProposalBatchResult()

        # Codex review B1: the source_signal_id partial unique index is what
        # makes the insert-then-catch-IntegrityError idempotency contract
        # below actually true. Its own creation (DatabaseManager._ensure_
        # portfolio_proposal_source_signal_unique_indexes) is deliberately
        # fail-open at DB-init time (a pre-existing duplicate must not crash
        # every process boot) — which means "the index doesn't exist" would
        # otherwise be silently invisible to this batch. Checking here turns
        # that into a structural fail-closed no-op instead: without the
        # index, two concurrent/overlapping batch runs could both pass the
        # active-proposal pre-check and insert duplicate auto-proposals for
        # the same signal, and a later index-creation retry would then fail
        # forever against the duplicate rows it created.
        if not self.repo.has_source_signal_unique_indexes():
            reason = (
                "source_signal_id partial unique index absent or invalid on one or "
                "both proposal tables — check DatabaseManager init logs for a "
                "critical 'could not create unique index' entry, or this deployment "
                "may be on a non-SQLite engine where the index is never created"
            )
            logger.error("[AutoProposal] Phase 5 idempotency index missing, refusing to run (%s)", reason)
            result.refused = True
            result.refused_reason = reason
            return result

        toss_account_ids = self.toss_linked_account_ids()
        if not toss_account_ids:
            logger.info("[AutoProposal] no active Toss-linked accounts; nothing to do")
            return result

        # Codex review M2: attempt a fresh Toss sync for every linked account
        # before reading held positions, so a same-day new buy/sell is
        # reflected before this batch sizes/dedupes off the ledger — sync is
        # best-effort and account-isolated (ADR 0003 IP-allowlist failures,
        # missing credentials, or a transient Toss upstream error only skip
        # that one account's freshness and fall back to its existing ledger
        # state; it must never block defensive proposals for positions this
        # system already knows about).
        self._sync_linked_accounts(toss_account_ids)

        now = _now_kst_naive()

        # account_id=None + a manual account_id filter (rather than one
        # get_portfolio_snapshot call per account) keeps this to a single
        # snapshot build and a single batched list_signals pass, mirroring
        # PortfolioRiskService's own "all accounts" aggregate path. This is
        # get_portfolio_snapshot, not get_risk_report — the latter would
        # additionally trigger drawdown-snapshot backfill this batch has no
        # use for.
        snapshot = self.portfolio_service.get_portfolio_snapshot(account_id=None, cost_method="fifo")
        positions = held_position_identities(
            snapshot,
            markets=_TOSS_MARKETS,
            account_ids=toss_account_ids,
        )
        if not positions:
            logger.info("[AutoProposal] no kr/us held positions in Toss-linked accounts; nothing to do")
            return result

        matches = resolve_defensive_signal_matches(
            positions,
            decision_signal_service=self.decision_signal_service,
        )
        if not matches:
            logger.info("[AutoProposal] no active defensive signals for held positions; nothing to do")
            return result

        min_confidence = float(getattr(self.config, "phase5_min_confidence", 0.6))
        slippage = float(getattr(self.config, "phase5_sell_slippage_bps", 50.0)) / 10000.0

        for match in matches:
            # Codex review M1: the previous version only wrapped the
            # create_proposal call itself — a failure in the quantity lookup
            # or the active-proposal dedup check (both outside that inner
            # try/except) would abort the whole loop instead of skipping
            # just this one signal (design spec §5 "그 신호만 skip, 배치
            # 계속"). Wrapping the entire per-signal call is the actual fix;
            # _process_match's own inner try/except around create_proposal
            # stays too, since it needs to build a more specific skip reason
            # than a bare exception repr.
            try:
                self._process_match(
                    match, min_confidence=min_confidence, slippage=slippage, now=now, result=result
                )
            except Exception as exc:
                signal = match.get("signal") or {}
                logger.exception(
                    "[AutoProposal] unexpected failure processing account_id=%s symbol=%s signal_id=%s",
                    match.get("account_id"), match.get("symbol"), signal.get("id"),
                )
                result.skipped.append({
                    "account_id": match.get("account_id"),
                    "symbol": match.get("symbol"),
                    "market": match.get("market"),
                    "signal_id": int(signal.get("id") or 0),
                    "action": str(signal.get("action") or ""),
                    "reason": f"unexpected error: {type(exc).__name__}: {exc}",
                })

        return result

    def toss_linked_account_ids(self) -> FrozenSet[int]:
        links = self.repo.list_broker_links(include_inactive=False)
        return frozenset(int(link.account_id) for link in links if link.provider == "toss")

    def _sync_linked_accounts(self, account_ids: FrozenSet[int]) -> None:
        for account_id in sorted(account_ids):
            try:
                self.broker_sync_service.sync_linked_account(account_id)
            except Exception as exc:
                # Best-effort only (design spec §3/§4, Codex review M2): a
                # sync outage for one account must not block this batch from
                # generating defensive proposals off that account's existing,
                # already-known ledger state — and must not affect any other
                # account's sync or the batch as a whole.
                logger.warning(
                    "[AutoProposal] portfolio sync failed for account_id=%s "
                    "(continuing with existing ledger state): %s: %s",
                    account_id, type(exc).__name__, exc,
                )

    # ------------------------------------------------------------------
    # Per-signal processing
    # ------------------------------------------------------------------
    def _process_match(
        self,
        match: Dict[str, Any],
        *,
        min_confidence: float,
        slippage: float,
        now: datetime,
        result: AutoProposalBatchResult,
    ) -> None:
        signal = match["signal"]
        account_id = match["account_id"]
        symbol = match["symbol"]
        market = match["market"]
        signal_id = int(signal.get("id") or 0)
        action = str(signal.get("action") or "")

        def skip(reason: str) -> None:
            logger.info(
                "[AutoProposal] skip account_id=%s symbol=%s market=%s signal_id=%s action=%s: %s",
                account_id, symbol, market, signal_id, action, reason,
            )
            result.skipped.append({
                "account_id": account_id,
                "symbol": symbol,
                "market": market,
                "signal_id": signal_id,
                "action": action,
                "reason": reason,
            })

        # --- filter: confidence (design spec §3, v1.1 "confidence 0~1") ---
        confidence = signal.get("confidence")
        if confidence is None:
            skip("confidence is null")
            return
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            skip(f"confidence is not numeric: {confidence!r}")
            return
        if not math.isfinite(confidence_value) or confidence_value < min_confidence:
            skip(f"confidence {confidence_value} < PHASE5_MIN_CONFIDENCE {min_confidence}")
            return

        # --- filter: plan_quality ---
        plan_quality = str(signal.get("plan_quality") or "unknown").strip().lower()
        if plan_quality in _INVALID_PLAN_QUALITIES:
            skip(f"plan_quality {plan_quality!r} is not eligible")
            return

        # --- filter: required price field validity (stop_loss, if present) ---
        stop_loss, stop_loss_error = _resolve_stop_loss(signal)
        if stop_loss_error:
            skip(stop_loss_error)
            return

        if action == "alert":
            # design spec §3 수량 산정: alert never produces a proposal —
            # only counted for the batch summary.
            result.alert_count += 1
            return
        if action not in ("sell", "reduce"):
            skip(f"unsupported action for auto-proposal: {action!r}")
            return

        held_qty = self.repo.get_cached_position_quantity(
            account_id=account_id, symbol=symbol, market=market
        )
        if action == "sell":
            quantity = held_qty
        else:  # reduce
            quantity = math.floor(held_qty / 2.0)
        if quantity is None or quantity <= 0:
            skip(f"sized quantity {quantity} <= 0 (held={held_qty}, action={action})")
            return

        try:
            storage_symbol, _toss_symbol, _resolved_market, _currency = _resolve_order_symbol(symbol)
        except ValueError as exc:
            skip(f"symbol not tradeable on Toss: {exc}")
            return

        if self.repo.has_active_proposal_for_symbol_side(
            account_id=account_id, storage_symbol=storage_symbol, side="sell", now=now
        ):
            skip("an active proposal already exists for this account/symbol/side")
            return

        try:
            if stop_loss is not None:
                self._create_conditional_proposal(
                    account_id=account_id,
                    symbol=symbol,
                    quantity=quantity,
                    stop_loss=stop_loss,
                    slippage=slippage,
                    now=now,
                    signal_id=signal_id,
                    result=result,
                )
            else:
                self._create_plain_proposal(
                    account_id=account_id,
                    symbol=symbol,
                    storage_symbol=storage_symbol,
                    quantity=quantity,
                    slippage=slippage,
                    signal_id=signal_id,
                    result=result,
                )
        except Exception as exc:
            # design spec §5: create_proposal validation failures (limit
            # caps, insufficient sellable quantity, FX fail-closed, the
            # 100M KRW hard reject, PendingProposalLimitExceededError, and a
            # source_signal_id partial-unique-index IntegrityError on a
            # same-day re-run) all skip only this signal — the batch itself
            # never aborts.
            skip(f"create_proposal failed: {type(exc).__name__}: {exc}")

    def _create_conditional_proposal(
        self,
        *,
        account_id: int,
        symbol: str,
        quantity: float,
        stop_loss: float,
        slippage: float,
        now: datetime,
        signal_id: int,
        result: AutoProposalBatchResult,
    ) -> None:
        # design spec v1.1 "조건주문 limit 갭다운": the STOP leg is LIMIT-only
        # on Toss, so limit == trigger would leave the order unfilled on a
        # gap-down through the trigger price — apply the same slippage the
        # immediate-sell path uses so the stop-loss route is never *less*
        # protective than the immediate one.
        limit_price = stop_loss * (1.0 - slippage)
        expire_date: date = (now + timedelta(days=_CONDITIONAL_EXPIRE_DAYS)).date()
        data = self.conditional_order_service.create_proposal(
            account_id=account_id,
            symbol=symbol,
            side="sell",
            trigger_price=stop_loss,
            limit_price=limit_price,
            quantity=quantity,
            expire_date=expire_date,
            generation_source="auto",
            source_signal_id=signal_id,
        )
        result.generated.append({
            "account_id": account_id,
            "symbol": symbol,
            "side": "sell",
            "quantity": quantity,
            "order_kind": "conditional",
            "proposal_uuid": data.get("proposal_uuid"),
            "signal_id": signal_id,
        })

    def _create_plain_proposal(
        self,
        *,
        account_id: int,
        symbol: str,
        storage_symbol: str,
        quantity: float,
        slippage: float,
        signal_id: int,
        result: AutoProposalBatchResult,
    ) -> None:
        # design spec v1.1 "즉시매도 현재가": derive the LIMIT price from a
        # live quote (get_realtime_quote, via the same reused helper Phase 3
        # itself uses for MARKET-order KRW estimation) rather than any
        # regex-parsed signal field. Fail-closed (skip) when no quote is
        # obtainable instead of proposing an unbounded/stale-priced order.
        current_price = _fetch_reference_price(storage_symbol)
        if current_price is None or current_price <= 0:
            raise ValueError(f"no realtime quote available for {storage_symbol}")
        limit_price = current_price * (1.0 - slippage)
        data = self.order_service.create_proposal(
            account_id=account_id,
            symbol=symbol,
            side="sell",
            quantity=quantity,
            order_type="LIMIT",
            price=limit_price,
            generation_source="auto",
            source_signal_id=signal_id,
        )
        result.generated.append({
            "account_id": account_id,
            "symbol": symbol,
            "side": "sell",
            "quantity": quantity,
            "order_kind": "plain",
            "proposal_uuid": data.get("proposal_uuid"),
            "signal_id": signal_id,
        })


def format_batch_summary(result: AutoProposalBatchResult) -> str:
    """Format the batch-end notification body (design spec §3 "노출":
    "승인 대기 자동 제안 N건: ..., alert K건 수동 검토")."""
    lines = [
        f"Phase 5 자동 주문 제안 배치: 승인 대기 {result.generated_count}건 생성, "
        f"alert {result.alert_count}건 수동 검토, skip {result.skipped_count}건"
    ]
    for item in result.generated[:20]:
        lines.append(
            f"- {item['symbol']} {item['side']} {item['quantity']}주 ({item['order_kind']})"
        )
    if result.generated_count > 20:
        lines.append(f"...외 {result.generated_count - 20}건")
    return "\n".join(lines)


def run_phase5_auto_proposal_batch(
    *,
    notifier: Any = None,
    config: Optional[Config] = None,
    service: Optional[AutoProposalService] = None,
) -> Optional[AutoProposalBatchResult]:
    """Top-level orchestration entrypoint for the daily pipeline hook (design
    spec §3 "트리거 위치"). No-ops (with a single log line) unless
    ``PHASE5_AUTO_PROPOSAL_ENABLED=true`` (strict parsing) **and** at least
    one active Toss broker link exists. Notification failure never raises —
    proposals are already durably committed by the time the summary is sent
    (design spec §5 "알림 채널 실패")."""
    cfg = config or get_config()
    if not bool(getattr(cfg, "phase5_auto_proposal_enabled", False)):
        logger.info("[AutoProposal] PHASE5_AUTO_PROPOSAL_ENABLED is not set; Phase 5 batch is a no-op")
        return None

    svc = service or AutoProposalService(config=cfg)
    if not svc.toss_linked_account_ids():
        logger.info("[AutoProposal] no active Toss-linked accounts; Phase 5 batch is a no-op")
        return None

    result = svc.run_batch()
    if result.refused:
        logger.error("[AutoProposal] Phase 5 batch refused to run: %s", result.refused_reason)
    else:
        logger.info(
            "[AutoProposal] Phase 5 batch complete: generated=%d skipped=%d alert=%d",
            result.generated_count, result.skipped_count, result.alert_count,
        )

    # design spec §3 "종료 시 요약 1건" — always sent once the batch actually
    # ran (including an all-zero result), not only when something happened:
    # an operator needs to be able to tell "ran, found nothing to do" apart
    # from "didn't run at all" (Codex review minor 1). The two earlier
    # no-op returns above (flag disabled / no linked accounts) intentionally
    # send nothing, since those are not runs of the batch.
    #
    # Codex 2nd-round review minor: a `refused` result (idempotency index
    # missing/invalid) must NOT be sent as an ordinary "0 generated" summary
    # — that would look identical to a legitimate empty run and hide an
    # operational fault behind a routine-looking notification. It gets its
    # own distinctly-worded, error-severity message instead.
    if notifier is not None:
        try:
            if notifier.is_available():
                if result.refused:
                    notifier.send(
                        f"Phase 5 refused: idempotency index missing/invalid — {result.refused_reason}",
                        route_type="report",
                        severity="error",
                    )
                else:
                    notifier.send(format_batch_summary(result), route_type="report")
        except Exception:
            logger.exception(
                "[AutoProposal] Phase 5 batch summary notification failed "
                "(proposals were already committed; this only affects visibility)"
            )

    return result

# -*- coding: utf-8 -*-
"""Toss Invest manual-approval order proposals (Phase 3 semi-automatic orders).

Semantics (source of truth:
docs/superpowers/specs/2026-07-17-toss-order-phase3-design.md — v2, redesigned
after a BLOCK-verdict independent review of the v1 implementation found 8
blocker + 6 major issues, mostly around missing atomicity and an incomplete
state machine for "a live order POST is in flight"):

- Two-step manual approval, never automatic: ``create_proposal`` validates and
  stores a ``pending`` proposal (10-minute TTL); ``execute_proposal`` is a
  *separate* call that requires ``confirm=True`` (even in dry-run — the point
  is that the caller's request shape never has to change when flipping
  ``TOSS_ORDER_LIVE``) before anything happens.
- Default is dry-run: unless ``TOSS_ORDER_LIVE`` is enabled (strict parsing —
  only the exact value ``"true"``, see ``src.config.parse_strict_true_env_bool``),
  ``execute_proposal`` never calls ``TossFetcher.place_order`` at all — it
  writes a ``dry_run_executed`` proposal + a ``mode='dry_run'`` audit event
  and returns. Dry-run never passes through the ``executing`` reservation
  state below — there is nothing to reserve against since Toss is never
  contacted.
- State machine v2: ``pending -> executing -> executed | failed |
  outcome_unknown``; ``pending -> canceled | expired | dry_run_executed``.
  Entering ``executing`` is an *atomic claim*
  (``PortfolioRepository.claim_proposal_for_execution``): inside one write
  transaction, re-validate the row is still ``pending``, re-check every
  amount cap against the live database state, and record the reservation —
  all before any Toss POST is attempted ("로그 없으면 주문 없음": if the
  reservation commit fails, the POST itself never happens). Two concurrent
  ``execute_proposal`` calls for the same proposal race on this one
  transaction; only one can claim it.
- Distributed-transaction contract for the POST outcome: an explicit Toss
  success resolves to ``executed``; an explicit Toss business-rule rejection
  resolves to ``failed``; anything ambiguous — a lost/timed-out response, a
  success response missing ``orderId``, Toss's own 409
  ``request-in-progress``, an ``idempotency-key-conflict``, or a DB write
  failure immediately after a successful POST — resolves to
  ``outcome_unknown`` instead of guessing in either direction.
  ``outcome_unknown`` (like ``executing``) keeps counting against the daily
  cap until ``reconcile_proposal`` resolves it by re-POSTing the same
  idempotent ``clientOrderId`` to Toss.
- Every KRW amount cap (per-order ``TOSS_ORDER_MAX_AMOUNT_KRW``, daily
  ``TOSS_ORDER_DAILY_MAX_AMOUNT_KRW``, and the unconditional
  100,000,000 KRW hard-reject that no config can raise) is enforced twice:
  once at proposal creation (best-effort, non-atomic — dry-run/pending has no
  money at risk yet) and again — recomputed, not trusted from the stored
  value, and atomically — inside the executing-claim transaction, since the
  10-minute TTL window is long enough for a USD order's KRW-converted
  estimate to drift with FX. The daily cap sums live reservations directly off
  ``PortfolioOrderProposal`` (not the audit log — see that model's docstring)
  for the current KST calendar date.
- FX fail-closed: a non-KRW order's KRW-converted estimate is only trusted
  when ``PortfolioService.convert_amount`` reports a genuine direct/inverse
  rate that is not flagged stale and is not the ``fallback_1_to_1`` safety
  net — otherwise ``FxRateUnavailableError`` fails the request closed rather
  than risking a $5,000 order being capped as if it were 5,000 KRW. On top of
  that flag-based check, the *wall-clock* age of the actual
  ``PortfolioFxRate`` row used (``PortfolioService.get_fx_rate_record``) is
  also checked: a row older than 24 hours fails closed the same way even when
  ``is_stale=False`` (design spec §3 "stale(24시간 초과)", reviewer re-review
  major 1) — ``is_stale`` only ever reflected a refresh-job's own assessment,
  not how long ago the row was actually written.
- ``clientOrderId="dsa-{proposal_uuid}"`` makes every live order POST
  idempotent-safe on Toss's side — this is exactly what
  ``reconcile_proposal`` relies on to resolve an ``outcome_unknown``/
  ``executing`` proposal: re-POSTing the same body with the same id either
  returns the existing order (converges to ``executed``), gets Toss's own
  ``request-in-progress`` (stays ``outcome_unknown``, try again later), or
  surfaces a genuine defect via ``idempotency-key-conflict``
  (``OrderIdempotencyConflictError``).
- A proposal never writes to ``portfolio_trades`` — a filled order is only
  reflected in the ledger later by the existing Phase 2 broker-link sync
  (design spec §5): this module's job ends at "the order now exists on Toss
  (or would have, in dry-run)", not at reconciling holdings.
- Account eligibility is one bundled check (design spec §3 "계좌 자격 단일
  검증"): the account must be active and its broker link must be an active
  ``provider='toss'`` link. Any failure raises ``BrokerLinkNotFoundError``
  uniformly (mapped to 404 at the API layer). Per the v3 auth clarification
  (design spec §3 "인증 (필수, v3 명확화)", reviewer major 2), this system has
  no per-session user identity — a session that passes the single shared
  admin auth check manages *every* account, full stop. There is deliberately
  no self-asserted caller-identity header (e.g. an ``X-Portfolio-Owner-Id``
  request header compared against an account's own ``owner_id``) gating this
  eligibility check: an unverified header a client can set to any value is
  not access control, it is theater, so this module never accepts or checks
  one. The *separate* "is the caller authenticated at all" requirement
  (``ADMIN_AUTH_ENABLED=true`` + a verified session — design spec §3 "인증
  필수", Codex blocker 1) is enforced at the API layer, since only that layer
  has access to the HTTP request/session.
- Cancellation is three distinct actions: ``cancel_proposal`` withdraws a
  still-``pending`` proposal that never reached Toss (no live-order gate
  needed — nothing to cancel there); ``cancel_order`` cancels an
  already-placed live order by its Toss ``orderId``, only for an order this
  system's own audit trail shows it placed, and only once its proposal is
  ``executed`` (an ``executing``/``outcome_unknown`` proposal must go through
  ``reconcile_proposal`` first — design spec v2 §3); ``reconcile_proposal``
  resolves an ``executing``/``outcome_unknown`` proposal by re-POSTing its
  idempotent ``clientOrderId``.
- Phase 5 F4c (design spec
  docs/superpowers/specs/2026-07-20-toss-auto-proposal-phase5-design.md,
  Codex adversarial review): ``execute_proposal`` additionally re-confirms
  the live price for an auto-generated (``generation_source == 'auto'``)
  LIMIT sell proposal right before the dry-run/live branch — refusing
  (``StalePriceReconfirmationRequiredError``) when no fresh, timestamped
  quote is obtainable or the price has drifted materially since creation.
  Strictly additive/auto-gated: a manual proposal's execute path never
  triggers this check. See ``_reconfirm_auto_sell_price``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from data_provider.base import DataFetchError
from data_provider.toss_fetcher import TossFetcher, TossOrderNotLiveError, TossOrderRejectedError
from data_provider.us_index_mapping import is_us_stock_code
from src.data.stock_index_loader import resolve_index_stock_code
from src.repositories.portfolio_repo import (
    PendingProposalCapExceededError,
    PortfolioRepository,
)
from src.services.market_symbol_utils import is_suffix_market_symbol, split_suffix_symbol
from src.services.portfolio_broker_sync_service import (
    BrokerLinkNotFoundError,
    TossNotConfiguredError,
    TossUpstreamError,
    _now_kst_naive,
)
from src.services.portfolio_service import PortfolioService
from src.storage import PortfolioOrderProposal

logger = logging.getLogger(__name__)

# Design spec §3: 1억원(100,000,000 KRW) 이상은 설정과 무관하게 무조건 거부 —
# confirmHighValueOrder is never auto-set by this system.
_HIGH_VALUE_HARD_REJECT_KRW = 100_000_000.0
_PROPOSAL_TTL_MINUTES = 10
_MAX_PENDING_PROPOSALS = 10
_EPS = 1e-6

# Design spec §3 "FX (fail-closed)" — "stale(24시간 초과)": a non-KRW order's
# KRW estimate is refused when the underlying PortfolioFxRate row's own
# updated_at wall-clock timestamp is older than this, even when its
# is_stale flag is False (reviewer re-review major 1 — is_stale only ever
# flagged a *refresh-job* data-quality problem, never how long ago the row
# itself was actually written).
_FX_RATE_MAX_AGE = timedelta(hours=24)

# Toss order-error codes whose outcome is inherently ambiguous — the request
# may or may not have actually created/found an order — so they resolve to
# ``outcome_unknown`` instead of ``failed`` (design spec v2 §3):
#   - "request-in-progress": Toss's own in-flight-duplicate 409.
#   - "idempotency-key-conflict": this clientOrderId already exists with a
#     different body — a real defect (surfaced via reconcile as
#     ``OrderIdempotencyConflictError``), but *not* proof no order exists.
_IN_DOUBT_TOSS_CODES = frozenset({"request-in-progress", "idempotency-key-conflict"})


class ProposalNotFoundError(Exception):
    """Raised when a proposal_uuid does not resolve to a row for the given account."""


class ProposalNotExecutableError(Exception):
    """Raised when execute/cancel is attempted on a proposal that is not (or
    is no longer) ``pending`` — carries the actual status in the message."""


class ProposalInProgressError(Exception):
    """Raised when execute is called on a proposal that is already
    ``executing``/``outcome_unknown`` — a concurrent claim (or an unresolved
    prior attempt) is already in flight; the caller should poll
    ``reconcile_proposal`` instead of executing again."""


class ProposalNotReconcilableError(Exception):
    """Raised when ``reconcile_proposal`` is called on a proposal that is not
    ``executing``/``outcome_unknown`` — there is nothing ambiguous to resolve."""


class OrderIdempotencyConflictError(Exception):
    """Raised when Toss's idempotent re-POST during reconcile reports
    ``idempotency-key-conflict`` — this ``clientOrderId`` already exists with
    a *different* request body than what this system is retrying, which is a
    genuine defect (a reused/corrupted clientOrderId), not a transient state.
    The proposal is left ``outcome_unknown`` for manual investigation."""


class ConfirmRequiredError(Exception):
    """Raised when execute is called without ``confirm=True`` — required in
    both dry-run and live mode (design spec §5)."""


class OrderTypeNotAllowedError(Exception):
    """Raised for a MARKET order proposal when ``TOSS_ORDER_ALLOW_MARKET`` is
    not enabled (design spec §3: LIMIT-only by default)."""


class HighValueOrderRejectedError(Exception):
    """Raised when the estimated KRW amount is at or above the unconditional
    100,000,000 KRW hard-reject threshold."""


class OrderLimitExceededError(Exception):
    """Raised when the estimated KRW amount exceeds the per-order or daily cap."""

    def __init__(self, *, limit_type: str, message: str):
        super().__init__(message)
        self.limit_type = limit_type  # "per_order" | "daily"


class InsufficientBuyingPowerError(Exception):
    """Raised when the estimated order amount exceeds Toss's reported cash buying power."""


class InsufficientSellableQuantityError(Exception):
    """Raised when the requested sell quantity exceeds Toss's reported sellable quantity."""


class PendingProposalLimitExceededError(Exception):
    """Raised when an account already has the maximum number of active pending proposals."""


class ReferencePriceUnavailableError(Exception):
    """Raised when a MARKET order proposal needs a reference quote (for KRW-cap
    estimation) and none could be obtained — fails closed rather than
    proposing an unbounded-amount market order."""


class FxRateUnavailableError(Exception):
    """Raised when a non-KRW order's KRW-converted estimate would rely on a
    missing, stale, or ``fallback_1_to_1`` exchange rate (design spec v2 §3
    FX fail-closed) — refuses to validate or cap the order rather than risk
    silently under-pricing it (e.g. treating $5,000 as 5,000 KRW)."""


class OrderNotFoundError(Exception):
    """Raised when a toss_order_id does not resolve to an order this system
    itself placed for the given account (no audit trail = not cancelable/
    queryable here)."""


class OrderAuditPersistFailedError(Exception):
    """Raised when a real Toss action (order placed/canceled, or its outcome
    determined) succeeded but the matching audit/proposal-status record could
    not be durably persisted even via the ``outcome_unknown`` fallback
    (design spec §7: a missing log entry must never let the daily cap
    silently regress). The real-world side effect already happened; this
    signals that manual reconciliation is required, not that nothing
    happened."""


def _resolve_order_symbol(raw_symbol: str) -> Tuple[str, str, str, str]:
    """Map a user-supplied symbol to ``(storage_symbol, toss_symbol, market, currency)``.

    Accepts the repository's canonical KR suffix form (``"005930.KS"``/
    ``"005930.KQ"``), a bare KR 6-digit code (resolved via the generated stock
    index the same way the Phase 2 sync service resolves opening-trade
    symbols, falling back to ``.KS`` — purely for display/audit here, since
    Toss's own order API only ever needs the bare code), or a US ticker
    as-is. Raises ``ValueError`` for anything else.
    """
    code = (raw_symbol or "").strip().upper()
    if not code:
        raise ValueError("symbol is required")

    if is_suffix_market_symbol(code, "kr"):
        parts = split_suffix_symbol(code)
        base = parts[0] if parts else code
        return code, base, "kr", "KRW"

    if code.isdigit() and len(code) == 6:
        resolved = resolve_index_stock_code(code)
        storage_symbol = resolved or f"{code}.KS"
        return storage_symbol, code, "kr", "KRW"

    if is_us_stock_code(code):
        return code, code, "us", "USD"

    raise ValueError(f"Unsupported symbol for a Toss order: {raw_symbol!r}")


def _fetch_reference_price(storage_symbol: str) -> Optional[float]:
    """Best-effort live quote lookup, used only to estimate a MARKET order's
    KRW amount for cap enforcement (never used as the order's own price —
    MARKET orders never carry a ``price`` field to Toss). Returns ``None`` on
    any failure so the caller can fail closed instead of proposing an
    unbounded-amount order."""
    try:
        from data_provider.base import DataFetcherManager

        quote = DataFetcherManager().get_realtime_quote(storage_symbol, log_final_failure=False)
    except Exception as exc:
        logger.warning("[TossOrder] reference price fetch failed for %s: %s", storage_symbol, exc)
        return None
    if quote is None:
        return None
    try:
        numeric = float(getattr(quote, "price", None))
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


@dataclass
class FreshQuote:
    """A live quote that has passed the Phase 5 freshness requirement (design
    spec F4). See ``_fetch_fresh_reference_quote``."""

    price: float
    source: Optional[str]
    provider_timestamp: str
    age_seconds: int


def _fetch_fresh_reference_quote(storage_symbol: str, *, max_age_seconds: float) -> Optional[FreshQuote]:
    """Best-effort live quote lookup with an **enforced** freshness
    requirement (Phase 5 Codex adversarial review F4: "즉시매도 proposals
    execute with an unverified stale price"). Unlike ``_fetch_reference_price``
    above (Phase 3's own helper, used only to bound a MARKET order's KRW cap
    estimate — where any quote, even an untimed provider fallback, is
    acceptable since price there never becomes the order's own execution
    price), this fails closed (returns ``None``) whenever the quote carries
    no verifiable provider timestamp at all, or when its age exceeds
    ``max_age_seconds`` — because here the price *is* the sell order's own
    LIMIT price. ``data_provider.base``'s ``_enrich_realtime_quote`` sets
    ``quote.provider_timestamp``/``quote.stale_seconds`` only when the
    underlying data source actually reported a market timestamp; a source
    that never does (or is momentarily unable to) makes every immediate-sell
    Phase 5 proposal skip fail-closed rather than price off an unverifiable
    quote — an intentional, documented trade-off for an opt-in defensive
    feature (design spec F4).

    Only used by the Phase 5 auto-generated immediate-sell path
    (``AutoProposalService``) and the auto-only execute-time price reconfirm
    (``PortfolioOrderService._reconfirm_auto_sell_price``) — never by Phase
    3/4's own manual price paths, which must not change behavior (additive
    only)."""
    try:
        from data_provider.base import DataFetcherManager

        quote = DataFetcherManager().get_realtime_quote(storage_symbol, log_final_failure=False)
    except Exception as exc:
        logger.warning("[TossOrder] fresh reference quote fetch failed for %s: %s", storage_symbol, exc)
        return None
    if quote is None:
        return None
    try:
        price = float(getattr(quote, "price", None))
    except (TypeError, ValueError):
        return None
    if not (price > 0):
        return None
    provider_timestamp = getattr(quote, "provider_timestamp", None)
    stale_seconds = getattr(quote, "stale_seconds", None)
    if provider_timestamp is None or stale_seconds is None:
        # Fail-closed: no verifiable market timestamp, so freshness cannot be
        # proven (design spec F4a "타임스탬프 없음 시 fail-closed skip").
        return None
    try:
        age_seconds = float(stale_seconds)
    except (TypeError, ValueError):
        return None
    if age_seconds < 0 or age_seconds > max_age_seconds:
        return None
    source = getattr(quote, "source", None)
    return FreshQuote(
        price=price,
        source=str(source) if source else None,
        provider_timestamp=str(provider_timestamp),
        age_seconds=int(age_seconds),
    )


class StalePriceReconfirmationRequiredError(Exception):
    """Raised when an auto-generated (Phase 5) immediate-sell proposal cannot
    be executed because a fresh, verifiable market quote could not be
    obtained, or the live price has drifted materially from the proposal's
    stored LIMIT price (design spec F4c "실행 시점 가격 갱신"). This is the
    residual protection specific to the immediate-sell path, where a human is
    present at execute time to react and re-review; the conditional-order
    (stop-loss) path has no equivalent control since Toss auto-executes on
    trigger with nobody present — that path's control is the F3 disclosure
    instead (see ``AutoProposalService`` module docstring)."""


class PortfolioOrderService:
    """Business logic for Toss order proposals: create/execute/reconcile/
    cancel/list, limit enforcement, and the dry-run/live mode branch."""

    def __init__(
        self,
        *,
        portfolio_service: Optional[PortfolioService] = None,
        repo: Optional[PortfolioRepository] = None,
        fetcher: Optional[TossFetcher] = None,
        config: Any = None,
    ):
        self.portfolio_service = portfolio_service or PortfolioService()
        self.repo = repo or PortfolioRepository()
        self._fetcher = fetcher
        self._config = config

    def _ensure_fetcher(self) -> TossFetcher:
        if self._fetcher is not None:
            return self._fetcher
        if not TossFetcher.has_configured_credentials():
            raise TossNotConfiguredError(
                "TOSS_CLIENT_ID/TOSS_CLIENT_SECRET not configured; order proposals are unavailable"
            )
        return TossFetcher()

    def _get_config(self) -> Any:
        if self._config is not None:
            return self._config
        try:
            from src.config import get_config

            return get_config()
        except Exception:
            return None

    def _market_orders_allowed(self) -> bool:
        cfg = self._get_config()
        if cfg is not None:
            return bool(getattr(cfg, "toss_order_allow_market", False))
        return False

    def _max_order_amount_krw(self) -> float:
        cfg = self._get_config()
        if cfg is not None:
            return float(getattr(cfg, "toss_order_max_amount_krw", 1_000_000.0))
        return 1_000_000.0

    def _daily_max_amount_krw(self) -> float:
        cfg = self._get_config()
        if cfg is not None:
            return float(getattr(cfg, "toss_order_daily_max_amount_krw", 5_000_000.0))
        return 5_000_000.0

    def _is_live(self) -> bool:
        return TossFetcher.is_order_live_enabled(self._config)

    # ------------------------------------------------------------------
    # Phase 5 F4 config (auto-only: quote freshness at execute-time reconfirm)
    # ------------------------------------------------------------------
    def _quote_max_age_seconds(self) -> float:
        cfg = self._get_config()
        if cfg is not None:
            return float(getattr(cfg, "phase5_quote_max_age_seconds", 600.0))
        return 600.0

    def _execute_price_drift_bps(self) -> float:
        cfg = self._get_config()
        if cfg is not None:
            return float(getattr(cfg, "phase5_execute_price_drift_bps", 200.0))
        return 200.0

    def _phase5_sell_slippage_bps(self) -> float:
        cfg = self._get_config()
        if cfg is not None:
            return float(getattr(cfg, "phase5_sell_slippage_bps", 50.0))
        return 50.0

    def _reconfirm_auto_sell_price(self, proposal: PortfolioOrderProposal) -> None:
        """F4c execute-time reconfirm for an auto-generated (Phase 5)
        immediate-sell proposal: fetch a fresh, freshness-verified quote and
        refuse execution — raising ``StalePriceReconfirmationRequiredError``
        — when none is obtainable, or when the live price has drifted
        materially from what the proposal's stored LIMIT price would be if
        it were (re)computed off today's quote with the same slippage. This
        is the one execution path where a human is present at execute time
        to react (design spec F4 "경로별": the conditional/stop-loss path has
        no equivalent — see the module-level docstring and F3's disclosure
        for that path's control instead). Only ever called for
        ``generation_source == 'auto'`` proposals; a manual proposal's
        execute path is completely unchanged."""
        max_age = self._quote_max_age_seconds()
        fresh = _fetch_fresh_reference_quote(proposal.storage_symbol, max_age_seconds=max_age)
        if fresh is None:
            raise StalePriceReconfirmationRequiredError(
                f"No fresh, timestamped market quote available for {proposal.storage_symbol} "
                f"(required max age {max_age:.0f}s); refusing to execute this auto-generated "
                f"sell proposal without a verifiably current price (design spec F4)"
            )
        if not proposal.price:
            return
        slippage = self._phase5_sell_slippage_bps() / 10000.0
        recomputed_limit = fresh.price * (1.0 - slippage)
        drift_threshold = self._execute_price_drift_bps() / 10000.0
        drift = abs(recomputed_limit - proposal.price) / proposal.price
        if drift >= drift_threshold:
            raise StalePriceReconfirmationRequiredError(
                f"Live price for {proposal.storage_symbol} would now reprice this proposal's "
                f"LIMIT to {recomputed_limit:,.4f} (from a fresh quote of {fresh.price:,.4f}), "
                f"which has drifted {drift * 100:.2f}% from the stored LIMIT {proposal.price:,.4f} "
                f"(threshold {drift_threshold * 100:.2f}%); refusing to execute without "
                f"re-review — cancel this proposal and let the next batch/manual review "
                f"re-price it off the current market (design spec F4c)"
            )

    # ------------------------------------------------------------------
    # Account eligibility (design spec §3 "계좌 자격 단일 검증"): active account
    # + active provider='toss' link, bundled. No caller-identity check here
    # (design spec v3 "인증 (필수, v3 명확화)", reviewer major 2) — a session
    # that clears ``_require_order_auth`` at the API layer manages every
    # account in this single-admin system; there is no self-asserted owner
    # header to (not) trust.
    # ------------------------------------------------------------------
    def _resolve_eligible_account_and_link(self, *, account_id: int) -> Tuple[Any, Any]:
        account = self.repo.get_account(account_id)
        if account is None:
            raise BrokerLinkNotFoundError(f"No active account_id={account_id}")
        link = self.repo.get_broker_link_by_account(account_id, active_only=True)
        if link is None or link.provider != "toss":
            raise BrokerLinkNotFoundError(f"No active Toss broker link for account_id={account_id}")
        return account, link

    # ------------------------------------------------------------------
    # Limit enforcement — best-effort pre-check at proposal creation only
    # (design spec §3/§4: validated again, atomically, at execute/claim time
    # via PortfolioRepository.claim_proposal_for_execution — a pending
    # proposal has no money reserved yet, so this check does not need to be
    # race-free the way the executing-claim does).
    # ------------------------------------------------------------------
    def _enforce_amount_caps_best_effort(self, *, account_id: int, est_amount_krw: float, now: datetime) -> None:
        if est_amount_krw >= _HIGH_VALUE_HARD_REJECT_KRW:
            raise HighValueOrderRejectedError(
                f"Estimated order amount {est_amount_krw:,.0f} KRW is at or above the "
                f"{_HIGH_VALUE_HARD_REJECT_KRW:,.0f} KRW hard-reject threshold; this system never "
                f"auto-confirms high-value orders (confirmHighValueOrder is never sent)"
            )
        per_order_cap = self._max_order_amount_krw()
        if est_amount_krw > per_order_cap + _EPS:
            raise OrderLimitExceededError(
                limit_type="per_order",
                message=(
                    f"Estimated order amount {est_amount_krw:,.0f} KRW exceeds the per-order cap "
                    f"{per_order_cap:,.0f} KRW (TOSS_ORDER_MAX_AMOUNT_KRW)"
                ),
            )
        daily_cap = self._daily_max_amount_krw()
        already_reserved = self.repo.sum_daily_reserved_and_executed_amount_krw(account_id, kst_date=now.date())
        if already_reserved + est_amount_krw > daily_cap + _EPS:
            raise OrderLimitExceededError(
                limit_type="daily",
                message=(
                    f"Estimated order amount {est_amount_krw:,.0f} KRW would push today's reserved+"
                    f"executed total to {already_reserved + est_amount_krw:,.0f} KRW, exceeding the "
                    f"daily cap {daily_cap:,.0f} KRW (TOSS_ORDER_DAILY_MAX_AMOUNT_KRW)"
                ),
            )

    def _estimate_amount_krw(
        self, *, order_type: str, storage_symbol: str, currency: str, quantity: float, price: Optional[float], now: datetime
    ) -> Tuple[float, float]:
        """Returns ``(amount_native, est_amount_krw)``. Raises
        ``ReferencePriceUnavailableError`` for a MARKET order with no
        reachable reference quote (fail closed), and ``FxRateUnavailableError``
        when a non-KRW conversion would rely on a missing/stale/fallback rate,
        *or* on a real rate row whose own wall-clock ``updated_at`` is more
        than 24 hours old even though ``is_stale`` reports ``False`` (design
        spec §3 FX fail-closed, reviewer re-review major 1 — ``is_stale`` only
        flags a refresh-job data-quality problem, not how long ago the row was
        actually written)."""
        reference_price = price
        if order_type == "MARKET":
            reference_price = _fetch_reference_price(storage_symbol)
            if reference_price is None:
                raise ReferencePriceUnavailableError(
                    f"Could not obtain a reference price for {storage_symbol} to validate a MARKET order"
                )
        amount_native = float(reference_price) * float(quantity)
        est_amount_krw, fx_stale, fx_source = self.portfolio_service.convert_amount(
            amount=amount_native,
            from_currency=currency,
            to_currency="KRW",
            as_of_date=now.date(),
        )
        if currency != "KRW" and (fx_stale or fx_source == "fallback_1_to_1"):
            raise FxRateUnavailableError(
                f"No reliable {currency}->KRW exchange rate available (source={fx_source!r}, "
                f"stale={fx_stale}); refusing to validate/execute a {currency} order without a "
                f"trustworthy FX rate (design spec §3 FX fail-closed)"
            )
        if currency != "KRW" and fx_source in ("direct_rate", "inverse_rate"):
            # Only enforced when a real DB-backed rate row was actually used
            # (fx_source is direct/inverse only when convert_amount found
            # one) — additive on top of the is_stale/fallback check above,
            # never a substitute for it.
            fx_record = self.portfolio_service.get_fx_rate_record(
                from_currency=currency, to_currency="KRW", as_of_date=now.date()
            )
            updated_at = getattr(fx_record, "updated_at", None) if fx_record is not None else None
            if updated_at is None:
                # Fail closed: fx_source claims a real DB-backed rate was used,
                # so a missing row / missing timestamp means we cannot prove
                # freshness — refuse rather than skip the wall-clock check
                # (design spec §3 FX fail-closed; 3rd-review residual).
                raise FxRateUnavailableError(
                    f"{currency}->KRW rate reported source={fx_source!r} but no verifiable rate "
                    f"row/updated_at was found; refusing to validate/execute a {currency} order "
                    f"without a provably fresh FX rate (design spec §3 FX fail-closed)"
                )
            if (datetime.now() - updated_at) > _FX_RATE_MAX_AGE:
                raise FxRateUnavailableError(
                    f"{currency}->KRW exchange rate row is {datetime.now() - updated_at} old "
                    f"(updated_at={updated_at.isoformat()}), older than the 24-hour wall-clock "
                    f"freshness limit; refusing to validate/execute a {currency} order without a "
                    f"recently-updated FX rate (design spec §3 FX fail-closed, stale(24시간 초과))"
                )
        return amount_native, est_amount_krw

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    @staticmethod
    def _serialize_proposal(row: PortfolioOrderProposal, *, mode: Optional[str] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "proposal_uuid": row.proposal_uuid,
            "account_id": int(row.account_id),
            "symbol": row.symbol,
            "storage_symbol": row.storage_symbol,
            "market": row.market,
            "currency": row.currency,
            "side": row.side,
            "order_type": row.order_type,
            "price": row.price,
            "quantity": row.quantity,
            "est_amount_krw": row.est_amount_krw,
            "status": row.status,
            "toss_order_id": row.toss_order_id,
            "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "executed_at": row.executed_at.isoformat() if row.executed_at else None,
            "generation_source": row.generation_source,
            "source_signal_id": row.source_signal_id,
            "generation_date": row.generation_date.isoformat() if row.generation_date else None,
        }
        if mode is not None:
            data["mode"] = mode
        return data

    # ------------------------------------------------------------------
    # Create proposal
    # ------------------------------------------------------------------
    def create_proposal(
        self,
        *,
        account_id: int,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "LIMIT",
        price: Optional[float] = None,
        generation_source: str = "manual",
        source_signal_id: Optional[int] = None,
        generation_date: Optional[date] = None,
        proposed_audit_detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """``generation_source``/``source_signal_id``/``generation_date``
        (Phase 5, additive, default ``'manual'``/``None``/``None``) are
        forwarded verbatim to the repo insert — see
        ``PortfolioRepository.create_order_proposal_with_audit`` for the
        composite-unique-index idempotency contract they enable (v6, Codex
        adversarial review F1+F2). ``proposed_audit_detail`` (F4b, additive)
        is forwarded verbatim too, for recording a live quote's source/
        timestamp/age on the ``proposed`` audit event. Every other
        validation/limit/FX/sellable-quantity check below is unchanged and
        runs identically for auto-generated proposals; the Phase 5 batch
        generator is just another caller of this same, unmodified path."""
        side_norm = (side or "").strip().lower()
        if side_norm not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        order_type_norm = (order_type or "LIMIT").strip().upper()
        if order_type_norm not in ("LIMIT", "MARKET"):
            raise ValueError("order_type must be 'LIMIT' or 'MARKET'")
        if order_type_norm == "MARKET" and not self._market_orders_allowed():
            raise OrderTypeNotAllowedError(
                "MARKET orders are disabled; set TOSS_ORDER_ALLOW_MARKET=true to opt in"
            )
        if quantity is None or quantity <= 0:
            raise ValueError("quantity must be > 0")
        if order_type_norm == "LIMIT":
            if price is None or price <= 0:
                raise ValueError("price is required for LIMIT orders")
        elif price is not None:
            raise ValueError("price must not be set for MARKET orders")

        _account, link = self._resolve_eligible_account_and_link(account_id=account_id)

        storage_symbol, toss_symbol, market, currency = _resolve_order_symbol(symbol)

        now = _now_kst_naive()
        amount_native, est_amount_krw = self._estimate_amount_krw(
            order_type=order_type_norm,
            storage_symbol=storage_symbol,
            currency=currency,
            quantity=quantity,
            price=price,
            now=now,
        )

        self._enforce_amount_caps_best_effort(account_id=account_id, est_amount_krw=est_amount_krw, now=now)

        fetcher = self._ensure_fetcher()
        if side_norm == "buy":
            try:
                buying_power = fetcher.get_buying_power(link.external_account_seq, currency=currency)
            except DataFetchError as exc:
                raise TossUpstreamError(str(exc)) from exc
            if amount_native > buying_power + _EPS:
                raise InsufficientBuyingPowerError(
                    f"Estimated order amount {amount_native:,.2f} {currency} exceeds buying power "
                    f"{buying_power:,.2f} {currency}"
                )
        else:
            try:
                sellable = fetcher.get_sellable_quantity(link.external_account_seq, symbol=toss_symbol)
            except DataFetchError as exc:
                raise TossUpstreamError(str(exc)) from exc
            if quantity > sellable + _EPS:
                raise InsufficientSellableQuantityError(
                    f"Requested quantity {quantity} exceeds sellable quantity {sellable}"
                )

        proposal_uuid = str(uuid.uuid4())
        expires_at = now + timedelta(minutes=_PROPOSAL_TTL_MINUTES)
        try:
            row = self.repo.create_order_proposal_with_audit(
                account_id=account_id,
                proposal_uuid=proposal_uuid,
                symbol=toss_symbol,
                storage_symbol=storage_symbol,
                market=market,
                currency=currency,
                side=side_norm,
                order_type=order_type_norm,
                price=price,
                quantity=float(quantity),
                est_amount_krw=est_amount_krw,
                created_at=now,
                expires_at=expires_at,
                max_pending_proposals=_MAX_PENDING_PROPOSALS,
                generation_source=generation_source,
                source_signal_id=source_signal_id,
                generation_date=generation_date,
                proposed_audit_detail=proposed_audit_detail,
            )
        except PendingProposalCapExceededError as exc:
            raise PendingProposalLimitExceededError(str(exc)) from exc

        mode_preview = "live" if self._is_live() else "dry_run"
        return self._serialize_proposal(row, mode=mode_preview)

    # ------------------------------------------------------------------
    # Execute proposal
    # ------------------------------------------------------------------
    def execute_proposal(
        self,
        *,
        account_id: int,
        proposal_uuid: str,
        confirm: bool,
    ) -> Dict[str, Any]:
        if not confirm:
            raise ConfirmRequiredError(
                "confirm: true is required to execute a proposal (required in dry-run mode too)"
            )

        now = _now_kst_naive()
        _account, link = self._resolve_eligible_account_and_link(account_id=account_id)

        proposal = self.repo.get_order_proposal(proposal_uuid, account_id=account_id, now=now)
        if proposal is None:
            raise ProposalNotFoundError(f"No proposal {proposal_uuid} for account_id={account_id}")

        if proposal.status in ("executed", "dry_run_executed"):
            # Idempotent retry (design spec §3): return the cached result
            # without a second Toss call.
            cached_mode = "live" if proposal.status == "executed" else "dry_run"
            return self._serialize_proposal(proposal, mode=cached_mode)
        if proposal.status in ("executing", "outcome_unknown"):
            raise ProposalInProgressError(
                f"proposal {proposal_uuid} is '{proposal.status}'; call .../reconcile instead of "
                f"executing again"
            )
        if proposal.status != "pending":
            raise ProposalNotExecutableError(
                f"proposal {proposal_uuid} is '{proposal.status}' and can no longer be executed"
            )

        # Re-validate fresh (design spec §3 "실행 시 재확인") — never trust the
        # stored est_amount_krw, since FX may have moved for a USD order.
        amount_native, est_amount_krw = self._estimate_amount_krw(
            order_type=proposal.order_type,
            storage_symbol=proposal.storage_symbol,
            currency=proposal.currency,
            quantity=proposal.quantity,
            price=proposal.price,
            now=now,
        )

        fetcher = self._ensure_fetcher()
        if proposal.side == "buy":
            try:
                buying_power = fetcher.get_buying_power(link.external_account_seq, currency=proposal.currency)
            except DataFetchError as exc:
                raise TossUpstreamError(str(exc)) from exc
            if amount_native > buying_power + _EPS:
                self._fail_proposal(
                    proposal_uuid=proposal_uuid,
                    account_id=account_id,
                    now=now,
                    error_code="insufficient-buying-power",
                    detail={"buying_power": buying_power},
                )
                raise InsufficientBuyingPowerError(
                    f"Estimated order amount {amount_native:,.2f} {proposal.currency} exceeds buying "
                    f"power {buying_power:,.2f} {proposal.currency}"
                )
        else:
            try:
                sellable = fetcher.get_sellable_quantity(link.external_account_seq, symbol=proposal.symbol)
            except DataFetchError as exc:
                raise TossUpstreamError(str(exc)) from exc
            if proposal.quantity > sellable + _EPS:
                self._fail_proposal(
                    proposal_uuid=proposal_uuid,
                    account_id=account_id,
                    now=now,
                    error_code="insufficient-sellable-quantity",
                    detail={"sellable_quantity": sellable},
                )
                raise InsufficientSellableQuantityError(
                    f"Requested quantity {proposal.quantity} exceeds sellable quantity {sellable}"
                )

            # F4c (Codex adversarial review, additive/auto-only): an
            # auto-generated (Phase 5) LIMIT sell proposal gets one more
            # execute-time check — a fresh-price reconfirm — right next to
            # the sellable-quantity re-check above. Gated strictly on
            # generation_source == 'auto' so Phase 3's existing manual-order
            # execute behavior is completely unchanged (additive only); the
            # conditional-order (stop-loss) approve path intentionally has
            # no equivalent, since it auto-executes on trigger with nobody
            # present to react (design spec F4 "경로별" — see
            # ``AutoProposalService`` module docstring and F3's disclosure).
            if proposal.generation_source == "auto" and proposal.order_type == "LIMIT" and proposal.price:
                try:
                    self._reconfirm_auto_sell_price(proposal)
                except StalePriceReconfirmationRequiredError as exc:
                    self._fail_proposal(
                        proposal_uuid=proposal_uuid,
                        account_id=account_id,
                        now=now,
                        error_code="stale-price-reconfirmation-required",
                        detail={"reason": str(exc)},
                    )
                    raise

        # ---- The one branch point: dry-run vs live (design spec §7 risk
        # note — keep this the *only* place the two paths diverge). Dry-run
        # never enters 'executing' — there is no Toss contact to reserve
        # against, so a plain best-effort cap check + single atomic
        # transition is sufficient (unlike the live path below). ----
        if not self._is_live():
            try:
                self._enforce_amount_caps_best_effort(account_id=account_id, est_amount_krw=est_amount_krw, now=now)
            except (HighValueOrderRejectedError, OrderLimitExceededError) as exc:
                self._fail_proposal(
                    proposal_uuid=proposal_uuid,
                    account_id=account_id,
                    now=now,
                    error_code="limit-exceeded",
                    detail={"reason": str(exc)},
                )
                raise
            updated = self.repo.transition_proposal(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                from_statuses={"pending"},
                to_status="dry_run_executed",
                event="executed",
                mode="dry_run",
                executed_at=now,
                est_amount_krw_override=est_amount_krw,
            )
            if updated is None:
                raise ProposalNotFoundError(f"No proposal {proposal_uuid} for account_id={account_id}")
            return self._serialize_proposal(updated, mode="dry_run")

        # ---- Live path: atomic claim (pending -> executing), then the one
        # Toss POST, then resolve to executed/failed/outcome_unknown. ----
        claim = self.repo.claim_proposal_for_execution(
            proposal_uuid=proposal_uuid,
            account_id=account_id,
            now=now,
            est_amount_krw=est_amount_krw,
            high_value_threshold_krw=_HIGH_VALUE_HARD_REJECT_KRW,
            per_order_cap_krw=self._max_order_amount_krw(),
            daily_cap_krw=self._daily_max_amount_krw(),
            eps=_EPS,
        )
        if claim.outcome == "not_found":
            raise ProposalNotFoundError(f"No proposal {proposal_uuid} for account_id={account_id}")
        if claim.outcome == "already_terminal":
            cached_mode = "live" if claim.proposal.status == "executed" else "dry_run"
            return self._serialize_proposal(claim.proposal, mode=cached_mode)
        if claim.outcome == "in_progress":
            raise ProposalInProgressError(
                f"proposal {proposal_uuid} is '{claim.proposal.status}'; call .../reconcile instead "
                f"of executing again"
            )
        if claim.outcome == "not_executable":
            raise ProposalNotExecutableError(
                f"proposal {proposal_uuid} is '{claim.proposal.status}' and can no longer be executed"
            )
        if claim.outcome == "rejected":
            if claim.limit_type == "high_value":
                raise HighValueOrderRejectedError(claim.reason or "high-value order rejected")
            raise OrderLimitExceededError(limit_type=claim.limit_type or "unknown", message=claim.reason or "limit exceeded")

        # claim.outcome == "claimed" — the reservation is durably committed;
        # only now do we attempt the actual Toss POST.
        client_order_id = f"dsa-{proposal_uuid}"
        try:
            result = fetcher.place_order(
                link.external_account_seq,
                symbol=proposal.symbol,
                side=proposal.side.upper(),
                order_type=proposal.order_type,
                quantity=proposal.quantity,
                price=proposal.price,
                client_order_id=client_order_id,
            )
        except TossOrderNotLiveError as exc:
            # Should be unreachable (this service already checked
            # self._is_live() before claiming) — a mismatch here means the
            # service- and fetcher-level live checks disagreed mid-request
            # (e.g. config reloaded between the two checks). No HTTP call was
            # made, so this is unambiguously 'failed', not 'outcome_unknown'.
            logger.critical(
                "[TossOrder] service/fetcher live-gate mismatch for proposal_uuid=%s account_id=%s: %s",
                proposal_uuid,
                account_id,
                exc,
            )
            self._resolve_execution_outcome(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                outcome="failed",
                error_code="order-live-gate-mismatch",
                detail={"reason": str(exc)},
            )
            raise
        except TossOrderRejectedError as exc:
            if exc.code in _IN_DOUBT_TOSS_CODES:
                updated = self._resolve_execution_outcome(
                    proposal_uuid=proposal_uuid,
                    account_id=account_id,
                    now=now,
                    outcome="outcome_unknown",
                    error_code=exc.code,
                    detail={"status_code": exc.status_code, "message": exc.message, "data": exc.data},
                )
                return self._serialize_proposal(updated, mode="live")
            self._resolve_execution_outcome(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                outcome="failed",
                error_code=exc.code,
                detail={"status_code": exc.status_code, "message": exc.message, "data": exc.data},
            )
            raise
        except DataFetchError as exc:
            # Response lost / timeout / other transient upstream failure
            # *after* the reservation is already committed — we cannot tell
            # whether Toss actually created the order, so this resolves to
            # outcome_unknown (design spec v2 §3), not a re-raised error that
            # would leave the proposal's true state unrecorded.
            updated = self._resolve_execution_outcome(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                outcome="outcome_unknown",
                error_code="network-error",
                detail={"reason": str(exc)},
            )
            return self._serialize_proposal(updated, mode="live")

        toss_order_id = result.get("orderId") if isinstance(result, dict) else None
        if not toss_order_id:
            updated = self._resolve_execution_outcome(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                outcome="outcome_unknown",
                error_code="missing-order-id",
                detail={"reason": "place_order succeeded without an orderId", "result": result},
            )
            return self._serialize_proposal(updated, mode="live")

        updated = self._resolve_execution_outcome(
            proposal_uuid=proposal_uuid,
            account_id=account_id,
            now=now,
            outcome="executed",
            toss_order_id=toss_order_id,
        )
        return self._serialize_proposal(updated, mode="live")

    def _resolve_execution_outcome(
        self,
        *,
        proposal_uuid: str,
        account_id: int,
        now: datetime,
        outcome: str,
        toss_order_id: Optional[str] = None,
        error_code: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        from_statuses: Optional[set] = None,
    ) -> PortfolioOrderProposal:
        """Persist the ``{executing|outcome_unknown} -> {executed|failed|
        outcome_unknown}`` transition (``from_statuses`` defaults to just
        ``{"executing"}`` for the original execute-time POST; reconcile
        passes ``{"executing", "outcome_unknown"}`` since by the time it
        runs the row may already sit at either). If the *primary* write
        itself fails, best-effort fall back to writing ``outcome_unknown``
        instead (design spec §7 — "POST 후 DB 실패" is itself one of the
        outcome_unknown triggers, so even a DB hiccup right after a
        definitive Toss result must not silently drop the reservation from
        the daily cap). Only if *that* fallback also fails do we give up and
        raise ``OrderAuditPersistFailedError`` (manual reconciliation
        required)."""
        event_map = {"executed": "executed", "failed": "rejected", "outcome_unknown": "outcome_unknown"}
        resolved_from_statuses = from_statuses if from_statuses is not None else {"executing"}
        try:
            updated = self.repo.transition_proposal(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                from_statuses=resolved_from_statuses,
                to_status=outcome,
                event=event_map[outcome],
                mode="live",
                toss_order_id=toss_order_id,
                executed_at=now if outcome == "executed" else None,
                error_code=error_code,
                detail=detail,
            )
        except Exception as exc:
            logger.critical(
                "[TossOrder] CRITICAL: could not persist outcome=%s for proposal_uuid=%s account_id=%s "
                "(toss_order_id=%s): %s -- attempting outcome_unknown fallback",
                outcome,
                proposal_uuid,
                account_id,
                toss_order_id,
                exc,
            )
            if outcome == "outcome_unknown":
                raise OrderAuditPersistFailedError(
                    f"Could not persist outcome_unknown for proposal_uuid={proposal_uuid} "
                    f"(account_id={account_id}); manual reconciliation required: {exc}"
                ) from exc
            try:
                updated = self.repo.transition_proposal(
                    proposal_uuid=proposal_uuid,
                    account_id=account_id,
                    now=now,
                    from_statuses=resolved_from_statuses,
                    to_status="outcome_unknown",
                    event="outcome_unknown",
                    mode="live",
                    toss_order_id=toss_order_id,
                    detail={
                        "fallback_reason": "primary transition write failed",
                        "original_outcome": outcome,
                        "original_error_code": error_code,
                        "write_error": str(exc),
                    },
                )
            except Exception as exc2:
                logger.critical(
                    "[TossOrder] CRITICAL: outcome_unknown fallback also failed for proposal_uuid=%s "
                    "account_id=%s: %s -- manual reconciliation required",
                    proposal_uuid,
                    account_id,
                    exc2,
                )
                raise OrderAuditPersistFailedError(
                    f"Order outcome ({outcome}) for proposal_uuid={proposal_uuid} "
                    f"(account_id={account_id}, toss_order_id={toss_order_id}) could not be recorded "
                    f"even as outcome_unknown; manual reconciliation required: {exc2}"
                ) from exc2

        if updated is None:
            raise ProposalNotFoundError(f"No proposal {proposal_uuid} for account_id={account_id}")
        return updated

    def _fail_proposal(
        self,
        *,
        proposal_uuid: str,
        account_id: int,
        now: datetime,
        error_code: str,
        detail: Dict[str, Any],
    ) -> None:
        self.repo.transition_proposal(
            proposal_uuid=proposal_uuid,
            account_id=account_id,
            now=now,
            from_statuses={"pending"},
            to_status="failed",
            event="rejected",
            error_code=error_code,
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Reconcile an executing/outcome_unknown proposal (design spec v2 §3):
    # re-POST the same idempotent clientOrderId to converge on the real
    # outcome.
    # ------------------------------------------------------------------
    def reconcile_proposal(self, *, account_id: int, proposal_uuid: str) -> Dict[str, Any]:
        now = _now_kst_naive()
        _account, link = self._resolve_eligible_account_and_link(account_id=account_id)

        proposal = self.repo.get_order_proposal(proposal_uuid, account_id=account_id, now=now)
        if proposal is None:
            raise ProposalNotFoundError(f"No proposal {proposal_uuid} for account_id={account_id}")
        if proposal.status not in ("executing", "outcome_unknown"):
            raise ProposalNotReconcilableError(
                f"proposal {proposal_uuid} is '{proposal.status}'; only 'executing'/'outcome_unknown' "
                f"proposals can be reconciled"
            )

        fetcher = self._ensure_fetcher()
        client_order_id = f"dsa-{proposal_uuid}"
        try:
            result = fetcher.place_order(
                link.external_account_seq,
                symbol=proposal.symbol,
                side=proposal.side.upper(),
                order_type=proposal.order_type,
                quantity=proposal.quantity,
                price=proposal.price,
                client_order_id=client_order_id,
            )
        except TossOrderRejectedError as exc:
            if exc.code == "idempotency-key-conflict":
                self.repo.append_standalone_order_audit(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=proposal.symbol,
                    side=proposal.side,
                    order_type=proposal.order_type,
                    price=proposal.price,
                    quantity=proposal.quantity,
                    currency=proposal.currency,
                    est_amount_krw=proposal.est_amount_krw,
                    mode="live",
                    event="reconciled",
                    toss_order_id=None,
                    error_code=exc.code,
                    detail={"status_code": exc.status_code, "message": exc.message, "data": exc.data},
                    created_at=now,
                )
                logger.critical(
                    "[TossOrder] CRITICAL: idempotency-key-conflict reconciling proposal_uuid=%s "
                    "account_id=%s — clientOrderId reused with a different body; manual investigation "
                    "required",
                    proposal_uuid,
                    account_id,
                )
                raise OrderIdempotencyConflictError(
                    f"clientOrderId dsa-{proposal_uuid} already exists on Toss with a different request "
                    f"body; this is a defect, not a transient state — manual investigation required"
                ) from exc
            if exc.code == "request-in-progress":
                self.repo.append_standalone_order_audit(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=proposal.symbol,
                    side=proposal.side,
                    order_type=proposal.order_type,
                    price=proposal.price,
                    quantity=proposal.quantity,
                    currency=proposal.currency,
                    est_amount_krw=proposal.est_amount_krw,
                    mode="live",
                    event="reconciled",
                    toss_order_id=None,
                    error_code=exc.code,
                    detail={"status_code": exc.status_code, "message": exc.message, "data": exc.data},
                    created_at=now,
                )
                return self._serialize_proposal(
                    self.repo.get_order_proposal(proposal_uuid, account_id=account_id), mode="live"
                )
            # Any other explicit rejection: Toss is authoritatively telling us
            # no order exists/can exist under this clientOrderId.
            updated = self._resolve_execution_outcome(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                outcome="failed",
                error_code=exc.code,
                detail={"status_code": exc.status_code, "message": exc.message, "data": exc.data, "via": "reconcile"},
                from_statuses={"executing", "outcome_unknown"},
            )
            self.repo.append_standalone_order_audit(
                account_id=account_id,
                proposal_uuid=proposal_uuid,
                symbol=proposal.symbol,
                side=proposal.side,
                order_type=proposal.order_type,
                price=proposal.price,
                quantity=proposal.quantity,
                currency=proposal.currency,
                est_amount_krw=proposal.est_amount_krw,
                mode="live",
                event="reconciled",
                toss_order_id=None,
                error_code=exc.code,
                created_at=now,
            )
            return self._serialize_proposal(updated, mode="live")
        except DataFetchError as exc:
            raise TossUpstreamError(str(exc)) from exc

        toss_order_id = result.get("orderId") if isinstance(result, dict) else None
        if not toss_order_id:
            # A 2xx reconcile re-POST with no orderId is exactly as ambiguous
            # as the same case during the original execute-time POST (design
            # spec v2 §3 "orderId 누락" -> outcome_unknown) — it must not
            # silently drop the state/audit trail just because this happened
            # during reconcile instead (reviewer re-review minor finding):
            # keep the proposal outcome_unknown (it already was, or moves
            # from executing to outcome_unknown here), record the reconcile
            # attempt in the audit trail, and still surface a loud explicit
            # error to the caller rather than returning as if nothing had
            # gone wrong.
            updated = self._resolve_execution_outcome(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                outcome="outcome_unknown",
                error_code="missing-order-id",
                detail={
                    "reason": "reconcile place_order succeeded without an orderId",
                    "result": result,
                    "via": "reconcile",
                },
                from_statuses={"executing", "outcome_unknown"},
            )
            self.repo.append_standalone_order_audit(
                account_id=account_id,
                proposal_uuid=proposal_uuid,
                symbol=proposal.symbol,
                side=proposal.side,
                order_type=proposal.order_type,
                price=proposal.price,
                quantity=proposal.quantity,
                currency=proposal.currency,
                est_amount_krw=proposal.est_amount_krw,
                mode="live",
                event="reconciled",
                toss_order_id=None,
                error_code="missing-order-id",
                detail={"result": result},
                created_at=now,
            )
            raise TossUpstreamError(
                f"[Toss] reconcile place_order succeeded without an orderId: {result!r}; proposal "
                f"{proposal_uuid} remains 'outcome_unknown' pending further reconciliation"
            )

        updated = self._resolve_execution_outcome(
            proposal_uuid=proposal_uuid,
            account_id=account_id,
            now=now,
            outcome="executed",
            toss_order_id=toss_order_id,
            from_statuses={"executing", "outcome_unknown"},
        )
        self.repo.append_standalone_order_audit(
            account_id=account_id,
            proposal_uuid=proposal_uuid,
            symbol=proposal.symbol,
            side=proposal.side,
            order_type=proposal.order_type,
            price=proposal.price,
            quantity=proposal.quantity,
            currency=proposal.currency,
            est_amount_krw=proposal.est_amount_krw,
            mode="live",
            event="reconciled",
            toss_order_id=toss_order_id,
            created_at=now,
        )
        return self._serialize_proposal(updated, mode="live")

    # ------------------------------------------------------------------
    # Cancel a still-pending proposal (never reached Toss)
    # ------------------------------------------------------------------
    def cancel_proposal(self, *, account_id: int, proposal_uuid: str) -> Dict[str, Any]:
        self._resolve_eligible_account_and_link(account_id=account_id)
        now = _now_kst_naive()
        updated = self.repo.transition_proposal(
            proposal_uuid=proposal_uuid,
            account_id=account_id,
            now=now,
            from_statuses={"pending"},
            to_status="canceled",
            event="canceled",
        )
        if updated is None:
            raise ProposalNotFoundError(f"No proposal {proposal_uuid} for account_id={account_id}")
        if updated.status != "canceled":
            raise ProposalNotExecutableError(
                f"proposal {proposal_uuid} is '{updated.status}' and can no longer be canceled"
            )
        return self._serialize_proposal(updated)

    # ------------------------------------------------------------------
    # List proposals
    # ------------------------------------------------------------------
    def list_proposals(
        self,
        *,
        account_id: int,
        status: Optional[str] = None,
        generation_source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self._resolve_eligible_account_and_link(account_id=account_id)
        now = _now_kst_naive()
        rows = self.repo.list_order_proposals(
            account_id, status=status, generation_source=generation_source, now=now
        )
        return [self._serialize_proposal(row) for row in rows]

    # ------------------------------------------------------------------
    # Cancel an already-placed live order (distinct from cancel_proposal)
    # ------------------------------------------------------------------
    def cancel_order(self, *, account_id: int, toss_order_id: str) -> Dict[str, Any]:
        _account, link = self._resolve_eligible_account_and_link(account_id=account_id)

        proposal = self.repo.get_order_proposal_by_toss_order_id(toss_order_id, account_id=account_id)
        if proposal is None:
            raise OrderNotFoundError(
                f"No self-issued order {toss_order_id} for account_id={account_id}; only orders this "
                f"system placed (an audit trail must exist) can be canceled here"
            )
        if proposal.status in ("executing", "outcome_unknown"):
            raise ProposalNotReconcilableError(
                f"proposal {proposal.proposal_uuid} is '{proposal.status}'; call .../reconcile before "
                f"attempting to cancel its order (design spec v2 §3)"
            )

        fetcher = self._ensure_fetcher()
        now = _now_kst_naive()
        try:
            result = fetcher.cancel_order(link.external_account_seq, toss_order_id)
        except TossOrderRejectedError as exc:
            try:
                self.repo.append_standalone_order_audit(
                    account_id=account_id,
                    proposal_uuid=proposal.proposal_uuid,
                    symbol=proposal.symbol,
                    side=proposal.side,
                    order_type=proposal.order_type,
                    price=proposal.price,
                    quantity=proposal.quantity,
                    currency=proposal.currency,
                    est_amount_krw=proposal.est_amount_krw,
                    mode="live",
                    event="cancel_rejected",
                    toss_order_id=toss_order_id,
                    error_code=exc.code,
                    detail={"status_code": exc.status_code, "message": exc.message, "data": exc.data},
                    created_at=now,
                )
            except Exception:
                logger.warning(
                    "[TossOrder] failed to record cancel_rejected audit for toss_order_id=%s",
                    toss_order_id,
                    exc_info=True,
                )
            raise
        except DataFetchError as exc:
            raise TossUpstreamError(str(exc)) from exc

        try:
            self.repo.append_standalone_order_audit(
                account_id=account_id,
                proposal_uuid=proposal.proposal_uuid,
                symbol=proposal.symbol,
                side=proposal.side,
                order_type=proposal.order_type,
                price=proposal.price,
                quantity=proposal.quantity,
                currency=proposal.currency,
                est_amount_krw=proposal.est_amount_krw,
                mode="live",
                event="canceled",
                toss_order_id=toss_order_id,
                detail={"result": result},
                created_at=now,
            )
        except Exception as exc:
            logger.critical(
                "[TossOrder] CRITICAL: order %s canceled on Toss but the audit record could not be "
                "persisted (account_id=%s): %s -- manual reconciliation required",
                toss_order_id,
                account_id,
                exc,
            )
            raise OrderAuditPersistFailedError(
                f"Order {toss_order_id} was canceled on Toss but could not be recorded "
                f"(account_id={account_id}); manual reconciliation required: {exc}"
            ) from exc

        return {"toss_order_id": toss_order_id, "canceled": True}

    # ------------------------------------------------------------------
    # Order status passthrough (read-only)
    # ------------------------------------------------------------------
    def get_order_status(self, *, account_id: int, toss_order_id: str) -> Dict[str, Any]:
        _account, link = self._resolve_eligible_account_and_link(account_id=account_id)

        proposal = self.repo.get_order_proposal_by_toss_order_id(toss_order_id, account_id=account_id)
        if proposal is None:
            raise OrderNotFoundError(
                f"No self-issued order {toss_order_id} for account_id={account_id}"
            )

        fetcher = self._ensure_fetcher()
        try:
            return fetcher.get_order(link.external_account_seq, toss_order_id)
        except DataFetchError as exc:
            raise TossUpstreamError(str(exc)) from exc

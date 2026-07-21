# -*- coding: utf-8 -*-
"""Toss Invest server-side conditional-order proposals (Phase 4).

Semantics (source of truth:
docs/superpowers/specs/2026-07-19-toss-conditional-order-phase4-design.md,
building on Phase 3's contracts:
docs/superpowers/specs/2026-07-17-toss-order-phase3-design.md):

- Two-step flow, same shape as Phase 3 but a *different meaning for the
  second step*: ``create_proposal`` validates and stores a ``pending``
  proposal (10-minute TTL, same as Phase 3); ``approve_proposal`` is a
  separate call requiring ``confirm=True`` — but here "approve" means
  *register a server-side conditional order with Toss*, not "execute an
  order". Once registered, Toss auto-executes the underlying LIMIT order
  the moment the STOP trigger price is touched, with **no further approval
  step this system can interpose** — Toss's API has no "confirm the
  trigger" call (design spec §1). Approving a conditional-order proposal is
  therefore consenting to future unattended execution, a materially
  different (and explicitly spec-accepted) safety posture than Phase 3's
  "every real order needs its own execute call".
- Default is dry-run, same flag (``TOSS_ORDER_LIVE``, strict parsing) as
  Phase 3: unless live, ``approve_proposal`` never calls
  ``TossFetcher.place_conditional_order`` at all — it writes a
  ``dry_run_approved`` proposal + a ``mode='dry_run'`` audit event and
  returns (design spec §3 "dry-run 의미론": *no* "register but don't
  execute" middle ground exists for a conditional order — registering *is*
  execution-delegation, so dry-run must never register at all).
- Local state machine (see ``PortfolioConditionalOrderProposal``'s
  docstring in ``src/storage.py`` for the full table): ``pending ->
  approving -> approved | registration_failed | registration_unknown``;
  ``pending -> canceled | expired | dry_run_approved``; ``approved ->
  triggered_completed | toss_expired | toss_canceled | paused``;
  ``registration_unknown -> approved | registration_failed`` (via
  reconcile). Entering ``approving`` is the same atomic-claim pattern as
  Phase 3's ``executing`` (``PortfolioRepository.claim_conditional_proposal_for_approval``).
- Shared daily KRW cap: this module's registrations and Phase 3's order
  executions draw against **one combined ceiling**
  (``PortfolioRepository._sum_reserved_and_live_amount_in_session``, design
  spec §3 "한도 산입": "Phase 3 v3의 일일 한도 합산 로직에 조건주문 미확정분
  합류") — a conditional-order registration can be blocked by Phase 3
  reservations, and vice versa. See that repository method's docstring for
  the exact per-status counting rules.
- **Deviation from the design spec, discovered against the live Toss
  OpenAPI spec (openapi.tossinvest.com, info.version "1.2.4"), reviewed and
  accepted before implementation**: the design spec's reconcile mechanism
  ("GET /conditional-orders 목록에서 clientOrderId 매칭") is not
  implementable as written — Toss's list/detail response schema
  (``ConditionalOrderDetailResponse``) carries no ``clientOrderId`` field
  at all (only the one-time create response does), and there is no
  ``clientOrderId`` query filter either. ``reconcile_proposal`` below
  instead does a best-effort *attribute match* across both the ``OPEN`` and
  ``CLOSED`` Toss listings. Re-POSTing (Phase 3's own reconcile strategy)
  was explicitly rejected as an alternative even though the create-request
  schema's own doc comment claims idempotent-retry behavior ("동일한 값으로
  재요청 시 중복 생성을 방지") — the design spec explicitly forbids re-POST
  here ("재POST는 이중 등록 위험") and Toss's documentation has already been
  shown to be internally inconsistent elsewhere (probe A table's
  PROFIT_RATE ambiguity), so betting a money-write on an unverified
  idempotency claim, against an explicit spec safety decision, was
  rejected.
- **v2 (Codex BLOCK-verdict independent review: 2 blocker + 3 major
  findings, resolved via a coordinator-confirmed convergence contract)** —
  the attribute-match reconcile above had two blocker-level gaps in its v1
  form, both fixed here:
    - *Mismatched-order blocker*: matching on side/trigger/limit/quantity/
      expiry alone, and returning the *first* match, could mistake a
      different order (on Toss, or from a different proposal) that happens
      to share every one of those attributes for this proposal's own
      registration — silently attaching the wrong ``conditionalOrderId``.
      Fixed in ``_is_attribute_match``/``_search_toss_for_match``: every
      candidate across ``OPEN``+``CLOSED`` is collected (not just the
      first), the item's own ``symbol`` is checked explicitly (defense in
      depth beyond the list call's server-side ``symbol=`` filter), and the
      candidate's Toss ``createdAt`` must fall inside ``[this proposal's
      approving-claim time (reserved_at) - 5 minutes, now]``. Only when
      **exactly one** candidate survives all of that does reconcile adopt
      it; zero or two-or-more candidates both stay ``registration_unknown``
      (never ``registration_failed``, never an arbitrary pick) with the
      candidate count recorded in the audit trail.
    - *In-flight-POST-race blocker*: reconcile could previously claim an
      ``approving`` row while its own ``approve_proposal`` POST was still
      in flight, writing ``registration_unknown`` (or even a *wrong*
      matched order) moments before the real POST response arrived — whose
      own outcome-resolution transition (``from_statuses={"approving"}``
      only) would then silently no-op, permanently losing the real
      ``conditionalOrderId``. Fixed two ways: (1) reconcile now only ever
      touches a genuinely-``approving`` row if its claim
      (``reserved_at``) is older than 10 minutes — a normal approve call
      resolves ``approving`` within one request, so anything still
      ``approving`` past that window is crash recovery, not a live POST
      (``PortfolioRepository.reconcile_claim_stale_approving``; a fresher
      ``approving`` claim is refused with
      ``ConditionalApprovalInProgressError``, mapped to ``409
      approval-in-progress``); (2) ``_resolve_registration_outcome``'s
      default ``from_statuses`` now includes ``registration_unknown``
      alongside ``approving``, so the real POST's outcome always wins and
      gets recorded no matter which of the two states reconcile has moved
      the row to in the meantime — "POST 결과가 authoritative" in every
      interleaving, and no terminal state ever accepts a further
      transition.
- **v3 (Codex 2nd-round independent review: BLOCK verdict — blocker 2
  (ownership + delayed-POST loss) + minor 1, coordinator-confirmed
  convergence contract — "네 결함이 전부 원격 conditionalOrderId의 로컬
  소유권을 확정할 수 없다는 같은 뿌리")**:
    - *Ownership blocker (R1)*: even a *unique* attribute-match candidate
      does not prove *this* proposal owns it — a same-attribute *different*
      local proposal that registered first, or is itself still racing to
      register, could own that exact remote order. Closed four ways: (1) a
      DB-level partial unique index on ``toss_conditional_order_id``
      (``DatabaseManager._ensure_conditional_order_toss_id_unique_index``)
      makes two proposals sharing one remote ID impossible at the storage
      layer; (2) ``reconcile_proposal`` drops any remote candidate already
      recorded on *another* local proposal, any status
      (``PortfolioRepository.find_conditional_order_ids_owned_by_others``);
      (3) before adopting a match, ``reconcile_proposal`` also checks for a
      same-attribute *other* local proposal still ``approving``/
      ``registration_unknown`` — a "local contender" — and refuses to adopt
      if one exists, since a single remote candidate cannot then be safely
      attributed to either one by attributes alone
      (``PortfolioRepository.list_other_unresolved_conditional_proposals``,
      ``candidate_count``/``local_contender_count`` both recorded in the
      reconcile audit row either way); (4) if a race still slips past (2)+(3)
      between the read and the write, the DB unique index in (1) turns the
      write into an ``IntegrityError``, caught and converted to a
      ``registration_unknown`` audit event rather than a crash or a wrong
      adoption. **Residual, explicitly accepted risk**: a user manually
      placing an identical-attribute conditional order via the Toss app/API
      at the same symbol/side/trigger/limit/quantity/expireDate, within the
      same 5-minute match window, cannot be distinguished from this
      proposal's own registration by attributes alone — the time window and
      candidate-uniqueness checks shrink this to a narrow, deliberate-
      collision scenario but cannot eliminate it structurally (design spec
      §5/§7).
    - *Delayed-POST-loss blocker (R2)*: even with the B2/v2 fix (reconcile
      only preempts an ``approving`` claim once it is stale), a POST that
      is *itself* still genuinely in flight when reconcile's stale-claim
      window opens could have its eventual result silently dropped if
      reconcile (or ``force_resolve_proposal``) has since moved the row to
      a status ``_resolve_registration_outcome``'s ``from_statuses`` no
      longer contains. Closed structurally, not just detected: every
      conditional-order write HTTP call is bounded by
      ``TossFetcher._CONDITIONAL_ORDER_WRITE_TIMEOUT_SECONDS``, giving a
      worst-case-per-call bound of
      ``TossFetcher._CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS`` (60s) —
      this module asserts at import time that
      ``_RECONCILE_STALE_APPROVING_AFTER`` is at least 10x that bound, so a
      POST that is still alive in a live process can never still be
      in-flight by the time reconcile's stale-claim window opens; the only
      way a claim goes stale is that the process that held it is gone,
      meaning there is no "delayed POST" left to race against. The residual
      case this cannot rule out — a written-but-never-delivered outcome
      resolving *after* the row has already reached a different terminal
      state some other way (e.g. an operator's ``force_resolve_proposal``
      call, or reconcile itself finding and adopting a *different*,
      genuinely-terminal Toss status) — is covered by an audit-only safety
      net in ``_resolve_registration_outcome``: whenever its transition
      lands on anything other than exactly the requested outcome/ID (a
      no-op because ``from_statuses`` no longer matched the row's actual
      status), it appends a ``conditional_registration_conflict`` audit
      event carrying the POST's real outcome/ID alongside the row's actual
      final state, and logs at ``ERROR`` — the local state itself is left
      untouched (never silently overwritten by a stale POST result), but an
      operator now has the evidence needed to check Toss directly for a
      possibly orphaned/duplicate registration.
    - *Minor (R3)*: ``_search_toss_for_match`` now dedupes by
      ``conditionalOrderId`` across the ``OPEN``+``CLOSED`` listings — an
      order that transitions between the two calls (e.g. it fills or
      expires mid-search) previously could be counted twice, artificially
      inflating the candidate count into a false "ambiguous" verdict; the
      ``CLOSED`` listing (queried after ``OPEN``) wins on a duplicate ID
      since it reflects the more recent read.
- Critically, **a failed/ambiguous attribute-match search never resolves to
  ``registration_failed``** — only an explicit Toss create-time 4xx does
  that. An inconclusive search leaves the proposal ``registration_unknown``
  (reservation held, surfaced for manual confirmation) precisely because a
  false-negative match must never release the daily-cap hold on an order
  that is, in reality, live and will auto-execute on Toss regardless of
  what this system believes. Because this can, by construction, leave a
  proposal ``registration_unknown`` indefinitely if the registration
  genuinely never happened (Codex review major 3 — an availability gap, not
  a money-safety one), ``force_resolve_proposal`` provides an authenticated,
  reason-required manual escape hatch: an operator who has independently
  confirmed on the Toss app/API that no matching order exists can
  force-transition ``registration_unknown -> registration_failed`` (which
  also releases the reservation, since ``registration_failed`` never counts
  toward the daily cap).
- Cancellation is unified (unlike Phase 3's split ``cancel_proposal``/
  ``cancel_order``) — the design spec's data flow has one endpoint
  (``DELETE .../conditional-orders/proposals/{uuid}``) for both a still-
  ``pending`` withdrawal (local only, Toss never contacted) and an
  ``approved``/``paused`` cancellation (Toss ``DELETE
  /conditional-orders/{id}``, then ``toss_canceled``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from data_provider.base import DataFetchError
from data_provider.toss_fetcher import (
    TossFetcher,
    TossOrderNotLiveError,
    TossOrderRejectedError,
    _CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS,
)
from src.repositories.portfolio_repo import (
    PendingConditionalProposalCapExceededError,
    PortfolioRepository,
)
from src.services.portfolio_broker_sync_service import (
    TossUpstreamError,
    _now_kst_naive,
    _parse_kst_datetime,
)
from src.services.portfolio_order_service import (
    _HIGH_VALUE_HARD_REJECT_KRW,
    _IN_DOUBT_TOSS_CODES,
    ConfirmRequiredError,
    HighValueOrderRejectedError,
    InsufficientBuyingPowerError,
    InsufficientSellableQuantityError,
    OrderAuditPersistFailedError,
    OrderLimitExceededError,
    PendingProposalLimitExceededError,
    PortfolioOrderService,
    _resolve_order_symbol,
)
from src.services.portfolio_service import PortfolioService
from src.storage import PortfolioConditionalOrderProposal

logger = logging.getLogger(__name__)

_PROPOSAL_TTL_MINUTES = 10
_MAX_PENDING_CONDITIONAL_PROPOSALS = 10  # design spec silent -> inherit Phase 3's guardrail count
_EXPIRE_DATE_MAX_DAYS = 7  # design spec §3 "expireDate 상한"
_EPS = 1e-6

# Codex adversarial review F3 (accept-and-disclose): every row in this table
# is a STOP + LIMIT-leg conditional order (this service's "타입 스코프" —
# see PortfolioConditionalOrderProposal's docstring), so the LIMIT-only leg's
# gap-down non-execution risk is structural, not something a slippage/collar
# setting can remove. Defined here (not in auto_proposal_service, which
# imports it) because this module owns the conditional-order model/service
# and _serialize_proposal needs it to populate the additive
# ``execution_risk_disclosure`` field on auto-generated proposals (see
# coordinator-confirmed follow-up: expose on the approve/list payload too,
# not just the audit trail and batch notification).
_CONDITIONAL_STOP_EXECUTION_RISK_NOTE = (
    "LIMIT 전용 손절 조건주문 — 갭다운 시 미체결 가능. 트리거 시 Toss가 자동 실행하며 "
    "추가 확인 절차가 없습니다(design spec F3)."
)

# Bounded page-count cap for reconcile's OPEN/CLOSED attribute-match search
# (this is a user-triggered search operation, not an unbounded background
# scan) — mirrors the spirit of TossFetcher.get_closed_orders's
# _ORDERS_MAX_PAGES safety cap, sized smaller since a single account should
# never have anywhere near this many conditional orders outstanding.
_RECONCILE_MAX_PAGES_PER_STATUS = 20

# Codex BLOCK review blocker 2 / coordinator-confirmed convergence contract:
# reconcile only preempts an 'approving' claim once it is this stale — a
# normal approve_proposal call resolves 'approving' within a single request,
# so anything still 'approving' after this window almost certainly means the
# process that held the claim died mid-POST (crash recovery), not that a
# POST is still genuinely in flight.
_RECONCILE_STALE_APPROVING_AFTER = timedelta(minutes=10)

# Codex 2nd-round review R2 (coordinator-confirmed convergence contract):
# enforced at import time so this relationship can never silently drift —
# _RECONCILE_STALE_APPROVING_AFTER must stay a comfortable (>=10x) multiple
# of the worst-case wall-clock time a single conditional-order write HTTP
# call can take (see TossFetcher._CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS'
# docstring for the 4x-timeout derivation). This is what makes "a POST that
# is still genuinely in flight when reconcile's stale-claim window opens"
# structurally impossible for any live process: reserved_at is set the
# instant the atomic claim commits, strictly before the POST is even issued,
# so by the time _RECONCILE_STALE_APPROVING_AFTER has elapsed since then, a
# live process's POST call (bounded by _CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS)
# has necessarily already returned one way or another.
assert _RECONCILE_STALE_APPROVING_AFTER >= 10 * timedelta(
    seconds=_CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS
), (
    "_RECONCILE_STALE_APPROVING_AFTER must stay >= 10x "
    "TossFetcher._CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS -- otherwise a live "
    "process's still-in-flight conditional-order POST could be preempted by reconcile "
    "(Codex 2nd-round review R2)"
)

# Codex BLOCK review blocker 1 / coordinator-confirmed convergence contract:
# a reconcile attribute-match candidate's Toss `createdAt` must fall within
# [approving claim time - this window, now] to count — bounds how far a
# same-attribute *different* order (created well before or after this
# proposal's own approve attempt) can still be mistaken for a match.
_RECONCILE_MATCH_TIME_WINDOW_BEFORE = timedelta(minutes=5)

# Toss's raw conditional-order lifecycle -> this system's local status
# (design spec §2 probe A table / live OpenAPI ConditionalOrderDetailResponse
# status enum). WATCHING/ORDERING/ORDERED all collapse to the local
# "approved" umbrella (still monitored/reserved); the finer distinction is
# kept in the separate toss_status column, not encoded into the local status.
_TOSS_STATUS_TO_LOCAL = {
    "WATCHING": "approved",
    "ORDERING": "approved",
    "ORDERED": "approved",
    "PAUSED": "paused",
    "COMPLETED": "triggered_completed",
    "EXPIRED": "toss_expired",
}


class ConditionalProposalNotFoundError(Exception):
    """Raised when a proposal_uuid does not resolve to a row for the given account."""


class ConditionalProposalNotApprovableError(Exception):
    """Raised when approve is attempted on a proposal that is not (or is no
    longer) ``pending`` — carries the actual status in the message."""


class ConditionalProposalInProgressError(Exception):
    """Raised when approve is called on a proposal already ``approving``/
    ``registration_unknown`` — a concurrent claim (or an unresolved prior
    attempt) is already in flight; the caller should poll
    ``reconcile_proposal`` instead."""


class ConditionalProposalNotReconcilableError(Exception):
    """Raised when ``reconcile_proposal`` (or a cancel attempt that requires
    reconcile first) is called on a proposal that is not ``approving``/
    ``registration_unknown``."""


class ConditionalApprovalInProgressError(Exception):
    """Raised when ``reconcile_proposal`` is called on an ``approving``
    proposal whose claim is still fresh (within
    ``_RECONCILE_STALE_APPROVING_AFTER``) — a real ``approve_proposal`` POST
    is plausibly still in flight, so reconcile must not preempt it (Codex
    BLOCK review blocker 2). Distinct from
    ``ConditionalProposalInProgressError`` (raised by ``approve_proposal``
    itself on a second concurrent approve call) so the API layer can map
    this to its own ``409 approval-in-progress`` error code."""


class ConditionalProposalNotCancelableError(Exception):
    """Raised when cancel is attempted on a proposal in a status that
    supports neither the pending-withdrawal nor the approved-cancel path
    (i.e. an already-terminal status)."""


class ConditionalProposalNotForceResolvableError(Exception):
    """Raised when ``force_resolve_proposal`` is called on a proposal that
    is not ``registration_unknown`` — the manual escape hatch only ever
    applies to that one status (design spec §7 / Codex review major 3:
    "실제 미등록 registration_unknown이 영구 reservation으로 남습니다")."""


class ExpireDateTooFarError(Exception):
    """Raised when ``expire_date`` exceeds the 7-day cap (design spec §3
    "expireDate 상한"), checked at both proposal creation and approval."""


def _make_client_order_id(proposal_uuid: str) -> str:
    """Toss ``clientOrderId`` for a conditional-order create POST.

    Deliberately **not** the design spec's literal ``dsa-cond-{proposal_uuid}``
    format (9 + 36 = 45 chars) — verified against the live Toss OpenAPI spec,
    ``ConditionalOrderCreateRequest.clientOrderId`` has a hard
    ``maxLength: 36`` (matching the pattern ``^[a-zA-Z0-9\\-_]+$``), so the
    spec's own literal format string cannot be sent at all. This uses
    ``"dc-" + uuid_without_dashes`` (3 + 32 = 35 chars, within the limit and
    pattern-valid) instead, while preserving a reversible, collision-safe
    mapping back to ``proposal_uuid`` for audit/debugging (not for matching
    — see this module's docstring on why Toss's list/detail schema cannot
    be matched by ``clientOrderId`` at all)."""
    return f"dc-{proposal_uuid.replace('-', '')}"


def _validate_expire_date(expire_date: date, *, now: datetime) -> None:
    if expire_date is None:
        raise ValueError("expire_date is required")
    today = now.date()
    if expire_date < today:
        raise ValueError(f"expire_date {expire_date.isoformat()} must not be in the past")
    max_date = today + timedelta(days=_EXPIRE_DATE_MAX_DAYS)
    if expire_date > max_date:
        raise ExpireDateTooFarError(
            f"expire_date {expire_date.isoformat()} exceeds the {_EXPIRE_DATE_MAX_DAYS}-day cap "
            f"(max {max_date.isoformat()}, design spec §3 'expireDate 상한')"
        )


def _map_toss_status_to_local(toss_status: str) -> str:
    """Map Toss's raw conditional-order status to this system's local
    status. An unrecognized value (a future Toss status this system does
    not yet know about) fails open to ``"approved"`` (still monitored,
    non-terminal, reservation held) rather than guessing a terminal state —
    releasing a reservation on a guess would risk the same "기록 없는 등록"
    class of bug the whole design avoids elsewhere."""
    mapped = _TOSS_STATUS_TO_LOCAL.get(toss_status)
    if mapped is None:
        logger.warning(
            "[TossConditionalOrder] unrecognized Toss conditional-order status=%r; "
            "treating as non-terminal 'approved' (fail open, reservation held)",
            toss_status,
        )
        return "approved"
    return mapped


def _safe_decimal(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PortfolioConditionalOrderService:
    """Business logic for Toss server-side conditional-order proposals:
    create/approve/reconcile/cancel/list/get, plus the observation-list
    lazy refresh and the bulk sync endpoint."""

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
        # Internal reuse-only instance (never exposed) — shares this
        # service's own repo/portfolio_service/fetcher/config so every
        # reused check (FX fail-closed estimate, account eligibility,
        # fetcher construction) runs against exactly the same state this
        # service otherwise would, without duplicating that logic (design
        # spec §3 "공유 가능한 헬퍼는 중복 구현 대신 재사용"). Phase 3's
        # public signatures/behavior are untouched by this reuse.
        self._order_service = PortfolioOrderService(
            portfolio_service=self.portfolio_service,
            repo=self.repo,
            fetcher=self._fetcher,
            config=self._config,
        )

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

    def _get_config(self) -> Any:
        if self._config is not None:
            return self._config
        try:
            from src.config import get_config

            return get_config()
        except Exception:
            return None

    def _is_live(self) -> bool:
        return TossFetcher.is_order_live_enabled(self._config)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    @staticmethod
    def _serialize_proposal(
        row: PortfolioConditionalOrderProposal, *, mode: Optional[str] = None
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "proposal_uuid": row.proposal_uuid,
            "account_id": int(row.account_id),
            "symbol": row.symbol,
            "storage_symbol": row.storage_symbol,
            "market": row.market,
            "currency": row.currency,
            "side": row.side,
            "trigger_price": row.trigger_price,
            "limit_price": row.limit_price,
            "quantity": row.quantity,
            "est_amount_krw": row.est_amount_krw,
            "expire_date": row.expire_date.isoformat(),
            "status": row.status,
            "toss_status": row.toss_status,
            "toss_conditional_order_id": row.toss_conditional_order_id,
            "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "approved_at": row.approved_at.isoformat() if row.approved_at else None,
            "generation_source": row.generation_source,
            "source_signal_id": row.source_signal_id,
            "generation_date": row.generation_date.isoformat() if row.generation_date else None,
            # Codex adversarial review F3 follow-up (coordinator-confirmed):
            # surface the residual gap-down non-execution risk directly on
            # the approve/list payload, not only in the audit trail and
            # batch notification. Populated only for Phase 5 auto-generated
            # proposals — a human creating a manual conditional order is
            # explicitly configuring the STOP/LIMIT leg themselves and is
            # assumed to already understand its mechanics; additive/optional
            # so existing (pre-v6) clients that ignore unknown keys are
            # unaffected.
            "execution_risk_disclosure": (
                _CONDITIONAL_STOP_EXECUTION_RISK_NOTE if row.generation_source == "auto" else None
            ),
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
        trigger_price: float,
        limit_price: float,
        quantity: float,
        expire_date: date,
        generation_source: str = "manual",
        source_signal_id: Optional[int] = None,
        generation_date: Optional[date] = None,
        extra_cond_proposed_audit_detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """``generation_source``/``source_signal_id``/``generation_date``
        (Phase 5, additive) — see ``PortfolioOrderService.create_proposal``'s
        docstring; the same composite-unique-index idempotency contract
        (v6, Codex adversarial review F1+F2) applies here.
        ``extra_cond_proposed_audit_detail`` (F3, additive) is forwarded
        verbatim to ``PortfolioRepository.create_conditional_order_proposal_
        with_audit`` — used by the Phase 5 generator to record the
        gap-down/non-execution-risk disclosure on a stop-loss proposal it
        creates (design spec F3 "accept-and-disclose")."""
        side_norm = (side or "").strip().lower()
        if side_norm not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if trigger_price is None or trigger_price <= 0:
            raise ValueError("trigger_price must be > 0")
        if limit_price is None or limit_price <= 0:
            raise ValueError("limit_price must be > 0")
        if quantity is None or quantity <= 0:
            raise ValueError("quantity must be > 0")

        now = _now_kst_naive()
        _validate_expire_date(expire_date, now=now)

        _account, link = self._order_service._resolve_eligible_account_and_link(account_id=account_id)

        storage_symbol, toss_symbol, market, currency = _resolve_order_symbol(symbol)

        amount_native, est_amount_krw = self._order_service._estimate_amount_krw(
            order_type="LIMIT",
            storage_symbol=storage_symbol,
            currency=currency,
            quantity=quantity,
            price=limit_price,
            now=now,
        )

        self._order_service._enforce_amount_caps_best_effort(
            account_id=account_id, est_amount_krw=est_amount_krw, now=now
        )

        fetcher = self._order_service._ensure_fetcher()
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
        client_order_id = _make_client_order_id(proposal_uuid)
        expires_at = now + timedelta(minutes=_PROPOSAL_TTL_MINUTES)
        try:
            row = self.repo.create_conditional_order_proposal_with_audit(
                account_id=account_id,
                proposal_uuid=proposal_uuid,
                symbol=toss_symbol,
                storage_symbol=storage_symbol,
                market=market,
                currency=currency,
                side=side_norm,
                trigger_price=float(trigger_price),
                limit_price=float(limit_price),
                quantity=float(quantity),
                est_amount_krw=est_amount_krw,
                expire_date=expire_date,
                client_order_id=client_order_id,
                created_at=now,
                expires_at=expires_at,
                max_pending_proposals=_MAX_PENDING_CONDITIONAL_PROPOSALS,
                generation_source=generation_source,
                source_signal_id=source_signal_id,
                generation_date=generation_date,
                extra_cond_proposed_audit_detail=extra_cond_proposed_audit_detail,
            )
        except PendingConditionalProposalCapExceededError as exc:
            raise PendingProposalLimitExceededError(str(exc)) from exc

        mode_preview = "live" if self._is_live() else "dry_run"
        return self._serialize_proposal(row, mode=mode_preview)

    # ------------------------------------------------------------------
    # Approve proposal (= register with Toss; see module docstring)
    # ------------------------------------------------------------------
    def approve_proposal(
        self,
        *,
        account_id: int,
        proposal_uuid: str,
        confirm: bool,
    ) -> Dict[str, Any]:
        if not confirm:
            raise ConfirmRequiredError(
                "confirm: true is required to approve a conditional-order proposal (required in "
                "dry-run mode too) — approval registers a Toss-side conditional order that Toss "
                "auto-executes without further confirmation once triggered"
            )

        now = _now_kst_naive()
        _account, link = self._order_service._resolve_eligible_account_and_link(account_id=account_id)

        proposal = self.repo.get_conditional_order_proposal(proposal_uuid, account_id=account_id, now=now)
        if proposal is None:
            raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")

        if proposal.status in ("dry_run_approved", "approved", "paused"):
            cached_mode = "dry_run" if proposal.status == "dry_run_approved" else "live"
            return self._serialize_proposal(proposal, mode=cached_mode)
        if proposal.status in ("approving", "registration_unknown"):
            raise ConditionalProposalInProgressError(
                f"proposal {proposal_uuid} is '{proposal.status}'; call .../reconcile instead of "
                f"approving again"
            )
        if proposal.status != "pending":
            raise ConditionalProposalNotApprovableError(
                f"proposal {proposal_uuid} is '{proposal.status}' and can no longer be approved"
            )

        # Re-validate expireDate at approve time too (design spec §3
        # "expireDate 상한": "제안 생성·승인 양쪽에서" validated).
        _validate_expire_date(proposal.expire_date, now=now)

        # Re-validate fresh (mirrors Phase 3 "실행 시 재확인") — never trust
        # the stored est_amount_krw, since FX may have moved for a USD order.
        amount_native, est_amount_krw = self._order_service._estimate_amount_krw(
            order_type="LIMIT",
            storage_symbol=proposal.storage_symbol,
            currency=proposal.currency,
            quantity=proposal.quantity,
            price=proposal.limit_price,
            now=now,
        )

        fetcher = self._order_service._ensure_fetcher()
        if proposal.side == "buy":
            try:
                buying_power = fetcher.get_buying_power(link.external_account_seq, currency=proposal.currency)
            except DataFetchError as exc:
                raise TossUpstreamError(str(exc)) from exc
            if amount_native > buying_power + _EPS:
                self._fail_pending_proposal(
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
                self._fail_pending_proposal(
                    proposal_uuid=proposal_uuid,
                    account_id=account_id,
                    now=now,
                    error_code="insufficient-sellable-quantity",
                    detail={"sellable_quantity": sellable},
                )
                raise InsufficientSellableQuantityError(
                    f"Requested quantity {proposal.quantity} exceeds sellable quantity {sellable}"
                )

        # ---- dry-run vs live: the one branch point (design spec §3
        # "dry-run 의미론" — dry-run never registers, full stop). ----
        if not self._is_live():
            try:
                self._order_service._enforce_amount_caps_best_effort(
                    account_id=account_id, est_amount_krw=est_amount_krw, now=now
                )
            except (HighValueOrderRejectedError, OrderLimitExceededError) as exc:
                self._fail_pending_proposal(
                    proposal_uuid=proposal_uuid,
                    account_id=account_id,
                    now=now,
                    error_code="limit-exceeded",
                    detail={"reason": str(exc)},
                )
                raise
            updated = self.repo.transition_conditional_proposal(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                from_statuses={"pending"},
                to_status="dry_run_approved",
                event="cond_dry_run_approved",
                mode="dry_run",
                approved_at=now,
                est_amount_krw_override=est_amount_krw,
                detail={"note": "dry-run: no Toss conditional-order registration POST was made"},
            )
            if updated is None:
                raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
            return self._serialize_proposal(updated, mode="dry_run")

        # ---- Live path: atomic claim (pending -> approving), then the one
        # Toss POST, then resolve to approved/registration_failed/
        # registration_unknown. ----
        claim = self.repo.claim_conditional_proposal_for_approval(
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
            raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
        if claim.outcome == "already_terminal":
            cached_mode = "dry_run" if claim.proposal.status == "dry_run_approved" else "live"
            return self._serialize_proposal(claim.proposal, mode=cached_mode)
        if claim.outcome == "already_approved":
            return self._serialize_proposal(claim.proposal, mode="live")
        if claim.outcome == "in_progress":
            raise ConditionalProposalInProgressError(
                f"proposal {proposal_uuid} is '{claim.proposal.status}'; call .../reconcile instead "
                f"of approving again"
            )
        if claim.outcome == "not_executable":
            raise ConditionalProposalNotApprovableError(
                f"proposal {proposal_uuid} is '{claim.proposal.status}' and can no longer be approved"
            )
        if claim.outcome == "rejected":
            if claim.limit_type == "high_value":
                raise HighValueOrderRejectedError(claim.reason or "high-value order rejected")
            raise OrderLimitExceededError(limit_type=claim.limit_type or "unknown", message=claim.reason or "limit exceeded")

        # claim.outcome == "claimed" — the reservation is durably committed;
        # only now do we attempt the actual Toss registration POST.
        client_order_id = proposal.client_order_id
        try:
            result = fetcher.place_conditional_order(
                link.external_account_seq,
                symbol=proposal.symbol,
                side=proposal.side.upper(),
                trigger_price=proposal.trigger_price,
                limit_price=proposal.limit_price,
                quantity=proposal.quantity,
                expire_date=proposal.expire_date.isoformat(),
                client_order_id=client_order_id,
            )
        except TossOrderNotLiveError as exc:
            # Should be unreachable (this service already checked
            # self._is_live() before claiming) — a mismatch here means the
            # service- and fetcher-level live checks disagreed mid-request.
            # No HTTP call was made, so this is unambiguously
            # 'registration_failed', not 'registration_unknown'.
            logger.critical(
                "[TossConditionalOrder] service/fetcher live-gate mismatch for proposal_uuid=%s "
                "account_id=%s: %s",
                proposal_uuid,
                account_id,
                exc,
            )
            self._resolve_registration_outcome(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                outcome="registration_failed",
                error_code="order-live-gate-mismatch",
                detail={"reason": str(exc)},
            )
            raise
        except TossOrderRejectedError as exc:
            if exc.code in _IN_DOUBT_TOSS_CODES:
                updated = self._resolve_registration_outcome(
                    proposal_uuid=proposal_uuid,
                    account_id=account_id,
                    now=now,
                    outcome="registration_unknown",
                    error_code=exc.code,
                    detail={"status_code": exc.status_code, "message": exc.message, "data": exc.data},
                )
                return self._serialize_proposal(updated, mode="live")
            self._resolve_registration_outcome(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                outcome="registration_failed",
                error_code=exc.code,
                detail={"status_code": exc.status_code, "message": exc.message, "data": exc.data},
            )
            raise
        except DataFetchError as exc:
            # Response lost / timeout / other transient upstream failure
            # *after* the reservation is already committed — resolves to
            # registration_unknown (design spec §5 "429"/lost-response), not
            # a re-raised error that would leave the true state unrecorded.
            updated = self._resolve_registration_outcome(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                outcome="registration_unknown",
                error_code="network-error",
                detail={"reason": str(exc)},
            )
            return self._serialize_proposal(updated, mode="live")

        conditional_order_id = result.get("conditionalOrderId") if isinstance(result, dict) else None
        if not conditional_order_id:
            updated = self._resolve_registration_outcome(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                outcome="registration_unknown",
                error_code="missing-conditional-order-id",
                detail={"reason": "place_conditional_order succeeded without a conditionalOrderId", "result": result},
            )
            return self._serialize_proposal(updated, mode="live")

        updated = self._resolve_registration_outcome(
            proposal_uuid=proposal_uuid,
            account_id=account_id,
            now=now,
            outcome="approved",
            conditional_order_id=conditional_order_id,
        )
        return self._serialize_proposal(updated, mode="live")

    def _resolve_registration_outcome(
        self,
        *,
        proposal_uuid: str,
        account_id: int,
        now: datetime,
        outcome: str,
        conditional_order_id: Optional[str] = None,
        error_code: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        from_statuses: Optional[set] = None,
    ) -> PortfolioConditionalOrderProposal:
        """Persist the ``{approving|registration_unknown} -> {approved|
        registration_failed|registration_unknown}`` transition — mirrors
        Phase 3's ``_resolve_execution_outcome`` fallback-to-
        ``registration_unknown``-then-``OrderAuditPersistFailedError``
        contract exactly (design spec §7-equivalent: a DB write failure
        right after a definitive Toss result must not silently drop the
        reservation).

        The default ``from_statuses`` is ``{"approving", "registration_unknown"}``
        — **not** just ``{"approving"}`` (Codex BLOCK review blocker 2 fix,
        coordinator-confirmed convergence contract: "POST 결과가
        authoritative"). This is what lets the real Toss POST outcome always
        win the row's final state even when ``reconcile_proposal`` has
        concurrently taken the row over as ``registration_unknown`` (via
        ``reconcile_claim_stale_approving``'s stale-claim takeover) while
        this exact POST was still in flight: without
        ``registration_unknown`` in the default set, this transition would
        become a silent no-op the moment reconcile wins the race, and the
        real ``conditionalOrderId`` a successful POST just returned would
        never be recorded anywhere (design spec's "기록 없으면 등록 없음"
        inverted into "등록됐지만 기록이 없음" — exactly the bug class the
        whole distributed-transaction contract exists to prevent). Terminal
        statuses (``approved``, ``registration_failed``, etc.) are still
        never accepted as a ``from_statuses`` starting point here — only a
        caller-supplied ``from_statuses`` can widen it further (none does)."""
        event_map = {
            "approved": "cond_approved",
            "registration_failed": "cond_reg_failed",
            "registration_unknown": "cond_reg_unknown",
        }
        resolved_from_statuses = (
            from_statuses if from_statuses is not None else {"approving", "registration_unknown"}
        )
        try:
            updated = self.repo.transition_conditional_proposal(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                from_statuses=resolved_from_statuses,
                to_status=outcome,
                event=event_map[outcome],
                mode="live",
                toss_conditional_order_id=conditional_order_id,
                approved_at=now if outcome == "approved" else None,
                toss_status="WATCHING" if outcome == "approved" else None,
                error_code=error_code,
                detail=detail,
            )
        except IntegrityError as exc:
            # Codex 3rd-round review R1d (coordinator-confirmed convergence
            # contract): the primary transition failed because
            # conditional_order_id is already owned by *another* local
            # proposal (the partial unique index on toss_conditional_order_id
            # caught it) -- this POST genuinely succeeded/failed and Toss
            # returned a real ID, but that ID belongs to someone else's row
            # now (e.g. that proposal's own reconcile adopted it first).
            # Root cause of the prior bug: the generic fallback below used
            # to retry the registration_unknown transition with the SAME
            # conditional_order_id, which hit the SAME unique index a
            # second time, raised a second IntegrityError, and surfaced as
            # an unhandled OrderAuditPersistFailedError (500) with the row
            # stuck in 'approving' forever. Fixed by handling IntegrityError
            # here explicitly (never falling through to the generic
            # `except Exception` below) and never writing that ID onto this
            # row at all -- fall back to registration_unknown with NO id,
            # which is always writable (no unique-index collision possible
            # on a NULL), and leave a loud, distinct audit trail so an
            # operator knows this proposal's real registration outcome was
            # observed but could not be recorded here.
            logger.error(
                "[TossConditionalOrder] CONFLICT: proposal_uuid=%s account_id=%s POST resolved "
                "outcome=%s with conditional_order_id=%s, but that ID is already owned by another "
                "local proposal (unique-index conflict on the primary transition write) -- falling "
                "back to registration_unknown with NO id recorded on this row (Codex 3rd-round "
                "review R1d): %s",
                proposal_uuid,
                account_id,
                outcome,
                conditional_order_id,
                exc,
            )
            try:
                updated = self.repo.transition_conditional_proposal(
                    proposal_uuid=proposal_uuid,
                    account_id=account_id,
                    now=now,
                    from_statuses=resolved_from_statuses,
                    to_status="registration_unknown",
                    event="cond_reg_unknown",
                    mode="live",
                    # Deliberately NOT passing toss_conditional_order_id here
                    # -- that ID belongs to another proposal and must never
                    # be written onto this row.
                    detail={
                        "fallback_reason": "primary transition write hit a unique-index conflict "
                        "(conditional_order_id already owned by another proposal)",
                        "original_outcome": outcome,
                        "original_error_code": error_code,
                    },
                )
            except Exception as exc2:
                logger.critical(
                    "[TossConditionalOrder] CRITICAL: registration_unknown (no-id) fallback also "
                    "failed for proposal_uuid=%s account_id=%s after a unique-index conflict: %s "
                    "-- manual reconciliation required",
                    proposal_uuid,
                    account_id,
                    exc2,
                )
                raise OrderAuditPersistFailedError(
                    f"Registration outcome ({outcome}) for proposal_uuid={proposal_uuid} "
                    f"(account_id={account_id}) hit a unique-index conflict on "
                    f"conditional_order_id={conditional_order_id} and the no-id "
                    f"registration_unknown fallback also failed; manual reconciliation required: "
                    f"{exc2}"
                ) from exc2

            if updated is None:
                raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")

            try:
                self.repo.append_standalone_conditional_audit(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=updated.symbol,
                    side=updated.side,
                    limit_price=updated.limit_price,
                    quantity=updated.quantity,
                    currency=updated.currency,
                    est_amount_krw=updated.est_amount_krw,
                    mode="live",
                    event="conditional_registration_conflict",
                    toss_conditional_order_id=conditional_order_id,
                    created_at=now,
                    error_code="owned-by-another-proposal",
                    detail={
                        "post_outcome": outcome,
                        "post_conditional_order_id": conditional_order_id,
                        "post_error_code": error_code,
                        "reason": "conditional_order_id already owned by another local proposal",
                        "local_status_after": updated.status,
                    },
                )
            except Exception:
                logger.critical(
                    "[TossConditionalOrder] CRITICAL: could not record the "
                    "conditional_registration_conflict audit event for proposal_uuid=%s "
                    "account_id=%s -- manual reconciliation required",
                    proposal_uuid,
                    account_id,
                    exc_info=True,
                )

            # The conflict audit above already documents this outcome in
            # full; return directly rather than falling through to the
            # generic R2-2 audit-net check below (which would otherwise
            # fire a second, redundant conflict audit for the same event).
            return updated
        except Exception as exc:
            logger.critical(
                "[TossConditionalOrder] CRITICAL: could not persist outcome=%s for proposal_uuid=%s "
                "account_id=%s (conditional_order_id=%s): %s -- attempting registration_unknown fallback",
                outcome,
                proposal_uuid,
                account_id,
                conditional_order_id,
                exc,
            )
            if outcome == "registration_unknown":
                raise OrderAuditPersistFailedError(
                    f"Could not persist registration_unknown for proposal_uuid={proposal_uuid} "
                    f"(account_id={account_id}); manual reconciliation required: {exc}"
                ) from exc
            try:
                updated = self.repo.transition_conditional_proposal(
                    proposal_uuid=proposal_uuid,
                    account_id=account_id,
                    now=now,
                    from_statuses=resolved_from_statuses,
                    to_status="registration_unknown",
                    event="cond_reg_unknown",
                    mode="live",
                    toss_conditional_order_id=conditional_order_id,
                    detail={
                        "fallback_reason": "primary transition write failed",
                        "original_outcome": outcome,
                        "original_error_code": error_code,
                        "write_error": str(exc),
                    },
                )
            except Exception as exc2:
                logger.critical(
                    "[TossConditionalOrder] CRITICAL: registration_unknown fallback also failed for "
                    "proposal_uuid=%s account_id=%s: %s -- manual reconciliation required",
                    proposal_uuid,
                    account_id,
                    exc2,
                )
                raise OrderAuditPersistFailedError(
                    f"Registration outcome ({outcome}) for proposal_uuid={proposal_uuid} "
                    f"(account_id={account_id}, conditional_order_id={conditional_order_id}) could not "
                    f"be recorded even as registration_unknown; manual reconciliation required: {exc2}"
                ) from exc2

        if updated is None:
            raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")

        # Codex 2nd-round review R2-2 (coordinator-confirmed convergence
        # contract, audit-net / residual defense): the timeout-vs-stale-
        # threshold relationship asserted at import time (see
        # _RECONCILE_STALE_APPROVING_AFTER above) rules out a *live*
        # process's still-in-flight POST losing this race, but a genuinely
        # delayed result (e.g. this call itself running very late for some
        # other reason, or the row having moved on via
        # force_resolve_proposal / reconcile finding a different terminal
        # Toss status in the meantime) can still land here after
        # `transition_conditional_proposal`'s own from_statuses check no
        # longer matches the row's actual status -- a silent no-op that
        # would otherwise drop the real POST outcome with no trace. Detect
        # that here and leave a loud audit trail instead of silently
        # discarding it; the local row's state is never overwritten by this
        # check -- it only ever appends evidence for manual Toss-side
        # reconciliation.
        outcome_applied = updated.status == outcome and (
            conditional_order_id is None or updated.toss_conditional_order_id == conditional_order_id
        )
        if not outcome_applied:
            logger.error(
                "[TossConditionalOrder] CONFLICT: POST resolved outcome=%s (conditional_order_id=%s, "
                "error_code=%s) for proposal_uuid=%s account_id=%s but the local row is now "
                "status=%s toss_conditional_order_id=%s -- POST outcome was NOT applied (from_statuses "
                "no longer matched); manual Toss-side reconciliation may be required to rule out an "
                "orphaned/duplicate registration (Codex 2nd-round review R2-2)",
                outcome,
                conditional_order_id,
                error_code,
                proposal_uuid,
                account_id,
                updated.status,
                updated.toss_conditional_order_id,
            )
            try:
                self.repo.append_standalone_conditional_audit(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=updated.symbol,
                    side=updated.side,
                    limit_price=updated.limit_price,
                    quantity=updated.quantity,
                    currency=updated.currency,
                    est_amount_krw=updated.est_amount_krw,
                    mode="live",
                    event="conditional_registration_conflict",
                    toss_conditional_order_id=conditional_order_id,
                    created_at=now,
                    error_code="post-outcome-not-applied",
                    detail={
                        "post_outcome": outcome,
                        "post_conditional_order_id": conditional_order_id,
                        "post_error_code": error_code,
                        "local_status_after": updated.status,
                        "local_toss_conditional_order_id_after": updated.toss_conditional_order_id,
                    },
                )
            except Exception:
                logger.critical(
                    "[TossConditionalOrder] CRITICAL: could not even record the "
                    "conditional_registration_conflict audit event for proposal_uuid=%s "
                    "account_id=%s -- manual reconciliation required",
                    proposal_uuid,
                    account_id,
                    exc_info=True,
                )
        else:
            # Codex 3rd-round review R2b (minor, operational traceability):
            # the ordinary, expected convergence path (no race, no conflict)
            # previously left no explicit trace here at all -- one DEBUG
            # line confirming the POST outcome landed exactly as requested
            # makes it possible to grep/trace the happy path too, not just
            # the conflict branch above.
            logger.debug(
                "[TossConditionalOrder] outcome=%s converged normally for proposal_uuid=%s "
                "account_id=%s (status=%s, toss_conditional_order_id=%s)",
                outcome,
                proposal_uuid,
                account_id,
                updated.status,
                updated.toss_conditional_order_id,
            )

        return updated

    def _fail_pending_proposal(
        self,
        *,
        proposal_uuid: str,
        account_id: int,
        now: datetime,
        error_code: str,
        detail: Dict[str, Any],
    ) -> None:
        """A rejection *before* the atomic claim (buying-power/sellable-
        quantity/cap failures re-checked at approve time) never reached
        Toss, so it resolves straight from ``pending`` to
        ``registration_failed`` (never ``registration_unknown``)."""
        self.repo.transition_conditional_proposal(
            proposal_uuid=proposal_uuid,
            account_id=account_id,
            now=now,
            from_statuses={"pending"},
            to_status="registration_failed",
            event="cond_reg_failed",
            error_code=error_code,
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Reconcile an approving/registration_unknown proposal — see module
    # docstring for the design-spec deviation this implements (attribute
    # match, not clientOrderId match; no-match stays registration_unknown).
    # ------------------------------------------------------------------
    def reconcile_proposal(self, *, account_id: int, proposal_uuid: str) -> Dict[str, Any]:
        now = _now_kst_naive()
        _account, link = self._order_service._resolve_eligible_account_and_link(account_id=account_id)

        # Codex BLOCK review blocker 2 fix (coordinator-confirmed
        # convergence contract): reconcile must not preempt an in-flight
        # approve_proposal POST. This atomically gates entry — 'approving'
        # is only handed over once its claim is stale (crash recovery);
        # otherwise reconcile is refused outright. See
        # PortfolioRepository.reconcile_claim_stale_approving.
        claim = self.repo.reconcile_claim_stale_approving(
            proposal_uuid=proposal_uuid,
            account_id=account_id,
            now=now,
            stale_after=_RECONCILE_STALE_APPROVING_AFTER,
        )
        if claim.outcome == "not_found":
            raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
        if claim.outcome == "not_reconcilable":
            raise ConditionalProposalNotReconcilableError(
                f"proposal {proposal_uuid} is '{claim.proposal.status}'; only 'approving'/"
                f"'registration_unknown' proposals can be reconciled"
            )
        if claim.outcome == "approval_in_progress":
            raise ConditionalApprovalInProgressError(
                f"proposal {proposal_uuid} is 'approving' with a claim younger than "
                f"{_RECONCILE_STALE_APPROVING_AFTER}; a real approve POST is plausibly still in "
                f"flight — retry reconcile later or wait for the approve call to resolve"
            )

        # claim.outcome == "ready": the row is now guaranteed
        # 'registration_unknown' (either already was, or this call just
        # atomically took over a stale 'approving' claim).
        proposal = claim.proposal

        fetcher = self._order_service._ensure_fetcher()
        try:
            matches = self._search_toss_for_match(fetcher, link.external_account_seq, proposal, now=now)
        except DataFetchError as exc:
            raise TossUpstreamError(str(exc)) from exc

        # Codex 2nd-round review R1-2 (coordinator-confirmed convergence
        # contract): a candidate already recorded — any status — on some
        # *other* local proposal can never legitimately be re-adopted here,
        # no matter how uniquely it otherwise matched this proposal's
        # attributes. Drop those before deciding candidate count.
        owned_by_others: set = set()
        if matches:
            owned_by_others = self.repo.find_conditional_order_ids_owned_by_others(
                (conditional_order_id for conditional_order_id, _ in matches),
                exclude_proposal_uuid=proposal_uuid,
            )
            if owned_by_others:
                matches = [m for m in matches if m[0] not in owned_by_others]

        # Codex 2nd-round review R1-3: a same-attribute *other* local
        # proposal that is itself still unresolved (approving/
        # registration_unknown) means a single surviving remote candidate
        # still cannot be safely attributed to *this* proposal by
        # attributes alone — it could just as well be the other proposal's
        # own registration. Refuse to adopt in that case regardless of how
        # many remote candidates were found.
        local_contenders = self.repo.list_other_unresolved_conditional_proposals(
            account_id=account_id, exclude_proposal_uuid=proposal_uuid
        )
        local_contender_count = sum(
            1 for other in local_contenders if self._is_same_order_attributes(other, proposal)
        )

        if local_contender_count > 0 or len(matches) != 1:
            # Codex BLOCK review blocker 1 fix (kept from v2, extended by
            # v3's ownership checks above): 0 candidates (genuinely not
            # found / not yet visible / all owned elsewhere), >=2 candidates
            # (ambiguous — some *other* order shares this proposal's
            # attributes), and a nonzero local-contender count are all
            # handled identically — advisor-reviewed safety rule: an
            # inconclusive search is never proof of non-registration, and
            # this system must never guess which of several candidates (or
            # which of several same-attribute local proposals) actually
            # owns a match. Stays registration_unknown; both counts are
            # recorded so an operator inspecting the audit trail can tell
            # "not found" apart from "ambiguous" apart from "another local
            # proposal is racing for the same attributes".
            updated = self.repo.transition_conditional_proposal(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                from_statuses={"registration_unknown"},
                to_status="registration_unknown",
                event="cond_reconciled",
                mode="live",
                error_code=(
                    "local-contender"
                    if local_contender_count > 0
                    else ("not-found-on-toss" if not matches else "ambiguous-match")
                ),
                detail={
                    "candidate_count": len(matches),
                    "local_contender_count": local_contender_count,
                    "owned_by_other_proposal_count": len(owned_by_others),
                    "note": (
                        f"{local_contender_count} other unresolved local proposal(s) share this "
                        "proposal's exact attributes; staying registration_unknown rather than "
                        "guessing which one a single remote candidate belongs to"
                        if local_contender_count > 0
                        else (
                            "no attribute match found across OPEN/CLOSED Toss listings (after "
                            "excluding candidates already owned by another local proposal); staying "
                            "registration_unknown — an unmatched search is not proof of "
                            "non-registration (Toss's list/detail schema carries no clientOrderId "
                            "to match on)"
                            if not matches
                            else (
                                f"{len(matches)} candidates matched this proposal's attributes "
                                "ambiguously; staying registration_unknown rather than guessing "
                                "which one is this proposal's own registration"
                            )
                        )
                    ),
                },
            )
            if updated is None:
                raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
            return self._serialize_proposal(updated, mode="live")

        conditional_order_id, toss_status = matches[0]
        local_status = _map_toss_status_to_local(toss_status)

        # Codex 3rd-round review R1c (coordinator-confirmed convergence
        # contract): the ownership/local-contender checks above ran in
        # ordinary read sessions, so a same-attribute proposal could still
        # enter 'approving' (or the candidate ID could still be claimed by
        # someone else) in the gap between those reads and this write. All
        # network I/O (the Toss listing search) is already done by this
        # point — adopt_reconciled_order_if_uncontended re-verifies both
        # checks *inside* the same BEGIN IMMEDIATE write transaction that
        # performs the adoption, closing that gap: SQLite serializes every
        # writer that goes through portfolio_write_session, so nothing else
        # can commit a conflicting change while this transaction holds the
        # write lock.
        try:
            outcome = self.repo.adopt_reconciled_order_if_uncontended(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                conditional_order_id=conditional_order_id,
                toss_status=toss_status,
                to_status=local_status,
            )
        except IntegrityError as exc:
            # Codex 2nd-round review R1-4 (defense in depth): under SQLite
            # this should be unreachable now that R1c's atomic recheck runs
            # inside the same write-locked transaction as the write itself
            # — kept as a belt-and-braces fallback in case a future backend
            # or code path writes to this table without going through
            # portfolio_write_session's BEGIN IMMEDIATE discipline. Stay
            # registration_unknown rather than accept a duplicate-owned ID.
            logger.warning(
                "[TossConditionalOrder] reconcile adopt for proposal_uuid=%s account_id=%s hit a "
                "unique-index conflict on conditional_order_id=%s despite the atomic recheck "
                "(Codex 2nd-round review R1-4 fallback); staying registration_unknown: %s",
                proposal_uuid,
                account_id,
                conditional_order_id,
                exc,
            )
            updated = self.repo.transition_conditional_proposal(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                from_statuses={"registration_unknown"},
                to_status="registration_unknown",
                event="cond_reconciled",
                mode="live",
                error_code="unique-conflict-on-adopt",
                detail={
                    "candidate_conditional_order_id": conditional_order_id,
                    "note": (
                        "unique-index conflict adopting this candidate despite the atomic recheck -- "
                        "another local proposal won the race for this exact conditionalOrderId"
                    ),
                },
            )
            if updated is None:
                raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
            return self._serialize_proposal(updated, mode="live")

        if outcome.outcome == "not_found":
            raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
        # "not_reconcilable" (something else already resolved the row) and
        # "contended" (the atomic recheck cancelled adoption) and "adopted"
        # all carry the row's actual current state in outcome.proposal —
        # serialize it as-is in every case, idempotent-retry style.
        return self._serialize_proposal(outcome.proposal, mode="live")

    def _search_toss_for_match(
        self,
        fetcher: TossFetcher,
        account_seq: Any,
        proposal: PortfolioConditionalOrderProposal,
        *,
        now: datetime,
    ) -> List[Tuple[str, str]]:
        """Best-effort attribute-match search across Toss's ``OPEN`` and
        ``CLOSED`` conditional-order listings, scoped to this proposal's
        symbol via the list API's own ``symbol`` query filter. Returns
        **every distinct** candidate found (Codex BLOCK review blocker 1:
        returning only the first match let a same-attribute *different*
        order be mistaken for this proposal's own registration) — the
        caller decides what an empty or ambiguous (``len() > 1``) result
        means; this method never picks a winner on its own.

        Codex 2nd-round review R3 (coordinator-confirmed convergence
        contract, minor): candidates are deduped by ``conditionalOrderId``
        across the ``OPEN``+``CLOSED`` listings — an order that transitions
        between the two calls (e.g. it fills or expires mid-search) would
        otherwise be counted as two separate candidates, artificially
        inflating an unambiguous single match into a false "ambiguous
        multiple candidates" verdict. ``CLOSED`` is queried after ``OPEN``,
        so on a duplicate ID its status wins (the more recently observed
        read)."""
        matches_by_id: Dict[str, str] = {}
        for status in ("OPEN", "CLOSED"):
            cursor: Optional[str] = None
            for _page in range(_RECONCILE_MAX_PAGES_PER_STATUS):
                page = fetcher.list_conditional_orders(
                    account_seq, status=status, symbol=proposal.symbol, cursor=cursor
                )
                for item in page["conditionalOrders"]:
                    if self._is_attribute_match(item, proposal, now=now):
                        conditional_order_id = item.get("conditionalOrderId")
                        toss_status = item.get("status")
                        if conditional_order_id and toss_status:
                            matches_by_id[str(conditional_order_id)] = str(toss_status)
                if not page["hasNext"]:
                    break
                cursor = page["nextCursor"]
        return list(matches_by_id.items())

    @staticmethod
    def _is_attribute_match(
        item: Dict[str, Any], proposal: PortfolioConditionalOrderProposal, *, now: datetime
    ) -> bool:
        """Codex BLOCK review blocker 1 / coordinator-confirmed convergence
        contract: every one of the following must hold, not just
        side/trigger/limit/quantity/expiry —
          (a) ``symbol`` matches this proposal's — checked here explicitly
              as defense-in-depth even though the list call already sends
              ``symbol=`` as a server-side filter (a filter this system does
              not control the exact semantics of);
          (b) side/trigger price/limit price/quantity/expireDate all match
              (unchanged from the original implementation);
          (c) the candidate's Toss ``createdAt`` falls within
              ``[this proposal's approving claim time (reserved_at) -
              _RECONCILE_MATCH_TIME_WINDOW_BEFORE, now]`` — bounds how far a
              same-attribute *different* order (created well before or
              after this proposal's own approve attempt) can still pass.
        A proposal with no recorded ``reserved_at`` (should be unreachable —
        every 'registration_unknown' row passed through the atomic claim
        first) or a candidate with an unparseable ``createdAt`` fails the
        match rather than skipping the check.
        """
        if str(item.get("symbol") or "").strip().upper() != (proposal.symbol or "").strip().upper():
            return False
        if item.get("type") != "SINGLE":
            return False
        first = item.get("first")
        if not isinstance(first, dict):
            return False
        if (first.get("orderSide") or "").upper() != proposal.side.upper():
            return False
        trigger = _safe_decimal(first.get("triggerPrice"))
        limit_price = _safe_decimal(first.get("orderPrice"))
        qty = _safe_decimal(item.get("quantity"))
        if trigger is None or limit_price is None or qty is None:
            return False
        if abs(trigger - proposal.trigger_price) > _EPS * max(1.0, abs(proposal.trigger_price)):
            return False
        if abs(limit_price - proposal.limit_price) > _EPS * max(1.0, abs(proposal.limit_price)):
            return False
        if abs(qty - proposal.quantity) > _EPS * max(1.0, abs(proposal.quantity)):
            return False
        if item.get("expireDate") != proposal.expire_date.isoformat():
            return False

        claimed_at = proposal.reserved_at
        if claimed_at is None:
            return False
        created_at = _parse_kst_datetime(item.get("createdAt"))
        if created_at is None:
            return False
        window_start = claimed_at - _RECONCILE_MATCH_TIME_WINDOW_BEFORE
        if created_at < window_start or created_at > now:
            return False

        return True

    @staticmethod
    def _is_same_order_attributes(
        a: PortfolioConditionalOrderProposal, b: PortfolioConditionalOrderProposal
    ) -> bool:
        """Codex 2nd-round review R1-3 helper: do two *local* proposal rows
        describe the same order (symbol/side/trigger/limit/quantity/
        expire_date), independent of Toss's own records? Used to detect a
        same-attribute "local contender" during reconcile — two proposals
        this similar cannot be told apart by attributes alone, so a single
        remote candidate must never be adopted while one exists."""
        if (a.symbol or "").strip().upper() != (b.symbol or "").strip().upper():
            return False
        if (a.side or "").strip().lower() != (b.side or "").strip().lower():
            return False
        if abs(a.trigger_price - b.trigger_price) > _EPS * max(1.0, abs(b.trigger_price)):
            return False
        if abs(a.limit_price - b.limit_price) > _EPS * max(1.0, abs(b.limit_price)):
            return False
        if abs(a.quantity - b.quantity) > _EPS * max(1.0, abs(b.quantity)):
            return False
        if a.expire_date != b.expire_date:
            return False
        return True

    # ------------------------------------------------------------------
    # Cancel (unified: pending -> local withdrawal; approved/paused ->
    # Toss DELETE then toss_canceled)
    # ------------------------------------------------------------------
    def cancel_proposal(self, *, account_id: int, proposal_uuid: str) -> Dict[str, Any]:
        _account, link = self._order_service._resolve_eligible_account_and_link(account_id=account_id)
        now = _now_kst_naive()

        proposal = self.repo.get_conditional_order_proposal(proposal_uuid, account_id=account_id, now=now)
        if proposal is None:
            raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")

        if proposal.status == "pending":
            updated = self.repo.transition_conditional_proposal(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                from_statuses={"pending"},
                to_status="canceled",
                event="cond_canceled",
            )
            if updated is None:
                raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
            if updated.status != "canceled":
                raise ConditionalProposalNotCancelableError(
                    f"proposal {proposal_uuid} is '{updated.status}' and can no longer be canceled"
                )
            return self._serialize_proposal(updated)

        if proposal.status in ("approving", "registration_unknown"):
            raise ConditionalProposalNotReconcilableError(
                f"proposal {proposal_uuid} is '{proposal.status}'; call .../reconcile before "
                f"attempting to cancel it"
            )

        if proposal.status in ("approved", "paused"):
            if not proposal.toss_conditional_order_id:
                raise OrderAuditPersistFailedError(
                    f"proposal {proposal_uuid} is '{proposal.status}' but has no recorded "
                    f"conditionalOrderId; manual reconciliation required before it can be canceled"
                )
            fetcher = self._order_service._ensure_fetcher()
            try:
                fetcher.cancel_conditional_order(link.external_account_seq, proposal.toss_conditional_order_id)
            except TossOrderRejectedError as exc:
                try:
                    self.repo.append_standalone_conditional_audit(
                        account_id=account_id,
                        proposal_uuid=proposal_uuid,
                        symbol=proposal.symbol,
                        side=proposal.side,
                        limit_price=proposal.limit_price,
                        quantity=proposal.quantity,
                        currency=proposal.currency,
                        est_amount_krw=proposal.est_amount_krw,
                        mode="live",
                        event="cond_cancel_rejected",
                        toss_conditional_order_id=proposal.toss_conditional_order_id,
                        error_code=exc.code,
                        detail={"status_code": exc.status_code, "message": exc.message, "data": exc.data},
                        created_at=now,
                    )
                except Exception:
                    logger.warning(
                        "[TossConditionalOrder] failed to record cond_cancel_rejected audit for "
                        "conditional_order_id=%s",
                        proposal.toss_conditional_order_id,
                        exc_info=True,
                    )
                raise
            except DataFetchError as exc:
                raise TossUpstreamError(str(exc)) from exc

            updated = self.repo.transition_conditional_proposal(
                proposal_uuid=proposal_uuid,
                account_id=account_id,
                now=now,
                from_statuses={"approved", "paused"},
                to_status="toss_canceled",
                event="cond_toss_canceled",
                mode="live",
                toss_conditional_order_id=proposal.toss_conditional_order_id,
            )
            if updated is None:
                raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
            return self._serialize_proposal(updated, mode="live")

        raise ConditionalProposalNotCancelableError(
            f"proposal {proposal_uuid} is '{proposal.status}' and can no longer be canceled"
        )

    # ------------------------------------------------------------------
    # Manual force-resolve (design spec §7 / Codex BLOCK review major 3,
    # coordinator-confirmed convergence contract): an authenticated,
    # explicit escape hatch for a proposal permanently stuck
    # 'registration_unknown' because reconcile's attribute-match search can
    # never positively prove non-registration (see reconcile_proposal /
    # this module's docstring). This is an OPERATOR action, not an
    # automated recovery — it must only be used after the operator has
    # independently confirmed on the Toss app/API that no matching
    # conditional order actually exists; calling this while an order is
    # genuinely live on Toss would silently release the daily-cap
    # reservation for an order that can still auto-execute.
    # ------------------------------------------------------------------
    def force_resolve_proposal(
        self, *, account_id: int, proposal_uuid: str, confirm: bool, reason: str
    ) -> Dict[str, Any]:
        if not confirm:
            raise ConfirmRequiredError(
                "confirm: true is required to force-resolve a conditional-order proposal"
            )
        reason_clean = (reason or "").strip()
        if not reason_clean:
            raise ValueError(
                "reason is required to force-resolve a proposal — record why the operator "
                "confirmed on Toss that no matching order actually exists"
            )

        now = _now_kst_naive()
        self._order_service._resolve_eligible_account_and_link(account_id=account_id)

        proposal = self.repo.get_conditional_order_proposal(proposal_uuid, account_id=account_id, now=now)
        if proposal is None:
            raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
        if proposal.status != "registration_unknown":
            raise ConditionalProposalNotForceResolvableError(
                f"proposal {proposal_uuid} is '{proposal.status}'; force-resolve is only allowed "
                f"from 'registration_unknown'"
            )

        updated = self.repo.transition_conditional_proposal(
            proposal_uuid=proposal_uuid,
            account_id=account_id,
            now=now,
            from_statuses={"registration_unknown"},
            to_status="registration_failed",
            event="cond_force_resolved",
            mode="live",
            error_code="manual-force-resolve",
            detail={"reason": reason_clean},
        )
        if updated is None:
            raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
        if updated.status != "registration_failed":
            raise ConditionalProposalNotForceResolvableError(
                f"proposal {proposal_uuid} is '{updated.status}'; force-resolve is only allowed "
                f"from 'registration_unknown'"
            )
        return self._serialize_proposal(updated, mode="live")

    # ------------------------------------------------------------------
    # List / get proposals (local rows, no Toss contact)
    # ------------------------------------------------------------------
    def list_proposals(
        self,
        *,
        account_id: int,
        status: Optional[str] = None,
        generation_source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self._order_service._resolve_eligible_account_and_link(account_id=account_id)
        now = _now_kst_naive()
        rows = self.repo.list_conditional_order_proposals(
            account_id, status=status, generation_source=generation_source, now=now
        )
        return [self._serialize_proposal(row) for row in rows]

    def get_proposal(self, *, account_id: int, proposal_uuid: str) -> Dict[str, Any]:
        self._order_service._resolve_eligible_account_and_link(account_id=account_id)
        now = _now_kst_naive()
        row = self.repo.get_conditional_order_proposal(proposal_uuid, account_id=account_id, now=now)
        if row is None:
            raise ConditionalProposalNotFoundError(f"No conditional proposal {proposal_uuid} for account_id={account_id}")
        return self._serialize_proposal(row)

    # ------------------------------------------------------------------
    # Observation list with lazy per-row Toss status refresh (design spec
    # §3 "상태 동기화" (a)), and the bulk sync endpoint (b). Neither ever
    # re-POSTs a registration — both are read-only against Toss.
    # ------------------------------------------------------------------
    def list_conditional_orders_with_lazy_refresh(
        self, *, account_id: int, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        self._order_service._resolve_eligible_account_and_link(account_id=account_id)
        now = _now_kst_naive()
        rows = self.repo.list_conditional_order_proposals(account_id, status=status, now=now)
        refreshed = [self._maybe_refresh_row(account_id=account_id, row=row, now=now) for row in rows]
        return [self._serialize_proposal(r) for r in refreshed]

    def sync_proposals(self, *, account_id: int) -> Dict[str, Any]:
        self._order_service._resolve_eligible_account_and_link(account_id=account_id)
        now = _now_kst_naive()
        rows = self.repo.list_conditional_order_proposals(account_id, now=now)
        targets = [r for r in rows if r.status in ("approved", "paused") and r.toss_conditional_order_id]
        updated_count = 0
        for row in targets:
            before_status = row.status
            after = self._maybe_refresh_row(account_id=account_id, row=row, now=now)
            if after.status != before_status:
                updated_count += 1
        return {"checked": len(targets), "updated": updated_count}

    def _maybe_refresh_row(
        self, *, account_id: int, row: PortfolioConditionalOrderProposal, now: datetime
    ) -> PortfolioConditionalOrderProposal:
        """Read-only per-row Toss status refresh, fails open to the stale
        local row on any error (a lazy list-view refresh must not break the
        list just because one row's Toss lookup failed)."""
        if row.status not in ("approved", "paused") or not row.toss_conditional_order_id:
            return row
        try:
            _account, link = self._order_service._resolve_eligible_account_and_link(account_id=account_id)
            fetcher = self._order_service._ensure_fetcher()
            detail = fetcher.get_conditional_order(link.external_account_seq, row.toss_conditional_order_id)
        except Exception as exc:
            logger.info(
                "[TossConditionalOrder] lazy status refresh failed for proposal_uuid=%s: %s",
                row.proposal_uuid,
                exc,
            )
            return row

        toss_status = detail.get("status") if isinstance(detail, dict) else None
        if not toss_status:
            return row
        local_status = _map_toss_status_to_local(toss_status)
        if local_status == row.status and toss_status == row.toss_status:
            return row  # no material change — avoid audit-log churn

        updated = self.repo.transition_conditional_proposal(
            proposal_uuid=row.proposal_uuid,
            account_id=account_id,
            now=now,
            from_statuses={row.status},
            to_status=local_status,
            event="cond_sync",
            mode="live",
            toss_conditional_order_id=row.toss_conditional_order_id,
            approved_at=now if local_status in ("approved", "paused") and row.approved_at is None else None,
            toss_status=toss_status,
            detail={"via": "lazy_refresh"},
        )
        return updated if updated is not None else row

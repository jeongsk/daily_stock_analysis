# -*- coding: utf-8 -*-
"""Portfolio repository.

Provides DB access helpers for portfolio account/events/snapshot tables.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError

from src.storage import (
    DatabaseManager,
    PortfolioAccount,
    PortfolioBrokerLink,
    PortfolioCashLedger,
    PortfolioConditionalOrderProposal,
    PortfolioCorporateAction,
    PortfolioDailySnapshot,
    PortfolioFxRate,
    PortfolioOrderAudit,
    PortfolioOrderProposal,
    PortfolioPosition,
    PortfolioPositionLot,
    PortfolioTrade,
    StockDaily,
)

logger = logging.getLogger(__name__)


class DuplicateTradeUidError(Exception):
    """Raised when trade_uid conflicts with existing record in one account."""


class DuplicateTradeDedupHashError(Exception):
    """Raised when dedup hash conflicts with existing record in one account."""


class PortfolioBusyError(Exception):
    """Raised when SQLite write serialization cannot acquire the ledger lock."""


class DuplicateBrokerLinkError(Exception):
    """Raised when account_id already has an active PortfolioBrokerLink row."""


class PendingProposalCapExceededError(Exception):
    """Raised inside the same write transaction as the pending-proposal count
    check + insert (design spec v2 §3: "pending 10건 상한 검사도 count+insert를
    동일 write 트랜잭션으로" — the count-then-insert TOCTOU that let concurrent
    creates both slip past the 10-pending-proposal cap)."""


class PendingConditionalProposalCapExceededError(Exception):
    """Raised inside the same write transaction as the pending-conditional-
    proposal count check + insert (Phase 4 design spec — mirrors Phase 3's
    ``PendingProposalCapExceededError`` count+insert-in-one-transaction
    pattern; the design spec is silent on a conditional-specific pending
    cap, so this inherits Phase 3's guardrail philosophy per the
    implementation brief's "스펙이 침묵하는 세부는 Phase 3 구현의 기존 패턴을
    따르세요")."""


_CONDITIONAL_ORDER_ATTRIBUTE_MATCH_EPS = 1e-6


def _conditional_order_attributes_match(
    a: "PortfolioConditionalOrderProposal", b: "PortfolioConditionalOrderProposal"
) -> bool:
    """Codex 3rd-round review R1c helper: do two *local* proposal rows
    describe the same order (symbol/side/trigger/limit/quantity/
    expire_date)? Deliberately duplicated from
    ``PortfolioConditionalOrderService._is_same_order_attributes`` (not
    imported — services import repositories, not the reverse, and this
    check must run *inside*
    ``PortfolioRepository.adopt_reconciled_order_if_uncontended``'s write
    transaction, not via a service-layer call)."""
    if (a.symbol or "").strip().upper() != (b.symbol or "").strip().upper():
        return False
    if (a.side or "").strip().lower() != (b.side or "").strip().lower():
        return False
    eps = _CONDITIONAL_ORDER_ATTRIBUTE_MATCH_EPS
    if abs(a.trigger_price - b.trigger_price) > eps * max(1.0, abs(b.trigger_price)):
        return False
    if abs(a.limit_price - b.limit_price) > eps * max(1.0, abs(b.limit_price)):
        return False
    if abs(a.quantity - b.quantity) > eps * max(1.0, abs(b.quantity)):
        return False
    if a.expire_date != b.expire_date:
        return False
    return True


class ConditionalClaimOutcome:
    """Result of ``PortfolioRepository.claim_conditional_proposal_for_approval``
    — the atomic ``pending -> approving`` claim (Phase 4 design spec §3
    "로컬 상태기계", mirroring Phase 3's ``ClaimOutcome``/
    ``claim_proposal_for_execution`` pattern exactly). Also reused (with a
    different, non-overlapping outcome subset) by
    ``reconcile_claim_stale_approving`` below — see that method's docstring
    (Codex BLOCK review blocker 2: reconcile must not preempt an in-flight
    ``approving`` claim except as a bounded, atomic crash-recovery path).

    ``outcome`` is one of (``claim_conditional_proposal_for_approval``):
      - ``"not_found"``: no such proposal for this account.
      - ``"already_terminal"``: already ``dry_run_approved``/
        ``registration_failed``/``triggered_completed``/``toss_expired``/
        ``toss_canceled``/``canceled``/``expired`` — idempotent-retry return.
      - ``"in_progress"``: already ``approving``/``registration_unknown`` —
        a concurrent approve (or an unresolved prior attempt) is in flight;
        caller should point the client at ``.../reconcile``.
      - ``"already_approved"``: already ``approved``/``paused`` — an
        idempotent-retry return distinct from ``already_terminal`` since
        these are non-terminal (still being monitored by Toss).
      - ``"rejected"``: still ``pending`` but failed a cap check inside this
        same transaction — the row is now ``registration_failed`` and
        ``reason``/``limit_type`` explain why (never reached Toss).
      - ``"claimed"``: the row is now ``approving`` with the reservation
        recorded — caller may proceed to POST to Toss.

    ``outcome`` is one of (``reconcile_claim_stale_approving``):
      - ``"not_found"``: no such proposal for this account.
      - ``"not_reconcilable"``: status is neither ``approving`` nor
        ``registration_unknown`` — nothing for reconcile to do.
      - ``"approval_in_progress"``: status is ``approving`` and the claim
        (``reserved_at``) is still fresh (within the stale-claim window) —
        a real approve POST is plausibly still in flight; reconcile must
        not touch it.
      - ``"ready"``: status is already ``registration_unknown`` (no
        takeover needed), or was ``approving`` and stale enough that this
        call atomically took it over as ``registration_unknown`` — either
        way, the caller may now proceed with the attribute-match search.

    ``outcome`` is one of (``adopt_reconciled_order_if_uncontended``,
    Codex 3rd-round review R1c):
      - ``"not_found"``: no such proposal for this account.
      - ``"not_reconcilable"``: status is no longer ``registration_unknown``
        — something else (another reconcile call, the original approve
        POST, force-resolve) already resolved it since the caller's entry
        gate; idempotent-retry return, caller should just serialize
        ``proposal`` as-is.
      - ``"contended"``: re-verified *inside this same write transaction*
        that the candidate ID is owned by another proposal, or that a
        same-attribute local proposal is still unresolved — adoption is
        cancelled, the row stays ``registration_unknown`` (an audit row
        recording ``local_contender_count``/``owned_by_other_proposal_count``
        has already been appended by this call). ``detail`` carries those
        counts too for the caller's own logging.
      - ``"adopted"``: the row was uncontended and has been transitioned
        to the requested status with the candidate's ID recorded, all
        inside this one transaction.
    """

    __slots__ = ("outcome", "proposal", "reason", "limit_type", "detail")

    def __init__(
        self,
        outcome: str,
        *,
        proposal: Optional["PortfolioConditionalOrderProposal"] = None,
        reason: Optional[str] = None,
        limit_type: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.outcome = outcome
        self.proposal = proposal
        self.reason = reason
        self.limit_type = limit_type
        self.detail = detail


class ClaimOutcome:
    """Result of ``PortfolioRepository.claim_proposal_for_execution`` — the
    atomic pending -> executing claim (design spec v3 §3 "일일 한도 원자성").

    ``outcome`` is one of:
      - ``"not_found"``: no such proposal for this account.
      - ``"already_terminal"``: already ``executed``/``dry_run_executed`` —
        caller should treat this as an idempotent-retry return.
      - ``"in_progress"``: already ``executing``/``outcome_unknown`` — a
        concurrent execute (or an unresolved prior attempt) is already in
        flight; caller should point the client at ``.../reconcile``.
      - ``"not_executable"``: some other terminal state (``canceled`` /
        ``expired`` / ``failed``).
      - ``"rejected"``: still ``pending`` but failed a cap check inside this
        same transaction — the row is now ``failed`` and ``reason``/
        ``limit_type`` explain why.
      - ``"claimed"``: the row is now ``executing`` with the reservation
        recorded — caller may proceed to POST to Toss.
    """

    __slots__ = ("outcome", "proposal", "reason", "limit_type")

    def __init__(
        self,
        outcome: str,
        *,
        proposal: Optional["PortfolioOrderProposal"] = None,
        reason: Optional[str] = None,
        limit_type: Optional[str] = None,
    ) -> None:
        self.outcome = outcome
        self.proposal = proposal
        self.reason = reason
        self.limit_type = limit_type


class PortfolioRepository:
    """DB access layer for portfolio P0 domain."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # Account CRUD
    # ------------------------------------------------------------------
    def create_account(
        self,
        *,
        name: str,
        broker: Optional[str],
        market: str,
        base_currency: str,
        owner_id: Optional[str] = None,
    ) -> PortfolioAccount:
        with self.db.get_session() as session:
            row = PortfolioAccount(
                owner_id=owner_id,
                name=name,
                broker=broker,
                market=market,
                base_currency=base_currency,
                is_active=True,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get_account(self, account_id: int, include_inactive: bool = False) -> Optional[PortfolioAccount]:
        with self.db.get_session() as session:
            return self.get_account_in_session(
                session=session,
                account_id=account_id,
                include_inactive=include_inactive,
            )

    def list_accounts(self, include_inactive: bool = False) -> List[PortfolioAccount]:
        with self.db.get_session() as session:
            query = select(PortfolioAccount)
            if not include_inactive:
                query = query.where(PortfolioAccount.is_active.is_(True))
            rows = session.execute(query.order_by(PortfolioAccount.id.asc())).scalars().all()
            return list(rows)

    def get_account_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        include_inactive: bool = False,
    ) -> Optional[PortfolioAccount]:
        conditions = [PortfolioAccount.id == account_id]
        if not include_inactive:
            conditions.append(PortfolioAccount.is_active.is_(True))
        return session.execute(
            select(PortfolioAccount).where(and_(*conditions)).limit(1)
        ).scalar_one_or_none()

    def update_account(self, account_id: int, fields: Dict[str, Any]) -> Optional[PortfolioAccount]:
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioAccount).where(PortfolioAccount.id == account_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = datetime.now()
            session.commit()
            session.refresh(row)
            return row

    def deactivate_account(self, account_id: int) -> bool:
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioAccount).where(PortfolioAccount.id == account_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return False
            row.is_active = False
            row.updated_at = datetime.now()
            session.commit()
            return True

    # ------------------------------------------------------------------
    # Event writes
    # ------------------------------------------------------------------
    @contextmanager
    def portfolio_write_session(self):
        session = self.db.get_session()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        except OperationalError as exc:
            session.close()
            if self._is_sqlite_locked_error(exc):
                raise PortfolioBusyError("Portfolio ledger is busy; please retry shortly.") from exc
            raise

        try:
            yield session
            session.commit()
        except OperationalError as exc:
            session.rollback()
            if self._is_sqlite_locked_error(exc):
                raise PortfolioBusyError("Portfolio ledger is busy; please retry shortly.") from exc
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def add_trade(
        self,
        *,
        account_id: int,
        trade_uid: Optional[str],
        symbol: str,
        market: str,
        currency: str,
        trade_date: date,
        side: str,
        quantity: float,
        price: float,
        fee: float,
        tax: float,
        note: Optional[str] = None,
        dedup_hash: Optional[str] = None,
    ) -> PortfolioTrade:
        with self.portfolio_write_session() as session:
            row = self.add_trade_in_session(
                session=session,
                account_id=account_id,
                trade_uid=trade_uid,
                symbol=symbol,
                market=market,
                currency=currency,
                trade_date=trade_date,
                side=side,
                quantity=quantity,
                price=price,
                fee=fee,
                tax=tax,
                note=note,
                dedup_hash=dedup_hash,
            )
            session.expunge(row)
            return row

    def add_cash_ledger(
        self,
        *,
        account_id: int,
        event_date: date,
        direction: str,
        amount: float,
        currency: str,
        note: Optional[str] = None,
    ) -> PortfolioCashLedger:
        with self.portfolio_write_session() as session:
            row = self.add_cash_ledger_in_session(
                session=session,
                account_id=account_id,
                event_date=event_date,
                direction=direction,
                amount=amount,
                currency=currency,
                note=note,
            )
            session.expunge(row)
            return row

    def add_corporate_action(
        self,
        *,
        account_id: int,
        symbol: str,
        market: str,
        currency: str,
        effective_date: date,
        action_type: str,
        cash_dividend_per_share: Optional[float] = None,
        split_ratio: Optional[float] = None,
        note: Optional[str] = None,
    ) -> PortfolioCorporateAction:
        with self.portfolio_write_session() as session:
            row = self.add_corporate_action_in_session(
                session=session,
                account_id=account_id,
                symbol=symbol,
                market=market,
                currency=currency,
                effective_date=effective_date,
                action_type=action_type,
                cash_dividend_per_share=cash_dividend_per_share,
                split_ratio=split_ratio,
                note=note,
            )
            session.expunge(row)
            return row

    def delete_trade(self, trade_id: int) -> bool:
        with self.portfolio_write_session() as session:
            return self.delete_trade_in_session(session=session, trade_id=trade_id)

    def delete_cash_ledger(self, entry_id: int) -> bool:
        with self.portfolio_write_session() as session:
            return self.delete_cash_ledger_in_session(session=session, entry_id=entry_id)

    def delete_corporate_action(self, action_id: int) -> bool:
        with self.portfolio_write_session() as session:
            return self.delete_corporate_action_in_session(session=session, action_id=action_id)

    def has_trade_uid(self, account_id: int, trade_uid: Optional[str]) -> bool:
        """Return True when trade_uid already exists in the account."""
        uid = (trade_uid or "").strip()
        if not uid:
            return False
        with self.db.get_session() as session:
            return self.has_trade_uid_in_session(session=session, account_id=account_id, trade_uid=uid)

    def has_trade_dedup_hash(self, account_id: int, dedup_hash: Optional[str]) -> bool:
        """Return True when dedup hash already exists in the account."""
        hash_value = (dedup_hash or "").strip()
        if not hash_value:
            return False
        with self.db.get_session() as session:
            return self.has_trade_dedup_hash_in_session(
                session=session,
                account_id=account_id,
                dedup_hash=hash_value,
            )

    def has_trade_uid_in_session(self, *, session: Any, account_id: int, trade_uid: str) -> bool:
        row = session.execute(
            select(PortfolioTrade.id).where(
                and_(
                    PortfolioTrade.account_id == account_id,
                    PortfolioTrade.trade_uid == trade_uid,
                )
            ).limit(1)
        ).scalar_one_or_none()
        return row is not None

    def has_trade_dedup_hash_in_session(self, *, session: Any, account_id: int, dedup_hash: str) -> bool:
        row = session.execute(
            select(PortfolioTrade.id).where(
                and_(
                    PortfolioTrade.account_id == account_id,
                    PortfolioTrade.dedup_hash == dedup_hash,
                )
            ).limit(1)
        ).scalar_one_or_none()
        return row is not None

    def add_trade_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        trade_uid: Optional[str],
        symbol: str,
        market: str,
        currency: str,
        trade_date: date,
        side: str,
        quantity: float,
        price: float,
        fee: float,
        tax: float,
        note: Optional[str] = None,
        dedup_hash: Optional[str] = None,
    ) -> PortfolioTrade:
        row = PortfolioTrade(
            account_id=account_id,
            trade_uid=trade_uid,
            symbol=symbol,
            market=market,
            currency=currency,
            trade_date=trade_date,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            tax=tax,
            note=note,
            dedup_hash=dedup_hash,
        )
        session.add(row)
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=account_id,
            from_date=trade_date,
        )
        try:
            session.flush()
        except IntegrityError as exc:
            raise self._translate_trade_integrity_error(
                exc=exc,
                account_id=account_id,
                trade_uid=trade_uid,
                dedup_hash=dedup_hash,
            ) from exc
        session.refresh(row)
        return row

    def add_cash_ledger_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        event_date: date,
        direction: str,
        amount: float,
        currency: str,
        note: Optional[str] = None,
    ) -> PortfolioCashLedger:
        row = PortfolioCashLedger(
            account_id=account_id,
            event_date=event_date,
            direction=direction,
            amount=amount,
            currency=currency,
            note=note,
        )
        session.add(row)
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=account_id,
            from_date=event_date,
        )
        session.flush()
        session.refresh(row)
        return row

    def add_corporate_action_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        symbol: str,
        market: str,
        currency: str,
        effective_date: date,
        action_type: str,
        cash_dividend_per_share: Optional[float] = None,
        split_ratio: Optional[float] = None,
        note: Optional[str] = None,
    ) -> PortfolioCorporateAction:
        row = PortfolioCorporateAction(
            account_id=account_id,
            symbol=symbol,
            market=market,
            currency=currency,
            effective_date=effective_date,
            action_type=action_type,
            cash_dividend_per_share=cash_dividend_per_share,
            split_ratio=split_ratio,
            note=note,
        )
        session.add(row)
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=account_id,
            from_date=effective_date,
        )
        session.flush()
        session.refresh(row)
        return row

    def delete_trade_in_session(self, *, session: Any, trade_id: int) -> bool:
        row = session.execute(
            select(PortfolioTrade).where(PortfolioTrade.id == trade_id).limit(1)
        ).scalar_one_or_none()
        if row is None:
            return False
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=int(row.account_id),
            from_date=row.trade_date,
        )
        session.delete(row)
        session.flush()
        return True

    def delete_cash_ledger_in_session(self, *, session: Any, entry_id: int) -> bool:
        row = session.execute(
            select(PortfolioCashLedger).where(PortfolioCashLedger.id == entry_id).limit(1)
        ).scalar_one_or_none()
        if row is None:
            return False
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=int(row.account_id),
            from_date=row.event_date,
        )
        session.delete(row)
        session.flush()
        return True

    def delete_corporate_action_in_session(self, *, session: Any, action_id: int) -> bool:
        row = session.execute(
            select(PortfolioCorporateAction).where(PortfolioCorporateAction.id == action_id).limit(1)
        ).scalar_one_or_none()
        if row is None:
            return False
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=int(row.account_id),
            from_date=row.effective_date,
        )
        session.delete(row)
        session.flush()
        return True

    # ------------------------------------------------------------------
    # Event reads
    # ------------------------------------------------------------------
    def list_trades(self, account_id: int, as_of: date) -> List[PortfolioTrade]:
        with self.db.get_session() as session:
            return self.list_trades_in_session(session=session, account_id=account_id, as_of=as_of)

    def list_trades_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        as_of: date,
    ) -> List[PortfolioTrade]:
        rows = session.execute(
            select(PortfolioTrade)
            .where(
                and_(
                    PortfolioTrade.account_id == account_id,
                    PortfolioTrade.trade_date <= as_of,
                )
            )
            .order_by(PortfolioTrade.trade_date.asc(), PortfolioTrade.id.asc())
        ).scalars().all()
        return list(rows)

    def list_cash_ledger(self, account_id: int, as_of: date) -> List[PortfolioCashLedger]:
        with self.db.get_session() as session:
            return self.list_cash_ledger_in_session(session=session, account_id=account_id, as_of=as_of)

    def list_cash_ledger_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        as_of: date,
    ) -> List[PortfolioCashLedger]:
        rows = session.execute(
            select(PortfolioCashLedger)
            .where(
                and_(
                    PortfolioCashLedger.account_id == account_id,
                    PortfolioCashLedger.event_date <= as_of,
                )
            )
            .order_by(PortfolioCashLedger.event_date.asc(), PortfolioCashLedger.id.asc())
        ).scalars().all()
        return list(rows)

    def list_corporate_actions(self, account_id: int, as_of: date) -> List[PortfolioCorporateAction]:
        with self.db.get_session() as session:
            return self.list_corporate_actions_in_session(session=session, account_id=account_id, as_of=as_of)

    def list_corporate_actions_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        as_of: date,
    ) -> List[PortfolioCorporateAction]:
        rows = session.execute(
            select(PortfolioCorporateAction)
            .where(
                and_(
                    PortfolioCorporateAction.account_id == account_id,
                    PortfolioCorporateAction.effective_date <= as_of,
                )
            )
            .order_by(PortfolioCorporateAction.effective_date.asc(), PortfolioCorporateAction.id.asc())
        ).scalars().all()
        return list(rows)

    def get_first_activity_date(self, *, account_id: int, as_of: date) -> Optional[date]:
        """Return earliest event date (trade/cash/corporate action) for one account."""
        with self.db.get_session() as session:
            first_trade = session.execute(
                select(func.min(PortfolioTrade.trade_date)).where(
                    and_(
                        PortfolioTrade.account_id == account_id,
                        PortfolioTrade.trade_date <= as_of,
                    )
                )
            ).scalar_one()
            first_cash = session.execute(
                select(func.min(PortfolioCashLedger.event_date)).where(
                    and_(
                        PortfolioCashLedger.account_id == account_id,
                        PortfolioCashLedger.event_date <= as_of,
                    )
                )
            ).scalar_one()
            first_action = session.execute(
                select(func.min(PortfolioCorporateAction.effective_date)).where(
                    and_(
                        PortfolioCorporateAction.account_id == account_id,
                        PortfolioCorporateAction.effective_date <= as_of,
                    )
                )
            ).scalar_one()

            candidates = [item for item in (first_trade, first_cash, first_action) if item is not None]
            if not candidates:
                return None
            return min(candidates)

    def query_trades(
        self,
        *,
        account_id: Optional[int],
        date_from: Optional[date],
        date_to: Optional[date],
        symbols: Optional[List[str]],
        side: Optional[str],
        page: int,
        page_size: int,
    ) -> Tuple[List[PortfolioTrade], int]:
        with self.db.get_session() as session:
            conditions = []
            if account_id is not None:
                conditions.append(PortfolioTrade.account_id == account_id)
            if date_from is not None:
                conditions.append(PortfolioTrade.trade_date >= date_from)
            if date_to is not None:
                conditions.append(PortfolioTrade.trade_date <= date_to)
            if symbols:
                conditions.append(PortfolioTrade.symbol.in_(symbols))
            if side:
                conditions.append(PortfolioTrade.side == side)

            data_query = select(PortfolioTrade).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioTrade.account_id,
            )
            count_query = select(func.count()).select_from(PortfolioTrade).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioTrade.account_id,
            )
            conditions.append(PortfolioAccount.is_active.is_(True))
            if conditions:
                where_clause = and_(*conditions)
                data_query = data_query.where(where_clause)
                count_query = count_query.where(where_clause)

            total = int(session.execute(count_query).scalar_one() or 0)
            rows = session.execute(
                data_query
                .order_by(PortfolioTrade.trade_date.desc(), PortfolioTrade.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).scalars().all()
            return list(rows), total

    def query_cash_ledger(
        self,
        *,
        account_id: Optional[int],
        date_from: Optional[date],
        date_to: Optional[date],
        direction: Optional[str],
        page: int,
        page_size: int,
    ) -> Tuple[List[PortfolioCashLedger], int]:
        with self.db.get_session() as session:
            conditions = []
            if account_id is not None:
                conditions.append(PortfolioCashLedger.account_id == account_id)
            if date_from is not None:
                conditions.append(PortfolioCashLedger.event_date >= date_from)
            if date_to is not None:
                conditions.append(PortfolioCashLedger.event_date <= date_to)
            if direction:
                conditions.append(PortfolioCashLedger.direction == direction)

            data_query = select(PortfolioCashLedger).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioCashLedger.account_id,
            )
            count_query = select(func.count()).select_from(PortfolioCashLedger).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioCashLedger.account_id,
            )
            conditions.append(PortfolioAccount.is_active.is_(True))
            if conditions:
                where_clause = and_(*conditions)
                data_query = data_query.where(where_clause)
                count_query = count_query.where(where_clause)

            total = int(session.execute(count_query).scalar_one() or 0)
            rows = session.execute(
                data_query
                .order_by(PortfolioCashLedger.event_date.desc(), PortfolioCashLedger.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).scalars().all()
            return list(rows), total

    def query_corporate_actions(
        self,
        *,
        account_id: Optional[int],
        date_from: Optional[date],
        date_to: Optional[date],
        symbols: Optional[List[str]],
        action_type: Optional[str],
        page: int,
        page_size: int,
    ) -> Tuple[List[PortfolioCorporateAction], int]:
        with self.db.get_session() as session:
            conditions = []
            if account_id is not None:
                conditions.append(PortfolioCorporateAction.account_id == account_id)
            if date_from is not None:
                conditions.append(PortfolioCorporateAction.effective_date >= date_from)
            if date_to is not None:
                conditions.append(PortfolioCorporateAction.effective_date <= date_to)
            if symbols:
                conditions.append(PortfolioCorporateAction.symbol.in_(symbols))
            if action_type:
                conditions.append(PortfolioCorporateAction.action_type == action_type)

            data_query = select(PortfolioCorporateAction).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioCorporateAction.account_id,
            )
            count_query = select(func.count()).select_from(PortfolioCorporateAction).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioCorporateAction.account_id,
            )
            conditions.append(PortfolioAccount.is_active.is_(True))
            if conditions:
                where_clause = and_(*conditions)
                data_query = data_query.where(where_clause)
                count_query = count_query.where(where_clause)

            total = int(session.execute(count_query).scalar_one() or 0)
            rows = session.execute(
                data_query
                .order_by(PortfolioCorporateAction.effective_date.desc(), PortfolioCorporateAction.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).scalars().all()
            return list(rows), total

    # ------------------------------------------------------------------
    # Price / FX
    # ------------------------------------------------------------------
    def get_latest_close(self, symbol: str, as_of: date) -> Optional[float]:
        close = self.get_latest_close_with_date(symbol=symbol, as_of=as_of)
        return close[0] if close is not None else None

    def get_latest_close_with_date(self, symbol: str, as_of: date) -> Optional[Tuple[float, date]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(StockDaily)
                .where(
                    and_(
                        StockDaily.code == symbol,
                        StockDaily.date <= as_of,
                    )
                )
                .order_by(desc(StockDaily.date))
                .limit(1)
            ).scalar_one_or_none()
            if row is None or row.close is None:
                return None
            return float(row.close), row.date

    def save_fx_rate(
        self,
        *,
        from_currency: str,
        to_currency: str,
        rate_date: date,
        rate: float,
        source: str = "manual",
        is_stale: bool = False,
    ) -> None:
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioFxRate).where(
                    and_(
                        PortfolioFxRate.from_currency == from_currency,
                        PortfolioFxRate.to_currency == to_currency,
                        PortfolioFxRate.rate_date == rate_date,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    PortfolioFxRate(
                        from_currency=from_currency,
                        to_currency=to_currency,
                        rate_date=rate_date,
                        rate=rate,
                        source=source,
                        is_stale=is_stale,
                    )
                )
            else:
                existing.rate = rate
                existing.source = source
                existing.is_stale = is_stale
                existing.updated_at = datetime.now()
            session.commit()

    def get_latest_fx_rate(
        self,
        *,
        from_currency: str,
        to_currency: str,
        as_of: date,
    ) -> Optional[PortfolioFxRate]:
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioFxRate)
                .where(
                    and_(
                        PortfolioFxRate.from_currency == from_currency,
                        PortfolioFxRate.to_currency == to_currency,
                        PortfolioFxRate.rate_date <= as_of,
                    )
                )
                .order_by(desc(PortfolioFxRate.rate_date))
                .limit(1)
            ).scalar_one_or_none()
            return row

    def list_daily_snapshots_for_risk(
        self,
        *,
        as_of: date,
        cost_method: str,
        account_id: Optional[int] = None,
        lookback_days: int = 180,
    ) -> List[PortfolioDailySnapshot]:
        """Load snapshot rows in ascending date order for risk monitoring."""
        with self.db.get_session() as session:
            query = (
                select(PortfolioDailySnapshot)
                .join(
                    PortfolioAccount,
                    PortfolioAccount.id == PortfolioDailySnapshot.account_id,
                )
                .where(
                    and_(
                        PortfolioDailySnapshot.snapshot_date <= as_of,
                        PortfolioDailySnapshot.cost_method == cost_method,
                        PortfolioAccount.is_active.is_(True),
                    )
                )
            )
            if account_id is not None:
                query = query.where(PortfolioDailySnapshot.account_id == account_id)
            rows = session.execute(
                query.order_by(
                    PortfolioDailySnapshot.snapshot_date.asc(),
                    PortfolioDailySnapshot.account_id.asc(),
                )
            ).scalars().all()
            if lookback_days <= 0:
                return list(rows)
            # Keep only the latest N calendar days window for risk calculations.
            cutoff_ordinal = as_of.toordinal() - lookback_days
            return [row for row in rows if row.snapshot_date.toordinal() >= cutoff_ordinal]

    def list_cached_position_identities(
        self,
        *,
        account_id: Optional[int] = None,
    ) -> List[Tuple[str, str]]:
        """Return market/symbol identities from cached non-zero positions only."""
        with self.db.get_session() as session:
            query = (
                select(PortfolioPosition.market, PortfolioPosition.symbol)
                .join(PortfolioAccount, PortfolioPosition.account_id == PortfolioAccount.id)
                .where(
                    PortfolioPosition.quantity > 0,
                    PortfolioAccount.is_active.is_(True),
                )
            )
            if account_id is not None:
                query = query.where(PortfolioPosition.account_id == account_id)
            rows = session.execute(
                query.order_by(
                    PortfolioPosition.market.asc(),
                    PortfolioPosition.symbol.asc(),
                )
            ).all()
            seen = set()
            identities: List[Tuple[str, str]] = []
            for market, symbol in rows:
                market_text = str(market or "").strip().lower()
                symbol_text = str(symbol or "").strip()
                identity = (market_text, symbol_text)
                if market_text and symbol_text and identity not in seen:
                    seen.add(identity)
                    identities.append(identity)
            return identities

    # ------------------------------------------------------------------
    # Snapshot / position cache
    # ------------------------------------------------------------------
    def replace_positions_and_lots(
        self,
        *,
        account_id: int,
        cost_method: str,
        positions: Iterable[Dict[str, Any]],
        lots: Iterable[Dict[str, Any]],
        valuation_currency: str,
    ) -> None:
        with self.db.get_session() as session:
            session.execute(
                delete(PortfolioPosition).where(
                    and_(
                        PortfolioPosition.account_id == account_id,
                        PortfolioPosition.cost_method == cost_method,
                    )
                )
            )
            session.execute(
                delete(PortfolioPositionLot).where(
                    and_(
                        PortfolioPositionLot.account_id == account_id,
                        PortfolioPositionLot.cost_method == cost_method,
                    )
                )
            )

            for item in positions:
                session.add(
                    PortfolioPosition(
                        account_id=account_id,
                        cost_method=cost_method,
                        symbol=item["symbol"],
                        market=item["market"],
                        currency=item["currency"],
                        quantity=float(item["quantity"]),
                        avg_cost=float(item["avg_cost"]),
                        total_cost=float(item["total_cost"]),
                        last_price=float(item["last_price"]),
                        market_value_base=float(item["market_value_base"]),
                        unrealized_pnl_base=float(item["unrealized_pnl_base"]),
                        valuation_currency=valuation_currency,
                    )
                )

            for lot in lots:
                session.add(
                    PortfolioPositionLot(
                        account_id=account_id,
                        cost_method=cost_method,
                        symbol=lot["symbol"],
                        market=lot["market"],
                        currency=lot["currency"],
                        open_date=lot["open_date"],
                        remaining_quantity=float(lot["remaining_quantity"]),
                        unit_cost=float(lot["unit_cost"]),
                        source_trade_id=lot.get("source_trade_id"),
                    )
                )

            session.commit()

    def _invalidate_account_cache_in_session(self, *, session: Any, account_id: int, from_date: date) -> None:
        session.execute(
            delete(PortfolioPositionLot).where(PortfolioPositionLot.account_id == account_id)
        )
        session.execute(
            delete(PortfolioPosition).where(PortfolioPosition.account_id == account_id)
        )
        session.execute(
            delete(PortfolioDailySnapshot).where(
                and_(
                    PortfolioDailySnapshot.account_id == account_id,
                    PortfolioDailySnapshot.snapshot_date >= from_date,
                )
            )
        )

    @staticmethod
    def _is_sqlite_locked_error(exc: OperationalError) -> bool:
        err_text = str(getattr(exc, "orig", exc)).lower()
        return any(
            token in err_text
            for token in (
                "database is locked",
                "database schema is locked",
                "database table is locked",
            )
        )

    @staticmethod
    def _translate_trade_integrity_error(
        *,
        exc: IntegrityError,
        account_id: int,
        trade_uid: Optional[str],
        dedup_hash: Optional[str],
    ) -> Exception:
        err_text = str(getattr(exc, "orig", exc)).lower()
        if trade_uid and ("uix_portfolio_trade_uid" in err_text or "unique" in err_text):
            return DuplicateTradeUidError(
                f"Duplicate trade_uid for account_id={account_id}: {trade_uid}"
            )
        if dedup_hash and (
            "uix_portfolio_trade_dedup_hash" in err_text
            or "portfolio_trades.account_id, portfolio_trades.dedup_hash" in err_text
            or ("unique" in err_text and "dedup_hash" in err_text)
        ):
            return DuplicateTradeDedupHashError(
                f"Duplicate dedup_hash for account_id={account_id}: {dedup_hash}"
            )
        return exc

    def upsert_daily_snapshot(
        self,
        *,
        account_id: int,
        snapshot_date: date,
        cost_method: str,
        base_currency: str,
        total_cash: float,
        total_market_value: float,
        total_equity: float,
        unrealized_pnl: float,
        realized_pnl: float,
        fee_total: float,
        tax_total: float,
        fx_stale: bool,
        payload: str,
    ) -> None:
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioDailySnapshot).where(
                    and_(
                        PortfolioDailySnapshot.account_id == account_id,
                        PortfolioDailySnapshot.snapshot_date == snapshot_date,
                        PortfolioDailySnapshot.cost_method == cost_method,
                    )
                ).limit(1)
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    PortfolioDailySnapshot(
                        account_id=account_id,
                        snapshot_date=snapshot_date,
                        cost_method=cost_method,
                        base_currency=base_currency,
                        total_cash=total_cash,
                        total_market_value=total_market_value,
                        total_equity=total_equity,
                        unrealized_pnl=unrealized_pnl,
                        realized_pnl=realized_pnl,
                        fee_total=fee_total,
                        tax_total=tax_total,
                        fx_stale=fx_stale,
                        payload=payload,
                    )
                )
            else:
                existing.base_currency = base_currency
                existing.total_cash = total_cash
                existing.total_market_value = total_market_value
                existing.total_equity = total_equity
                existing.unrealized_pnl = unrealized_pnl
                existing.realized_pnl = realized_pnl
                existing.fee_total = fee_total
                existing.tax_total = tax_total
                existing.fx_stale = fx_stale
                existing.payload = payload
                existing.updated_at = datetime.now()
            session.commit()

    def replace_positions_lots_and_snapshot(
        self,
        *,
        account_id: int,
        snapshot_date: date,
        cost_method: str,
        base_currency: str,
        total_cash: float,
        total_market_value: float,
        total_equity: float,
        unrealized_pnl: float,
        realized_pnl: float,
        fee_total: float,
        tax_total: float,
        fx_stale: bool,
        payload: str,
        positions: Iterable[Dict[str, Any]],
        lots: Iterable[Dict[str, Any]],
        valuation_currency: str,
    ) -> None:
        """Atomically refresh position cache and daily snapshot in one transaction."""
        with self.db.get_session() as session:
            session.execute(
                delete(PortfolioPosition).where(
                    and_(
                        PortfolioPosition.account_id == account_id,
                        PortfolioPosition.cost_method == cost_method,
                    )
                )
            )
            session.execute(
                delete(PortfolioPositionLot).where(
                    and_(
                        PortfolioPositionLot.account_id == account_id,
                        PortfolioPositionLot.cost_method == cost_method,
                    )
                )
            )

            for item in positions:
                session.add(
                    PortfolioPosition(
                        account_id=account_id,
                        cost_method=cost_method,
                        symbol=item["symbol"],
                        market=item["market"],
                        currency=item["currency"],
                        quantity=float(item["quantity"]),
                        avg_cost=float(item["avg_cost"]),
                        total_cost=float(item["total_cost"]),
                        last_price=float(item["last_price"]),
                        market_value_base=float(item["market_value_base"]),
                        unrealized_pnl_base=float(item["unrealized_pnl_base"]),
                        valuation_currency=valuation_currency,
                    )
                )

            for lot in lots:
                session.add(
                    PortfolioPositionLot(
                        account_id=account_id,
                        cost_method=cost_method,
                        symbol=lot["symbol"],
                        market=lot["market"],
                        currency=lot["currency"],
                        open_date=lot["open_date"],
                        remaining_quantity=float(lot["remaining_quantity"]),
                        unit_cost=float(lot["unit_cost"]),
                        source_trade_id=lot.get("source_trade_id"),
                    )
                )

            existing = session.execute(
                select(PortfolioDailySnapshot).where(
                    and_(
                        PortfolioDailySnapshot.account_id == account_id,
                        PortfolioDailySnapshot.snapshot_date == snapshot_date,
                        PortfolioDailySnapshot.cost_method == cost_method,
                    )
                ).limit(1)
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    PortfolioDailySnapshot(
                        account_id=account_id,
                        snapshot_date=snapshot_date,
                        cost_method=cost_method,
                        base_currency=base_currency,
                        total_cash=total_cash,
                        total_market_value=total_market_value,
                        total_equity=total_equity,
                        unrealized_pnl=unrealized_pnl,
                        realized_pnl=realized_pnl,
                        fee_total=fee_total,
                        tax_total=tax_total,
                        fx_stale=fx_stale,
                        payload=payload,
                    )
                )
            else:
                existing.base_currency = base_currency
                existing.total_cash = total_cash
                existing.total_market_value = total_market_value
                existing.total_equity = total_equity
                existing.unrealized_pnl = unrealized_pnl
                existing.realized_pnl = realized_pnl
                existing.fee_total = fee_total
                existing.tax_total = tax_total
                existing.fx_stale = fx_stale
                existing.payload = payload
                existing.updated_at = datetime.now()

            session.commit()

    # ------------------------------------------------------------------
    # Broker link (Phase 2 hybrid sync)
    # ------------------------------------------------------------------
    def create_broker_link(
        self,
        *,
        account_id: int,
        provider: str,
        external_account_seq: str,
        external_account_no: Optional[str],
        linked_at: datetime,
        snapshot_boundary_at: datetime,
        last_synced_at: datetime,
        active: bool = True,
    ) -> PortfolioBrokerLink:
        """Insert a standalone broker-link row (no account/trade creation).

        Used directly by tests that pre-seed a link without going through
        ``link_toss_account``'s atomic new-account flow. Production link
        creation for a brand-new account goes through
        ``create_broker_link_with_opening_trades`` instead, which wraps the
        account row, its opening trades, and this link row in one transaction
        (design spec §3 link atomicity).
        """
        with self.db.get_session() as session:
            row = PortfolioBrokerLink(
                account_id=account_id,
                provider=provider,
                external_account_seq=str(external_account_seq),
                external_account_no=external_account_no,
                linked_at=linked_at,
                snapshot_boundary_at=snapshot_boundary_at,
                last_synced_at=last_synced_at,
                active=active,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise DuplicateBrokerLinkError(
                    f"account_id={account_id} is already linked to a broker account"
                ) from exc
            session.refresh(row)
            session.expunge(row)
            return row

    def create_broker_link_with_opening_trades(
        self,
        *,
        account_name: str,
        broker: Optional[str],
        market: str,
        base_currency: str,
        owner_id: Optional[str],
        provider: str,
        external_account_seq: str,
        external_account_no: Optional[str],
        linked_at: datetime,
        snapshot_boundary_at: datetime,
        last_synced_at: datetime,
        opening_trades: Iterable[Dict[str, Any]],
    ) -> Tuple[PortfolioAccount, PortfolioBrokerLink, int]:
        """Atomically create a portfolio account, its opening trades, and the
        broker link row in a single DB transaction.

        Design spec §3 "링크 원자성": a mid-way failure (e.g. a duplicate
        opening ``trade_uid``) rolls back the account creation too, so this
        never leaves an orphan portfolio account or a partially-populated
        ledger behind. Each ``opening_trades`` item is a dict with keys
        ``symbol``, ``market``, ``currency``, ``trade_date``, ``quantity``,
        ``price``, ``trade_uid``, and optional ``note``.
        """
        with self.portfolio_write_session() as session:
            account = PortfolioAccount(
                owner_id=owner_id,
                name=account_name,
                broker=broker,
                market=market,
                base_currency=base_currency,
                is_active=True,
            )
            session.add(account)
            session.flush()  # populate account.id for the trades/link below

            imported = 0
            for item in opening_trades:
                self.add_trade_in_session(
                    session=session,
                    account_id=int(account.id),
                    trade_uid=item["trade_uid"],
                    symbol=item["symbol"],
                    market=item["market"],
                    currency=item["currency"],
                    trade_date=item["trade_date"],
                    side="buy",
                    quantity=float(item["quantity"]),
                    price=float(item["price"]),
                    fee=0.0,
                    tax=0.0,
                    note=item.get("note"),
                )
                imported += 1

            link = PortfolioBrokerLink(
                account_id=account.id,
                provider=provider,
                external_account_seq=str(external_account_seq),
                external_account_no=external_account_no,
                linked_at=linked_at,
                snapshot_boundary_at=snapshot_boundary_at,
                last_synced_at=last_synced_at,
                active=True,
            )
            session.add(link)
            session.flush()
            session.refresh(account)
            session.refresh(link)
            session.expunge(account)
            session.expunge(link)
            return account, link, imported

    def get_broker_link_by_account(
        self,
        account_id: int,
        *,
        active_only: bool = False,
    ) -> Optional[PortfolioBrokerLink]:
        with self.db.get_session() as session:
            conditions = [PortfolioBrokerLink.account_id == account_id]
            if active_only:
                conditions.append(PortfolioBrokerLink.active.is_(True))
            row = session.execute(
                select(PortfolioBrokerLink).where(and_(*conditions)).limit(1)
            ).scalar_one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    def get_broker_link_by_external(
        self,
        *,
        provider: str,
        external_account_seq: str,
    ) -> Optional[PortfolioBrokerLink]:
        """Find any link row (active or inactive) for one broker account —
        used to detect an active-link conflict (409) or an inactive link
        eligible for relink reactivation (design spec §3/§5)."""
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioBrokerLink)
                .where(
                    and_(
                        PortfolioBrokerLink.provider == provider,
                        PortfolioBrokerLink.external_account_seq == str(external_account_seq),
                    )
                )
                .order_by(PortfolioBrokerLink.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    def reactivate_broker_link(self, *, link_id: int) -> Optional[PortfolioBrokerLink]:
        """Flip ``active=True`` on an existing (inactive) link row, preserving
        its ``snapshot_boundary_at``/``last_synced_at``/``last_reconciled_at``
        cursor — no opening trades are recreated (design spec §3 unlink/relink)."""
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioBrokerLink).where(PortfolioBrokerLink.id == link_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.active = True
            row.updated_at = datetime.now()
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row

    def list_broker_links(self, *, include_inactive: bool = False) -> List[PortfolioBrokerLink]:
        with self.db.get_session() as session:
            query = select(PortfolioBrokerLink)
            if not include_inactive:
                query = query.where(PortfolioBrokerLink.active.is_(True))
            rows = session.execute(
                query.order_by(PortfolioBrokerLink.id.asc())
            ).scalars().all()
            rows = list(rows)
            for row in rows:
                session.expunge(row)
            return rows

    def update_broker_link_sync(
        self,
        *,
        account_id: int,
        candidate_last_synced_at: datetime,
        last_reconciled_at: Optional[datetime],
    ) -> Optional[PortfolioBrokerLink]:
        """Advance the sync cursor monotonically: ``last_synced_at`` only moves
        forward (design spec §3 cursor redesign (c)) — a candidate that is not
        strictly greater than the currently-stored value is silently dropped,
        which is what keeps a slow/delayed concurrent sync call from regressing
        a cursor a faster concurrent call already advanced past it.
        ``last_reconciled_at`` always updates unconditionally (reconciliation
        is always computed fresh against current holdings).
        """
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioBrokerLink).where(PortfolioBrokerLink.account_id == account_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            if candidate_last_synced_at > row.last_synced_at:
                row.last_synced_at = candidate_last_synced_at
            if last_reconciled_at is not None:
                row.last_reconciled_at = last_reconciled_at
            row.updated_at = datetime.now()
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row

    def deactivate_broker_link(self, account_id: int) -> bool:
        """Unlink = deactivate, never delete: the row (and its cursor) is kept
        so a future relink to the same external account can resume from it
        (design spec §3 unlink/relink)."""
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioBrokerLink).where(PortfolioBrokerLink.account_id == account_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return False
            row.active = False
            row.updated_at = datetime.now()
            session.commit()
            return True

    # ------------------------------------------------------------------
    # Order proposals (Toss Invest Phase 3 — two-step manual-approval orders)
    # ------------------------------------------------------------------

    @staticmethod
    def _audit_row(
        *,
        account_id: int,
        proposal_uuid: str,
        symbol: str,
        side: str,
        order_type: str,
        price: Optional[float],
        quantity: float,
        currency: str,
        est_amount_krw: float,
        mode: Optional[str],
        event: str,
        toss_order_id: Optional[str],
        created_at: datetime,
        error_code: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> PortfolioOrderAudit:
        return PortfolioOrderAudit(
            account_id=account_id,
            proposal_uuid=proposal_uuid,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            quantity=quantity,
            currency=currency,
            est_amount_krw=est_amount_krw,
            mode=mode,
            event=event,
            toss_order_id=toss_order_id,
            error_code=error_code,
            detail=json.dumps(detail) if detail is not None else None,
            created_at=created_at,
        )

    def create_order_proposal_with_audit(
        self,
        *,
        account_id: int,
        proposal_uuid: str,
        symbol: str,
        storage_symbol: str,
        market: str,
        currency: str,
        side: str,
        order_type: str,
        price: Optional[float],
        quantity: float,
        est_amount_krw: float,
        created_at: datetime,
        expires_at: datetime,
        max_pending_proposals: int,
    ) -> PortfolioOrderProposal:
        """Atomically insert the proposal row and its ``proposed`` audit event
        (design spec §7: status and audit trail must never drift apart).

        The pending-proposal-count cap check and the insert happen inside the
        same write transaction (design spec v2 §3 "pending 10건 상한 검사도
        count+insert를 동일 write 트랜잭션으로") — raises
        ``PendingProposalCapExceededError`` instead of inserting when the
        account already has ``max_pending_proposals`` non-expired ``pending``
        rows, closing the count-then-insert TOCTOU that let two concurrent
        creates both slip past a separately-read count.
        """
        with self.portfolio_write_session() as session:
            pending_count = int(
                session.execute(
                    select(func.count(PortfolioOrderProposal.id)).where(
                        and_(
                            PortfolioOrderProposal.account_id == account_id,
                            PortfolioOrderProposal.status == "pending",
                            PortfolioOrderProposal.expires_at > created_at,
                        )
                    )
                ).scalar_one()
            )
            if pending_count >= max_pending_proposals:
                raise PendingProposalCapExceededError(
                    f"account_id={account_id} already has {pending_count} pending proposals "
                    f"(max {max_pending_proposals})"
                )

            row = PortfolioOrderProposal(
                account_id=account_id,
                proposal_uuid=proposal_uuid,
                symbol=symbol,
                storage_symbol=storage_symbol,
                market=market,
                currency=currency,
                side=side,
                order_type=order_type,
                price=price,
                quantity=quantity,
                est_amount_krw=est_amount_krw,
                status="pending",
                created_at=created_at,
                expires_at=expires_at,
            )
            session.add(row)
            session.flush()
            session.add(
                self._audit_row(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    price=price,
                    quantity=quantity,
                    currency=currency,
                    est_amount_krw=est_amount_krw,
                    mode=None,
                    event="proposed",
                    toss_order_id=None,
                    created_at=created_at,
                )
            )
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row

    def _materialize_expiry_in_session(
        self, *, session: Any, row: PortfolioOrderProposal, now: datetime
    ) -> None:
        """If ``row`` is ``pending`` and past its TTL, flip it to ``expired``
        and append the matching audit event, all inside the caller's open
        write transaction. Idempotent: once materialized, ``row.status`` is
        no longer ``pending`` so a repeat call is a no-op."""
        if row.status == "pending" and row.expires_at <= now:
            row.status = "expired"
            row.updated_at = datetime.now()
            session.add(
                self._audit_row(
                    account_id=row.account_id,
                    proposal_uuid=row.proposal_uuid,
                    symbol=row.symbol,
                    side=row.side,
                    order_type=row.order_type,
                    price=row.price,
                    quantity=row.quantity,
                    currency=row.currency,
                    est_amount_krw=row.est_amount_krw,
                    mode=None,
                    event="expired",
                    toss_order_id=None,
                    created_at=now,
                )
            )
            session.flush()

    def _materialize_expiry_standalone(
        self, *, proposal_uuid: str, account_id: Optional[int], now: datetime
    ) -> None:
        with self.portfolio_write_session() as session:
            conditions = [PortfolioOrderProposal.proposal_uuid == proposal_uuid]
            if account_id is not None:
                conditions.append(PortfolioOrderProposal.account_id == account_id)
            row = session.execute(
                select(PortfolioOrderProposal).where(and_(*conditions)).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return
            self._materialize_expiry_in_session(session=session, row=row, now=now)

    def get_order_proposal(
        self,
        proposal_uuid: str,
        *,
        account_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Optional[PortfolioOrderProposal]:
        """Fetch one proposal, lazily materializing TTL expiry first when
        ``now`` is given (design spec: expiry has no background sweeper)."""
        if now is not None:
            self._materialize_expiry_standalone(proposal_uuid=proposal_uuid, account_id=account_id, now=now)
        with self.db.get_session() as session:
            conditions = [PortfolioOrderProposal.proposal_uuid == proposal_uuid]
            if account_id is not None:
                conditions.append(PortfolioOrderProposal.account_id == account_id)
            row = session.execute(
                select(PortfolioOrderProposal).where(and_(*conditions)).limit(1)
            ).scalar_one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    def get_order_proposal_by_toss_order_id(
        self, toss_order_id: str, *, account_id: int
    ) -> Optional[PortfolioOrderProposal]:
        """Find the proposal that produced ``toss_order_id`` for one account —
        the "was this order actually self-issued" check the cancel-a-placed-
        order endpoint requires (design spec §3)."""
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioOrderProposal).where(
                    and_(
                        PortfolioOrderProposal.toss_order_id == toss_order_id,
                        PortfolioOrderProposal.account_id == account_id,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    def list_order_proposals(
        self,
        account_id: int,
        *,
        status: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> List[PortfolioOrderProposal]:
        """List proposals for one account, optionally filtered by status.
        When ``now`` is given, every still-``pending``-but-overdue row is
        materialized to ``expired`` first so a polling caller sees the true
        status without waiting for a future ``get``/``execute`` call to
        trigger it."""
        if now is not None:
            with self.db.get_session() as session:
                overdue_uuids = session.execute(
                    select(PortfolioOrderProposal.proposal_uuid).where(
                        and_(
                            PortfolioOrderProposal.account_id == account_id,
                            PortfolioOrderProposal.status == "pending",
                            PortfolioOrderProposal.expires_at <= now,
                        )
                    )
                ).scalars().all()
            for proposal_uuid in overdue_uuids:
                self._materialize_expiry_standalone(proposal_uuid=proposal_uuid, account_id=account_id, now=now)

        with self.db.get_session() as session:
            conditions = [PortfolioOrderProposal.account_id == account_id]
            if status is not None:
                conditions.append(PortfolioOrderProposal.status == status)
            rows = session.execute(
                select(PortfolioOrderProposal).where(and_(*conditions)).order_by(PortfolioOrderProposal.id.desc())
            ).scalars().all()
            rows = list(rows)
            for row in rows:
                session.expunge(row)
            return rows

    def count_active_pending_proposals(self, account_id: int, *, now: datetime) -> int:
        """Count non-expired ``pending`` proposals for the per-account cap
        (design spec §3: max 10 pending proposals per account)."""
        with self.db.get_session() as session:
            return int(
                session.execute(
                    select(func.count(PortfolioOrderProposal.id)).where(
                        and_(
                            PortfolioOrderProposal.account_id == account_id,
                            PortfolioOrderProposal.status == "pending",
                            PortfolioOrderProposal.expires_at > now,
                        )
                    )
                ).scalar_one()
            )

    @staticmethod
    def _sum_reserved_and_live_amount_in_session(
        session: Any, *, account_id: int, kst_date: date
    ) -> float:
        """The v3 daily-cap sum, evaluated against ``PortfolioOrderProposal``
        directly (not the audit log — an audit log sum would double count a
        proposal that emits both an ``executing`` and a later ``executed``
        audit row for the same reservation) inside the caller's *own* open
        write transaction, so this reflects every reservation any other
        already-committed transaction has made — and, combined with
        ``BEGIN IMMEDIATE`` serializing writers, every reservation any
        transaction racing to commit *right now* will make (design spec v3
        §3 "일일 한도 원자성").

        Counts, for one KST calendar date ``kst_date``:
          - **every** ``executing``/``outcome_unknown`` row for this account,
            *regardless of its ``reserved_at`` date* — still non-terminal,
            counted in full against every date's cap until reconciled
            (design spec v3 "일일 한도 원자성"). This is deliberately
            date-agnostic: a v2 implementation that only counted a reservation
            on its own ``reserved_at`` date let a pre-midnight
            ``executing``/``outcome_unknown`` reservation drop out of the very
            next calendar day's claim-time sum, letting a concurrent claim on
            "day 2" spend the full daily cap on top of an amount that, in
            reality, is still an open, unresolved liability from "day 1"
            (reviewer re-review blocker 1). Counting it on every date is a
            deliberately conservative over-count (the design spec explicitly
            accepts double-counting an unresolved reservation over "the cap is
            an upper bound, not a target");
          - every ``executed`` row whose ``reserved_at`` *or* ``executed_at``
            falls on this specific date (a reservation straddling local
            midnight still counts on both its reservation and confirmation
            dates once resolved — design spec's conservative call).
        ``dry_run_executed``/``canceled``/``expired``/``failed`` never count.

        Phase 4 (design spec
        docs/superpowers/specs/2026-07-19-toss-conditional-order-phase4-design.md
        §3 "한도 산입": "Phase 3 v3의 일일 한도 합산 로직에 조건주문 미확정분
        합류") — this is that join point: the return value also folds in
        every unresolved ``PortfolioConditionalOrderProposal`` reservation
        for this account/date (see ``_sum_conditional_reserved_amount_in_session``
        below), so this one function is the single shared daily-cap ceiling
        both Phase 3's ``claim_proposal_for_execution`` and Phase 4's
        ``claim_conditional_proposal_for_approval`` check against — a
        conditional-order registration counts against (and is blocked by)
        the same daily total as a plain order, and vice versa.
        """
        day_start = datetime.combine(kst_date, time.min)
        day_end = day_start + timedelta(days=1)

        def _in_day(column):
            return and_(column >= day_start, column < day_end)

        total = session.execute(
            select(func.coalesce(func.sum(PortfolioOrderProposal.est_amount_krw), 0.0)).where(
                and_(
                    PortfolioOrderProposal.account_id == account_id,
                    or_(
                        PortfolioOrderProposal.status.in_(("executing", "outcome_unknown")),
                        and_(
                            PortfolioOrderProposal.status == "executed",
                            or_(
                                _in_day(PortfolioOrderProposal.reserved_at),
                                _in_day(PortfolioOrderProposal.executed_at),
                            ),
                        ),
                    ),
                )
            )
        ).scalar_one()
        conditional_total = PortfolioRepository._sum_conditional_reserved_amount_in_session(
            session, account_id=account_id, kst_date=kst_date
        )
        return float(total or 0.0) + conditional_total

    @staticmethod
    def _sum_conditional_reserved_amount_in_session(
        session: Any, *, account_id: int, kst_date: date
    ) -> float:
        """The Phase 4 half of the shared daily-cap sum (see the docstring
        above this call site) — same date-agnostic-while-unresolved
        philosophy as Phase 3's sum, applied to
        ``PortfolioConditionalOrderProposal``:

          - **every** ``approving``/``registration_unknown``/``approved``/
            ``paused`` row counts in full, *regardless of date* — all four
            represent an unresolved liability (a claim in flight, an
            ambiguous registration, or a live Toss-side monitored order
            that could auto-execute at any moment) until it resolves to a
            terminal state or ``triggered_completed``.
          - every ``triggered_completed`` row (Toss observed ``COMPLETED`` —
            the conditional-order equivalent of Phase 3's ``executed``)
            whose ``reserved_at`` *or* ``approved_at`` falls on this date.
          - ``canceled``/``expired``/``dry_run_approved``/
            ``registration_failed``/``toss_expired``/``toss_canceled``
            never count.
        """
        day_start = datetime.combine(kst_date, time.min)
        day_end = day_start + timedelta(days=1)

        def _in_day(column):
            return and_(column >= day_start, column < day_end)

        total = session.execute(
            select(func.coalesce(func.sum(PortfolioConditionalOrderProposal.est_amount_krw), 0.0)).where(
                and_(
                    PortfolioConditionalOrderProposal.account_id == account_id,
                    or_(
                        PortfolioConditionalOrderProposal.status.in_(
                            ("approving", "registration_unknown", "approved", "paused")
                        ),
                        and_(
                            PortfolioConditionalOrderProposal.status == "triggered_completed",
                            or_(
                                _in_day(PortfolioConditionalOrderProposal.reserved_at),
                                _in_day(PortfolioConditionalOrderProposal.approved_at),
                            ),
                        ),
                    ),
                )
            )
        ).scalar_one()
        return float(total or 0.0)

    def sum_daily_reserved_and_executed_amount_krw(self, account_id: int, *, kst_date: date) -> float:
        """Read-only (non-atomic) view of the same v3 daily-cap sum, for
        best-effort pre-checks (``create_proposal``'s early friendly
        rejection — the authoritative check is
        ``claim_proposal_for_execution``'s in-transaction version above) and
        for status/report display. Since Phase 4, this already folds in
        conditional-order reservations too (see
        ``_sum_reserved_and_live_amount_in_session``) — used unchanged by
        both Phase 3's and Phase 4's best-effort proposal-creation checks."""
        with self.db.get_session() as session:
            return self._sum_reserved_and_live_amount_in_session(session, account_id=account_id, kst_date=kst_date)

    def claim_proposal_for_execution(
        self,
        *,
        proposal_uuid: str,
        account_id: int,
        now: datetime,
        est_amount_krw: float,
        high_value_threshold_krw: float,
        per_order_cap_krw: float,
        daily_cap_krw: float,
        eps: float = 1e-6,
    ) -> ClaimOutcome:
        """Atomic ``pending -> executing`` claim (design spec v2 §3): inside
        one write transaction, materialize TTL expiry if due, then — only if
        the row is still ``pending`` — re-validate the high-value hard
        reject, the per-order cap, and the daily cap (via
        ``_sum_reserved_and_live_amount_in_session`` against this same
        session) before inserting the ``executing`` reservation. A failing
        cap check transitions the row straight to ``failed`` in the same
        transaction rather than leaving it ``pending`` for a caller to
        separately fail — there is no window between "checked the caps" and
        "recorded the reservation" for a concurrent claim to slip through.

        See ``ClaimOutcome`` for the possible ``outcome`` values.
        """
        with self.portfolio_write_session() as session:
            row = session.execute(
                select(PortfolioOrderProposal).where(
                    and_(
                        PortfolioOrderProposal.proposal_uuid == proposal_uuid,
                        PortfolioOrderProposal.account_id == account_id,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return ClaimOutcome("not_found")

            self._materialize_expiry_in_session(session=session, row=row, now=now)

            if row.status in ("executed", "dry_run_executed"):
                session.refresh(row)
                session.expunge(row)
                return ClaimOutcome("already_terminal", proposal=row)
            if row.status in ("executing", "outcome_unknown"):
                session.refresh(row)
                session.expunge(row)
                return ClaimOutcome("in_progress", proposal=row)
            if row.status != "pending":
                session.refresh(row)
                session.expunge(row)
                return ClaimOutcome("not_executable", proposal=row)

            def _reject(*, limit_type: str, error_code: str, reason: str) -> ClaimOutcome:
                row.status = "failed"
                row.updated_at = datetime.now()
                session.add(
                    self._audit_row(
                        account_id=account_id,
                        proposal_uuid=proposal_uuid,
                        symbol=row.symbol,
                        side=row.side,
                        order_type=row.order_type,
                        price=row.price,
                        quantity=row.quantity,
                        currency=row.currency,
                        est_amount_krw=est_amount_krw,
                        mode="live",
                        event="rejected",
                        toss_order_id=None,
                        created_at=now,
                        error_code=error_code,
                        detail={"reason": reason},
                    )
                )
                session.flush()
                session.refresh(row)
                session.expunge(row)
                return ClaimOutcome("rejected", proposal=row, reason=reason, limit_type=limit_type)

            if est_amount_krw >= high_value_threshold_krw:
                return _reject(
                    limit_type="high_value",
                    error_code="high-value-hard-reject",
                    reason=(
                        f"Estimated order amount {est_amount_krw:,.0f} KRW is at or above the "
                        f"{high_value_threshold_krw:,.0f} KRW hard-reject threshold"
                    ),
                )
            if est_amount_krw > per_order_cap_krw + eps:
                return _reject(
                    limit_type="per_order",
                    error_code="limit-exceeded",
                    reason=(
                        f"Estimated order amount {est_amount_krw:,.0f} KRW exceeds the per-order cap "
                        f"{per_order_cap_krw:,.0f} KRW"
                    ),
                )
            already_reserved = self._sum_reserved_and_live_amount_in_session(
                session, account_id=account_id, kst_date=now.date()
            )
            if already_reserved + est_amount_krw > daily_cap_krw + eps:
                return _reject(
                    limit_type="daily",
                    error_code="limit-exceeded",
                    reason=(
                        f"Estimated order amount {est_amount_krw:,.0f} KRW would push today's reserved+"
                        f"executed total to {already_reserved + est_amount_krw:,.0f} KRW, exceeding the "
                        f"daily cap {daily_cap_krw:,.0f} KRW"
                    ),
                )

            row.status = "executing"
            row.reserved_at = now
            row.est_amount_krw = est_amount_krw
            row.updated_at = datetime.now()
            session.add(
                self._audit_row(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=row.symbol,
                    side=row.side,
                    order_type=row.order_type,
                    price=row.price,
                    quantity=row.quantity,
                    currency=row.currency,
                    est_amount_krw=est_amount_krw,
                    mode="live",
                    event="executing",
                    toss_order_id=None,
                    created_at=now,
                )
            )
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return ClaimOutcome("claimed", proposal=row)

    def transition_proposal(
        self,
        *,
        proposal_uuid: str,
        account_id: int,
        now: datetime,
        from_statuses: Iterable[str],
        to_status: str,
        event: str,
        mode: Optional[str] = None,
        toss_order_id: Optional[str] = None,
        executed_at: Optional[datetime] = None,
        error_code: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        est_amount_krw_override: Optional[float] = None,
    ) -> Optional[PortfolioOrderProposal]:
        """Atomically materialize TTL expiry if due, then — only if the row's
        resulting status is still one of ``from_statuses`` — apply the
        requested transition and append a matching audit row in the same DB
        transaction (design spec §7 status/audit consistency).

        Returns the proposal reflecting its *actual* final status, which may
        differ from ``to_status`` (e.g. it had just expired, or was already
        executed by a prior call) — callers compare ``row.status`` against
        what they expected rather than relying on a boolean, so they can
        react precisely: an already-``executed``/``dry_run_executed`` row is
        an idempotent-retry return, not a fresh transition (design spec §3
        clientOrderId idempotency). Returns ``None`` only when no such
        proposal exists at all for this account.
        """
        with self.portfolio_write_session() as session:
            row = session.execute(
                select(PortfolioOrderProposal).where(
                    and_(
                        PortfolioOrderProposal.proposal_uuid == proposal_uuid,
                        PortfolioOrderProposal.account_id == account_id,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None

            self._materialize_expiry_in_session(session=session, row=row, now=now)

            if row.status not in set(from_statuses):
                session.refresh(row)
                session.expunge(row)
                return row

            row.status = to_status
            row.updated_at = datetime.now()
            if executed_at is not None:
                row.executed_at = executed_at
            if toss_order_id is not None:
                row.toss_order_id = toss_order_id

            est_amount = est_amount_krw_override if est_amount_krw_override is not None else row.est_amount_krw
            session.add(
                self._audit_row(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=row.symbol,
                    side=row.side,
                    order_type=row.order_type,
                    price=row.price,
                    quantity=row.quantity,
                    currency=row.currency,
                    est_amount_krw=est_amount,
                    mode=mode,
                    event=event,
                    toss_order_id=toss_order_id,
                    created_at=now,
                    error_code=error_code,
                    detail=detail,
                )
            )
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row

    def append_standalone_order_audit(
        self,
        *,
        account_id: int,
        proposal_uuid: str,
        symbol: str,
        side: str,
        order_type: str,
        price: Optional[float],
        quantity: float,
        currency: str,
        est_amount_krw: float,
        mode: Optional[str],
        event: str,
        toss_order_id: Optional[str],
        created_at: datetime,
        error_code: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> PortfolioOrderAudit:
        """Append one audit row without touching ``PortfolioOrderProposal.status``
        — used by the "cancel an already-placed order" flow, where the
        proposal itself stays ``executed`` (it *was* executed; only the
        resulting broker order is now canceled — a distinct fact tracked here,
        not a proposal-state concept). Raises whatever
        ``portfolio_write_session`` raises on a commit failure (design spec
        §7: the caller must treat that as this action having failed, not as a
        silently-dropped log line)."""
        with self.portfolio_write_session() as session:
            audit = self._audit_row(
                account_id=account_id,
                proposal_uuid=proposal_uuid,
                symbol=symbol,
                side=side,
                order_type=order_type,
                price=price,
                quantity=quantity,
                currency=currency,
                est_amount_krw=est_amount_krw,
                mode=mode,
                event=event,
                toss_order_id=toss_order_id,
                created_at=created_at,
                error_code=error_code,
                detail=detail,
            )
            session.add(audit)
            session.flush()
            session.refresh(audit)
            session.expunge(audit)
            return audit

    def list_order_audits(self, account_id: int, *, proposal_uuid: Optional[str] = None) -> List[PortfolioOrderAudit]:
        """List audit rows for one account (optionally scoped to one
        proposal), oldest first — used for order-status/history responses.
        Shared verbatim by Phase 4 conditional-order proposals too (the
        audit table is keyed by ``account_id``/``proposal_uuid``, not by
        which proposal table the row originated from — see
        ``_conditional_audit_row``)."""
        with self.db.get_session() as session:
            conditions = [PortfolioOrderAudit.account_id == account_id]
            if proposal_uuid is not None:
                conditions.append(PortfolioOrderAudit.proposal_uuid == proposal_uuid)
            rows = session.execute(
                select(PortfolioOrderAudit).where(and_(*conditions)).order_by(PortfolioOrderAudit.id.asc())
            ).scalars().all()
            rows = list(rows)
            for row in rows:
                session.expunge(row)
            return rows

    # ------------------------------------------------------------------
    # Conditional order proposals (Toss Invest Phase 4 — server-side
    # SINGLE/STOP conditional orders). Design spec
    # docs/superpowers/specs/2026-07-19-toss-conditional-order-phase4-design.md
    # §3 "저장 모델"/"로컬 상태기계". Mirrors the Phase 3 order-proposal section
    # above structurally (same atomic-claim/transition/TTL patterns); the
    # shared ``PortfolioOrderAudit`` table (and its append-only DB trigger)
    # is reused unchanged — see ``_conditional_audit_row``.
    # ------------------------------------------------------------------

    @staticmethod
    def _conditional_audit_row(
        *,
        account_id: int,
        proposal_uuid: str,
        symbol: str,
        side: str,
        limit_price: Optional[float],
        quantity: float,
        currency: str,
        est_amount_krw: float,
        mode: Optional[str],
        event: str,
        toss_conditional_order_id: Optional[str],
        created_at: datetime,
        error_code: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> PortfolioOrderAudit:
        """Build one Phase 4 conditional-order audit row on the shared,
        append-only ``PortfolioOrderAudit`` table (design spec §3 "저장 모델":
        "감사는 기존 PortfolioOrderAudit 재사용"). ``order_type`` is always
        recorded as ``"LIMIT"`` (the only leg type Phase 4 ever sends);
        ``price`` holds the leg's *limit* price, never the STOP trigger
        price (put ``trigger_price`` in ``detail`` instead) — this matches
        how ``est_amount_krw`` itself is computed (limit_price × quantity,
        not trigger_price × quantity, design spec §3 "한도 산입"). Event
        names use a ``cond_`` prefix, not the design spec's literal
        ``conditional_`` prefix — several of the spec's own event names
        (e.g. ``conditional_registration_unknown``, 33 chars) do not fit
        this shared table's existing ``event = String(24)`` column, which
        this Phase 4 change must not widen (additive-only contract on an
        existing, already-reviewed Phase 3 table). ``toss_order_id`` —
        despite the column name, inherited unchanged from Phase 3 — holds
        the Toss ``conditionalOrderId`` for every ``cond_*`` event once
        known; there is no separate column for it."""
        return PortfolioOrderAudit(
            account_id=account_id,
            proposal_uuid=proposal_uuid,
            symbol=symbol,
            side=side,
            order_type="LIMIT",
            price=limit_price,
            quantity=quantity,
            currency=currency,
            est_amount_krw=est_amount_krw,
            mode=mode,
            event=event,
            toss_order_id=toss_conditional_order_id,
            error_code=error_code,
            detail=json.dumps(detail) if detail is not None else None,
            created_at=created_at,
        )

    def create_conditional_order_proposal_with_audit(
        self,
        *,
        account_id: int,
        proposal_uuid: str,
        symbol: str,
        storage_symbol: str,
        market: str,
        currency: str,
        side: str,
        trigger_price: float,
        limit_price: float,
        quantity: float,
        est_amount_krw: float,
        expire_date: date,
        client_order_id: str,
        created_at: datetime,
        expires_at: datetime,
        max_pending_proposals: int,
    ) -> PortfolioConditionalOrderProposal:
        """Atomically insert the conditional-order proposal row and its
        ``cond_proposed`` audit event, with the pending-proposal-count cap
        check and insert in the same write transaction (mirrors Phase 3's
        ``create_order_proposal_with_audit`` TOCTOU fix — design spec is
        silent on a conditional-specific pending cap, so this inherits
        Phase 3's guardrail count of 10, per the implementation brief's
        "스펙이 침묵하는 세부는 Phase 3 구현의 기존 패턴을 따르세요")."""
        with self.portfolio_write_session() as session:
            pending_count = int(
                session.execute(
                    select(func.count(PortfolioConditionalOrderProposal.id)).where(
                        and_(
                            PortfolioConditionalOrderProposal.account_id == account_id,
                            PortfolioConditionalOrderProposal.status == "pending",
                            PortfolioConditionalOrderProposal.expires_at > created_at,
                        )
                    )
                ).scalar_one()
            )
            if pending_count >= max_pending_proposals:
                raise PendingConditionalProposalCapExceededError(
                    f"account_id={account_id} already has {pending_count} pending conditional "
                    f"proposals (max {max_pending_proposals})"
                )

            row = PortfolioConditionalOrderProposal(
                account_id=account_id,
                proposal_uuid=proposal_uuid,
                symbol=symbol,
                storage_symbol=storage_symbol,
                market=market,
                currency=currency,
                side=side,
                trigger_price=trigger_price,
                limit_price=limit_price,
                quantity=quantity,
                est_amount_krw=est_amount_krw,
                expire_date=expire_date,
                client_order_id=client_order_id,
                status="pending",
                created_at=created_at,
                expires_at=expires_at,
            )
            session.add(row)
            session.flush()
            session.add(
                self._conditional_audit_row(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=symbol,
                    side=side,
                    limit_price=limit_price,
                    quantity=quantity,
                    currency=currency,
                    est_amount_krw=est_amount_krw,
                    mode=None,
                    event="cond_proposed",
                    toss_conditional_order_id=None,
                    created_at=created_at,
                    detail={"trigger_price": trigger_price, "expire_date": expire_date.isoformat()},
                )
            )
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row

    def _materialize_conditional_expiry_in_session(
        self, *, session: Any, row: PortfolioConditionalOrderProposal, now: datetime
    ) -> None:
        """Same lazy-TTL-materialization pattern as Phase 3's
        ``_materialize_expiry_in_session`` — only the still-``pending`` TTL
        (``expires_at``, 10 minutes) is handled here; the Toss-side
        ``expire_date`` (up to 7 days) is observed via sync, not a local
        timer."""
        if row.status == "pending" and row.expires_at <= now:
            row.status = "expired"
            row.updated_at = datetime.now()
            session.add(
                self._conditional_audit_row(
                    account_id=row.account_id,
                    proposal_uuid=row.proposal_uuid,
                    symbol=row.symbol,
                    side=row.side,
                    limit_price=row.limit_price,
                    quantity=row.quantity,
                    currency=row.currency,
                    est_amount_krw=row.est_amount_krw,
                    mode=None,
                    event="cond_expired",
                    toss_conditional_order_id=None,
                    created_at=now,
                )
            )
            session.flush()

    def _materialize_conditional_expiry_standalone(
        self, *, proposal_uuid: str, account_id: Optional[int], now: datetime
    ) -> None:
        with self.portfolio_write_session() as session:
            conditions = [PortfolioConditionalOrderProposal.proposal_uuid == proposal_uuid]
            if account_id is not None:
                conditions.append(PortfolioConditionalOrderProposal.account_id == account_id)
            row = session.execute(
                select(PortfolioConditionalOrderProposal).where(and_(*conditions)).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return
            self._materialize_conditional_expiry_in_session(session=session, row=row, now=now)

    def get_conditional_order_proposal(
        self,
        proposal_uuid: str,
        *,
        account_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Optional[PortfolioConditionalOrderProposal]:
        """Fetch one conditional-order proposal, lazily materializing pending
        TTL expiry first when ``now`` is given."""
        if now is not None:
            self._materialize_conditional_expiry_standalone(proposal_uuid=proposal_uuid, account_id=account_id, now=now)
        with self.db.get_session() as session:
            conditions = [PortfolioConditionalOrderProposal.proposal_uuid == proposal_uuid]
            if account_id is not None:
                conditions.append(PortfolioConditionalOrderProposal.account_id == account_id)
            row = session.execute(
                select(PortfolioConditionalOrderProposal).where(and_(*conditions)).limit(1)
            ).scalar_one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    def list_conditional_order_proposals(
        self,
        account_id: int,
        *,
        status: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> List[PortfolioConditionalOrderProposal]:
        """List conditional-order proposals for one account, optionally
        filtered by status; lazily materializes overdue-``pending`` TTL
        expiry first when ``now`` is given (mirrors
        ``list_order_proposals``)."""
        if now is not None:
            with self.db.get_session() as session:
                overdue_uuids = session.execute(
                    select(PortfolioConditionalOrderProposal.proposal_uuid).where(
                        and_(
                            PortfolioConditionalOrderProposal.account_id == account_id,
                            PortfolioConditionalOrderProposal.status == "pending",
                            PortfolioConditionalOrderProposal.expires_at <= now,
                        )
                    )
                ).scalars().all()
            for proposal_uuid in overdue_uuids:
                self._materialize_conditional_expiry_standalone(proposal_uuid=proposal_uuid, account_id=account_id, now=now)

        with self.db.get_session() as session:
            conditions = [PortfolioConditionalOrderProposal.account_id == account_id]
            if status is not None:
                conditions.append(PortfolioConditionalOrderProposal.status == status)
            rows = session.execute(
                select(PortfolioConditionalOrderProposal)
                .where(and_(*conditions))
                .order_by(PortfolioConditionalOrderProposal.id.desc())
            ).scalars().all()
            rows = list(rows)
            for row in rows:
                session.expunge(row)
            return rows

    def find_conditional_order_ids_owned_by_others(
        self, toss_conditional_order_ids: Iterable[str], *, exclude_proposal_uuid: str
    ) -> Set[str]:
        """Codex 2nd-round review R1-2 (coordinator-confirmed convergence
        contract): return the subset of ``toss_conditional_order_ids`` that
        are already recorded — **regardless of status** — on some *other*
        local proposal row (any account; a remote ``conditionalOrderId`` is
        Toss's own identifier, not scoped to this system's account rows).
        Callers must drop these from reconcile's candidate set before
        adopting a match — an ID already owned elsewhere can never
        legitimately be re-adopted by this proposal, ambiguous-looking
        uniqueness in the remote search notwithstanding. This is an
        application-layer check; ``DatabaseManager._ensure_conditional_order_toss_id_unique_index``
        is the DB-level backstop for the race window between this read and
        the eventual write."""
        ids = {str(x) for x in toss_conditional_order_ids if x}
        if not ids:
            return set()
        with self.db.get_session() as session:
            rows = session.execute(
                select(PortfolioConditionalOrderProposal.toss_conditional_order_id).where(
                    and_(
                        PortfolioConditionalOrderProposal.toss_conditional_order_id.in_(ids),
                        PortfolioConditionalOrderProposal.proposal_uuid != exclude_proposal_uuid,
                    )
                )
            ).scalars().all()
            return {str(r) for r in rows if r}

    def list_other_unresolved_conditional_proposals(
        self, *, account_id: int, exclude_proposal_uuid: str
    ) -> List[PortfolioConditionalOrderProposal]:
        """Codex 2nd-round review R1-3 (coordinator-confirmed convergence
        contract): list every *other* conditional-order proposal on this
        account still ``approving``/``registration_unknown`` (i.e. not yet
        known to have registered or definitively failed). Used by
        ``PortfolioConditionalOrderService.reconcile_proposal`` to detect a
        same-attribute "local contender" — if another unresolved proposal
        shares this one's exact symbol/side/trigger/limit/quantity/
        expire_date, a single remote candidate cannot be safely attributed
        to either one by attributes alone, so reconcile must not adopt it
        even when the remote search itself found exactly one match."""
        with self.db.get_session() as session:
            rows = session.execute(
                select(PortfolioConditionalOrderProposal).where(
                    and_(
                        PortfolioConditionalOrderProposal.account_id == account_id,
                        PortfolioConditionalOrderProposal.proposal_uuid != exclude_proposal_uuid,
                        PortfolioConditionalOrderProposal.status.in_(("approving", "registration_unknown")),
                    )
                )
            ).scalars().all()
            rows = list(rows)
            for row in rows:
                session.expunge(row)
            return rows

    def adopt_reconciled_order_if_uncontended(
        self,
        *,
        proposal_uuid: str,
        account_id: int,
        now: datetime,
        conditional_order_id: str,
        toss_status: str,
        to_status: str,
    ) -> ConditionalClaimOutcome:
        """Codex 3rd-round review R1c (coordinator-confirmed convergence
        contract — "경쟁자 조회~채택이 원자적이지 않다"): re-verify ownership
        (``find_conditional_order_ids_owned_by_others``) and local-contender
        (``list_other_unresolved_conditional_proposals``) exclusivity, and
        adopt the candidate if uncontended, **all inside one
        ``BEGIN IMMEDIATE`` write transaction** — closing the TOCTOU gap
        between ``PortfolioConditionalOrderService.reconcile_proposal``'s
        earlier advisory pre-checks (ordinary read sessions, no write lock)
        and the eventual status/ID write. SQLite's ``BEGIN IMMEDIATE``
        acquires the RESERVED lock for the whole transaction, so as long as
        every writer to this table goes through
        ``PortfolioRepository.portfolio_write_session`` (they all do), no
        other proposal can enter ``approving``/acquire this exact
        ``conditional_order_id`` between this method's re-check and its own
        write — the race the advisory pre-checks alone could not close.

        Callers MUST have already done all network I/O (the Toss
        OPEN/CLOSED listing search) *before* calling this — this method
        touches only the local DB, never Toss, so no network I/O ever runs
        inside the write-locked transaction.

        Returns a ``ConditionalClaimOutcome`` — see that class's docstring
        for the four possible ``outcome`` values."""
        with self.portfolio_write_session() as session:
            row = session.execute(
                select(PortfolioConditionalOrderProposal).where(
                    and_(
                        PortfolioConditionalOrderProposal.proposal_uuid == proposal_uuid,
                        PortfolioConditionalOrderProposal.account_id == account_id,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return ConditionalClaimOutcome("not_found")

            if row.status != "registration_unknown":
                # Something else (a concurrent reconcile call, the original
                # approve POST's own outcome resolution, or force-resolve)
                # already moved this row on since the caller's entry gate —
                # idempotent-retry return, never overwrite it.
                session.refresh(row)
                session.expunge(row)
                return ConditionalClaimOutcome("not_reconcilable", proposal=row)

            owned_by_other_proposal_count = session.execute(
                select(func.count()).select_from(PortfolioConditionalOrderProposal).where(
                    and_(
                        PortfolioConditionalOrderProposal.toss_conditional_order_id == conditional_order_id,
                        PortfolioConditionalOrderProposal.proposal_uuid != proposal_uuid,
                    )
                )
            ).scalar_one()

            contenders = session.execute(
                select(PortfolioConditionalOrderProposal).where(
                    and_(
                        PortfolioConditionalOrderProposal.account_id == account_id,
                        PortfolioConditionalOrderProposal.proposal_uuid != proposal_uuid,
                        PortfolioConditionalOrderProposal.status.in_(("approving", "registration_unknown")),
                    )
                )
            ).scalars().all()
            local_contender_count = sum(
                1 for other in contenders if _conditional_order_attributes_match(other, row)
            )

            if owned_by_other_proposal_count > 0 or local_contender_count > 0:
                row.updated_at = datetime.now()
                session.add(
                    self._conditional_audit_row(
                        account_id=account_id,
                        proposal_uuid=proposal_uuid,
                        symbol=row.symbol,
                        side=row.side,
                        limit_price=row.limit_price,
                        quantity=row.quantity,
                        currency=row.currency,
                        est_amount_krw=row.est_amount_krw,
                        mode="live",
                        event="cond_reconciled",
                        toss_conditional_order_id=None,
                        created_at=now,
                        error_code="local-contender" if local_contender_count > 0 else "owned-by-other-proposal",
                        detail={
                            "candidate_conditional_order_id": conditional_order_id,
                            "local_contender_count": local_contender_count,
                            "owned_by_other_proposal_count": int(owned_by_other_proposal_count),
                            "note": (
                                "re-verified under the write lock immediately before adoption "
                                "(Codex 3rd-round review R1c) -- a contender/owner appeared between "
                                "the caller's advisory pre-check and this atomic recheck; adoption "
                                "cancelled, staying registration_unknown"
                            ),
                        },
                    )
                )
                session.flush()
                session.refresh(row)
                session.expunge(row)
                return ConditionalClaimOutcome(
                    "contended",
                    proposal=row,
                    detail={
                        "local_contender_count": local_contender_count,
                        "owned_by_other_proposal_count": int(owned_by_other_proposal_count),
                    },
                )

            row.status = to_status
            row.updated_at = datetime.now()
            row.toss_conditional_order_id = conditional_order_id
            row.toss_status = toss_status
            row.last_synced_at = now
            if to_status in ("approved", "paused"):
                row.approved_at = now
            session.add(
                self._conditional_audit_row(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=row.symbol,
                    side=row.side,
                    limit_price=row.limit_price,
                    quantity=row.quantity,
                    currency=row.currency,
                    est_amount_krw=row.est_amount_krw,
                    mode="live",
                    event="cond_reconciled",
                    toss_conditional_order_id=conditional_order_id,
                    created_at=now,
                    detail={"via": "reconcile", "matched_toss_status": toss_status, "candidate_count": 1},
                )
            )
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return ConditionalClaimOutcome("adopted", proposal=row)

    def claim_conditional_proposal_for_approval(
        self,
        *,
        proposal_uuid: str,
        account_id: int,
        now: datetime,
        est_amount_krw: float,
        high_value_threshold_krw: float,
        per_order_cap_krw: float,
        daily_cap_krw: float,
        eps: float = 1e-6,
    ) -> ConditionalClaimOutcome:
        """Atomic ``pending -> approving`` claim — the Phase 4 analog of
        Phase 3's ``claim_proposal_for_execution`` (design spec §3 "로컬
        상태기계": "원자적 claim: Phase 3 execute와 동일 패턴"). Inside one
        write transaction: materialize TTL expiry if due, then — only if
        still ``pending`` — re-validate the high-value hard reject, the
        per-order cap, and the *shared* daily cap (via
        ``_sum_reserved_and_live_amount_in_session``, which folds in both
        Phase 3 and Phase 4 reservations) before inserting the
        ``approving`` reservation. A failing cap check transitions the row
        straight to ``registration_failed`` in the same transaction — it
        never reached Toss, so ``registration_failed`` (not
        ``registration_unknown``) is correct here.

        This is only ever called on the live path — dry-run never claims
        (mirrors Phase 3: nothing to reserve against since Toss is never
        contacted)."""
        with self.portfolio_write_session() as session:
            row = session.execute(
                select(PortfolioConditionalOrderProposal).where(
                    and_(
                        PortfolioConditionalOrderProposal.proposal_uuid == proposal_uuid,
                        PortfolioConditionalOrderProposal.account_id == account_id,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return ConditionalClaimOutcome("not_found")

            self._materialize_conditional_expiry_in_session(session=session, row=row, now=now)

            terminal_statuses = {
                "canceled",
                "expired",
                "dry_run_approved",
                "registration_failed",
                "triggered_completed",
                "toss_expired",
                "toss_canceled",
            }
            if row.status in terminal_statuses:
                session.refresh(row)
                session.expunge(row)
                return ConditionalClaimOutcome("already_terminal", proposal=row)
            if row.status in ("approving", "registration_unknown"):
                session.refresh(row)
                session.expunge(row)
                return ConditionalClaimOutcome("in_progress", proposal=row)
            if row.status in ("approved", "paused"):
                session.refresh(row)
                session.expunge(row)
                return ConditionalClaimOutcome("already_approved", proposal=row)
            if row.status != "pending":
                # Defensive — every known status is covered above; a future
                # unlisted status must not silently fall through to a claim.
                session.refresh(row)
                session.expunge(row)
                return ConditionalClaimOutcome("not_executable", proposal=row)

            def _reject(*, limit_type: str, error_code: str, reason: str) -> ConditionalClaimOutcome:
                row.status = "registration_failed"
                row.updated_at = datetime.now()
                session.add(
                    self._conditional_audit_row(
                        account_id=account_id,
                        proposal_uuid=proposal_uuid,
                        symbol=row.symbol,
                        side=row.side,
                        limit_price=row.limit_price,
                        quantity=row.quantity,
                        currency=row.currency,
                        est_amount_krw=est_amount_krw,
                        mode="live",
                        event="cond_rejected",
                        toss_conditional_order_id=None,
                        created_at=now,
                        error_code=error_code,
                        detail={"reason": reason},
                    )
                )
                session.flush()
                session.refresh(row)
                session.expunge(row)
                return ConditionalClaimOutcome("rejected", proposal=row, reason=reason, limit_type=limit_type)

            if est_amount_krw >= high_value_threshold_krw:
                return _reject(
                    limit_type="high_value",
                    error_code="high-value-hard-reject",
                    reason=(
                        f"Estimated order amount {est_amount_krw:,.0f} KRW is at or above the "
                        f"{high_value_threshold_krw:,.0f} KRW hard-reject threshold"
                    ),
                )
            if est_amount_krw > per_order_cap_krw + eps:
                return _reject(
                    limit_type="per_order",
                    error_code="limit-exceeded",
                    reason=(
                        f"Estimated order amount {est_amount_krw:,.0f} KRW exceeds the per-order cap "
                        f"{per_order_cap_krw:,.0f} KRW"
                    ),
                )
            already_reserved = self._sum_reserved_and_live_amount_in_session(
                session, account_id=account_id, kst_date=now.date()
            )
            if already_reserved + est_amount_krw > daily_cap_krw + eps:
                return _reject(
                    limit_type="daily",
                    error_code="limit-exceeded",
                    reason=(
                        f"Estimated order amount {est_amount_krw:,.0f} KRW would push today's reserved+"
                        f"executed total to {already_reserved + est_amount_krw:,.0f} KRW, exceeding the "
                        f"daily cap {daily_cap_krw:,.0f} KRW"
                    ),
                )

            row.status = "approving"
            row.reserved_at = now
            row.est_amount_krw = est_amount_krw
            row.updated_at = datetime.now()
            session.add(
                self._conditional_audit_row(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=row.symbol,
                    side=row.side,
                    limit_price=row.limit_price,
                    quantity=row.quantity,
                    currency=row.currency,
                    est_amount_krw=est_amount_krw,
                    mode="live",
                    event="cond_approving",
                    toss_conditional_order_id=None,
                    created_at=now,
                )
            )
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return ConditionalClaimOutcome("claimed", proposal=row)

    def reconcile_claim_stale_approving(
        self,
        *,
        proposal_uuid: str,
        account_id: int,
        now: datetime,
        stale_after: timedelta,
    ) -> ConditionalClaimOutcome:
        """Reconcile's entry gate (Codex BLOCK review blocker 2 fix — design
        spec §5 "429"/§3 reconcile contract, coordinator-confirmed
        convergence contract): reconcile must never preempt a genuinely
        in-flight ``approve_proposal`` call. A ``registration_unknown`` row
        is always fair game (that is reconcile's whole purpose). An
        ``approving`` row is fair game **only** if its claim
        (``reserved_at``) is older than ``stale_after`` — a normal approve
        call resolves ``approving`` to a terminal-ish outcome within a
        single request, so anything still ``approving`` after
        ``stale_after`` almost certainly means the process that held the
        claim died mid-POST (crash recovery), not that a POST is still
        genuinely in flight.

        The staleness check and the ``approving -> registration_unknown``
        takeover happen atomically in one write transaction — there is no
        window between "checked staleness" and "took over the row" for a
        concurrent approve's own POST-outcome resolution to race against
        (that resolution's own ``from_statuses`` includes
        ``registration_unknown``, so it still converges correctly even if
        this takeover runs first — see
        ``PortfolioConditionalOrderService._resolve_registration_outcome``).

        See ``ConditionalClaimOutcome`` for the possible ``outcome`` values.
        """
        with self.portfolio_write_session() as session:
            row = session.execute(
                select(PortfolioConditionalOrderProposal).where(
                    and_(
                        PortfolioConditionalOrderProposal.proposal_uuid == proposal_uuid,
                        PortfolioConditionalOrderProposal.account_id == account_id,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return ConditionalClaimOutcome("not_found")

            if row.status == "registration_unknown":
                session.refresh(row)
                session.expunge(row)
                return ConditionalClaimOutcome("ready", proposal=row)

            if row.status != "approving":
                session.refresh(row)
                session.expunge(row)
                return ConditionalClaimOutcome("not_reconcilable", proposal=row)

            claimed_at = row.reserved_at
            if claimed_at is None or (now - claimed_at) < stale_after:
                session.refresh(row)
                session.expunge(row)
                return ConditionalClaimOutcome("approval_in_progress", proposal=row)

            row.status = "registration_unknown"
            row.updated_at = datetime.now()
            session.add(
                self._conditional_audit_row(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=row.symbol,
                    side=row.side,
                    limit_price=row.limit_price,
                    quantity=row.quantity,
                    currency=row.currency,
                    est_amount_krw=row.est_amount_krw,
                    mode="live",
                    event="cond_reg_unknown",
                    toss_conditional_order_id=None,
                    created_at=now,
                    error_code="stale-approving-claim",
                    detail={
                        "reason": (
                            f"approving claim older than {stale_after} (reserved_at="
                            f"{claimed_at.isoformat()}); reconcile taking over as crash recovery"
                        )
                    },
                )
            )
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return ConditionalClaimOutcome("ready", proposal=row)

    def transition_conditional_proposal(
        self,
        *,
        proposal_uuid: str,
        account_id: int,
        now: datetime,
        from_statuses: Iterable[str],
        to_status: str,
        event: str,
        mode: Optional[str] = None,
        toss_conditional_order_id: Optional[str] = None,
        approved_at: Optional[datetime] = None,
        toss_status: Optional[str] = None,
        error_code: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        est_amount_krw_override: Optional[float] = None,
    ) -> Optional[PortfolioConditionalOrderProposal]:
        """Atomically materialize pending-TTL expiry if due, then — only if
        the row's resulting status is still one of ``from_statuses`` —
        apply the requested transition and append a matching audit row in
        the same DB transaction (mirrors Phase 3's ``transition_proposal``).
        Used for every non-claim transition: dry-run resolution, POST
        outcome resolution (approved/registration_failed/
        registration_unknown), reconcile, cancel, and sync-driven Toss
        status updates (via ``toss_status``/``to_status`` together).

        Returns the proposal reflecting its *actual* final status, which
        may differ from ``to_status`` (e.g. concurrently resolved already)
        — callers compare ``row.status`` against what they expected, same
        idempotent-retry contract as Phase 3. Returns ``None`` only when no
        such proposal exists at all for this account."""
        with self.portfolio_write_session() as session:
            row = session.execute(
                select(PortfolioConditionalOrderProposal).where(
                    and_(
                        PortfolioConditionalOrderProposal.proposal_uuid == proposal_uuid,
                        PortfolioConditionalOrderProposal.account_id == account_id,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None

            self._materialize_conditional_expiry_in_session(session=session, row=row, now=now)

            if row.status not in set(from_statuses):
                session.refresh(row)
                session.expunge(row)
                return row

            row.status = to_status
            row.updated_at = datetime.now()
            if approved_at is not None:
                row.approved_at = approved_at
            if toss_conditional_order_id is not None:
                row.toss_conditional_order_id = toss_conditional_order_id
            if toss_status is not None:
                row.toss_status = toss_status
                row.last_synced_at = now

            est_amount = est_amount_krw_override if est_amount_krw_override is not None else row.est_amount_krw
            session.add(
                self._conditional_audit_row(
                    account_id=account_id,
                    proposal_uuid=proposal_uuid,
                    symbol=row.symbol,
                    side=row.side,
                    limit_price=row.limit_price,
                    quantity=row.quantity,
                    currency=row.currency,
                    est_amount_krw=est_amount,
                    mode=mode,
                    event=event,
                    toss_conditional_order_id=(
                        toss_conditional_order_id if toss_conditional_order_id is not None else row.toss_conditional_order_id
                    ),
                    created_at=now,
                    error_code=error_code,
                    detail=detail,
                )
            )
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row

    def append_standalone_conditional_audit(
        self,
        *,
        account_id: int,
        proposal_uuid: str,
        symbol: str,
        side: str,
        limit_price: Optional[float],
        quantity: float,
        currency: str,
        est_amount_krw: float,
        mode: Optional[str],
        event: str,
        toss_conditional_order_id: Optional[str],
        created_at: datetime,
        error_code: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> PortfolioOrderAudit:
        """Append one audit row without touching
        ``PortfolioConditionalOrderProposal.status`` — used by reconcile's
        "no attribute match found" case (status stays ``registration_unknown``,
        only the reconcile *attempt* is logged) and by a rejected
        Toss-side cancel (the proposal stays ``approved``/``paused``; only
        the cancel attempt failed). Mirrors ``append_standalone_order_audit``."""
        with self.portfolio_write_session() as session:
            audit = self._conditional_audit_row(
                account_id=account_id,
                proposal_uuid=proposal_uuid,
                symbol=symbol,
                side=side,
                limit_price=limit_price,
                quantity=quantity,
                currency=currency,
                est_amount_krw=est_amount_krw,
                mode=mode,
                event=event,
                toss_conditional_order_id=toss_conditional_order_id,
                created_at=created_at,
                error_code=error_code,
                detail=detail,
            )
            session.add(audit)
            session.flush()
            session.refresh(audit)
            session.expunge(audit)
            return audit

# -*- coding: utf-8 -*-
"""Portfolio repository.

Provides DB access helpers for portfolio account/events/snapshot tables.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError

from src.storage import (
    DatabaseManager,
    PortfolioAccount,
    PortfolioBrokerLink,
    PortfolioCashLedger,
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
        return float(total or 0.0)

    def sum_daily_reserved_and_executed_amount_krw(self, account_id: int, *, kst_date: date) -> float:
        """Read-only (non-atomic) view of the same v3 daily-cap sum, for
        best-effort pre-checks (``create_proposal``'s early friendly
        rejection — the authoritative check is
        ``claim_proposal_for_execution``'s in-transaction version above) and
        for status/report display."""
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
        proposal), oldest first — used for order-status/history responses."""
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

# -*- coding: utf-8 -*-
"""Toss Invest portfolio broker-link sync service (Phase 2 hybrid sync).

Semantics (source of truth:
docs/superpowers/specs/2026-07-17-toss-portfolio-sync-design.md, revised
2026-07-17 after an independent code review):

- Link: snapshot current Toss holdings into one synthetic "opening" trade per
  symbol (buy, quantity=held quantity, price=average purchase price), then
  create the portfolio account (``market='kr'``, ``base_currency='KRW'``) and
  link it to the Toss account — account creation, every opening trade, and the
  link row are one atomic DB transaction (design spec §3 link atomicity).
  Positions stay a derived replay cache (event-sourcing model) — this never
  writes to the position cache directly.
- Snapshot boundary: ``T0`` (right before the holdings call) and ``T1`` (right
  after the holdings response is received) are recorded; ``snapshot_boundary_at
  = T1`` is stored permanently on the link row and never moves again.
  Incremental sync only ever considers orders filled strictly after this
  boundary — it is the only thing preventing the opening snapshot and the
  order-sync replay from double-counting the same position (there is no atomic
  cutover between "snapshot taken" and "order sync begins", so a timestamp
  boundary is load-bearing, not a convenience filter).
- Sync: pull ``CLOSED`` Toss orders filled strictly after the link's
  ``snapshot_boundary_at``, append them as trades (idempotent via
  ``trade_uid``), advance ``last_synced_at`` (a query-range *optimization*
  cursor, not a dedup boundary — every sync overlap-rescans and relies on
  ``trade_uid`` uniqueness for dedup), and reconcile the replayed ledger
  positions against Toss's current holdings. A failed order (oversell,
  missing/invalid ``averageFilledPrice``, or a malformed order) is reported in
  the sync response's ``failed[]`` and holds the cursor back to just before its
  fill time so the next sync retries it instead of silently skipping it
  forever. Reconciliation (``drift[]``) only detects and reports quantity
  mismatches — it never auto-corrects the ledger.
- Unlink/relink: unlink deactivates the link row (``active=False``) instead of
  deleting it, preserving ``snapshot_boundary_at``/``last_synced_at``. Relinking
  the same Toss account reactivates that row and resumes from its preserved
  cursor — no new opening trades are created, and orders filled during the
  unlinked gap are recovered by the next sync's overlap re-scan.
- Read-only: this module only calls ``GET /api/v1/accounts``, ``/holdings``,
  and ``/orders``. It never calls order create/modify/cancel endpoints.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from data_provider.base import DataFetchError
from data_provider.realtime_types import safe_float
from data_provider.toss_fetcher import TossFetcher
from src.data.stock_index_loader import resolve_index_stock_code
from src.repositories.portfolio_repo import (
    DuplicateBrokerLinkError,
    DuplicateTradeDedupHashError,
    DuplicateTradeUidError,
    PortfolioRepository,
)
from src.services.portfolio_service import (
    PortfolioConflictError,
    PortfolioOversellError,
    PortfolioService,
)

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_DRIFT_EPS = 1e-9
_PROVIDER_TOSS = "toss"
_ACCOUNT_TYPE_BROKERAGE = "BROKERAGE"
_OVERLAP_RESCAN_DAYS = 3
_CURSOR_HOLDBACK = timedelta(seconds=1)


class TossNotConfiguredError(Exception):
    """Raised when TOSS_CLIENT_ID/TOSS_CLIENT_SECRET are not configured."""


class TossUpstreamError(Exception):
    """Raised when a Toss OpenAPI call fails (403 IP not allow-listed, network,
    HTTP error, rate limit exhausted, ...)."""


class BrokerLinkNotFoundError(Exception):
    """Raised when no active broker link exists for the given portfolio account id."""


class BrokerLinkConflictError(Exception):
    """Raised when the target Toss account already has an active broker link."""


class AmbiguousBrokerAccountError(Exception):
    """Raised when account_seq is not given and more than one Toss account is
    available to link — the caller must disambiguate."""

    def __init__(self, accounts: List[Dict[str, Any]]):
        self.accounts = accounts
        super().__init__(
            f"Multiple Toss accounts available ({len(accounts)}); pass account_seq to disambiguate"
        )


# ----------------------------------------------------------------------
# In-process per-account serialization for sync. Cursor writes are also
# monotonic at the DB layer (PortfolioRepository.update_broker_link_sync), so
# this lock is defense-in-depth against two concurrent sync calls for the same
# account interleaving their order-fetch/import/cursor-advance steps within
# one process (design spec §3 concurrency decision) — it does not protect
# against multi-process deployments, which is why the DB-level monotonic
# check is the real backstop.
# ----------------------------------------------------------------------
_account_locks: Dict[int, threading.Lock] = {}
_account_locks_guard = threading.Lock()


def _get_account_lock(account_id: int) -> threading.Lock:
    with _account_locks_guard:
        lock = _account_locks.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _account_locks[account_id] = lock
        return lock


def _now_kst_naive() -> datetime:
    """Current KST wall-clock time as a naive datetime.

    The broker-link cursor/boundary are compared directly against Toss
    ``execution.filledAt`` — see ``PortfolioBrokerLink``'s docstring in
    ``src/storage.py`` for why this module intentionally does not follow the
    codebase's usual UTC-naive timestamp convention.
    """
    return datetime.now(timezone.utc).astimezone(_KST).replace(tzinfo=None)


def _parse_kst_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Parse a Toss timestamp string into a naive KST wall-clock datetime.

    Toss's ``execution.filledAt`` is an ISO 8601 string carrying an explicit
    ``+09:00`` offset (confirmed against the live API) — this converts
    tz-aware strings to KST and drops the offset. A bare (offset-less) ISO
    string is assumed already KST, for defensiveness. Returns ``None`` on any
    parse failure so callers can skip the record instead of aborting the whole
    sync.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(_KST).replace(tzinfo=None)
    return parsed


def _is_brokerage_account(item: Dict[str, Any]) -> bool:
    """Return True unless ``accountType`` is present and explicitly non-BROKERAGE.

    Toss's docs state ``GET /api/v1/accounts`` only ever exposes BROKERAGE
    accounts, so this is a defensive check (future account types), not a load-
    bearing filter — missing/blank ``accountType`` fails open toward inclusion
    rather than silently hiding a real account over a field-name surprise.
    """
    account_type = item.get("accountType")
    if account_type is None:
        return True
    return str(account_type).strip().upper() == _ACCOUNT_TYPE_BROKERAGE


def _resolve_symbol_and_market(
    raw_symbol: str,
    *,
    market_country_hint: Optional[str] = None,
) -> Tuple[str, str, bool]:
    """Map a raw Toss symbol to ``(stored_symbol, market, resolved)``.

    KR: Toss returns bare 6-digit codes with no exchange suffix; resolve the
    ``.KS``/``.KQ`` suffix via the generated stock index (design spec §5 KR
    symbol mapping edge case). Falls back to ``.KS`` when unresolved, with
    ``resolved=False`` so the caller can flag the trade note for the
    reconciliation pass to catch. US: ticker as-is.
    """
    code = (raw_symbol or "").strip().upper()
    hint = (market_country_hint or "").strip().upper()
    looks_kr_bare = code.isdigit() and len(code) == 6
    is_kr = hint == "KR" or (not hint and looks_kr_bare)
    if is_kr:
        resolved = resolve_index_stock_code(code)
        if resolved:
            return resolved, "kr", True
        return f"{code}.KS", "kr", False
    return code, "us", True


def _default_currency_for_market(market: str) -> str:
    return "KRW" if market == "kr" else "USD"


class PortfolioBrokerSyncService:
    """Business logic for Toss broker-link account creation, incremental order
    sync, and drift reconciliation."""

    def __init__(
        self,
        *,
        portfolio_service: Optional[PortfolioService] = None,
        repo: Optional[PortfolioRepository] = None,
        fetcher: Optional[TossFetcher] = None,
    ):
        self.portfolio_service = portfolio_service or PortfolioService()
        self.repo = repo or PortfolioRepository()
        self._fetcher = fetcher

    def _ensure_fetcher(self) -> TossFetcher:
        """Return a usable TossFetcher, or raise TossNotConfiguredError.

        An injected fetcher (tests) always bypasses the credential check —
        production callers get a real ``TossFetcher()`` gated on
        ``has_configured_credentials()``.
        """
        if self._fetcher is not None:
            return self._fetcher
        if not TossFetcher.has_configured_credentials():
            raise TossNotConfiguredError(
                "TOSS_CLIENT_ID/TOSS_CLIENT_SECRET not configured; broker-link sync is unavailable"
            )
        return TossFetcher()

    # ------------------------------------------------------------------
    # Link
    # ------------------------------------------------------------------
    def link_toss_account(
        self,
        *,
        name: Optional[str] = None,
        account_seq: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Link a Toss brokerage account as a portfolio account.

        The match key for "is this Toss account already linked?" is the
        resolved Toss ``accountSeq`` (``provider='toss'`` +
        ``external_account_seq``), not a caller-supplied portfolio
        ``account_id`` — the API intentionally does not accept an
        ``account_id`` parameter (design spec §3: reusing a caller-chosen
        ``account_id`` let a KRW opening snapshot land on a non-KR/KRW
        account, breaking the KR KRW P&L precision contract).

        - No existing link row for this Toss account -> brand-new account +
          opening trades + link row, created atomically
          (``PortfolioRepository.create_broker_link_with_opening_trades``).
        - An *active* link row already exists for this Toss account -> 409
          (``BrokerLinkConflictError``).
        - An *inactive* link row exists (i.e. a prior unlink) and its
          portfolio account is still live -> reactivate that row in place and
          resume from its preserved cursor; no new opening trades (design
          spec §3/§5 unlink/relink).
        """
        fetcher = self._ensure_fetcher()

        try:
            accounts = fetcher.get_accounts()
        except DataFetchError as exc:
            raise TossUpstreamError(str(exc)) from exc

        brokerage_accounts = [a for a in accounts if _is_brokerage_account(a)]
        if not brokerage_accounts:
            raise ValueError("No Toss BROKERAGE accounts available to link")

        selected: Optional[Dict[str, Any]] = None
        if account_seq is not None:
            seq_norm = str(account_seq).strip()
            for candidate in brokerage_accounts:
                if str(candidate.get("accountSeq")) == seq_norm:
                    selected = candidate
                    break
            if selected is None:
                raise ValueError(f"account_seq={account_seq} not found among Toss brokerage accounts")
        elif len(brokerage_accounts) == 1:
            selected = brokerage_accounts[0]
        else:
            raise AmbiguousBrokerAccountError(brokerage_accounts)

        selected_seq = str(selected.get("accountSeq"))
        selected_no = selected.get("accountNo")

        existing_link = self.repo.get_broker_link_by_external(
            provider=_PROVIDER_TOSS, external_account_seq=selected_seq
        )
        if existing_link is not None:
            if existing_link.active:
                raise BrokerLinkConflictError(
                    f"Toss account_seq={selected_seq} already has an active broker link "
                    f"(account_id={existing_link.account_id})"
                )
            existing_account = self.repo.get_account(int(existing_link.account_id), include_inactive=True)
            if existing_account is not None and bool(existing_account.is_active):
                reactivated = self.repo.reactivate_broker_link(link_id=int(existing_link.id))
                if reactivated is None:
                    # existing_link was just read successfully and this module
                    # never deletes broker-link rows (only deactivates them),
                    # so this should be unreachable — but fail loudly instead
                    # of silently falling through to a brand-new account if
                    # it somehow happens.
                    raise TossUpstreamError(
                        f"broker link id={existing_link.id} vanished during reactivation"
                    )
                return {
                    "account_id": int(reactivated.account_id),
                    "account_name": existing_account.name,
                    "provider": _PROVIDER_TOSS,
                    "external_account_seq": selected_seq,
                    "external_account_no": str(selected_no) if selected_no is not None else None,
                    "snapshot_at": reactivated.snapshot_boundary_at.isoformat(),
                    "imported": 0,
                    "skipped_duplicates": 0,
                    "reactivated": True,
                }
            # The linked portfolio account was hard-deleted (DELETE /accounts)
            # since this link was last active — the stale link row is not
            # reusable (design spec §3: "완전히 새로 시작하려면 포트폴리오
            # 계좌 자체를 삭제한 뒤 새 링크를 만든다"). Fall through to the
            # brand-new creation path below; the stale row is left in place
            # for audit purposes but is no longer resolvable to a live account.

        # ------------------------------------------------------------------
        # Brand-new account: snapshot boundary T0 (before holdings call) / T1
        # (after holdings response) bracket the holdings request itself.
        # ------------------------------------------------------------------
        try:
            holdings = fetcher.get_holdings(selected_seq)
        except DataFetchError as exc:
            raise TossUpstreamError(str(exc)) from exc
        snapshot_boundary_at = _now_kst_naive()  # T1

        account_name = (name or "").strip() or f"Toss {selected_no or selected_seq}"

        opening_trades: List[Dict[str, Any]] = []
        for item in holdings.get("items") or []:
            quantity = safe_float(item.get("quantity"))
            if quantity is None or quantity <= 0:
                continue
            avg_price = safe_float(item.get("averagePurchasePrice"))
            if avg_price is None or avg_price <= 0:
                continue

            raw_symbol = str(item.get("symbol") or "")
            symbol, market, resolved = _resolve_symbol_and_market(
                raw_symbol, market_country_hint=item.get("marketCountry")
            )
            currency = str(item.get("currency") or "").strip().upper() or _default_currency_for_market(market)
            note = f"toss_opening_snapshot:{snapshot_boundary_at.isoformat()}"
            if not resolved:
                note += f";toss_kr_symbol_unresolved:{raw_symbol}"

            opening_trades.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "currency": currency,
                    "trade_date": snapshot_boundary_at.date(),
                    "quantity": quantity,
                    "price": avg_price,
                    "trade_uid": f"toss:opening:{selected_seq}:{raw_symbol}",
                    "note": note,
                }
            )

        try:
            account, link, imported = self.repo.create_broker_link_with_opening_trades(
                account_name=account_name,
                broker=_PROVIDER_TOSS,
                market="kr",
                base_currency="KRW",
                owner_id=owner_id,
                provider=_PROVIDER_TOSS,
                external_account_seq=selected_seq,
                external_account_no=str(selected_no) if selected_no is not None else None,
                linked_at=snapshot_boundary_at,
                snapshot_boundary_at=snapshot_boundary_at,
                last_synced_at=snapshot_boundary_at,
                opening_trades=opening_trades,
            )
        except (DuplicateTradeUidError, DuplicateTradeDedupHashError) as exc:
            # Two holdings rows resolved to the same trade_uid (duplicate
            # symbol in the Toss response) — the whole link rolls back
            # (design spec §3 link atomicity); surface it as an upstream data
            # problem rather than a generic 500.
            raise TossUpstreamError(f"Toss holdings response has a duplicate symbol: {exc}") from exc
        except DuplicateBrokerLinkError as exc:
            raise BrokerLinkConflictError(str(exc)) from exc

        return {
            "account_id": int(account.id),
            "account_name": account.name,
            "provider": _PROVIDER_TOSS,
            "external_account_seq": selected_seq,
            "external_account_no": str(selected_no) if selected_no is not None else None,
            "snapshot_at": snapshot_boundary_at.isoformat(),
            "imported": imported,
            "skipped_duplicates": 0,
            "reactivated": False,
        }

    # ------------------------------------------------------------------
    # List / unlink
    # ------------------------------------------------------------------
    def list_links(self) -> List[Dict[str, Any]]:
        """List every *active* broker link. Works regardless of Toss credential
        state (design spec §5: "목록/해제는 동작"). Inactive (unlinked) rows
        are internal cursor-preservation state, not user-visible links."""
        rows = self.repo.list_broker_links(include_inactive=False)
        result: List[Dict[str, Any]] = []
        for row in rows:
            account = self.repo.get_account(int(row.account_id), include_inactive=True)
            result.append(
                {
                    "account_id": int(row.account_id),
                    "account_name": account.name if account is not None else None,
                    "provider": row.provider,
                    "external_account_seq": row.external_account_seq,
                    "external_account_no": row.external_account_no,
                    "linked_at": row.linked_at.isoformat() if row.linked_at else None,
                    "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
                    "last_reconciled_at": row.last_reconciled_at.isoformat() if row.last_reconciled_at else None,
                }
            )
        return result

    def unlink(self, account_id: int) -> bool:
        """Deactivate the broker link (``active=False``) — the portfolio
        account, its trade ledger, and the link's cursor are all preserved
        (design spec §3 unlink/relink). Returns False if no link row exists
        for this account at all."""
        return self.repo.deactivate_broker_link(account_id)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
    def sync_linked_account(self, account_id: int) -> Dict[str, Any]:
        """Import new filled orders since the link cursor and reconcile drift.

        Cursor semantics (design spec §3, revised 2026-07-17):
        - ``snapshot_boundary_at`` is the immutable dedup boundary: only orders
          with ``execution.filledAt > snapshot_boundary_at`` are ever eligible.
        - ``last_synced_at`` is a query-range optimization only. Every sync
          re-fetches from ``max(snapshot_boundary_at, last_synced_at - 3
          days).date()`` (overlap re-scan) and relies on the ``trade_uid``
          unique constraint (via ``PortfolioConflictError``) to collapse
          duplicates from the re-scanned window — this is what makes the sync
          resilient to API latency and to orders that share a fill timestamp
          with the previous cursor.
        - A failed order (missing/invalid ``averageFilledPrice``, oversell, or
          a malformed order missing ``orderId``/``side``) is reported in
          ``failed[]`` and is *not* recorded. The new cursor is held back to
          one second before the earliest failed order's fill time so the next
          sync retries it — a persistently failing order stalls the cursor
          there and is re-reported every sync instead of being silently
          skipped forever.
        - The cursor write is monotonic at the DB layer
          (``PortfolioRepository.update_broker_link_sync``): a candidate that
          is not strictly greater than the currently-stored value never
          regresses it.
        """
        fetcher = self._ensure_fetcher()

        lock = _get_account_lock(account_id)
        with lock:
            link = self.repo.get_broker_link_by_account(account_id, active_only=True)
            if link is None:
                raise BrokerLinkNotFoundError(f"No active broker link for account_id={account_id}")

            sync_started_at = _now_kst_naive()
            boundary = link.snapshot_boundary_at
            overlap_from = link.last_synced_at - timedelta(days=_OVERLAP_RESCAN_DAYS)
            from_date = max(boundary, overlap_from).date()

            try:
                raw_orders = fetcher.get_closed_orders(
                    link.external_account_seq,
                    from_date=from_date,
                )
            except DataFetchError as exc:
                raise TossUpstreamError(str(exc)) from exc

            candidates: List[Tuple[datetime, float, Dict[str, Any]]] = []
            for order in raw_orders:
                execution = order.get("execution") or {}
                filled_qty = safe_float(execution.get("filledQuantity"))
                if filled_qty is None or filled_qty <= 0:
                    # CLOSED but never filled (e.g. canceled before any execution).
                    continue
                filled_at = _parse_kst_datetime(execution.get("filledAt"))
                if filled_at is None:
                    logger.warning(
                        "[TossSync] account_id=%s order_id=%s has an unparsable filledAt=%r; skipped",
                        account_id,
                        order.get("orderId"),
                        execution.get("filledAt"),
                    )
                    continue
                if filled_at <= boundary:
                    continue
                candidates.append((filled_at, filled_qty, order))

            # Deterministic replay order: ascending fill time, then orderId as tiebreaker.
            candidates.sort(key=lambda item: (item[0], str(item[2].get("orderId") or "")))

            imported = 0
            skipped_duplicates = 0
            failed: List[Dict[str, Any]] = []
            failed_filled_ats: List[datetime] = []

            for filled_at, filled_qty, order in candidates:
                order_id = str(order.get("orderId") or "")
                execution = order.get("execution") or {}
                side_raw = str(order.get("side") or "").strip().upper()
                side = "buy" if side_raw == "BUY" else "sell" if side_raw == "SELL" else None
                raw_symbol = str(order.get("symbol") or "")

                if side is None or not order_id:
                    failed.append(
                        {
                            "type": "malformed_order",
                            "symbol": raw_symbol or None,
                            "order_id": order_id or None,
                            "filled_at": filled_at.isoformat(),
                            "reason": f"unsupported side={order.get('side')!r} or missing orderId",
                        }
                    )
                    failed_filled_ats.append(filled_at)
                    logger.warning(
                        "[TossSync] account_id=%s order has unsupported side=%r or missing orderId; "
                        "recorded as failed, cursor held back",
                        account_id,
                        order.get("side"),
                    )
                    continue

                # averageFilledPrice is required — no order.price fallback (a
                # market/partial fill's average price can diverge materially
                # from the order's limit/reference price, and silently
                # substituting it would distort cost basis; design spec §3).
                avg_price = safe_float(execution.get("averageFilledPrice"))
                if avg_price is None or avg_price <= 0:
                    failed.append(
                        {
                            "type": "missing_average_price",
                            "symbol": raw_symbol,
                            "order_id": order_id,
                            "filled_at": filled_at.isoformat(),
                            "reason": "execution.averageFilledPrice missing or non-positive",
                        }
                    )
                    failed_filled_ats.append(filled_at)
                    logger.warning(
                        "[TossSync] account_id=%s order_id=%s missing/invalid averageFilledPrice; "
                        "recorded as failed, cursor held back",
                        account_id,
                        order_id,
                    )
                    continue

                fee = safe_float(execution.get("commission")) or 0.0
                tax = safe_float(execution.get("tax")) or 0.0
                symbol, market, resolved = _resolve_symbol_and_market(raw_symbol)
                currency = str(order.get("currency") or "").strip().upper() or _default_currency_for_market(market)
                note = f"toss:{order_id}"
                if not resolved:
                    note += f";toss_kr_symbol_unresolved:{raw_symbol}"

                trade_uid = f"toss:{order_id}"
                try:
                    self.portfolio_service.record_trade(
                        account_id=account_id,
                        symbol=symbol,
                        trade_date=filled_at.date(),
                        side=side,
                        quantity=filled_qty,
                        price=avg_price,
                        fee=fee,
                        tax=tax,
                        market=market,
                        currency=currency,
                        trade_uid=trade_uid,
                        note=note,
                    )
                    imported += 1
                except PortfolioConflictError:
                    # Expected on an overlap re-scan: the trade_uid unique
                    # constraint is the real dedup mechanism, not the cursor.
                    skipped_duplicates += 1
                except PortfolioOversellError as exc:
                    failed.append(
                        {
                            "type": "oversell",
                            "symbol": symbol,
                            "order_id": order_id,
                            "filled_at": filled_at.isoformat(),
                            "requested_quantity": round(exc.requested_quantity, 8),
                            "available_quantity": round(exc.available_quantity, 8),
                        }
                    )
                    failed_filled_ats.append(filled_at)
                    logger.warning(
                        "[TossSync] account_id=%s order_id=%s oversell detected; recorded as "
                        "failed, cursor held back: %s",
                        account_id,
                        order_id,
                        exc,
                    )

            if failed_filled_ats:
                candidate_last_synced_at = min(failed_filled_ats) - _CURSOR_HOLDBACK
            else:
                candidate_last_synced_at = sync_started_at

            drift = self._compute_position_drift(
                account_id=account_id,
                external_account_seq=link.external_account_seq,
                fetcher=fetcher,
                as_of=sync_started_at.date(),
            )

            updated = self.repo.update_broker_link_sync(
                account_id=account_id,
                candidate_last_synced_at=candidate_last_synced_at,
                last_reconciled_at=sync_started_at,
            )
            final_last_synced_at = updated.last_synced_at if updated is not None else link.last_synced_at
            final_last_reconciled_at = (
                updated.last_reconciled_at if updated is not None and updated.last_reconciled_at is not None
                else sync_started_at
            )

            return {
                "account_id": account_id,
                "imported": imported,
                "skipped_duplicates": skipped_duplicates,
                "failed": failed,
                "drift": drift,
                "last_synced_at": final_last_synced_at.isoformat(),
                "last_reconciled_at": final_last_reconciled_at.isoformat(),
            }

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def _compute_position_drift(
        self,
        *,
        account_id: int,
        external_account_seq: str,
        fetcher: TossFetcher,
        as_of: date,
    ) -> List[Dict[str, Any]]:
        """Compare replayed ledger positions against fresh Toss holdings.

        Detection only — never writes back to the ledger (CONTEXT.md
        reconciliation contract). A symbol absent from both sides after a
        full sell-out is not drift (§5 "매도 후 잔고 0 종목" edge case) —
        the EPS-filtered dict construction below naturally excludes it from
        both maps. Oversell/malformed-order failures are reported via
        ``failed[]`` in ``sync_linked_account``, not here — this method only
        ever emits ``quantity_mismatch``.
        """
        try:
            holdings = fetcher.get_holdings(external_account_seq)
        except DataFetchError as exc:
            raise TossUpstreamError(str(exc)) from exc

        broker_positions: Dict[str, float] = {}
        for item in holdings.get("items") or []:
            quantity = safe_float(item.get("quantity")) or 0.0
            if quantity <= _DRIFT_EPS:
                continue
            raw_symbol = str(item.get("symbol") or "")
            symbol, _market, _resolved = _resolve_symbol_and_market(
                raw_symbol, market_country_hint=item.get("marketCountry")
            )
            broker_positions[symbol] = broker_positions.get(symbol, 0.0) + quantity

        snapshot = self.portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of,
            cost_method="fifo",
            include_realtime=False,
        )
        ledger_positions: Dict[str, float] = {}
        for account_payload in snapshot.get("accounts") or []:
            for position in account_payload.get("positions") or []:
                symbol = str(position.get("symbol") or "")
                if not symbol:
                    continue
                qty = float(position.get("quantity") or 0.0)
                if qty <= _DRIFT_EPS:
                    continue
                ledger_positions[symbol] = ledger_positions.get(symbol, 0.0) + qty

        drift: List[Dict[str, Any]] = []
        for symbol in sorted(set(ledger_positions) | set(broker_positions)):
            ledger_qty = ledger_positions.get(symbol, 0.0)
            broker_qty = broker_positions.get(symbol, 0.0)
            diff = ledger_qty - broker_qty
            if abs(diff) > _DRIFT_EPS:
                drift.append(
                    {
                        "type": "quantity_mismatch",
                        "symbol": symbol,
                        "ledger_qty": round(ledger_qty, 8),
                        "broker_qty": round(broker_qty, 8),
                        "diff": round(diff, 8),
                    }
                )
        return drift

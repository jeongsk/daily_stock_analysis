# -*- coding: utf-8 -*-
"""Offline tests for the Toss broker-link hybrid sync (Phase 2, revised
2026-07-17 after an independent code review — see
docs/superpowers/specs/2026-07-17-toss-portfolio-sync-design.md).

Covers the design spec's §6 offline list: link creation as one atomic
transaction (account + opening trades + link row, with rollback on a
mid-way duplicate), the snapshot boundary (permanent dedup cutover,
independent of the optimization cursor), overlap re-scan + trade_uid dedup
convergence, held-back cursor retry for failed orders (missing
averageFilledPrice / oversell / malformed order), monotonic cursor
advancement, partial-fill reflection, reconciliation drift (quantity
mismatch / ledger-only / broker-only), unlink deactivation -> relink cursor
inheritance, not-configured 4xx, and KR/US symbol+currency mapping. A single
`-m network` smoke test (accounts+holdings only, read-only) is included for
local runs with real credentials + an allow-listed IP; it is not executed by
CI or by `pytest -m "not network"`.

TossFetcher is always mocked/faked here — this suite makes no real HTTP calls.
Real-envelope parsing (multi-page pagination, hasNext/nextCursor strictness)
is covered separately in tests/test_toss_fetcher.py, which mocks at the HTTP
layer instead of at FakeTossFetcher's parsed-list boundary.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# Keep this test runnable when optional LLM runtime deps are not installed.
try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from data_provider.base import DataFetchError
from data_provider.toss_fetcher import TossFetcher
from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_broker_sync_service import (
    AmbiguousBrokerAccountError,
    BrokerLinkConflictError,
    BrokerLinkNotFoundError,
    PortfolioBrokerSyncService,
    TossNotConfiguredError,
    TossUpstreamError,
)
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager


class FakeTossFetcher:
    """Deterministic stand-in for TossFetcher's account/holdings/orders calls.

    Operates at the *parsed* boundary (get_closed_orders already returns a
    flat list of order dicts) — envelope-shape strictness (multi-page
    pagination, hasNext/nextCursor validation) is exercised against the real
    HTTP layer in tests/test_toss_fetcher.py instead.
    """

    def __init__(
        self,
        *,
        accounts: Optional[List[Dict[str, Any]]] = None,
        holdings_sequence: Optional[List[Dict[str, Any]]] = None,
        orders: Optional[List[Dict[str, Any]]] = None,
        raise_on: Optional[set] = None,
    ) -> None:
        self._accounts = (
            accounts
            if accounts is not None
            else [{"accountNo": "1234567890", "accountSeq": 555, "accountType": "BROKERAGE"}]
        )
        # Each get_holdings() call pops the next payload; the last one repeats
        # once exhausted so link (1 call) and sync's reconciliation (1 call)
        # can share a single-element list when the holdings snapshot is static.
        self._holdings_sequence = list(holdings_sequence or [{"items": []}])
        self._orders = orders if orders is not None else []
        self._raise_on = raise_on or set()
        self.get_accounts_calls = 0
        self.get_holdings_calls: List[Any] = []
        self.get_closed_orders_calls: List[Any] = []

    def get_accounts(self) -> List[Dict[str, Any]]:
        self.get_accounts_calls += 1
        if "accounts" in self._raise_on:
            raise DataFetchError("[Toss] simulated accounts failure")
        return self._accounts

    def get_holdings(self, account_seq: Any) -> Dict[str, Any]:
        self.get_holdings_calls.append(account_seq)
        if "holdings" in self._raise_on:
            raise DataFetchError("[Toss] simulated holdings failure")
        if len(self._holdings_sequence) > 1:
            return self._holdings_sequence.pop(0)
        return self._holdings_sequence[0]

    def get_closed_orders(
        self,
        account_seq: Any,
        *,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        self.get_closed_orders_calls.append((account_seq, from_date, to_date))
        if "orders" in self._raise_on:
            raise DataFetchError("[Toss] simulated orders failure")
        return self._orders


def _order(
    *,
    order_id: str,
    symbol: str,
    side: str,
    filled_quantity: str,
    filled_at: str,
    average_filled_price: Optional[str] = "70000",
    commission: str = "0",
    tax: str = "0",
    currency: str = "KRW",
    order_quantity: Optional[str] = None,
) -> Dict[str, Any]:
    execution: Dict[str, Any] = {
        "filledQuantity": filled_quantity,
        "filledAmount": "0",
        "commission": commission,
        "tax": tax,
        "filledAt": filled_at,
        "settlementDate": filled_at[:10],
    }
    if average_filled_price is not None:
        execution["averageFilledPrice"] = average_filled_price
    return {
        "orderId": order_id,
        "symbol": symbol,
        "side": side,
        "orderType": "MARKET",
        "status": "CLOSED",
        "price": "0",  # deliberately different from averageFilledPrice; must never be used as a fallback
        "quantity": order_quantity or filled_quantity,
        "currency": currency,
        "orderedAt": filled_at,
        "execution": execution,
    }


class PortfolioBrokerSyncServiceTestCase(unittest.TestCase):
    """Service-level tests: link, sync, drift, dedup, KR/US mapping."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"
        self.db_path = Path(self.temp_dir.name) / "portfolio_broker_sync_test.db"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()

        self.db = DatabaseManager.get_instance()
        self.portfolio_service = PortfolioService()
        self.repo = PortfolioRepository()

        # Deterministic KR symbol resolution regardless of whether a generated
        # stock index is present in this environment.
        self._resolve_patch = patch(
            "src.services.portfolio_broker_sync_service.resolve_index_stock_code",
            side_effect=self._fake_resolve_index_stock_code,
        )
        self._resolve_patch.start()

    def tearDown(self) -> None:
        self._resolve_patch.stop()
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    @staticmethod
    def _fake_resolve_index_stock_code(code: str) -> Optional[str]:
        mapping = {"005930": "005930.KS", "035720": "035720.KQ"}
        return mapping.get(code)

    def _make_service(self, fetcher: FakeTossFetcher) -> PortfolioBrokerSyncService:
        return PortfolioBrokerSyncService(
            portfolio_service=self.portfolio_service,
            repo=self.repo,
            fetcher=fetcher,
        )

    def _link_account_directly(
        self,
        *,
        external_account_seq: str = "555",
        cursor: datetime,
        boundary: Optional[datetime] = None,
        active: bool = True,
    ) -> int:
        """Create a portfolio account + broker link without going through
        link_toss_account(), for tests that only exercise sync_linked_account()."""
        created = self.portfolio_service.create_account(
            name="Toss KR", broker="toss", market="kr", base_currency="KRW"
        )
        account_id = int(created["id"])
        self.repo.create_broker_link(
            account_id=account_id,
            provider="toss",
            external_account_seq=external_account_seq,
            external_account_no="1234567890",
            linked_at=cursor,
            snapshot_boundary_at=boundary if boundary is not None else cursor,
            last_synced_at=cursor,
            active=active,
        )
        return account_id

    # ------------------------------------------------------------------
    # Link — creation, atomicity, boundary
    # ------------------------------------------------------------------
    def test_link_creates_account_opening_trades_and_link_row(self) -> None:
        fetcher = FakeTossFetcher(
            holdings_sequence=[
                {
                    "items": [
                        {
                            "symbol": "005930",
                            "name": "삼성전자",
                            "marketCountry": "KR",
                            "currency": "KRW",
                            "quantity": "10",
                            "averagePurchasePrice": "70000",
                        },
                        {
                            "symbol": "AAPL",
                            "name": "Apple",
                            "marketCountry": "US",
                            "currency": "USD",
                            "quantity": "2",
                            "averagePurchasePrice": "150.5",
                        },
                    ]
                }
            ]
        )
        service = self._make_service(fetcher)

        result = service.link_toss_account(name="My Toss")

        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped_duplicates"], 0)
        self.assertFalse(result["reactivated"])
        self.assertEqual(result["external_account_seq"], "555")
        self.assertEqual(result["external_account_no"], "1234567890")

        account_id = result["account_id"]
        account = self.repo.get_account(account_id)
        self.assertEqual(account.market, "kr")
        self.assertEqual(account.base_currency, "KRW")
        self.assertEqual(account.broker, "toss")

        trades, total = self.repo.query_trades(
            account_id=account_id, date_from=None, date_to=None, symbols=None, side=None, page=1, page_size=20
        )
        self.assertEqual(total, 2)
        by_symbol = {t.symbol: t for t in trades}
        self.assertIn("005930.KS", by_symbol)
        self.assertIn("AAPL", by_symbol)
        kr_trade = by_symbol["005930.KS"]
        self.assertEqual(kr_trade.side, "buy")
        self.assertEqual(kr_trade.quantity, 10.0)
        self.assertEqual(kr_trade.price, 70000.0)
        self.assertEqual(kr_trade.market, "kr")
        self.assertEqual(kr_trade.currency, "KRW")
        self.assertEqual(kr_trade.trade_uid, "toss:opening:555:005930")
        us_trade = by_symbol["AAPL"]
        self.assertEqual(us_trade.market, "us")
        self.assertEqual(us_trade.currency, "USD")
        self.assertEqual(us_trade.trade_uid, "toss:opening:555:AAPL")

        link = self.repo.get_broker_link_by_account(account_id)
        self.assertIsNotNone(link)
        self.assertTrue(link.active)
        self.assertEqual(link.provider, "toss")
        self.assertEqual(link.external_account_seq, "555")
        # snapshot_boundary_at (T1) seeds last_synced_at at link time — they
        # only diverge once a sync runs.
        self.assertEqual(link.last_synced_at, link.linked_at)
        self.assertEqual(link.snapshot_boundary_at, link.linked_at)

    def test_link_kr_symbol_unresolved_falls_back_to_ks_with_note(self) -> None:
        fetcher = FakeTossFetcher(
            holdings_sequence=[
                {
                    "items": [
                        {
                            "symbol": "999999",
                            "marketCountry": "KR",
                            "currency": "KRW",
                            "quantity": "5",
                            "averagePurchasePrice": "1000",
                        }
                    ]
                }
            ]
        )
        service = self._make_service(fetcher)

        result = service.link_toss_account()
        account_id = result["account_id"]
        trades, _ = self.repo.query_trades(
            account_id=account_id, date_from=None, date_to=None, symbols=None, side=None, page=1, page_size=20
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].symbol, "999999.KS")
        self.assertIn("toss_kr_symbol_unresolved:999999", trades[0].note)

    def test_link_skips_zero_quantity_and_invalid_price_holdings(self) -> None:
        fetcher = FakeTossFetcher(
            holdings_sequence=[
                {
                    "items": [
                        {"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "0", "averagePurchasePrice": "70000"},
                        {"symbol": "035720", "marketCountry": "KR", "currency": "KRW", "quantity": "3", "averagePurchasePrice": "0"},
                        {"symbol": "AAPL", "marketCountry": "US", "currency": "USD", "quantity": "1", "averagePurchasePrice": "100"},
                    ]
                }
            ]
        )
        service = self._make_service(fetcher)
        result = service.link_toss_account()
        self.assertEqual(result["imported"], 1)

    def test_link_no_brokerage_accounts_raises_value_error(self) -> None:
        fetcher = FakeTossFetcher(accounts=[{"accountNo": "1", "accountSeq": 1, "accountType": "CMA"}])
        service = self._make_service(fetcher)
        with self.assertRaises(ValueError):
            service.link_toss_account()

    def test_link_ambiguous_accounts_without_account_seq(self) -> None:
        fetcher = FakeTossFetcher(
            accounts=[
                {"accountNo": "111", "accountSeq": 1, "accountType": "BROKERAGE"},
                {"accountNo": "222", "accountSeq": 2, "accountType": "BROKERAGE"},
            ]
        )
        service = self._make_service(fetcher)
        with self.assertRaises(AmbiguousBrokerAccountError) as ctx:
            service.link_toss_account()
        self.assertEqual(len(ctx.exception.accounts), 2)

    def test_link_account_seq_disambiguates(self) -> None:
        fetcher = FakeTossFetcher(
            accounts=[
                {"accountNo": "111", "accountSeq": 1, "accountType": "BROKERAGE"},
                {"accountNo": "222", "accountSeq": 2, "accountType": "BROKERAGE"},
            ],
            holdings_sequence=[{"items": []}],
        )
        service = self._make_service(fetcher)
        result = service.link_toss_account(account_seq="2")
        self.assertEqual(result["external_account_seq"], "2")
        self.assertEqual(result["external_account_no"], "222")

    def test_link_not_configured_raises_before_any_toss_call(self) -> None:
        with patch.object(TossFetcher, "has_configured_credentials", return_value=False):
            service = PortfolioBrokerSyncService(
                portfolio_service=self.portfolio_service, repo=self.repo
            )
            with self.assertRaises(TossNotConfiguredError):
                service.link_toss_account()

    def test_link_holdings_failure_leaves_no_orphan_account(self) -> None:
        fetcher = FakeTossFetcher(raise_on={"holdings"})
        service = self._make_service(fetcher)
        with self.assertRaises(TossUpstreamError):
            service.link_toss_account()
        self.assertEqual(len(self.portfolio_service.list_accounts()), 0)

    def test_link_accounts_failure_wraps_as_upstream_error(self) -> None:
        fetcher = FakeTossFetcher(raise_on={"accounts"})
        service = self._make_service(fetcher)
        with self.assertRaises(TossUpstreamError):
            service.link_toss_account()

    def test_link_duplicate_holdings_symbol_rolls_back_whole_transaction(self) -> None:
        """A duplicate symbol in one holdings response would collide on the
        deterministic opening trade_uid — the atomic transaction must roll
        back the account creation too, leaving no orphan account or partial
        ledger (design spec §3 link atomicity, Codex major 5)."""
        fetcher = FakeTossFetcher(
            holdings_sequence=[
                {
                    "items": [
                        {"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "10", "averagePurchasePrice": "70000"},
                        {"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "5", "averagePurchasePrice": "71000"},
                    ]
                }
            ]
        )
        service = self._make_service(fetcher)
        with self.assertRaises(TossUpstreamError):
            service.link_toss_account()
        self.assertEqual(len(self.portfolio_service.list_accounts()), 0)
        self.assertEqual(len(self.repo.list_broker_links(include_inactive=True)), 0)

    # ------------------------------------------------------------------
    # Link — request no longer accepts a caller-chosen account_id (Codex major 3)
    # ------------------------------------------------------------------
    def test_link_service_signature_has_no_account_id_parameter(self) -> None:
        import inspect

        sig = inspect.signature(PortfolioBrokerSyncService.link_toss_account)
        self.assertNotIn("account_id", sig.parameters)

    # ------------------------------------------------------------------
    # Link — unlink (deactivate) / relink (reactivate, cursor inheritance)
    # ------------------------------------------------------------------
    def test_unlink_deactivates_keeps_account_and_trades(self) -> None:
        fetcher = FakeTossFetcher(
            holdings_sequence=[
                {"items": [{"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "1", "averagePurchasePrice": "70000"}]}
            ]
        )
        service = self._make_service(fetcher)
        result = service.link_toss_account()
        account_id = result["account_id"]

        self.assertTrue(service.unlink(account_id))
        # Deactivated, not deleted: get_broker_link_by_account(active_only=False) still finds it.
        link = self.repo.get_broker_link_by_account(account_id)
        self.assertIsNotNone(link)
        self.assertFalse(link.active)
        self.assertIsNone(self.repo.get_broker_link_by_account(account_id, active_only=True))
        self.assertIsNotNone(self.repo.get_account(account_id))
        trades, total = self.repo.query_trades(
            account_id=account_id, date_from=None, date_to=None, symbols=None, side=None, page=1, page_size=20
        )
        self.assertEqual(total, 1)

        # Unlinking an account with no link row at all reports False.
        self.assertFalse(service.unlink(999999))

    def test_relink_reactivates_inherits_cursor_no_new_opening_trades(self) -> None:
        holdings_payload = {
            "items": [
                {
                    "symbol": "005930",
                    "marketCountry": "KR",
                    "currency": "KRW",
                    "quantity": "10",
                    "averagePurchasePrice": "70000",
                }
            ]
        }
        fetcher = FakeTossFetcher(holdings_sequence=[dict(holdings_payload)])
        service = self._make_service(fetcher)
        first = service.link_toss_account(name="Toss")
        account_id = first["account_id"]
        self.assertEqual(first["imported"], 1)

        # Advance the cursor via a sync before unlinking, to prove relink
        # preserves *that* advanced cursor rather than resetting it.
        advanced_cursor = datetime(2026, 7, 20, 9, 0, 0)
        self.repo.update_broker_link_sync(
            account_id=account_id, candidate_last_synced_at=advanced_cursor, last_reconciled_at=advanced_cursor
        )

        self.assertTrue(service.unlink(account_id))

        fetcher2 = FakeTossFetcher(holdings_sequence=[dict(holdings_payload)])
        service2 = self._make_service(fetcher2)
        second = service2.link_toss_account()

        self.assertEqual(second["account_id"], account_id)
        self.assertTrue(second["reactivated"])
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["skipped_duplicates"], 0)
        # No new opening trades: get_holdings was never called on the relink path.
        self.assertEqual(fetcher2.get_holdings_calls, [])
        self.assertEqual(len(self.portfolio_service.list_accounts()), 1)
        trades, total = self.repo.query_trades(
            account_id=account_id, date_from=None, date_to=None, symbols=None, side=None, page=1, page_size=20
        )
        self.assertEqual(total, 1)

        link = self.repo.get_broker_link_by_account(account_id, active_only=True)
        self.assertIsNotNone(link)
        self.assertEqual(link.last_synced_at, advanced_cursor)

    def test_relink_conflict_when_still_active(self) -> None:
        fetcher = FakeTossFetcher(holdings_sequence=[{"items": []}])
        service = self._make_service(fetcher)
        service.link_toss_account()

        fetcher2 = FakeTossFetcher(holdings_sequence=[{"items": []}])
        service2 = self._make_service(fetcher2)
        with self.assertRaises(BrokerLinkConflictError):
            service2.link_toss_account()
        # The conflict is only detectable after resolving which Toss account
        # this is (accountSeq is the match key, not a caller-supplied id) —
        # so get_accounts() is still called once before the 409.
        self.assertEqual(fetcher2.get_accounts_calls, 1)
        self.assertEqual(fetcher2.get_holdings_calls, [])

    def test_relink_after_hard_deleted_account_creates_brand_new_account(self) -> None:
        """If the linked portfolio account was hard-deleted (DELETE
        /accounts) while the link was inactive, the stale link row is not
        reusable — relinking the same Toss account must create a fresh
        account instead of reactivating onto a dead one."""
        holdings_payload = {
            "items": [
                {
                    "symbol": "005930",
                    "marketCountry": "KR",
                    "currency": "KRW",
                    "quantity": "10",
                    "averagePurchasePrice": "70000",
                }
            ]
        }
        fetcher = FakeTossFetcher(holdings_sequence=[dict(holdings_payload)])
        service = self._make_service(fetcher)
        first = service.link_toss_account()
        first_account_id = first["account_id"]

        self.assertTrue(service.unlink(first_account_id))
        self.assertTrue(self.repo.deactivate_account(first_account_id))

        fetcher2 = FakeTossFetcher(holdings_sequence=[dict(holdings_payload)])
        service2 = self._make_service(fetcher2)
        second = service2.link_toss_account()

        self.assertNotEqual(second["account_id"], first_account_id)
        self.assertFalse(second["reactivated"])
        self.assertEqual(second["imported"], 1)
        self.assertEqual(fetcher2.get_holdings_calls, ["555"])

    # ------------------------------------------------------------------
    # Sync — snapshot boundary + overlap re-scan + trade_uid dedup
    # ------------------------------------------------------------------
    def test_sync_boundary_excludes_filled_at_equal_to_boundary(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)

        boundary_order = _order(
            order_id="A1",
            symbol="005930",
            side="BUY",
            filled_quantity="1",
            filled_at="2026-07-17T10:00:00",
        )
        after_order = _order(
            order_id="A2",
            symbol="005930",
            side="BUY",
            filled_quantity="2",
            filled_at="2026-07-17T10:00:01",
        )
        fetcher = FakeTossFetcher(
            orders=[boundary_order, after_order],
            holdings_sequence=[{"items": [{"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "2"}]}],
        )
        service = self._make_service(fetcher)

        # No failures this sync -> cursor advances to sync_start (design spec
        # §3 rule (b)), not to the imported order's own filled_at — pin the
        # clock so the assertion is deterministic.
        fixed_now = datetime(2026, 7, 17, 12, 0, 0)
        with patch(
            "src.services.portfolio_broker_sync_service._now_kst_naive",
            return_value=fixed_now,
        ):
            result = service.sync_linked_account(account_id)

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped_duplicates"], 0)
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["last_synced_at"], fixed_now.isoformat())
        trades, total = self.repo.query_trades(
            account_id=account_id, date_from=None, date_to=None, symbols=None, side=None, page=1, page_size=20
        )
        self.assertEqual(total, 1)
        self.assertEqual(trades[0].quantity, 2.0)

    def test_sync_uses_overlap_rescan_from_date_not_bare_cursor(self) -> None:
        """from_date = max(snapshot_boundary_at, last_synced_at - 3 days) —
        the query window must reach back 3 days before the cursor, not start
        exactly at it (design spec §3 cursor redesign)."""
        boundary = datetime(2026, 7, 1, 9, 0, 0)
        cursor = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=cursor, boundary=boundary)
        fetcher = FakeTossFetcher(orders=[], holdings_sequence=[{"items": []}])
        service = self._make_service(fetcher)

        service.sync_linked_account(account_id)

        self.assertEqual(len(fetcher.get_closed_orders_calls), 1)
        _seq, from_date, _to_date = fetcher.get_closed_orders_calls[0]
        self.assertEqual(from_date, date(2026, 7, 14))  # cursor - 3 days

    def test_sync_overlap_rescan_dedupes_via_trade_uid_not_double_counting(self) -> None:
        boundary = datetime(2026, 7, 10, 9, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        order = _order(
            order_id="B1",
            symbol="005930",
            side="BUY",
            filled_quantity="1",
            filled_at="2026-07-11T09:00:00",
        )
        fetcher1 = FakeTossFetcher(
            orders=[order],
            holdings_sequence=[{"items": [{"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "1"}]}],
        )
        service1 = self._make_service(fetcher1)
        first = service1.sync_linked_account(account_id)
        self.assertEqual(first["imported"], 1)

        # Second sync re-fetches the *same* order (overlap re-scan window
        # still covers it) — trade_uid uniqueness must collapse it to a skip,
        # never a second position.
        fetcher2 = FakeTossFetcher(
            orders=[order],
            holdings_sequence=[{"items": [{"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "1"}]}],
        )
        service2 = self._make_service(fetcher2)
        second = service2.sync_linked_account(account_id)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["skipped_duplicates"], 1)

        trades, total = self.repo.query_trades(
            account_id=account_id, date_from=None, date_to=None, symbols=None, side=None, page=1, page_size=20
        )
        self.assertEqual(total, 1)

    def test_sync_advances_cursor_to_sync_start_when_no_new_orders(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        fetcher = FakeTossFetcher(orders=[], holdings_sequence=[{"items": []}])
        service = self._make_service(fetcher)

        fixed_now = datetime(2026, 7, 18, 9, 30, 0)
        with patch(
            "src.services.portfolio_broker_sync_service._now_kst_naive",
            return_value=fixed_now,
        ):
            result = service.sync_linked_account(account_id)

        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["last_synced_at"], fixed_now.isoformat())

    def test_sync_partial_fill_uses_filled_quantity_not_order_quantity(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        order = _order(
            order_id="C1",
            symbol="005930",
            side="BUY",
            filled_quantity="4",
            order_quantity="10",
            filled_at="2026-07-17T11:00:00",
        )
        fetcher = FakeTossFetcher(
            orders=[order],
            holdings_sequence=[{"items": [{"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "4"}]}],
        )
        service = self._make_service(fetcher)

        result = service.sync_linked_account(account_id)

        self.assertEqual(result["imported"], 1)
        trades, _ = self.repo.query_trades(
            account_id=account_id, date_from=None, date_to=None, symbols=None, side=None, page=1, page_size=20
        )
        self.assertEqual(trades[0].quantity, 4.0)

    def test_sync_us_decimal_fill_quantity_parsed_precisely(self) -> None:
        """Real-shape US decimal fractional-share fill:
        filledQuantity='0.002686' (design spec §6 fixture requirement)."""
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        order = _order(
            order_id="US1",
            symbol="AAPL",
            side="BUY",
            filled_quantity="0.002686",
            average_filled_price="150.1234",
            filled_at="2026-07-17T22:30:00",
            currency="USD",
        )
        fetcher = FakeTossFetcher(
            orders=[order],
            holdings_sequence=[{"items": [{"symbol": "AAPL", "marketCountry": "US", "currency": "USD", "quantity": "0.002686"}]}],
        )
        service = self._make_service(fetcher)

        result = service.sync_linked_account(account_id)

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["failed"], [])
        trades, _ = self.repo.query_trades(
            account_id=account_id, date_from=None, date_to=None, symbols=None, side=None, page=1, page_size=20
        )
        self.assertAlmostEqual(trades[0].quantity, 0.002686, places=8)
        self.assertAlmostEqual(trades[0].price, 150.1234, places=4)

    def test_sync_ignores_closed_order_with_zero_fill(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        order = _order(
            order_id="D1",
            symbol="005930",
            side="BUY",
            filled_quantity="0",
            filled_at="2026-07-17T11:00:00",
        )
        fetcher = FakeTossFetcher(orders=[order], holdings_sequence=[{"items": []}])
        service = self._make_service(fetcher)

        result = service.sync_linked_account(account_id)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["skipped_duplicates"], 0)
        self.assertEqual(result["failed"], [])

    # ------------------------------------------------------------------
    # Sync — failed[] classification + held-back cursor retry (Codex blocker)
    # ------------------------------------------------------------------
    def test_sync_missing_average_price_is_failed_not_order_price_fallback(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        order = _order(
            order_id="E0",
            symbol="005930",
            side="BUY",
            filled_quantity="1",
            filled_at="2026-07-17T11:00:00",
            average_filled_price=None,  # missing entirely — order.price must NOT be used instead
        )
        fetcher = FakeTossFetcher(orders=[order], holdings_sequence=[{"items": []}])
        service = self._make_service(fetcher)

        result = service.sync_linked_account(account_id)

        self.assertEqual(result["imported"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["type"], "missing_average_price")
        self.assertEqual(result["failed"][0]["order_id"], "E0")
        trades, total = self.repo.query_trades(
            account_id=account_id, date_from=None, date_to=None, symbols=None, side=None, page=1, page_size=20
        )
        self.assertEqual(total, 0)  # never recorded at any price, including order.price

    def test_sync_oversell_recorded_as_failed_and_sync_continues(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)

        oversell_order = _order(
            order_id="E1",
            symbol="005930",
            side="SELL",
            filled_quantity="5",
            filled_at="2026-07-17T11:00:00",
        )
        later_buy_order = _order(
            order_id="E2",
            symbol="AAPL",
            side="BUY",
            filled_quantity="1",
            filled_at="2026-07-17T12:00:00",
            average_filled_price="100",
            currency="USD",
        )
        fetcher = FakeTossFetcher(
            orders=[oversell_order, later_buy_order],
            holdings_sequence=[{"items": [{"symbol": "AAPL", "marketCountry": "US", "currency": "USD", "quantity": "1"}]}],
        )
        service = self._make_service(fetcher)

        result = service.sync_linked_account(account_id)

        self.assertEqual(result["imported"], 1)  # later_buy_order still processed
        self.assertEqual(result["drift"], [])  # oversell is reported via failed[], not drift[]
        failed_oversell = [f for f in result["failed"] if f["type"] == "oversell"]
        self.assertEqual(len(failed_oversell), 1)
        self.assertEqual(failed_oversell[0]["order_id"], "E1")
        self.assertEqual(failed_oversell[0]["symbol"], "005930.KS")

    def test_sync_held_back_cursor_does_not_advance_past_earliest_failure(self) -> None:
        """Design spec §3/§5: a failed order holds the cursor back to just
        before its own fill time, even when a later order in the same sync
        succeeds — this is the exact bug Codex flagged as a blocker (a
        cursor advanced past a failed order permanently lost it)."""
        boundary = datetime(2026, 7, 17, 9, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)

        failing_order = _order(
            order_id="F1",
            symbol="005930",
            side="SELL",
            filled_quantity="5",
            filled_at="2026-07-17T11:00:00",
        )
        later_ok_order = _order(
            order_id="F2",
            symbol="AAPL",
            side="BUY",
            filled_quantity="1",
            filled_at="2026-07-17T12:00:00",
            average_filled_price="100",
            currency="USD",
        )
        fetcher = FakeTossFetcher(
            orders=[failing_order, later_ok_order],
            holdings_sequence=[{"items": [{"symbol": "AAPL", "marketCountry": "US", "currency": "USD", "quantity": "1"}]}],
        )
        service = self._make_service(fetcher)

        result = service.sync_linked_account(account_id)

        self.assertEqual(result["imported"], 1)
        self.assertEqual(len(result["failed"]), 1)
        # min(failed_filled_ats) - 1s = 2026-07-17T11:00:00 - 1s
        self.assertEqual(result["last_synced_at"], "2026-07-17T10:59:59")

    def test_sync_retries_held_back_order_once_it_can_be_recorded(self) -> None:
        """After a held-back sync, the next sync's overlap-rescan window
        still reaches the failed order's date, and once it can be recorded
        (e.g. the upstream data is no longer missing averageFilledPrice), it
        gets imported and the cursor finally advances past it."""
        boundary = datetime(2026, 7, 10, 9, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)

        broken_order = _order(
            order_id="G1",
            symbol="005930",
            side="BUY",
            filled_quantity="1",
            filled_at="2026-07-15T10:00:00",
            average_filled_price=None,
        )
        fetcher1 = FakeTossFetcher(orders=[broken_order], holdings_sequence=[{"items": []}])
        service1 = self._make_service(fetcher1)
        first = service1.sync_linked_account(account_id)
        self.assertEqual(first["imported"], 0)
        self.assertEqual(len(first["failed"]), 1)
        self.assertEqual(first["last_synced_at"], "2026-07-15T09:59:59")

        # Next sync's overlap window must still reach back to (at least) the
        # failed order's fill date, proving the held-back cursor is honored.
        fixed_now = datetime(2026, 7, 16, 9, 0, 0)
        fixed_order = _order(
            order_id="G1",
            symbol="005930",
            side="BUY",
            filled_quantity="1",
            filled_at="2026-07-15T10:00:00",
            average_filled_price="70000",  # now present
        )
        fetcher2 = FakeTossFetcher(
            orders=[fixed_order],
            holdings_sequence=[{"items": [{"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "1"}]}],
        )
        service2 = self._make_service(fetcher2)
        with patch(
            "src.services.portfolio_broker_sync_service._now_kst_naive",
            return_value=fixed_now,
        ):
            second = service2.sync_linked_account(account_id)

        _seq, from_date, _to_date = fetcher2.get_closed_orders_calls[0]
        self.assertLessEqual(from_date, date(2026, 7, 15))
        self.assertEqual(second["imported"], 1)
        self.assertEqual(second["failed"], [])
        self.assertEqual(second["last_synced_at"], fixed_now.isoformat())

    def test_sync_malformed_order_missing_order_id_is_failed(self) -> None:
        boundary = datetime(2026, 7, 17, 9, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        malformed = _order(
            order_id="",
            symbol="005930",
            side="BUY",
            filled_quantity="1",
            filled_at="2026-07-17T11:00:00",
        )
        fetcher = FakeTossFetcher(orders=[malformed], holdings_sequence=[{"items": []}])
        service = self._make_service(fetcher)

        result = service.sync_linked_account(account_id)

        self.assertEqual(result["imported"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["type"], "malformed_order")

    def test_sync_malformed_order_unsupported_side_is_failed(self) -> None:
        boundary = datetime(2026, 7, 17, 9, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        malformed = _order(
            order_id="H1",
            symbol="005930",
            side="SHORT",
            filled_quantity="1",
            filled_at="2026-07-17T11:00:00",
        )
        fetcher = FakeTossFetcher(orders=[malformed], holdings_sequence=[{"items": []}])
        service = self._make_service(fetcher)

        result = service.sync_linked_account(account_id)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["type"], "malformed_order")

    # ------------------------------------------------------------------
    # Sync — monotonic cursor (Codex major 4)
    # ------------------------------------------------------------------
    def test_update_broker_link_sync_is_monotonic(self) -> None:
        boundary = datetime(2026, 7, 17, 9, 0, 0)
        cursor = datetime(2026, 7, 20, 9, 0, 0)
        account_id = self._link_account_directly(cursor=cursor, boundary=boundary)

        # A stale/slow concurrent sync's candidate (earlier than the current
        # stored cursor) must never regress it.
        stale_candidate = datetime(2026, 7, 18, 9, 0, 0)
        updated = self.repo.update_broker_link_sync(
            account_id=account_id, candidate_last_synced_at=stale_candidate, last_reconciled_at=None
        )
        self.assertEqual(updated.last_synced_at, cursor)  # unchanged

        newer_candidate = datetime(2026, 7, 21, 9, 0, 0)
        updated2 = self.repo.update_broker_link_sync(
            account_id=account_id, candidate_last_synced_at=newer_candidate, last_reconciled_at=None
        )
        self.assertEqual(updated2.last_synced_at, newer_candidate)

    def test_sync_not_configured_raises(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        with patch.object(TossFetcher, "has_configured_credentials", return_value=False):
            service = PortfolioBrokerSyncService(
                portfolio_service=self.portfolio_service, repo=self.repo
            )
            with self.assertRaises(TossNotConfiguredError):
                service.sync_linked_account(account_id)

    def test_sync_link_not_found_raises(self) -> None:
        fetcher = FakeTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(BrokerLinkNotFoundError):
            service.sync_linked_account(999999)

    def test_sync_inactive_link_raises_not_found(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary, active=False)
        fetcher = FakeTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(BrokerLinkNotFoundError):
            service.sync_linked_account(account_id)

    def test_sync_upstream_error_wraps_orders_failure(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        fetcher = FakeTossFetcher(raise_on={"orders"})
        service = self._make_service(fetcher)
        with self.assertRaises(TossUpstreamError):
            service.sync_linked_account(account_id)

    # ------------------------------------------------------------------
    # Sync — reconciliation drift (quantity_mismatch only; oversell moved to failed[])
    # ------------------------------------------------------------------
    def test_sync_reconciliation_flags_quantity_mismatch(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        buy_order = _order(
            order_id="F1",
            symbol="005930",
            side="BUY",
            filled_quantity="10",
            filled_at="2026-07-17T11:00:00",
        )
        fetcher = FakeTossFetcher(
            orders=[buy_order],
            holdings_sequence=[{"items": [{"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "7"}]}],
        )
        service = self._make_service(fetcher)

        result = service.sync_linked_account(account_id)

        self.assertEqual(len(result["drift"]), 1)
        self.assertEqual(result["drift"][0]["type"], "quantity_mismatch")
        self.assertEqual(result["drift"][0]["symbol"], "005930.KS")
        self.assertEqual(result["drift"][0]["ledger_qty"], 10.0)
        self.assertEqual(result["drift"][0]["broker_qty"], 7.0)
        self.assertAlmostEqual(result["drift"][0]["diff"], 3.0)

    def test_sync_reconciliation_flags_broker_only_position(self) -> None:
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        fetcher = FakeTossFetcher(
            orders=[],
            holdings_sequence=[{"items": [{"symbol": "AAPL", "marketCountry": "US", "currency": "USD", "quantity": "3"}]}],
        )
        service = self._make_service(fetcher)

        result = service.sync_linked_account(account_id)
        mismatches = [d for d in result["drift"] if d["type"] == "quantity_mismatch"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["symbol"], "AAPL")
        self.assertEqual(mismatches[0]["ledger_qty"], 0.0)
        self.assertEqual(mismatches[0]["broker_qty"], 3.0)

    def test_sync_sold_out_symbol_is_not_drift(self) -> None:
        """Symbol absent from both ledger replay and holdings after a full
        sell-out is not drift (design spec §5)."""
        boundary = datetime(2026, 7, 17, 10, 0, 0)
        account_id = self._link_account_directly(cursor=boundary, boundary=boundary)
        fetcher = FakeTossFetcher(orders=[], holdings_sequence=[{"items": []}])
        service = self._make_service(fetcher)
        result = service.sync_linked_account(account_id)
        self.assertEqual(result["drift"], [])

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------
    def test_list_links_returns_only_active_links(self) -> None:
        fetcher = FakeTossFetcher(holdings_sequence=[{"items": []}])
        service = self._make_service(fetcher)
        result = service.link_toss_account(name="Toss KR")

        links = service.list_links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["account_id"], result["account_id"])
        self.assertEqual(links[0]["provider"], "toss")
        self.assertEqual(links[0]["external_account_seq"], "555")
        self.assertIsNotNone(links[0]["last_synced_at"])

        service.unlink(result["account_id"])
        self.assertEqual(service.list_links(), [])


class PortfolioBrokerSyncApiTestCase(unittest.TestCase):
    """API-level round trip: link -> sync -> list -> unlink, via _ThreadlessTestClient."""

    def setUp(self) -> None:
        import src.auth as auth

        auth._auth_enabled = None
        auth._session_secret = None
        auth._password_hash_salt = None
        auth._password_hash_stored = None
        auth._rate_limit = {}

        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.env_path = self.data_dir / ".env"
        self.db_path = self.data_dir / "portfolio_broker_sync_api_test.db"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()

        from api.app import create_app

        app = create_app(static_dir=self.data_dir / "empty-static")
        from fastapi.testclient import TestClient

        self.client = TestClient(app)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def test_link_toss_returns_400_when_not_configured(self) -> None:
        with patch.object(TossFetcher, "has_configured_credentials", return_value=False):
            resp = self.client.post("/api/v1/portfolio/links/toss", json={})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json().get("error"), "toss-not-configured")

    def test_link_sync_list_unlink_round_trip(self) -> None:
        fake = FakeTossFetcher(
            holdings_sequence=[
                {
                    "items": [
                        {
                            "symbol": "005930",
                            "marketCountry": "KR",
                            "currency": "KRW",
                            "quantity": "10",
                            "averagePurchasePrice": "70000",
                        }
                    ]
                },
                {"items": [{"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "10"}]},
            ]
        )
        with patch(
            "src.services.portfolio_broker_sync_service.TossFetcher"
        ) as mock_cls, patch(
            "src.services.portfolio_broker_sync_service.resolve_index_stock_code",
            return_value="005930.KS",
        ):
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake

            link_resp = self.client.post(
                "/api/v1/portfolio/links/toss", json={"name": "Toss KR"}
            )
            self.assertEqual(link_resp.status_code, 200, link_resp.text)
            link_body = link_resp.json()
            self.assertEqual(link_body["imported"], 1)
            self.assertFalse(link_body["reactivated"])
            account_id = link_body["account_id"]

            list_resp = self.client.get("/api/v1/portfolio/links")
            self.assertEqual(list_resp.status_code, 200, list_resp.text)
            self.assertEqual(len(list_resp.json()["links"]), 1)

            sync_resp = self.client.post(f"/api/v1/portfolio/links/{account_id}/sync")
            self.assertEqual(sync_resp.status_code, 200, sync_resp.text)
            sync_body = sync_resp.json()
            self.assertEqual(sync_body["imported"], 0)
            self.assertEqual(sync_body["drift"], [])
            self.assertEqual(sync_body["failed"], [])

            delete_resp = self.client.delete(f"/api/v1/portfolio/links/{account_id}")
            self.assertEqual(delete_resp.status_code, 200, delete_resp.text)

            list_after_resp = self.client.get("/api/v1/portfolio/links")
            self.assertEqual(list_after_resp.json()["links"], [])

            # Account and its opening trade must survive the unlink.
            accounts_resp = self.client.get("/api/v1/portfolio/accounts")
            self.assertEqual(len(accounts_resp.json()["accounts"]), 1)

    def test_link_toss_ignores_unknown_account_id_field(self) -> None:
        """The request schema no longer has an account_id field (Codex major
        3: it let a caller re-link the KRW opening snapshot onto an
        unrelated non-KR/KRW account). A client still sending it must be
        silently ignored, not honored."""
        fake = FakeTossFetcher(holdings_sequence=[{"items": []}])
        with patch("src.services.portfolio_broker_sync_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake
            resp = self.client.post(
                "/api/v1/portfolio/links/toss", json={"account_id": 999999}
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        # A brand-new account was created; the bogus account_id=999999 (which
        # does not even exist) was never consulted.
        self.assertNotEqual(resp.json()["account_id"], 999999)

    def test_link_toss_ambiguous_accounts_returns_400_with_detail(self) -> None:
        fake = FakeTossFetcher(
            accounts=[
                {"accountNo": "111", "accountSeq": 1, "accountType": "BROKERAGE"},
                {"accountNo": "222", "accountSeq": 2, "accountType": "BROKERAGE"},
            ]
        )
        with patch("src.services.portfolio_broker_sync_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake
            resp = self.client.post("/api/v1/portfolio/links/toss", json={})
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertEqual(body.get("error"), "toss_account_ambiguous")
        self.assertEqual(len(body.get("detail", {}).get("accounts", [])), 2)

    def test_sync_missing_link_returns_404(self) -> None:
        with patch.object(TossFetcher, "has_configured_credentials", return_value=True):
            resp = self.client.post("/api/v1/portfolio/links/999999/sync")
        self.assertEqual(resp.status_code, 404, resp.text)


class TestTossPortfolioBrokerSyncNetworkSmoke(unittest.TestCase):
    """Live smoke against the real Toss OpenAPI. Requires TOSS_CLIENT_ID/
    TOSS_CLIENT_SECRET plus an IP already allow-listed in Toss WTS (ADR 0003) —
    skipped otherwise. Read-only (accounts + holdings only, no order calls).
    Not run by CI or by `pytest -m "not network"`."""

    @pytest.mark.network
    def test_live_accounts_and_holdings(self) -> None:
        if not TossFetcher.has_configured_credentials():
            pytest.skip(
                "Toss 실측 스모크 스킵: TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 자격증명 + 허용 IP 필요"
            )
        fetcher = TossFetcher()
        accounts = fetcher.get_accounts()
        self.assertIsInstance(accounts, list)
        if not accounts:
            self.skipTest("Toss 실측 스모크 스킵: 계좌 없음")
        account_seq = accounts[0].get("accountSeq")
        holdings = fetcher.get_holdings(account_seq)
        self.assertIsInstance(holdings, dict)


if __name__ == "__main__":
    unittest.main()

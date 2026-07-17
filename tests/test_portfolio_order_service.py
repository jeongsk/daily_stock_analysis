# -*- coding: utf-8 -*-
"""Offline tests for Toss Invest manual-approval order proposals (Phase 3).

Covers the v2 design spec's §6 offline list:
docs/superpowers/specs/2026-07-17-toss-order-phase3-design.md (v2 — redesigned
after a BLOCK-verdict independent review of the v1 implementation: 8 blocker +
6 major findings, mostly missing atomicity and an incomplete state machine).

- dry-run default invariant: TOSS_ORDER_LIVE unset means place_order is never
  called by either the service or (separately, in test_toss_fetcher.py) the
  fetcher itself, at the mock/HTTP boundary respectively.
- state machine v2: pending -> executing -> executed/failed/outcome_unknown;
  pending -> canceled/expired/dry_run_executed.
- limits: per-order cap, daily cap (now a live in-transaction sum over
  PortfolioOrderProposal, dry-run excluded), and the unconditional
  >=100,000,000 KRW hard reject — enforced atomically inside the
  executing-claim transaction.
- FX fail-closed: a non-KRW order's KRW estimate is refused when the FX rate
  is stale or the 1:1 fallback.
- fault-injection: POST response lost / orderId missing / request-in-progress
  / a DB write failure right after a successful POST all resolve to
  outcome_unknown, resolved later by reconcile_proposal.
- concurrency: barrier-forced parallel execute of the same proposal claims
  exactly once; parallel execute of different proposals serializes the daily
  cap; parallel create serializes the pending-proposal cap.
- audit log append-only SQLite trigger (UPDATE/DELETE rejected at the DB
  level, not just by API absence).
- cancel-a-pending-proposal vs cancel-an-already-placed-order (two distinct
  actions; the latter requires a self-issued audit trail and refuses while
  executing/outcome_unknown).
- account eligibility (active account + active toss link only — v3 removed
  the self-asserted owner-id header/comparison entirely, see reviewer
  re-review major 2).

TossFetcher is always faked here — this suite makes no real HTTP calls.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
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
from data_provider.toss_fetcher import TossOrderRejectedError
from sqlalchemy.exc import IntegrityError
from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_broker_sync_service import BrokerLinkNotFoundError, TossNotConfiguredError
from src.services.portfolio_order_service import (
    ConfirmRequiredError,
    FxRateUnavailableError,
    HighValueOrderRejectedError,
    InsufficientBuyingPowerError,
    InsufficientSellableQuantityError,
    OrderAuditPersistFailedError,
    OrderIdempotencyConflictError,
    OrderLimitExceededError,
    OrderNotFoundError,
    OrderTypeNotAllowedError,
    PendingProposalLimitExceededError,
    PortfolioOrderService,
    ProposalInProgressError,
    ProposalNotExecutableError,
    ProposalNotFoundError,
    ProposalNotReconcilableError,
    ReferencePriceUnavailableError,
)
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager, PortfolioFxRate, PortfolioOrderAudit
from sqlalchemy import select


@pytest.fixture(autouse=True)
def _block_unmocked_network(request):
    """Design spec §6 global network-ban fixture, scoped to this suite: the
    service under test is faked via ``FakeOrderTossFetcher`` throughout, so
    no test here should ever reach a real socket. See the identical fixture
    in tests/test_toss_fetcher.py for the fetcher-level HTTP tests."""
    if request.node.get_closest_marker("network"):
        yield
        return
    original_connect = socket.socket.connect

    def _guarded_connect(self, address, *a, **kw):
        raise AssertionError(
            f"Unexpected real network connection attempt to {address!r} in "
            f"tests/test_portfolio_order_service.py — this suite must run fully offline"
        )

    socket.socket.connect = _guarded_connect
    try:
        yield
    finally:
        socket.socket.connect = original_connect


class FakeOrderTossFetcher:
    """Deterministic stand-in for TossFetcher's order-info/write calls.

    ``place_order_results``/``place_order_raises`` (if given) are consumed
    one-per-call, in order — this is what lets a test simulate reconcile's
    "same clientOrderId re-POSTed" flow (first call is ambiguous/lost,
    second call — via reconcile — succeeds).
    """

    def __init__(
        self,
        *,
        buying_power: float = 10_000_000.0,
        sellable_quantity: float = 100.0,
        place_order_result: Optional[Dict[str, Any]] = None,
        place_order_raise: Optional[Exception] = None,
        place_order_sequence: Optional[List[Any]] = None,
        cancel_order_result: Optional[Dict[str, Any]] = None,
        cancel_order_raise: Optional[Exception] = None,
        get_order_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.buying_power = buying_power
        self.sellable_quantity = sellable_quantity
        self._place_order_result = place_order_result or {"orderId": "toss-order-1", "clientOrderId": None}
        self._place_order_raise = place_order_raise
        self._place_order_sequence = list(place_order_sequence) if place_order_sequence else None
        self._cancel_order_result = cancel_order_result or {"orderId": "toss-cancel-op-1"}
        self._cancel_order_raise = cancel_order_raise
        self._get_order_result = get_order_result or {"orderId": "toss-order-1", "status": "FILLED"}
        self.place_order_calls: List[Any] = []
        self.cancel_order_calls: List[Any] = []
        self.get_order_calls: List[Any] = []
        self.get_buying_power_calls: List[Any] = []
        self.get_sellable_quantity_calls: List[Any] = []
        self._lock = threading.Lock()

    def get_buying_power(self, account_seq: Any, currency: str) -> float:
        self.get_buying_power_calls.append((account_seq, currency))
        return self.buying_power

    def get_sellable_quantity(self, account_seq: Any, symbol: str) -> float:
        self.get_sellable_quantity_calls.append((account_seq, symbol))
        return self.sellable_quantity

    def place_order(self, account_seq: Any, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            self.place_order_calls.append((account_seq, kwargs))
            if self._place_order_sequence:
                item = self._place_order_sequence.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        if self._place_order_raise is not None:
            raise self._place_order_raise
        return self._place_order_result

    def cancel_order(self, account_seq: Any, order_id: str) -> Dict[str, Any]:
        self.cancel_order_calls.append((account_seq, order_id))
        if self._cancel_order_raise is not None:
            raise self._cancel_order_raise
        return self._cancel_order_result

    def get_order(self, account_seq: Any, order_id: str) -> Dict[str, Any]:
        self.get_order_calls.append((account_seq, order_id))
        return self._get_order_result


def _run_concurrently(funcs: List[Any]) -> List[Any]:
    """Run each zero-arg callable in its own thread, released simultaneously
    by a shared Barrier (design spec §6 "barrier 기반 동시성"). Returns a list
    of ``("ok", value)``/``("error", exc)`` tuples in the same order as
    ``funcs``."""
    barrier = threading.Barrier(len(funcs))
    results: List[Any] = [None] * len(funcs)

    def _wrapper(index: int, fn: Any) -> None:
        barrier.wait()
        try:
            results[index] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001 - capturing for assertions
            results[index] = ("error", exc)

    threads = [threading.Thread(target=_wrapper, args=(i, fn)) for i, fn in enumerate(funcs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return results


class _PortfolioOrderTestBase(unittest.TestCase):
    """Shared setUp/tearDown/helpers only — deliberately holds no ``test_*``
    methods itself so that the concurrency/reconcile/append-only test
    classes below can reuse this fixture via inheritance without also
    re-running every case in ``PortfolioOrderServiceTestCase`` a second
    (third, fourth...) time."""
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"
        self.db_path = Path(self.temp_dir.name) / "portfolio_order_test.db"
        self._write_env()
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()

        self.db = DatabaseManager.get_instance()
        self.portfolio_service = PortfolioService()
        self.repo = PortfolioRepository()

        created = self.portfolio_service.create_account(
            name="Toss KR", broker="toss", market="kr", base_currency="KRW"
        )
        self.account_id = int(created["id"])
        self.repo.create_broker_link(
            account_id=self.account_id,
            provider="toss",
            external_account_seq="555",
            external_account_no="1234567890",
            linked_at=self._epoch(),
            snapshot_boundary_at=self._epoch(),
            last_synced_at=self._epoch(),
            active=True,
        )

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("TOSS_ORDER_LIVE", None)
        os.environ.pop("TOSS_ORDER_ALLOW_MARKET", None)
        os.environ.pop("TOSS_ORDER_MAX_AMOUNT_KRW", None)
        os.environ.pop("TOSS_ORDER_DAILY_MAX_AMOUNT_KRW", None)
        self.temp_dir.cleanup()

    def _write_env(self, extra_lines: Optional[List[str]] = None) -> None:
        lines = [
            "STOCK_LIST=600519",
            "GEMINI_API_KEY=test",
            "ADMIN_AUTH_ENABLED=false",
            f"DATABASE_PATH={self.db_path}",
        ]
        lines.extend(extra_lines or [])
        self.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _epoch():
        return datetime(2026, 1, 1, 0, 0, 0)

    def _make_service(self, fetcher: FakeOrderTossFetcher) -> PortfolioOrderService:
        return PortfolioOrderService(
            portfolio_service=self.portfolio_service,
            repo=self.repo,
            fetcher=fetcher,
        )

    def _set_live(self, live: bool) -> None:
        Config.reset_instance()
        if live:
            os.environ["TOSS_ORDER_LIVE"] = "true"
        else:
            os.environ.pop("TOSS_ORDER_LIVE", None)
        Config.reset_instance()

class PortfolioOrderServiceTestCase(_PortfolioOrderTestBase):
    # ------------------------------------------------------------------
    # Dry-run default invariant (design spec §6 mock-level assertion)
    # ------------------------------------------------------------------
    def test_dry_run_default_never_calls_fetcher_place_order(self) -> None:
        self._set_live(False)
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)

        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        self.assertEqual(proposal["mode"], "dry_run")
        self.assertEqual(proposal["status"], "pending")

        result = service.execute_proposal(
            account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True
        )

        self.assertEqual(fetcher.place_order_calls, [])
        self.assertEqual(result["status"], "dry_run_executed")
        self.assertEqual(result["mode"], "dry_run")
        self.assertIsNone(result["toss_order_id"])

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        events = [a.event for a in audits]
        self.assertEqual(events, ["proposed", "executed"])
        self.assertEqual(audits[-1].mode, "dry_run")

    def test_confirm_required_even_in_dry_run(self) -> None:
        self._set_live(False)
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        with self.assertRaises(ConfirmRequiredError):
            service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=False)
        self.assertEqual(fetcher.place_order_calls, [])

    # ------------------------------------------------------------------
    # Live mode: pending -> executing -> executed
    # ------------------------------------------------------------------
    def test_live_mode_calls_place_order_and_persists_toss_order_id(self) -> None:
        self._set_live(True)
        fetcher = FakeOrderTossFetcher(place_order_result={"orderId": "toss-order-42"})
        service = self._make_service(fetcher)

        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        self.assertEqual(proposal["mode"], "live")

        result = service.execute_proposal(
            account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True
        )
        self.assertEqual(len(fetcher.place_order_calls), 1)
        _account_seq, kwargs = fetcher.place_order_calls[0]
        self.assertEqual(kwargs["symbol"], "005930")
        self.assertEqual(kwargs["side"], "BUY")
        self.assertEqual(kwargs["order_type"], "LIMIT")
        self.assertEqual(kwargs["client_order_id"], f"dsa-{proposal['proposal_uuid']}")

        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["toss_order_id"], "toss-order-42")

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual([a.event for a in audits], ["proposed", "executing", "executed"])
        self.assertEqual(audits[-1].mode, "live")
        self.assertEqual(audits[-1].toss_order_id, "toss-order-42")

    def test_idempotent_execute_retry_returns_cached_result_without_second_toss_call(self) -> None:
        self._set_live(True)
        fetcher = FakeOrderTossFetcher(place_order_result={"orderId": "toss-order-7"})
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        first = service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        second = service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        self.assertEqual(len(fetcher.place_order_calls), 1)
        self.assertEqual(first["toss_order_id"], second["toss_order_id"])
        self.assertEqual(second["status"], "executed")

    def test_toss_rejection_marks_proposal_failed_with_audit(self) -> None:
        self._set_live(True)
        rejection = TossOrderRejectedError(
            status_code=422, code="insufficient-buying-power", message="주문 가능 금액이 부족합니다."
        )
        fetcher = FakeOrderTossFetcher(place_order_raise=rejection)
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        with self.assertRaises(TossOrderRejectedError):
            service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        stored = self.repo.get_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "failed")
        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual([a.event for a in audits], ["proposed", "executing", "rejected"])
        self.assertEqual(audits[-1].error_code, "insufficient-buying-power")

        # A failed proposal can no longer be executed again.
        with self.assertRaises(ProposalNotExecutableError):
            service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

    # ------------------------------------------------------------------
    # LIMIT-only default / MARKET opt-in
    # ------------------------------------------------------------------
    def test_market_order_rejected_without_optin(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(OrderTypeNotAllowedError):
            service.create_proposal(
                account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, order_type="MARKET"
            )

    def test_market_order_allowed_with_optin_uses_reference_price(self) -> None:
        os.environ["TOSS_ORDER_ALLOW_MARKET"] = "true"
        Config.reset_instance()
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        fake_quote = MagicMock(price=71000.0)
        with patch("data_provider.base.DataFetcherManager") as mock_cls:
            mock_cls.return_value.get_realtime_quote.return_value = fake_quote
            proposal = service.create_proposal(
                account_id=self.account_id, symbol="005930.KS", side="buy", quantity=2, order_type="MARKET"
            )
        self.assertEqual(proposal["order_type"], "MARKET")
        self.assertIsNone(proposal["price"])
        self.assertAlmostEqual(proposal["est_amount_krw"], 142000.0, places=2)

    def test_market_order_reference_price_unavailable_fails_closed(self) -> None:
        os.environ["TOSS_ORDER_ALLOW_MARKET"] = "true"
        Config.reset_instance()
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with patch("data_provider.base.DataFetcherManager") as mock_cls:
            mock_cls.return_value.get_realtime_quote.return_value = None
            with self.assertRaises(ReferencePriceUnavailableError):
                service.create_proposal(
                    account_id=self.account_id, symbol="005930.KS", side="buy", quantity=2, order_type="MARKET"
                )

    # ------------------------------------------------------------------
    # FX fail-closed (design spec v2 §3, Codex blocker 4)
    # ------------------------------------------------------------------
    def test_fx_stale_rate_rejects_non_krw_order(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with patch.object(
            self.portfolio_service, "convert_amount", return_value=(1_000_000.0, True, "direct_rate")
        ):
            with self.assertRaises(FxRateUnavailableError):
                service.create_proposal(account_id=self.account_id, symbol="AAPL", side="buy", quantity=1, price=150)

    def test_fx_fallback_1_to_1_rejects_non_krw_order(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with patch.object(
            self.portfolio_service, "convert_amount", return_value=(150.0, False, "fallback_1_to_1")
        ):
            with self.assertRaises(FxRateUnavailableError):
                service.create_proposal(account_id=self.account_id, symbol="AAPL", side="buy", quantity=1, price=150)

    def test_fx_fresh_direct_rate_allows_non_krw_order(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        # A real, freshly-written rate row must exist: the fail-closed
        # wall-clock check refuses direct_rate conversions it cannot verify
        # against an actual PortfolioFxRate row (3rd-review residual).
        self.repo.save_fx_rate(from_currency="USD", to_currency="KRW", rate_date=date.today(), rate=1350.0)
        with patch.object(
            self.portfolio_service, "convert_amount", return_value=(200_000.0, False, "direct_rate")
        ):
            proposal = service.create_proposal(account_id=self.account_id, symbol="AAPL", side="buy", quantity=1, price=150)
        self.assertEqual(proposal["est_amount_krw"], 200_000.0)

    def test_fx_wall_clock_stale_real_db_record_rejects_non_krw_order(self) -> None:
        """Design spec v3 FX "stale(24시간 초과)" / reviewer re-review major 1:
        a *real* ``PortfolioFxRate`` row with ``is_stale=False`` but an
        ``updated_at`` more than 24 hours old must still fail closed — this
        is deliberately not mocked at the ``convert_amount`` level (unlike
        the tests above) so it exercises the actual repo-backed wall-clock
        age check, not just the is_stale/fallback flags."""
        self.repo.save_fx_rate(
            from_currency="USD",
            to_currency="KRW",
            rate_date=date.today(),
            rate=1350.0,
            source="test",
            is_stale=False,
        )
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioFxRate).where(
                    PortfolioFxRate.from_currency == "USD",
                    PortfolioFxRate.to_currency == "KRW",
                )
            ).scalar_one()
            row.updated_at = datetime.now() - timedelta(hours=25)
            session.commit()

        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(FxRateUnavailableError):
            service.create_proposal(account_id=self.account_id, symbol="AAPL", side="buy", quantity=1, price=150)

    def test_fx_wall_clock_fresh_real_db_record_allows_non_krw_order(self) -> None:
        """Counterpart to the staleness test above: a real, recently-updated
        FX row (well under 24 hours old) must not be rejected by the
        wall-clock check."""
        self.repo.save_fx_rate(
            from_currency="USD",
            to_currency="KRW",
            rate_date=date.today(),
            rate=1350.0,
            source="test",
            is_stale=False,
        )
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(account_id=self.account_id, symbol="AAPL", side="buy", quantity=1, price=150)
        self.assertAlmostEqual(proposal["est_amount_krw"], 150 * 1350.0, places=2)

    def test_fx_identity_krw_order_never_checked(self) -> None:
        # KRW->KRW never touches the stale/fallback check at all.
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        self.assertEqual(proposal["currency"], "KRW")

    # ------------------------------------------------------------------
    # Amount caps
    # ------------------------------------------------------------------
    def test_per_order_cap_rejected(self) -> None:
        os.environ["TOSS_ORDER_MAX_AMOUNT_KRW"] = "500000"
        Config.reset_instance()
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(OrderLimitExceededError) as ctx:
            service.create_proposal(
                account_id=self.account_id, symbol="005930.KS", side="buy", quantity=10, price=70000
            )
        self.assertEqual(ctx.exception.limit_type, "per_order")

    def test_per_order_cap_rejected_atomically_at_execute_time(self) -> None:
        """The per-order/high-value/daily caps are re-checked *inside* the
        atomic executing-claim at execute time too, not only at create time —
        simulate a cap that tightens between create and execute."""
        self._set_live(True)
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=10, price=70000
        )
        os.environ["TOSS_ORDER_MAX_AMOUNT_KRW"] = "500000"
        Config.reset_instance()
        service2 = self._make_service(fetcher)
        with self.assertRaises(OrderLimitExceededError) as ctx:
            service2.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(ctx.exception.limit_type, "per_order")
        stored = self.repo.get_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "failed")
        self.assertEqual(fetcher.place_order_calls, [])

    def test_daily_cap_rejected_after_prior_live_execution_dry_run_excluded(self) -> None:
        # Dry-run executions must not count toward the daily cap at all —
        # run several that would blow the cap if (incorrectly) counted.
        self._set_live(False)
        os.environ["TOSS_ORDER_DAILY_MAX_AMOUNT_KRW"] = "1000000"
        Config.reset_instance()
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        for _ in range(3):
            proposal = service.create_proposal(
                account_id=self.account_id, symbol="005930.KS", side="buy", quantity=10, price=70000
            )
            service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        # Now go live: first live order (700,000 KRW) fits under the 1,000,000 cap.
        self._set_live(True)
        os.environ["TOSS_ORDER_DAILY_MAX_AMOUNT_KRW"] = "1000000"
        Config.reset_instance()
        fetcher_live = FakeOrderTossFetcher(place_order_result={"orderId": "toss-order-a"})
        service_live = self._make_service(fetcher_live)
        first = service_live.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=10, price=70000
        )
        service_live.execute_proposal(account_id=self.account_id, proposal_uuid=first["proposal_uuid"], confirm=True)

        # A second live order that would push the daily total over 1,000,000 is rejected.
        with self.assertRaises(OrderLimitExceededError) as ctx:
            service_live.create_proposal(
                account_id=self.account_id, symbol="005930.KS", side="buy", quantity=10, price=70000
            )
        self.assertEqual(ctx.exception.limit_type, "daily")

    def test_high_value_order_hard_rejected_regardless_of_config(self) -> None:
        os.environ["TOSS_ORDER_MAX_AMOUNT_KRW"] = "999999999999"
        os.environ["TOSS_ORDER_DAILY_MAX_AMOUNT_KRW"] = "999999999999"
        Config.reset_instance()
        fetcher = FakeOrderTossFetcher(buying_power=999_999_999_999.0)
        service = self._make_service(fetcher)
        with self.assertRaises(HighValueOrderRejectedError):
            service.create_proposal(
                account_id=self.account_id, symbol="005930.KS", side="buy", quantity=2000, price=70000
            )

    # ------------------------------------------------------------------
    # TTL expiry
    # ------------------------------------------------------------------
    def test_ttl_expiry_makes_execute_fail_and_transitions_to_expired(self) -> None:
        from src.services.portfolio_order_service import _now_kst_naive as real_now_kst_naive

        self._set_live(False)
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        created_at = real_now_kst_naive()
        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )

        future = created_at + timedelta(minutes=11)
        with patch("src.services.portfolio_order_service._now_kst_naive", return_value=future):
            with self.assertRaises(ProposalNotExecutableError) as ctx:
                service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertIn("expired", str(ctx.exception))

        stored = self.repo.get_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "expired")
        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual([a.event for a in audits], ["proposed", "expired"])
        self.assertEqual(fetcher.place_order_calls, [])

    # ------------------------------------------------------------------
    # Cancel: proposal-cancel vs already-placed-order-cancel
    # ------------------------------------------------------------------
    def test_cancel_proposal_before_execute(self) -> None:
        self._set_live(False)
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        result = service.cancel_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(result["status"], "canceled")

        with self.assertRaises(ProposalNotExecutableError):
            service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

    def test_cancel_order_requires_self_issued_audit_trail(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(OrderNotFoundError):
            service.cancel_order(account_id=self.account_id, toss_order_id="not-a-real-order")
        self.assertEqual(fetcher.cancel_order_calls, [])

    def test_cancel_order_success_appends_audit_without_changing_proposal_status(self) -> None:
        self._set_live(True)
        fetcher = FakeOrderTossFetcher(place_order_result={"orderId": "toss-order-99"})
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        result = service.cancel_order(account_id=self.account_id, toss_order_id="toss-order-99")
        self.assertEqual(result["toss_order_id"], "toss-order-99")
        self.assertTrue(result["canceled"])
        self.assertEqual(len(fetcher.cancel_order_calls), 1)

        stored = self.repo.get_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "executed")  # unchanged — cancel != un-execute

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual([a.event for a in audits], ["proposed", "executing", "executed", "canceled"])

    # ------------------------------------------------------------------
    # Pre-checks: buying power / sellable quantity / pending cap / not-found
    # ------------------------------------------------------------------
    def test_insufficient_buying_power_rejected_at_proposal(self) -> None:
        fetcher = FakeOrderTossFetcher(buying_power=0.0)
        service = self._make_service(fetcher)
        with self.assertRaises(InsufficientBuyingPowerError):
            service.create_proposal(
                account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
            )

    def test_insufficient_sellable_quantity_rejected_at_proposal(self) -> None:
        fetcher = FakeOrderTossFetcher(sellable_quantity=0.0)
        service = self._make_service(fetcher)
        with self.assertRaises(InsufficientSellableQuantityError):
            service.create_proposal(
                account_id=self.account_id, symbol="005930.KS", side="sell", quantity=1, price=70000
            )

    def test_pending_proposal_cap_of_ten(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        for _ in range(10):
            service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)
        with self.assertRaises(PendingProposalLimitExceededError):
            service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)

    def test_broker_link_not_found_raises(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(BrokerLinkNotFoundError):
            service.create_proposal(account_id=999999, symbol="005930.KS", side="buy", quantity=1, price=70000)

    def test_not_configured_raises_before_any_call(self) -> None:
        service = PortfolioOrderService(portfolio_service=self.portfolio_service, repo=self.repo)
        with patch("data_provider.toss_fetcher.TossFetcher.has_configured_credentials", return_value=False):
            with self.assertRaises(TossNotConfiguredError):
                service.create_proposal(
                    account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
                )

    # ------------------------------------------------------------------
    # Account eligibility: active account + active toss link + owner match
    # (design spec §3, Codex major 1)
    # ------------------------------------------------------------------
    def test_inactive_account_is_ineligible(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        self.portfolio_service.repo.deactivate_account(self.account_id)
        with self.assertRaises(BrokerLinkNotFoundError):
            service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)

    def test_non_toss_provider_link_is_ineligible(self) -> None:
        created = self.portfolio_service.create_account(name="Other Broker", broker="other", market="kr", base_currency="KRW")
        other_account_id = int(created["id"])
        self.repo.create_broker_link(
            account_id=other_account_id,
            provider="other-broker",
            external_account_seq="999",
            external_account_no="000",
            linked_at=self._epoch(),
            snapshot_boundary_at=self._epoch(),
            last_synced_at=self._epoch(),
            active=True,
        )
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(BrokerLinkNotFoundError):
            service.create_proposal(account_id=other_account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)

    def test_owner_id_set_does_not_gate_eligibility(self) -> None:
        """v3 (design spec §3 "인증 (필수, v3 명확화)", reviewer re-review major
        2): the service no longer accepts or compares any caller-identity
        argument at all — an account with ``owner_id`` set is exactly as
        reachable as one without; a verified caller manages every account in
        this single-admin system."""
        created = self.portfolio_service.create_account(
            name="Owned Account", broker="toss", market="kr", base_currency="KRW", owner_id="alice"
        )
        owned_account_id = int(created["id"])
        self.repo.create_broker_link(
            account_id=owned_account_id,
            provider="toss",
            external_account_seq="777",
            external_account_no="0007",
            linked_at=self._epoch(),
            snapshot_boundary_at=self._epoch(),
            last_synced_at=self._epoch(),
            active=True,
        )
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=owned_account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        self.assertEqual(proposal["status"], "pending")

    # ------------------------------------------------------------------
    # Symbol resolution (KR 6-digit / suffixed / US ticker)
    # ------------------------------------------------------------------
    def test_symbol_resolution_bare_kr_code_and_suffixed_agree(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with patch(
            "src.services.portfolio_order_service.resolve_index_stock_code",
            return_value="005930.KS",
        ):
            bare = service.create_proposal(
                account_id=self.account_id, symbol="005930", side="buy", quantity=1, price=70000
            )
        suffixed = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        self.assertEqual(bare["symbol"], "005930")
        self.assertEqual(bare["storage_symbol"], "005930.KS")
        self.assertEqual(suffixed["symbol"], "005930")
        self.assertEqual(suffixed["storage_symbol"], "005930.KS")

    def test_symbol_resolution_us_ticker(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        self.repo.save_fx_rate(from_currency="USD", to_currency="KRW", rate_date=date.today(), rate=1350.0)
        with patch.object(self.portfolio_service, "convert_amount", return_value=(150.0, False, "direct_rate")):
            proposal = service.create_proposal(account_id=self.account_id, symbol="AAPL", side="buy", quantity=1, price=150)
        self.assertEqual(proposal["symbol"], "AAPL")
        self.assertEqual(proposal["market"], "us")
        self.assertEqual(proposal["currency"], "USD")

    def test_unsupported_symbol_raises_value_error(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(ValueError):
            service.create_proposal(account_id=self.account_id, symbol="???", side="buy", quantity=1, price=70000)

    # ------------------------------------------------------------------
    # LIMIT field validation
    # ------------------------------------------------------------------
    def test_limit_order_requires_price(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(ValueError):
            service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1)

    def test_invalid_side_raises_value_error(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(ValueError):
            service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="hold", quantity=1, price=70000)

    def test_execute_missing_proposal_raises_not_found(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        with self.assertRaises(ProposalNotFoundError):
            service.execute_proposal(account_id=self.account_id, proposal_uuid="does-not-exist", confirm=True)

    def test_list_proposals_filters_by_status(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)
        proposal2 = service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)
        service.cancel_proposal(account_id=self.account_id, proposal_uuid=proposal2["proposal_uuid"])

        pending = service.list_proposals(account_id=self.account_id, status="pending")
        canceled = service.list_proposals(account_id=self.account_id, status="canceled")
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(canceled), 1)


class PortfolioOrderOutcomeAndReconcileTestCase(_PortfolioOrderTestBase):
    """Fault-injection + reconcile (design spec v2 §3/§6): every ambiguous
    live-POST outcome resolves to 'outcome_unknown', and reconcile_proposal
    is what converges it to a real terminal state via Toss's own
    clientOrderId idempotency. Reuses the same fixture/setUp as the base
    class (subclassing purely to share setUp/tearDown/helpers, not to
    re-run the base class's own tests — see ``test_*`` methods below only)."""

    def _make_pending_live_proposal(self, fetcher: FakeOrderTossFetcher):
        self._set_live(True)
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        return service, proposal

    def test_missing_order_id_resolves_to_outcome_unknown(self) -> None:
        fetcher = FakeOrderTossFetcher(place_order_result={"clientOrderId": "dsa-x"})  # no orderId
        service, proposal = self._make_pending_live_proposal(fetcher)
        result = service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result["status"], "outcome_unknown")
        stored = self.repo.get_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "outcome_unknown")

    def test_network_failure_after_claim_resolves_to_outcome_unknown(self) -> None:
        fetcher = FakeOrderTossFetcher(place_order_raise=DataFetchError("connection reset"))
        service, proposal = self._make_pending_live_proposal(fetcher)
        result = service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result["status"], "outcome_unknown")

    def test_request_in_progress_resolves_to_outcome_unknown_not_failed(self) -> None:
        rejection = TossOrderRejectedError(status_code=409, code="request-in-progress", message="처리 중입니다.")
        fetcher = FakeOrderTossFetcher(place_order_raise=rejection)
        service, proposal = self._make_pending_live_proposal(fetcher)
        result = service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result["status"], "outcome_unknown")

    def test_outcome_unknown_still_counts_toward_daily_cap(self) -> None:
        os.environ["TOSS_ORDER_DAILY_MAX_AMOUNT_KRW"] = "100000"
        Config.reset_instance()
        fetcher = FakeOrderTossFetcher(place_order_result={"clientOrderId": "dsa-x"})  # no orderId -> outcome_unknown
        self._set_live(True)
        service = self._make_service(fetcher)
        proposal = service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)
        result = service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result["status"], "outcome_unknown")

        with self.assertRaises(OrderLimitExceededError) as ctx:
            service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)
        self.assertEqual(ctx.exception.limit_type, "daily")

    def test_reconcile_resolves_outcome_unknown_to_executed(self) -> None:
        fetcher = FakeOrderTossFetcher(
            place_order_sequence=[
                {"clientOrderId": "dsa-x"},  # first attempt: no orderId -> outcome_unknown
                {"orderId": "toss-order-recovered"},  # reconcile re-POST: Toss returns the real order
            ]
        )
        service, proposal = self._make_pending_live_proposal(fetcher)
        first = service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(first["status"], "outcome_unknown")

        resolved = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(resolved["status"], "executed")
        self.assertEqual(resolved["toss_order_id"], "toss-order-recovered")
        self.assertEqual(len(fetcher.place_order_calls), 2)
        for _account_seq, kwargs in fetcher.place_order_calls:
            self.assertEqual(kwargs["client_order_id"], f"dsa-{proposal['proposal_uuid']}")

    def test_reconcile_request_in_progress_stays_outcome_unknown(self) -> None:
        fetcher = FakeOrderTossFetcher(
            place_order_sequence=[
                DataFetchError("timeout"),
                TossOrderRejectedError(status_code=409, code="request-in-progress", message="처리 중입니다."),
            ]
        )
        service, proposal = self._make_pending_live_proposal(fetcher)
        service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        resolved = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(resolved["status"], "outcome_unknown")

    def test_reconcile_idempotency_key_conflict_surfaces_defect(self) -> None:
        fetcher = FakeOrderTossFetcher(
            place_order_sequence=[
                DataFetchError("timeout"),
                TossOrderRejectedError(status_code=422, code="idempotency-key-conflict", message="충돌"),
            ]
        )
        service, proposal = self._make_pending_live_proposal(fetcher)
        service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        with self.assertRaises(OrderIdempotencyConflictError):
            service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        stored = self.repo.get_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "outcome_unknown")

    def test_reconcile_explicit_rejection_resolves_to_failed(self) -> None:
        fetcher = FakeOrderTossFetcher(
            place_order_sequence=[
                DataFetchError("timeout"),
                TossOrderRejectedError(status_code=400, code="order-hours-closed", message="장 마감"),
            ]
        )
        service, proposal = self._make_pending_live_proposal(fetcher)
        service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        resolved = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(resolved["status"], "failed")

    def test_reconcile_rejects_non_ambiguous_proposal(self) -> None:
        self._set_live(False)
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)
        with self.assertRaises(ProposalNotReconcilableError):
            service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])

    def test_execute_again_while_outcome_unknown_raises_in_progress(self) -> None:
        fetcher = FakeOrderTossFetcher(place_order_result={"clientOrderId": "dsa-x"})
        service, proposal = self._make_pending_live_proposal(fetcher)
        service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        with self.assertRaises(ProposalInProgressError):
            service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

    def test_cancel_order_blocked_while_outcome_unknown_reconcile_first(self) -> None:
        fetcher = FakeOrderTossFetcher(
            place_order_sequence=[
                {"orderId": "toss-order-later-known"},
            ]
        )
        self._set_live(True)
        service = self._make_service(fetcher)
        proposal = service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)
        service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        with self.assertRaises(OrderNotFoundError):
            # No toss_order_id was ever recorded for this proposal (it's
            # 'executed' via the normal path in this particular test setup,
            # not outcome_unknown) — this asserts the *lookup* path; the
            # dedicated in-progress block is asserted by the next test using
            # a proposal actually left outcome_unknown with a known orderId.
            service.cancel_order(account_id=self.account_id, toss_order_id="not-a-real-order")

    def test_cancel_order_blocked_when_proposal_still_executing_state(self) -> None:
        """Simulate an outcome_unknown proposal that *did* receive an
        orderId (a successful POST whose persist failed) — cancel must be
        refused in favor of reconcile."""
        fetcher = FakeOrderTossFetcher()
        self._set_live(True)
        service = self._make_service(fetcher)
        proposal = service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)

        original_transition = PortfolioRepository.transition_proposal

        def _flaky_transition(self_repo, *args, **kwargs):
            # The atomic claim (pending -> executing) goes through
            # ``claim_proposal_for_execution``, not ``transition_proposal`` —
            # so the *first* call to ``transition_proposal`` in a fresh live
            # execute is the post-POST executing -> executed resolution.
            if kwargs.get("to_status") == "executed":
                raise RuntimeError("simulated DB failure right after a successful POST")
            return original_transition(self_repo, *args, **kwargs)

        with patch.object(PortfolioRepository, "transition_proposal", _flaky_transition):
            fetcher._place_order_result = {"orderId": "toss-order-db-failed"}
            result = service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["toss_order_id"], "toss-order-db-failed")

        with self.assertRaises(ProposalNotReconcilableError):
            service.cancel_order(account_id=self.account_id, toss_order_id="toss-order-db-failed")

    def test_persistent_db_failure_after_post_raises_audit_persist_failed_and_keeps_reservation(self) -> None:
        """Design spec v3 test-gap #2: unlike the single-hiccup case above
        (where the outcome_unknown *fallback* write succeeds), a genuinely
        persistent DB outage means *both* the primary 'executed' transition
        write and its outcome_unknown fallback fail. ``execute_proposal``
        must then raise ``OrderAuditPersistFailedError`` — not swallow the
        failure or silently return a misleading result — and the proposal's
        reservation must remain exactly where the atomic claim
        (``claim_proposal_for_execution``, a separate write path that this
        patch does not touch) left it: 'executing', still counted toward
        today's daily cap, not silently dropped."""
        fetcher = FakeOrderTossFetcher(place_order_result={"orderId": "toss-order-db-down"})
        self._set_live(True)
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )

        def _always_fails(self_repo, *args, **kwargs):
            raise RuntimeError("simulated persistent DB outage")

        with patch.object(PortfolioRepository, "transition_proposal", _always_fails):
            with self.assertRaises(OrderAuditPersistFailedError):
                service.execute_proposal(
                    account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True
                )

        # The patch is no longer active — this reflects the proposal's true,
        # durably-committed state: still 'executing' (the atomic claim commit
        # via claim_proposal_for_execution succeeded before the POST; only
        # the *post*-POST resolution writes were ever patched to fail).
        stored = self.repo.get_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "executing")
        reserved = self.repo.sum_daily_reserved_and_executed_amount_krw(
            self.account_id, kst_date=stored.reserved_at.date()
        )
        self.assertGreaterEqual(reserved, stored.est_amount_krw)


class PortfolioOrderMidnightBoundaryTestCase(_PortfolioOrderTestBase):
    """Design spec v2 §3/§7 (Codex major 4): a reservation that straddles
    local midnight — reserved on one KST calendar date, confirmed executed on
    the next — must count against *both* dates' daily caps, conservatively,
    not just one. Exercised directly at the repo layer so ``reserved_at``/
    ``executed_at`` can be pinned to specific dates without depending on
    real wall-clock timing."""

    def test_reservation_crossing_midnight_counts_on_both_calendar_dates(self) -> None:
        day1 = datetime(2026, 3, 1, 23, 55, 0)
        day2 = datetime(2026, 3, 2, 0, 5, 0)

        proposal_uuid = "midnight-test-uuid"
        self.repo.create_order_proposal_with_audit(
            account_id=self.account_id,
            proposal_uuid=proposal_uuid,
            symbol="005930",
            storage_symbol="005930.KS",
            market="kr",
            currency="KRW",
            side="buy",
            order_type="LIMIT",
            price=70000.0,
            quantity=1.0,
            est_amount_krw=70000.0,
            created_at=day1,
            expires_at=day1 + timedelta(minutes=10),
            max_pending_proposals=10,
        )
        claim = self.repo.claim_proposal_for_execution(
            proposal_uuid=proposal_uuid,
            account_id=self.account_id,
            now=day1,
            est_amount_krw=70000.0,
            high_value_threshold_krw=100_000_000.0,
            per_order_cap_krw=1_000_000.0,
            daily_cap_krw=5_000_000.0,
        )
        self.assertEqual(claim.outcome, "claimed")

        # Before resolution: still 'executing' — v3 counts every
        # executing/outcome_unknown reservation on *every* date, not just its
        # own reserved_at date (reviewer re-review blocker 1: a v2
        # implementation that only counted it on day1 let it silently drop
        # out of day2's claim-time sum, letting a concurrent day2 claim spend
        # the full daily cap on top of this still-unresolved amount). So both
        # day1's and day2's sum already include it, even though day2 hasn't
        # had anything happen on it yet.
        self.assertEqual(
            self.repo.sum_daily_reserved_and_executed_amount_krw(self.account_id, kst_date=day1.date()), 70000.0
        )
        self.assertEqual(
            self.repo.sum_daily_reserved_and_executed_amount_krw(self.account_id, kst_date=day2.date()), 70000.0
        )

        # Resolves 'executed' at day2 (just after local midnight) — the
        # design spec requires counting conservatively on *both* dates now.
        self.repo.transition_proposal(
            proposal_uuid=proposal_uuid,
            account_id=self.account_id,
            now=day2,
            from_statuses={"executing"},
            to_status="executed",
            event="executed",
            mode="live",
            toss_order_id="toss-order-midnight",
            executed_at=day2,
        )

        self.assertEqual(
            self.repo.sum_daily_reserved_and_executed_amount_krw(self.account_id, kst_date=day1.date()), 70000.0
        )
        self.assertEqual(
            self.repo.sum_daily_reserved_and_executed_amount_krw(self.account_id, kst_date=day2.date()), 70000.0
        )

    def test_midnight_boundary_claim_race_day2_claim_includes_unresolved_day1_reservation(self) -> None:
        """Design spec v3 blocker 1 exact scenario: an ``executing``
        reservation claimed just before local midnight must still be
        included, in full, in a *different* proposal's daily-cap claim just
        after midnight — otherwise "day 2" could spend the entire daily cap
        on top of an amount that, in reality, is still an open, unresolved
        liability from "day 1"."""
        daily_cap = 100_000.0

        day1 = datetime(2026, 3, 1, 23, 55, 0)
        day2 = datetime(2026, 3, 2, 0, 5, 0)

        # Proposal A: claimed (executing) at day1, 23:55 — 70,000 KRW,
        # comfortably under the 100,000 cap alone. Never resolved (simulates
        # a still in-flight/ambiguous POST straddling midnight).
        proposal_a_uuid = "midnight-race-a"
        self.repo.create_order_proposal_with_audit(
            account_id=self.account_id,
            proposal_uuid=proposal_a_uuid,
            symbol="005930",
            storage_symbol="005930.KS",
            market="kr",
            currency="KRW",
            side="buy",
            order_type="LIMIT",
            price=70000.0,
            quantity=1.0,
            est_amount_krw=70000.0,
            created_at=day1,
            expires_at=day1 + timedelta(minutes=10),
            max_pending_proposals=10,
        )
        claim_a = self.repo.claim_proposal_for_execution(
            proposal_uuid=proposal_a_uuid,
            account_id=self.account_id,
            now=day1,
            est_amount_krw=70000.0,
            high_value_threshold_krw=100_000_000.0,
            per_order_cap_krw=1_000_000.0,
            daily_cap_krw=daily_cap,
        )
        self.assertEqual(claim_a.outcome, "claimed")

        # Proposal B: a *different* proposal, created and claimed at day2,
        # 00:05 — 40,000 KRW alone fits under the 100,000 daily cap, but
        # 70,000 (still-unresolved A) + 40,000 (B) = 110,000 exceeds it. A v2
        # implementation that dropped A's reservation from day2's sum (since
        # A's reserved_at is on day1) would have wrongly let B claim.
        proposal_b_uuid = "midnight-race-b"
        self.repo.create_order_proposal_with_audit(
            account_id=self.account_id,
            proposal_uuid=proposal_b_uuid,
            symbol="005930",
            storage_symbol="005930.KS",
            market="kr",
            currency="KRW",
            side="buy",
            order_type="LIMIT",
            price=40000.0,
            quantity=1.0,
            est_amount_krw=40000.0,
            created_at=day2,
            expires_at=day2 + timedelta(minutes=10),
            max_pending_proposals=10,
        )
        claim_b = self.repo.claim_proposal_for_execution(
            proposal_uuid=proposal_b_uuid,
            account_id=self.account_id,
            now=day2,
            est_amount_krw=40000.0,
            high_value_threshold_krw=100_000_000.0,
            per_order_cap_krw=1_000_000.0,
            daily_cap_krw=daily_cap,
        )

        self.assertEqual(claim_b.outcome, "rejected")
        self.assertEqual(claim_b.limit_type, "daily")
        stored_b = self.repo.get_order_proposal(proposal_b_uuid, account_id=self.account_id)
        self.assertEqual(stored_b.status, "failed")


class PortfolioOrderAuditAppendOnlyTestCase(_PortfolioOrderTestBase):
    """SQLite-level append-only enforcement (design spec v2 §7, Codex major
    finding: append-only had previously been API-only, not DB-enforced)."""

    def test_update_and_delete_are_rejected_by_the_database(self) -> None:
        fetcher = FakeOrderTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)
        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertGreaterEqual(len(audits), 1)

        with self.db.get_session() as session:
            row = session.get(PortfolioOrderAudit, audits[0].id)
            row.event = "tampered"
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()

        with self.db.get_session() as session:
            row = session.get(PortfolioOrderAudit, audits[0].id)
            session.delete(row)
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()


class PortfolioOrderConcurrencyTestCase(_PortfolioOrderTestBase):
    """Barrier-forced concurrency (design spec v2 §6): real thread
    contention against a temp-file SQLite DB, not a mocked lock."""

    def test_parallel_execute_of_same_proposal_claims_exactly_once(self) -> None:
        self._set_live(True)
        fetcher = FakeOrderTossFetcher(place_order_result={"orderId": "toss-order-race"})
        service = self._make_service(fetcher)
        proposal = service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)

        def _execute():
            svc = PortfolioOrderService(portfolio_service=PortfolioService(), repo=PortfolioRepository(), fetcher=fetcher)
            return svc.execute_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        results = _run_concurrently([_execute, _execute])

        self.assertEqual(len(fetcher.place_order_calls), 1)
        stored = self.repo.get_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "executed")
        # Each thread either succeeded (possibly via idempotent cache) or hit
        # ProposalInProgressError while the other thread's claim was mid-flight.
        for outcome, value in results:
            if outcome == "ok":
                self.assertEqual(value["status"], "executed")
            else:
                self.assertIsInstance(value, ProposalInProgressError)

    def test_parallel_execute_of_different_proposals_serializes_daily_cap(self) -> None:
        os.environ["TOSS_ORDER_DAILY_MAX_AMOUNT_KRW"] = "100000"
        Config.reset_instance()
        self._set_live(True)
        fetcher_a = FakeOrderTossFetcher(place_order_result={"orderId": "toss-order-a"})
        fetcher_b = FakeOrderTossFetcher(place_order_result={"orderId": "toss-order-b"})
        setup_service = self._make_service(fetcher_a)
        # Each proposal alone (70,000 KRW) fits the 100,000 daily cap; together
        # (140,000) they do not — exactly one execute must win.
        proposal_a = setup_service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)
        proposal_b = setup_service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)

        def _execute_a():
            svc = PortfolioOrderService(portfolio_service=PortfolioService(), repo=PortfolioRepository(), fetcher=fetcher_a)
            return svc.execute_proposal(account_id=self.account_id, proposal_uuid=proposal_a["proposal_uuid"], confirm=True)

        def _execute_b():
            svc = PortfolioOrderService(portfolio_service=PortfolioService(), repo=PortfolioRepository(), fetcher=fetcher_b)
            return svc.execute_proposal(account_id=self.account_id, proposal_uuid=proposal_b["proposal_uuid"], confirm=True)

        results = _run_concurrently([_execute_a, _execute_b])

        oks = [v for outcome, v in results if outcome == "ok"]
        errors = [v for outcome, v in results if outcome == "error"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OrderLimitExceededError)
        self.assertEqual(errors[0].limit_type, "daily")
        total_place_order_calls = len(fetcher_a.place_order_calls) + len(fetcher_b.place_order_calls)
        self.assertEqual(total_place_order_calls, 1)

    def test_parallel_create_serializes_pending_proposal_cap(self) -> None:
        fetcher = FakeOrderTossFetcher()

        def _create():
            svc = PortfolioOrderService(portfolio_service=PortfolioService(), repo=PortfolioRepository(), fetcher=fetcher)
            return svc.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)

        results = _run_concurrently([_create for _ in range(12)])
        oks = [v for outcome, v in results if outcome == "ok"]
        errors = [v for outcome, v in results if outcome == "error"]
        self.assertEqual(len(oks), 10)
        self.assertEqual(len(errors), 2)
        for exc in errors:
            self.assertIsInstance(exc, PendingProposalLimitExceededError)


class PortfolioOrderApiTestCase(unittest.TestCase):
    """API-level: order-write auth requirement (design spec v2 §3, Codex
    blocker 1), the v3 "no owner-id header gate" auth clarification (reviewer
    re-review major 2), the reconcile endpoint, and a representative
    error-mapping round trip, via the FastAPI test client."""

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
        self.db_path = self.data_dir / "portfolio_order_api_test.db"
        self._write_env(auth_enabled=False)
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()

        from api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(static_dir=self.data_dir / "empty-static")
        self.client = TestClient(app)
        # A session cookie is present by default so tests that patch
        # is_auth_enabled/verify_session to simulate an authenticated caller
        # don't also need to remember to set one; _require_order_auth still
        # requires *both* a non-empty cookie *and* a verify_session() pass,
        # so this alone changes nothing when auth is disabled/unmocked.
        self.client.cookies.set("dsa_session", "any-non-empty-value")

        self.repo = PortfolioRepository()
        created = PortfolioService().create_account(
            name="Toss KR", broker="toss", market="kr", base_currency="KRW"
        )
        self.account_id = int(created["id"])
        now = datetime(2026, 1, 1)
        self.repo.create_broker_link(
            account_id=self.account_id,
            provider="toss",
            external_account_seq="555",
            external_account_no="1234567890",
            linked_at=now,
            snapshot_boundary_at=now,
            last_synced_at=now,
            active=True,
        )

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def _write_env(self, *, auth_enabled: bool) -> None:
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    f"ADMIN_AUTH_ENABLED={'true' if auth_enabled else 'false'}",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Order-write auth requirement (Codex blocker 1)
    # ------------------------------------------------------------------
    def test_order_write_endpoints_403_when_auth_disabled(self) -> None:
        # ADMIN_AUTH_ENABLED=false (this class's default) must itself be a
        # 403 for every order-write endpoint — "auth is off" must never mean
        # "everyone may place a real order". Design spec v3 test-gap #1:
        # covers all five write endpoints, not just create — execute/
        # reconcile/cancel_proposal/cancel_placed_order must each fail closed
        # via ``_require_order_auth`` before the service (and therefore the
        # DB) is ever reached, so a nonexistent proposal_uuid/toss_order_id
        # is fine here.
        fake = FakeOrderTossFetcher()
        dummy_uuid = "does-not-exist"
        with patch("src.services.portfolio_order_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake

            responses = {
                "create": self.client.post(
                    f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
                    json={"symbol": "005930.KS", "side": "buy", "quantity": 1, "price": 70000},
                ),
                "execute": self.client.post(
                    f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{dummy_uuid}/execute",
                    json={"confirm": True},
                ),
                "reconcile": self.client.post(
                    f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{dummy_uuid}/reconcile"
                ),
                "cancel_proposal": self.client.delete(
                    f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{dummy_uuid}"
                ),
                "cancel_placed_order": self.client.post(
                    f"/api/v1/portfolio/links/{self.account_id}/orders/{dummy_uuid}/cancel"
                ),
            }

        for label, resp in responses.items():
            self.assertEqual(resp.status_code, 403, f"{label}: {resp.text}")
            self.assertEqual(resp.json().get("error"), "order-auth-required", label)

    def test_order_write_endpoints_401_when_auth_enabled_but_no_session(self) -> None:
        # Flip ADMIN_AUTH_ENABLED=true for real (not mocked) — the global
        # AuthMiddleware (unrelated to this feature's own auth gate) already
        # rejects any /api/v1/* call without a verified session in that case,
        # firing before this endpoint's own _require_order_auth ever runs.
        import src.auth as auth

        self._write_env(auth_enabled=True)
        auth._auth_enabled = None
        resp = self.client.post(
            f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
            json={"symbol": "005930.KS", "side": "buy", "quantity": 1, "price": 70000},
        )
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_create_list_execute_cancel_round_trip_with_verified_session(self) -> None:
        fake = FakeOrderTossFetcher()
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ), patch("src.services.portfolio_order_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake
            mock_cls.is_order_live_enabled.return_value = False
            self.client.cookies.set("dsa_session", "any-non-empty-value")

            create_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
                json={"symbol": "005930.KS", "side": "buy", "quantity": 1, "price": 70000},
            )
            self.assertEqual(create_resp.status_code, 200, create_resp.text)
            body = create_resp.json()
            self.assertEqual(body["mode"], "dry_run")
            self.assertEqual(body["status"], "pending")
            proposal_uuid = body["proposal_uuid"]

            list_resp = self.client.get(f"/api/v1/portfolio/links/{self.account_id}/orders/proposals")
            self.assertEqual(list_resp.status_code, 200, list_resp.text)
            self.assertEqual(len(list_resp.json()["proposals"]), 1)

            exec_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{proposal_uuid}/execute",
                json={"confirm": True},
            )
            self.assertEqual(exec_resp.status_code, 200, exec_resp.text)
            exec_body = exec_resp.json()
            self.assertEqual(exec_body["status"], "dry_run_executed")
            self.assertEqual(exec_body["mode"], "dry_run")
            self.assertEqual(fake.place_order_calls, [])

            cancel_resp = self.client.delete(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{proposal_uuid}"
            )
        # Already dry_run_executed (terminal) — cancel is now a conflict, not a 200.
        self.assertEqual(cancel_resp.status_code, 409, cancel_resp.text)

    def test_execute_without_confirm_returns_400(self) -> None:
        fake = FakeOrderTossFetcher()
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ), patch("src.services.portfolio_order_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake
            mock_cls.is_order_live_enabled.return_value = False
            create_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
                json={"symbol": "005930.KS", "side": "buy", "quantity": 1, "price": 70000},
            )
            proposal_uuid = create_resp.json()["proposal_uuid"]
            resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{proposal_uuid}/execute",
                json={"confirm": False},
            )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json().get("error"), "confirm_required")

    def test_cancel_proposal_endpoint(self) -> None:
        fake = FakeOrderTossFetcher()
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ), patch("src.services.portfolio_order_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake
            create_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
                json={"symbol": "005930.KS", "side": "buy", "quantity": 1, "price": 70000},
            )
            proposal_uuid = create_resp.json()["proposal_uuid"]
            cancel_resp = self.client.delete(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{proposal_uuid}"
            )
        self.assertEqual(cancel_resp.status_code, 200, cancel_resp.text)
        self.assertEqual(cancel_resp.json()["status"], "canceled")

    def test_create_proposal_not_configured_returns_400(self) -> None:
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ), patch("data_provider.toss_fetcher.TossFetcher.has_configured_credentials", return_value=False):
            resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
                json={"symbol": "005930.KS", "side": "buy", "quantity": 1, "price": 70000},
            )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json().get("error"), "toss-not-configured")

    def test_high_value_order_returns_422(self) -> None:
        fake = FakeOrderTossFetcher(buying_power=999_999_999_999.0)
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ), patch("src.services.portfolio_order_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake
            resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
                json={"symbol": "005930.KS", "side": "buy", "quantity": 2000, "price": 70000},
            )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json().get("error"), "high_value_order_rejected")

    def test_toss_business_rejection_maps_to_422_with_specific_code(self) -> None:
        rejection = TossOrderRejectedError(
            status_code=422, code="price-out-of-range", message="주문 가격이 허용 범위를 벗어났습니다."
        )
        fake = FakeOrderTossFetcher(place_order_raise=rejection)
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ), patch("src.services.portfolio_order_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake
            mock_cls.is_order_live_enabled.return_value = True
            create_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
                json={"symbol": "005930.KS", "side": "buy", "quantity": 1, "price": 70000},
            )
            proposal_uuid = create_resp.json()["proposal_uuid"]
            exec_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{proposal_uuid}/execute",
                json={"confirm": True},
            )
        self.assertEqual(exec_resp.status_code, 422, exec_resp.text)
        self.assertEqual(exec_resp.json().get("error"), "toss-price-out-of-range")

    def test_authenticated_session_manages_any_account_regardless_of_owner_id(self) -> None:
        """v3 auth clarification (design spec §3 "인증 (필수, v3 명확화)",
        reviewer re-review major 2): a verified session manages *every*
        account in this single-admin system, including one with ``owner_id``
        set — there is no self-asserted caller-identity header gating this at
        all anymore, so even a stray/mismatched ``X-Portfolio-Owner-Id``
        header (e.g. sent by a stale client) must not block access."""
        owned = PortfolioService().create_account(
            name="Owned", broker="toss", market="kr", base_currency="KRW", owner_id="alice"
        )
        owned_account_id = int(owned["id"])
        now = datetime(2026, 1, 1)
        self.repo.create_broker_link(
            account_id=owned_account_id,
            provider="toss",
            external_account_seq="777",
            external_account_no="0007",
            linked_at=now,
            snapshot_boundary_at=now,
            last_synced_at=now,
            active=True,
        )
        fake = FakeOrderTossFetcher()
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ), patch("src.services.portfolio_order_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake
            resp = self.client.post(
                f"/api/v1/portfolio/links/{owned_account_id}/orders/proposals",
                json={"symbol": "005930.KS", "side": "buy", "quantity": 1, "price": 70000},
                headers={"X-Portfolio-Owner-Id": "somebody-else-entirely"},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "pending")

    def test_reconcile_endpoint_resolves_outcome_unknown(self) -> None:
        fake = FakeOrderTossFetcher(
            place_order_sequence=[
                {"clientOrderId": "dsa-x"},  # no orderId -> outcome_unknown
                {"orderId": "toss-order-reconciled"},
            ]
        )
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ), patch("src.services.portfolio_order_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake
            mock_cls.is_order_live_enabled.return_value = True
            create_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
                json={"symbol": "005930.KS", "side": "buy", "quantity": 1, "price": 70000},
            )
            proposal_uuid = create_resp.json()["proposal_uuid"]
            exec_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{proposal_uuid}/execute",
                json={"confirm": True},
            )
            self.assertEqual(exec_resp.json()["status"], "outcome_unknown")

            reconcile_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{proposal_uuid}/reconcile"
            )
        self.assertEqual(reconcile_resp.status_code, 200, reconcile_resp.text)
        self.assertEqual(reconcile_resp.json()["status"], "executed")
        self.assertEqual(reconcile_resp.json()["toss_order_id"], "toss-order-reconciled")

    def test_cancel_placed_order_blocked_while_outcome_unknown(self) -> None:
        fake = FakeOrderTossFetcher(place_order_result={"clientOrderId": "dsa-x"})  # no orderId
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ), patch("src.services.portfolio_order_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fake
            mock_cls.is_order_live_enabled.return_value = True
            create_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
                json={"symbol": "005930.KS", "side": "buy", "quantity": 1, "price": 70000},
            )
            proposal_uuid = create_resp.json()["proposal_uuid"]
            self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/proposals/{proposal_uuid}/execute",
                json={"confirm": True},
            )
            # No toss_order_id was ever recorded (outcome_unknown with a
            # missing orderId) — cancel-by-order-id simply can't find it.
            cancel_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/orders/not-a-real-order/cancel"
            )
        self.assertEqual(cancel_resp.status_code, 404, cancel_resp.text)


if __name__ == "__main__":
    unittest.main()

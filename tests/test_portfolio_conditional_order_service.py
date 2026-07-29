# -*- coding: utf-8 -*-
"""Offline tests for Toss Invest server-side conditional-order proposals
(Phase 4).

Covers the design spec's §6 offline list:
docs/superpowers/specs/2026-07-19-toss-conditional-order-phase4-design.md

- dry-run default invariant: TOSS_ORDER_LIVE unset means
  place_conditional_order is never called (mirrors Phase 3's own dry-run
  invariant test in tests/test_portfolio_order_service.py).
- local state machine: pending -> approving -> approved/registration_failed/
  registration_unknown; pending -> canceled/expired/dry_run_approved;
  approved -> triggered_completed/toss_expired/toss_canceled/paused;
  registration_unknown -> approved/registration_failed (reconcile).
- expireDate 7-day cap enforced at both create and approve.
- shared daily KRW cap: a conditional-order reservation blocks a Phase 3
  order and vice versa (the cross-type regression the design spec's "한도
  산입" decision requires — this is the load-bearing bidirectional-cap
  contract, not merely a same-module regression).
- reconcile's attribute-match fallback (design spec deviation, see
  portfolio_conditional_order_service.py's module docstring): a match
  resolves to approved with the observed Toss status; **no match must
  never resolve to registration_failed** — it must stay
  registration_unknown (the safety invariant the whole reconcile redesign
  exists to preserve).
- cancel: pending (local only, no Toss call) vs approved (Toss DELETE then
  toss_canceled) vs approving/registration_unknown (refused, reconcile
  first).
- append-only audit: cond_* events land on the same
  PortfolioOrderAudit table Phase 3 already uses, under its existing
  append-only SQLite trigger.

TossFetcher is always faked here — this suite makes no real HTTP calls.
"""

from __future__ import annotations

import os
import socket
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
from data_provider.toss_fetcher import TossOrderRejectedError
from sqlalchemy.exc import IntegrityError
from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_broker_sync_service import _now_kst_naive
from src.services.portfolio_conditional_order_service import (
    ConditionalApprovalInProgressError,
    ConditionalProposalNotForceResolvableError,
    ConditionalProposalNotReconcilableError,
    ExpireDateTooFarError,
    PortfolioConditionalOrderService,
    _validate_expire_date,
)
from src.services.portfolio_order_service import (
    ConfirmRequiredError,
    HighValueOrderRejectedError,
    OrderLimitExceededError,
    PortfolioOrderService,
)
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager, PortfolioConditionalOrderProposal, PortfolioOrderAudit
from sqlalchemy import select


@pytest.fixture(autouse=True)
def _block_unmocked_network(request):
    """Same offline guard as tests/test_portfolio_order_service.py — the
    service under test is faked via FakeConditionalTossFetcher throughout."""
    if request.node.get_closest_marker("network"):
        yield
        return
    original_connect = socket.socket.connect

    def _guarded_connect(self, address, *a, **kw):
        raise AssertionError(
            f"Unexpected real network connection attempt to {address!r} in "
            f"tests/test_portfolio_conditional_order_service.py — this suite must run fully offline"
        )

    socket.socket.connect = _guarded_connect
    try:
        yield
    finally:
        socket.socket.connect = original_connect


class FakeConditionalTossFetcher:
    """Deterministic stand-in for TossFetcher — implements both the Phase 3
    order-write methods and the Phase 4 conditional-order methods, so the
    same fake can back both PortfolioOrderService and
    PortfolioConditionalOrderService in the cross-type daily-cap regression
    test below."""

    def __init__(
        self,
        *,
        buying_power: float = 10_000_000.0,
        sellable_quantity: float = 100.0,
        place_conditional_order_result: Optional[Dict[str, Any]] = None,
        place_conditional_order_raise: Optional[Exception] = None,
        cancel_conditional_order_raise: Optional[Exception] = None,
        get_conditional_order_result: Optional[Dict[str, Any]] = None,
        list_conditional_orders_pages: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        self.buying_power = buying_power
        self.sellable_quantity = sellable_quantity
        self._place_conditional_order_result = place_conditional_order_result or {
            "conditionalOrderId": "cond-1",
            "clientOrderId": None,
        }
        self._place_conditional_order_raise = place_conditional_order_raise
        self._cancel_conditional_order_raise = cancel_conditional_order_raise
        self._get_conditional_order_result = get_conditional_order_result or {
            "conditionalOrderId": "cond-1",
            "status": "WATCHING",
        }
        # {"OPEN": [...items...], "CLOSED": [...items...]}
        self._list_conditional_orders_pages = list_conditional_orders_pages or {"OPEN": [], "CLOSED": []}

        self.place_order_calls: List[Any] = []
        self.place_conditional_order_calls: List[Any] = []
        self.cancel_conditional_order_calls: List[Any] = []
        self.get_conditional_order_calls: List[Any] = []
        self.list_conditional_orders_calls: List[Any] = []

    # Phase 3 methods (shared use in the cross-type test) ----------------
    def get_buying_power(self, account_seq: Any, currency: str) -> float:
        return self.buying_power

    def get_sellable_quantity(self, account_seq: Any, symbol: str) -> float:
        return self.sellable_quantity

    def place_order(self, account_seq: Any, **kwargs: Any) -> Dict[str, Any]:
        self.place_order_calls.append((account_seq, kwargs))
        return {"orderId": "toss-order-1"}

    def cancel_order(self, account_seq: Any, order_id: str) -> Dict[str, Any]:
        return {"orderId": order_id}

    def get_order(self, account_seq: Any, order_id: str) -> Dict[str, Any]:
        return {"orderId": order_id, "status": "FILLED"}

    # Phase 4 conditional-order methods -----------------------------------
    def place_conditional_order(self, account_seq: Any, **kwargs: Any) -> Dict[str, Any]:
        self.place_conditional_order_calls.append((account_seq, kwargs))
        if self._place_conditional_order_raise is not None:
            raise self._place_conditional_order_raise
        return self._place_conditional_order_result

    def cancel_conditional_order(self, account_seq: Any, conditional_order_id: str) -> Dict[str, Any]:
        self.cancel_conditional_order_calls.append((account_seq, conditional_order_id))
        if self._cancel_conditional_order_raise is not None:
            raise self._cancel_conditional_order_raise
        return {}

    def get_conditional_order(self, account_seq: Any, conditional_order_id: str) -> Dict[str, Any]:
        self.get_conditional_order_calls.append((account_seq, conditional_order_id))
        return self._get_conditional_order_result

    def list_conditional_orders(
        self, account_seq: Any, *, status: str, symbol: Optional[str] = None, cursor: Optional[str] = None, limit: int = 100
    ) -> Dict[str, Any]:
        self.list_conditional_orders_calls.append((account_seq, status, symbol, cursor))
        items = self._list_conditional_orders_pages.get(status, [])
        return {"conditionalOrders": items, "hasNext": False, "nextCursor": None}


class _ConditionalOrderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"
        self.db_path = Path(self.temp_dir.name) / "portfolio_conditional_order_test.db"
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

    def _make_service(self, fetcher: FakeConditionalTossFetcher) -> PortfolioConditionalOrderService:
        return PortfolioConditionalOrderService(
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

    def _near_expire_date(self) -> date:
        # _now_kst_naive() is real wall-clock KST — use "today + 3 days" so
        # the 7-day cap is comfortably satisfied regardless of test run date.
        from src.services.portfolio_broker_sync_service import _now_kst_naive

        return (_now_kst_naive() + timedelta(days=3)).date()


class ConditionalOrderCreateApproveTestCase(_ConditionalOrderTestBase):
    def test_dry_run_default_never_calls_place_conditional_order(self) -> None:
        self._set_live(False)
        fetcher = FakeConditionalTossFetcher()
        service = self._make_service(fetcher)

        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        self.assertEqual(proposal["mode"], "dry_run")
        self.assertEqual(proposal["status"], "pending")

        result = service.approve_proposal(
            account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True
        )
        self.assertEqual(fetcher.place_conditional_order_calls, [])
        self.assertEqual(result["status"], "dry_run_approved")
        self.assertEqual(result["mode"], "dry_run")
        self.assertIsNone(result["toss_conditional_order_id"])

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        events = [a.event for a in audits]
        self.assertEqual(events, ["cond_proposed", "cond_dry_run_approved"])

    def test_confirm_required_even_in_dry_run(self) -> None:
        self._set_live(False)
        fetcher = FakeConditionalTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        with self.assertRaises(ConfirmRequiredError):
            service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=False)
        self.assertEqual(fetcher.place_conditional_order_calls, [])

    def test_live_mode_registers_and_persists_conditional_order_id(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(place_conditional_order_result={"conditionalOrderId": "cond-42"})
        service = self._make_service(fetcher)

        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        self.assertEqual(proposal["mode"], "live")

        result = service.approve_proposal(
            account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True
        )
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["toss_conditional_order_id"], "cond-42")
        self.assertEqual(result["toss_status"], "WATCHING")

        self.assertEqual(len(fetcher.place_conditional_order_calls), 1)
        _account_seq, kwargs = fetcher.place_conditional_order_calls[0]
        self.assertEqual(kwargs["symbol"], "005930")
        self.assertEqual(kwargs["side"], "SELL")
        self.assertEqual(kwargs["trigger_price"], 65000)
        self.assertEqual(kwargs["limit_price"], 64500)
        client_order_id = kwargs["client_order_id"]
        self.assertLessEqual(len(client_order_id), 36)
        self.assertTrue(client_order_id.startswith("dc-"))

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        events = [a.event for a in audits]
        self.assertEqual(events, ["cond_proposed", "cond_approving", "cond_approved"])

    def test_idempotent_retry_returns_cached_result_without_second_post(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(place_conditional_order_result={"conditionalOrderId": "cond-1"})
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        result_2 = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result_2["status"], "approved")
        self.assertEqual(len(fetcher.place_conditional_order_calls), 1)

    def test_create_rejects_expire_date_beyond_seven_days(self) -> None:
        self._set_live(False)
        fetcher = FakeConditionalTossFetcher()
        service = self._make_service(fetcher)
        from src.services.portfolio_broker_sync_service import _now_kst_naive

        too_far = (_now_kst_naive() + timedelta(days=8)).date()
        with self.assertRaises(ExpireDateTooFarError):
            service.create_proposal(
                account_id=self.account_id,
                symbol="005930.KS",
                side="sell",
                trigger_price=65000,
                limit_price=64500,
                quantity=1,
                expire_date=too_far,
            )

    def test_validate_expire_date_boundary_table(self) -> None:
        now = datetime(2026, 7, 19, 10, 0, 0)
        _validate_expire_date(date(2026, 7, 26), now=now)  # exactly +7 days: OK
        with self.assertRaises(ExpireDateTooFarError):
            _validate_expire_date(date(2026, 7, 27), now=now)  # +8 days: rejected
        with self.assertRaises(ValueError):
            _validate_expire_date(date(2026, 7, 18), now=now)  # past: rejected

    def test_high_value_order_hard_rejected_at_create_regardless_of_config(self) -> None:
        """Mirrors Phase 3's own
        test_high_value_order_hard_rejected_regardless_of_config: the
        best-effort cap check at create_proposal time already catches this
        (the same _enforce_amount_caps_best_effort reused from Phase 3) —
        approve_proposal's re-check exists for amounts that only become
        unsafe *between* create and approve (FX drift, a tightened cap, or
        another reservation consuming the daily cap in the meantime; see
        test_per_order_cap_exceeded_at_approve and the cross-type daily-cap
        tests below for that later-breach case)."""
        os.environ["TOSS_ORDER_MAX_AMOUNT_KRW"] = "999999999999"
        os.environ["TOSS_ORDER_DAILY_MAX_AMOUNT_KRW"] = "999999999999"
        Config.reset_instance()
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(buying_power=999_999_999_999.0)
        service = self._make_service(fetcher)
        with self.assertRaises(HighValueOrderRejectedError):
            service.create_proposal(
                account_id=self.account_id,
                symbol="005930.KS",
                side="buy",
                trigger_price=200_000,
                limit_price=200_000,
                quantity=600,  # 120,000,000 KRW >= 100,000,000 hard reject
                expire_date=self._near_expire_date(),
            )
        self.assertEqual(fetcher.place_conditional_order_calls, [])

    def test_per_order_cap_exceeded_at_approve_when_cap_tightens_after_create(self) -> None:
        """The cap is re-checked, not trusted from proposal-creation time —
        tighten TOSS_ORDER_MAX_AMOUNT_KRW *after* create (simulating a
        config change during the proposal's TTL window) so the breach is
        only visible at approve time."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=70000,
            limit_price=70000,
            quantity=1,  # 70,000 KRW — fits the default 1,000,000 per-order cap at create time
            expire_date=self._near_expire_date(),
        )
        os.environ["TOSS_ORDER_MAX_AMOUNT_KRW"] = "50000"
        Config.reset_instance()
        with self.assertRaises(OrderLimitExceededError) as ctx:
            service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(ctx.exception.limit_type, "per_order")
        stored = self.repo.get_conditional_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "registration_failed")
        self.assertEqual(fetcher.place_conditional_order_calls, [])

    def test_registration_unknown_on_in_doubt_toss_code(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(
                status_code=409, code="request-in-progress", message="in progress"
            )
        )
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        result = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result["status"], "registration_unknown")

    def test_registration_failed_on_explicit_rejection(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(
                status_code=422, code="invalid-request", message="bad trigger price"
            )
        )
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        with self.assertRaises(TossOrderRejectedError):
            service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        stored = self.repo.get_conditional_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "registration_failed")

    def test_registration_unknown_on_missing_conditional_order_id(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(place_conditional_order_result={"conditionalOrderId": None})
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        result = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result["status"], "registration_unknown")


class ConditionalOrderReconcileTestCase(_ConditionalOrderTestBase):
    def _make_unknown_proposal(self, fetcher: FakeConditionalTossFetcher, service: PortfolioConditionalOrderService) -> Dict[str, Any]:
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        return proposal

    @staticmethod
    def _matching_item(
        expire_date: date,
        *,
        conditional_order_id: str,
        status: str = "WATCHING",
        created_at: Optional[datetime] = None,
        symbol: str = "005930",
        side: str = "SELL",
        trigger_price: str = "65000",
        limit_price: str = "64500",
        quantity: str = "1",
    ) -> Dict[str, Any]:
        """Build one Toss conditional-order listing item matching the
        default proposal shape used throughout this test class
        (``_make_unknown_proposal``: symbol 005930, side SELL, trigger
        65000, limit 64500, quantity 1). ``created_at`` defaults to "now"
        (KST, ``+09:00`` offset — matches Toss's real ``createdAt`` shape)
        so it always falls inside the reconcile match time window unless a
        test deliberately overrides it."""
        ts = created_at or _now_kst_naive()
        return {
            "conditionalOrderId": conditional_order_id,
            "type": "SINGLE",
            "status": status,
            "symbol": symbol,
            "quantity": quantity,
            "expireDate": expire_date.isoformat(),
            "first": {"orderSide": side, "triggerPrice": trigger_price, "orderPrice": limit_price},
            "createdAt": ts.strftime("%Y-%m-%dT%H:%M:%S") + "+09:00",
        }

    def test_reconcile_match_found_resolves_to_approved(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = self._make_unknown_proposal(fetcher, service)
        result = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result["status"], "registration_unknown")

        expire_date = self._near_expire_date()
        fetcher._list_conditional_orders_pages["OPEN"] = [
            self._matching_item(expire_date, conditional_order_id="cond-found-1")
        ]
        reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(reconciled["status"], "approved")
        self.assertEqual(reconciled["toss_conditional_order_id"], "cond-found-1")
        self.assertEqual(reconciled["toss_status"], "WATCHING")

    def test_reconcile_no_match_stays_registration_unknown_never_failed(self) -> None:
        """Safety invariant (advisor-reviewed design-spec deviation): an
        unmatched attribute search must NEVER resolve to
        registration_failed — only an explicit Toss create-time rejection
        can do that. A false 'not found' verdict here would release the
        daily-cap hold on an order that might actually be live on Toss."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = self._make_unknown_proposal(fetcher, service)
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        # No matching item in either OPEN or CLOSED listings.
        reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(reconciled["status"], "registration_unknown")
        self.assertIsNone(reconciled["toss_conditional_order_id"])

        # The reservation must still count against the daily cap.
        total = self.repo.sum_daily_reserved_and_executed_amount_krw(
            self.account_id, kst_date=datetime.now().date()
        )
        self.assertGreater(total, 0.0)

    def test_reconcile_refused_on_already_approved_proposal(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(place_conditional_order_result={"conditionalOrderId": "cond-1"})
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        with self.assertRaises(ConditionalProposalNotReconcilableError):
            service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])

    # ------------------------------------------------------------------
    # Codex BLOCK review blocker 1 — attribute-match must require symbol
    # match, a createdAt time window, AND candidate uniqueness. Ambiguous
    # or out-of-window candidates must never be adopted.
    # ------------------------------------------------------------------
    def test_reconcile_two_identical_candidates_stays_unknown_not_arbitrary_pick(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = self._make_unknown_proposal(fetcher, service)
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        expire_date = self._near_expire_date()
        fetcher._list_conditional_orders_pages["OPEN"] = [
            self._matching_item(expire_date, conditional_order_id="cond-a"),
            self._matching_item(expire_date, conditional_order_id="cond-b"),
        ]
        reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(reconciled["status"], "registration_unknown")
        self.assertIsNone(reconciled["toss_conditional_order_id"])

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        last = audits[-1]
        self.assertEqual(last.error_code, "ambiguous-match")
        import json as _json

        self.assertEqual(_json.loads(last.detail)["candidate_count"], 2)

    def test_reconcile_symbol_mismatch_is_not_matched(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = self._make_unknown_proposal(fetcher, service)
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        expire_date = self._near_expire_date()
        # Same side/trigger/limit/quantity/expiry, but a different symbol —
        # must not be treated as this proposal's own registration even
        # though the Fake fetcher (unlike the real API) does not itself
        # filter by the `symbol=` query param.
        fetcher._list_conditional_orders_pages["OPEN"] = [
            self._matching_item(expire_date, conditional_order_id="cond-wrong-symbol", symbol="000660")
        ]
        reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(reconciled["status"], "registration_unknown")
        self.assertIsNone(reconciled["toss_conditional_order_id"])

    def test_reconcile_created_at_outside_window_is_not_matched(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = self._make_unknown_proposal(fetcher, service)
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        expire_date = self._near_expire_date()
        # Otherwise-identical candidate, but created 30 minutes before the
        # approving claim (reserved_at) — outside the [-5min, now] window.
        stale_created_at = _now_kst_naive() - timedelta(minutes=30)
        fetcher._list_conditional_orders_pages["OPEN"] = [
            self._matching_item(expire_date, conditional_order_id="cond-too-old", created_at=stale_created_at)
        ]
        reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(reconciled["status"], "registration_unknown")
        self.assertIsNone(reconciled["toss_conditional_order_id"])

    # ------------------------------------------------------------------
    # Codex 2nd-round review R1 — ownership contract: a unique attribute
    # match is not sufficient proof of ownership. It must also not already
    # be owned by another local proposal, and no same-attribute local
    # proposal may be racing for it; a DB-level unique-index conflict must
    # never be silently swallowed into a wrong adoption either.
    # ------------------------------------------------------------------
    def test_reconcile_candidate_owned_by_other_proposal_is_excluded(self) -> None:
        """R1-2: a remote candidate already recorded on a *different* local
        proposal (any status) must be excluded, even if it is otherwise the
        only attribute match — reconciling proposal B must never adopt
        proposal A's own registration just because A happened to register
        first."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_result={"conditionalOrderId": "cond-owned-by-a"}
        )
        service = self._make_service(fetcher)

        # Proposal A registers successfully and owns "cond-owned-by-a".
        proposal_a = self._make_unknown_proposal(fetcher, service)
        result_a = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_a["proposal_uuid"], confirm=True)
        self.assertEqual(result_a["status"], "approved")
        self.assertEqual(result_a["toss_conditional_order_id"], "cond-owned-by-a")

        # Proposal B has identical attributes but its own approve attempt
        # was ambiguous (in-doubt) -> registration_unknown.
        fetcher._place_conditional_order_raise = TossOrderRejectedError(
            status_code=409, code="request-in-progress", message="x"
        )
        proposal_b = self._make_unknown_proposal(fetcher, service)
        result_b = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_b["proposal_uuid"], confirm=True)
        self.assertEqual(result_b["status"], "registration_unknown")

        # The Toss listing only shows A's order (same attributes) -- B's
        # reconcile must not adopt it.
        expire_date = self._near_expire_date()
        fetcher._list_conditional_orders_pages["OPEN"] = [
            self._matching_item(expire_date, conditional_order_id="cond-owned-by-a")
        ]
        reconciled_b = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal_b["proposal_uuid"])
        self.assertEqual(reconciled_b["status"], "registration_unknown")
        self.assertIsNone(reconciled_b["toss_conditional_order_id"])

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal_b["proposal_uuid"])
        last = audits[-1]
        self.assertEqual(last.error_code, "not-found-on-toss")
        import json as _json

        detail = _json.loads(last.detail)
        self.assertEqual(detail["owned_by_other_proposal_count"], 1)
        self.assertEqual(detail["candidate_count"], 0)

        # A's own registration must be untouched by B's reconcile attempt.
        stored_a = self.repo.get_conditional_order_proposal(proposal_a["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored_a.status, "approved")
        self.assertEqual(stored_a.toss_conditional_order_id, "cond-owned-by-a")

    def test_reconcile_stays_unknown_while_local_contender_unresolved(self) -> None:
        """R1-3: a same-attribute *other* local proposal that is itself
        still unresolved (approving/registration_unknown) means a single
        remote candidate cannot be safely attributed to *this* proposal --
        it could just as well belong to the other one."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)

        # Proposal A: identical attributes, left 'registration_unknown'
        # (still unresolved -- a local contender for whatever Toss shows).
        proposal_a = self._make_unknown_proposal(fetcher, service)
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_a["proposal_uuid"], confirm=True)

        # Proposal B: identical attributes, also 'registration_unknown'.
        proposal_b = self._make_unknown_proposal(fetcher, service)
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_b["proposal_uuid"], confirm=True)

        expire_date = self._near_expire_date()
        fetcher._list_conditional_orders_pages["OPEN"] = [
            self._matching_item(expire_date, conditional_order_id="cond-ambiguous-owner")
        ]

        reconciled_b = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal_b["proposal_uuid"])
        self.assertEqual(reconciled_b["status"], "registration_unknown")
        self.assertIsNone(reconciled_b["toss_conditional_order_id"])

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal_b["proposal_uuid"])
        last = audits[-1]
        self.assertEqual(last.error_code, "local-contender")
        import json as _json

        detail = _json.loads(last.detail)
        self.assertEqual(detail["local_contender_count"], 1)
        self.assertEqual(detail["candidate_count"], 1)

    def test_reconcile_atomic_recheck_catches_ownership_conflict_even_if_advisory_check_is_fooled(self) -> None:
        """R1c (3rd-round review): even if the *advisory* application-layer
        ownership check is fooled (simulated here by patching it to report
        "unowned"), adopt_reconciled_order_if_uncontended's own re-check --
        run inside the same write-locked transaction as the adoption
        write -- must still catch that the candidate ID is already owned
        by another proposal and refuse to adopt it. This closes the gap a
        purely-advisory (non-atomic) check alone could not: the conflict is
        now detected as a normal 'contended' outcome, never even reaching
        the DB-level unique-index IntegrityError fallback."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_result={"conditionalOrderId": "cond-shared"}
        )
        service = self._make_service(fetcher)

        proposal_a = self._make_unknown_proposal(fetcher, service)
        result_a = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_a["proposal_uuid"], confirm=True)
        self.assertEqual(result_a["status"], "approved")

        fetcher._place_conditional_order_raise = TossOrderRejectedError(
            status_code=409, code="request-in-progress", message="x"
        )
        proposal_b = self._make_unknown_proposal(fetcher, service)
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_b["proposal_uuid"], confirm=True)

        expire_date = self._near_expire_date()
        fetcher._list_conditional_orders_pages["OPEN"] = [
            self._matching_item(expire_date, conditional_order_id="cond-shared")
        ]

        with patch.object(service.repo, "find_conditional_order_ids_owned_by_others", return_value=set()):
            reconciled_b = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal_b["proposal_uuid"])

        self.assertEqual(reconciled_b["status"], "registration_unknown")
        self.assertIsNone(reconciled_b["toss_conditional_order_id"])

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal_b["proposal_uuid"])
        last = audits[-1]
        self.assertEqual(last.error_code, "owned-by-other-proposal")
        import json as _json

        detail = _json.loads(last.detail)
        self.assertEqual(detail["owned_by_other_proposal_count"], 1)
        self.assertEqual(detail["candidate_conditional_order_id"], "cond-shared")

        stored_a = self.repo.get_conditional_order_proposal(proposal_a["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored_a.status, "approved")
        self.assertEqual(stored_a.toss_conditional_order_id, "cond-shared")

    def test_reconcile_stays_unknown_when_local_contender_appears_between_advisory_check_and_atomic_adopt(self) -> None:
        """R1c / R4 item 1 (3rd-round review): B's *advisory* local-contender
        pre-check sees zero contenders, but a same-attribute proposal A
        commits into 'approving' before B's atomic adopt transaction opens.
        The atomic recheck inside adopt_reconciled_order_if_uncontended
        must catch this and cancel the adoption -- B must stay
        registration_unknown, never silently adopt a candidate that may
        actually belong to A."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)

        proposal_a = self._make_unknown_proposal(fetcher, service)
        proposal_b = self._make_unknown_proposal(fetcher, service)
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_b["proposal_uuid"], confirm=True)

        expire_date = self._near_expire_date()
        fetcher._list_conditional_orders_pages["OPEN"] = [
            self._matching_item(expire_date, conditional_order_id="cond-race-injected")
        ]

        # A is still 'pending' at this point -- the advisory local-contender
        # pre-check (an ordinary read, before B's atomic adopt txn opens)
        # will see zero unresolved contenders. Inject A's atomic claim into
        # 'approving' by wrapping the repo's atomic adopt method: the real
        # method is invoked afterwards so the atomic recheck inside it sees
        # A's freshly-committed 'approving' row.
        original_adopt = PortfolioRepository.adopt_reconciled_order_if_uncontended

        def _adopt_after_injecting_race(self_repo, **kwargs):
            claim = self_repo.claim_conditional_proposal_for_approval(
                proposal_uuid=proposal_a["proposal_uuid"],
                account_id=self.account_id,
                now=_now_kst_naive(),
                est_amount_krw=64500.0,
                high_value_threshold_krw=100_000_000.0,
                per_order_cap_krw=1_000_000.0,
                daily_cap_krw=5_000_000.0,
            )
            assert claim.outcome == "claimed", claim.outcome
            return original_adopt(self_repo, **kwargs)

        with patch.object(PortfolioRepository, "adopt_reconciled_order_if_uncontended", _adopt_after_injecting_race):
            reconciled_b = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal_b["proposal_uuid"])

        self.assertEqual(reconciled_b["status"], "registration_unknown")
        self.assertIsNone(reconciled_b["toss_conditional_order_id"])

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal_b["proposal_uuid"])
        last = audits[-1]
        self.assertEqual(last.error_code, "local-contender")
        import json as _json

        detail = _json.loads(last.detail)
        self.assertEqual(detail["local_contender_count"], 1)

        stored_a = self.repo.get_conditional_order_proposal(proposal_a["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored_a.status, "approving")

    def test_reconcile_unique_index_conflict_fallback_when_atomic_adopt_itself_raises(self) -> None:
        """R1-4 (2nd-round) as defense-in-depth after R1c: under SQLite the
        atomic recheck inside adopt_reconciled_order_if_uncontended should
        make a raw IntegrityError at the reconcile call site unreachable in
        practice -- but reconcile_proposal must still handle one gracefully
        if it ever occurs (e.g. a future backend without the same write-lock
        discipline), resolving to registration_unknown rather than
        propagating a raw DB error."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = self._make_unknown_proposal(fetcher, service)
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        expire_date = self._near_expire_date()
        fetcher._list_conditional_orders_pages["OPEN"] = [
            self._matching_item(expire_date, conditional_order_id="cond-forced-conflict")
        ]

        with patch.object(
            service.repo,
            "adopt_reconciled_order_if_uncontended",
            side_effect=IntegrityError("stmt", {}, Exception("UNIQUE constraint failed")),
        ):
            reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])

        self.assertEqual(reconciled["status"], "registration_unknown")
        self.assertIsNone(reconciled["toss_conditional_order_id"])

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(audits[-1].error_code, "unique-conflict-on-adopt")

    def test_reconcile_dedupes_same_id_across_open_and_closed_listings(self) -> None:
        """R3 (minor): the same conditionalOrderId appearing in both the
        OPEN and CLOSED listings (an order transitioning mid-search) must
        be counted once, not twice -- otherwise a genuinely unambiguous
        match would be misreported as 'ambiguous-match'. CLOSED is queried
        after OPEN, so its status must win."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = self._make_unknown_proposal(fetcher, service)
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        expire_date = self._near_expire_date()
        fetcher._list_conditional_orders_pages["OPEN"] = [
            self._matching_item(expire_date, conditional_order_id="cond-dup", status="WATCHING")
        ]
        fetcher._list_conditional_orders_pages["CLOSED"] = [
            self._matching_item(expire_date, conditional_order_id="cond-dup", status="COMPLETED")
        ]

        reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(reconciled["status"], "triggered_completed")
        self.assertEqual(reconciled["toss_conditional_order_id"], "cond-dup")
        self.assertEqual(reconciled["toss_status"], "COMPLETED")

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        last = audits[-1]
        import json as _json

        self.assertEqual(_json.loads(last.detail)["candidate_count"], 1)

    # ------------------------------------------------------------------
    # Codex BLOCK review blocker 2 — reconcile must not preempt a fresh
    # 'approving' claim, and the eventual real POST outcome must still win
    # even after a stale-claim takeover.
    # ------------------------------------------------------------------
    def test_reconcile_refuses_fresh_approving_claim_with_409(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        proposal_uuid = proposal["proposal_uuid"]

        claim = self.repo.claim_conditional_proposal_for_approval(
            proposal_uuid=proposal_uuid,
            account_id=self.account_id,
            now=_now_kst_naive(),
            est_amount_krw=64500.0,
            high_value_threshold_krw=100_000_000.0,
            per_order_cap_krw=1_000_000.0,
            daily_cap_krw=5_000_000.0,
        )
        self.assertEqual(claim.outcome, "claimed")

        # reserved_at defaults to "now" — well within the 10-minute
        # staleness window — so reconcile must refuse, not preempt.
        with self.assertRaises(ConditionalApprovalInProgressError):
            service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid)

        stored = self.repo.get_conditional_order_proposal(proposal_uuid, account_id=self.account_id)
        self.assertEqual(stored.status, "approving")

    def test_reconcile_takes_over_stale_approving_claim(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        proposal_uuid = proposal["proposal_uuid"]

        claim = self.repo.claim_conditional_proposal_for_approval(
            proposal_uuid=proposal_uuid,
            account_id=self.account_id,
            now=_now_kst_naive(),
            est_amount_krw=64500.0,
            high_value_threshold_krw=100_000_000.0,
            per_order_cap_krw=1_000_000.0,
            daily_cap_krw=5_000_000.0,
        )
        self.assertEqual(claim.outcome, "claimed")

        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioConditionalOrderProposal).where(
                    PortfolioConditionalOrderProposal.proposal_uuid == proposal_uuid
                )
            ).scalar_one()
            row.reserved_at = _now_kst_naive() - timedelta(minutes=11)
            session.commit()

        # No matching Toss listing -> the takeover lands on
        # registration_unknown, not a guess.
        reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid)
        self.assertEqual(reconciled["status"], "registration_unknown")

    def test_post_outcome_wins_even_after_reconcile_takeover_of_stale_claim(self) -> None:
        """The core B2 convergence contract: if reconcile takes over a
        stale 'approving' claim as crash recovery, and the *original*
        approve POST for that same claim then resolves (success or an
        explicit rejection), that real outcome must still be recorded —
        not silently dropped because the row moved to
        'registration_unknown' out from under it."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        proposal_uuid = proposal["proposal_uuid"]

        claim = self.repo.claim_conditional_proposal_for_approval(
            proposal_uuid=proposal_uuid,
            account_id=self.account_id,
            now=_now_kst_naive(),
            est_amount_krw=64500.0,
            high_value_threshold_krw=100_000_000.0,
            per_order_cap_krw=1_000_000.0,
            daily_cap_krw=5_000_000.0,
        )
        self.assertEqual(claim.outcome, "claimed")

        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioConditionalOrderProposal).where(
                    PortfolioConditionalOrderProposal.proposal_uuid == proposal_uuid
                )
            ).scalar_one()
            row.reserved_at = _now_kst_naive() - timedelta(minutes=11)
            session.commit()

        reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid)
        self.assertEqual(reconciled["status"], "registration_unknown")

        # The original approve call's delayed POST now resolves — its
        # outcome resolution (from_statuses default now includes
        # registration_unknown) must still win and record the real ID.
        updated = service._resolve_registration_outcome(
            proposal_uuid=proposal_uuid,
            account_id=self.account_id,
            now=_now_kst_naive(),
            outcome="approved",
            conditional_order_id="cond-race-winner",
        )
        self.assertEqual(updated.status, "approved")
        self.assertEqual(updated.toss_conditional_order_id, "cond-race-winner")

        final = self.repo.get_conditional_order_proposal(proposal_uuid, account_id=self.account_id)
        self.assertEqual(final.status, "approved")
        self.assertEqual(final.toss_conditional_order_id, "cond-race-winner")


class ConditionalOrder429TestCase(_ConditionalOrderTestBase):
    """Codex review major 1 / design spec §5 "429": a rate-limited
    registration POST must resolve to registration_unknown, not be
    silently retried (the fetcher-level no-retry change is covered in
    tests/test_toss_fetcher.py; this covers the service-level outcome)."""

    def test_429_on_approve_resolves_to_registration_unknown(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=DataFetchError(
                "[Toss] 429 rate limited on conditional-order write; not retrying"
            )
        )
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        result = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result["status"], "registration_unknown")
        self.assertEqual(len(fetcher.place_conditional_order_calls), 1)

    def test_reconcile_stale_threshold_is_at_least_10x_write_timeout_worst_case(self) -> None:
        """R4-8: guards the coordinator-confirmed constant relationship
        directly (the module-level assert already enforces this at import
        time — this test documents and re-verifies it so a future change to
        either constant fails loudly here too, not just via an import-time
        AssertionError buried in a stack trace)."""
        from data_provider.toss_fetcher import _CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS
        from src.services.portfolio_conditional_order_service import _RECONCILE_STALE_APPROVING_AFTER

        self.assertGreaterEqual(
            _RECONCILE_STALE_APPROVING_AFTER,
            10 * timedelta(seconds=_CONDITIONAL_ORDER_WRITE_WORST_CASE_SECONDS),
        )


class ConditionalOrderPostRaceAuditNetTestCase(_ConditionalOrderTestBase):
    """Codex 2nd-round review R2-2 (coordinator-confirmed convergence
    contract): a genuinely delayed POST-outcome resolution that arrives
    after the row has already moved to a status
    ``_resolve_registration_outcome``'s ``from_statuses`` no longer covers
    must never be silently dropped — it must leave a
    ``conditional_registration_conflict`` audit event and must never
    overwrite the row's actual (already-settled) state."""

    def _conflict_audit(self, proposal_uuid: str):
        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal_uuid)
        conflicts = [a for a in audits if a.event == "conditional_registration_conflict"]
        self.assertEqual(len(conflicts), 1, f"expected exactly one conflict audit, got audits={[a.event for a in audits]}")
        return conflicts[0]

    def test_delayed_post_success_after_reconcile_terminal_is_not_applied_but_audited(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        proposal_uuid = proposal["proposal_uuid"]
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid, confirm=True)

        # Reconcile finds a match that Toss reports as already COMPLETED --
        # a genuinely terminal local status.
        expire_date = self._near_expire_date()
        fetcher._list_conditional_orders_pages["OPEN"] = [
            ConditionalOrderReconcileTestCase._matching_item(
                expire_date, conditional_order_id="cond-completed", status="COMPLETED"
            )
        ]
        reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid)
        self.assertEqual(reconciled["status"], "triggered_completed")

        # The *original* approve call's own POST now finally resolves --
        # with a genuinely different conditionalOrderId (a real, distinct
        # registration Toss actually processed) -- long after reconcile
        # already settled the row as terminal.
        updated = service._resolve_registration_outcome(
            proposal_uuid=proposal_uuid,
            account_id=self.account_id,
            now=_now_kst_naive(),
            outcome="approved",
            conditional_order_id="cond-delayed-success",
        )
        # The already-terminal local state must not be overwritten.
        self.assertEqual(updated.status, "triggered_completed")
        self.assertEqual(updated.toss_conditional_order_id, "cond-completed")

        conflict = self._conflict_audit(proposal_uuid)
        self.assertEqual(conflict.error_code, "post-outcome-not-applied")
        import json as _json

        detail = _json.loads(conflict.detail)
        self.assertEqual(detail["post_outcome"], "approved")
        self.assertEqual(detail["post_conditional_order_id"], "cond-delayed-success")
        self.assertEqual(detail["local_status_after"], "triggered_completed")

    def test_delayed_explicit_rejection_after_reconcile_terminal_is_audited(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        proposal_uuid = proposal["proposal_uuid"]
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid, confirm=True)

        expire_date = self._near_expire_date()
        fetcher._list_conditional_orders_pages["OPEN"] = [
            ConditionalOrderReconcileTestCase._matching_item(
                expire_date, conditional_order_id="cond-completed", status="COMPLETED"
            )
        ]
        reconciled = service.reconcile_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid)
        self.assertEqual(reconciled["status"], "triggered_completed")

        # A delayed explicit 4xx for the same original POST arrives late.
        updated = service._resolve_registration_outcome(
            proposal_uuid=proposal_uuid,
            account_id=self.account_id,
            now=_now_kst_naive(),
            outcome="registration_failed",
            error_code="some-explicit-rejection",
        )
        self.assertEqual(updated.status, "triggered_completed")

        conflict = self._conflict_audit(proposal_uuid)
        import json as _json

        detail = _json.loads(conflict.detail)
        self.assertEqual(detail["post_outcome"], "registration_failed")
        self.assertEqual(detail["post_error_code"], "some-explicit-rejection")

    def test_delayed_post_after_force_resolve_is_not_applied_but_audited(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        proposal_uuid = proposal["proposal_uuid"]
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid, confirm=True)

        service.force_resolve_proposal(
            account_id=self.account_id,
            proposal_uuid=proposal_uuid,
            confirm=True,
            reason="Confirmed via Toss app: no matching conditional order exists.",
        )
        stored = self.repo.get_conditional_order_proposal(proposal_uuid, account_id=self.account_id)
        self.assertEqual(stored.status, "registration_failed")

        # The original POST turns out to have actually succeeded, arriving
        # only after the operator already force-resolved the proposal.
        updated = service._resolve_registration_outcome(
            proposal_uuid=proposal_uuid,
            account_id=self.account_id,
            now=_now_kst_naive(),
            outcome="approved",
            conditional_order_id="cond-too-late",
        )
        self.assertEqual(updated.status, "registration_failed")

        conflict = self._conflict_audit(proposal_uuid)
        import json as _json

        detail = _json.loads(conflict.detail)
        self.assertEqual(detail["post_outcome"], "approved")
        self.assertEqual(detail["post_conditional_order_id"], "cond-too-late")
        self.assertEqual(detail["local_status_after"], "registration_failed")

    def test_approved_write_unique_conflict_falls_back_to_registration_unknown_with_no_id(self) -> None:
        """R1d (3rd-round review): approve's own POST succeeds and returns a
        real conditionalOrderId, but writing 'approved' with that ID onto
        this row hits the partial unique index because another proposal
        already owns it. Root cause of the prior bug: the fallback used to
        retry the SAME id, hitting the SAME index a second time and
        surfacing as an unhandled OrderAuditPersistFailedError (500) with
        the proposal stuck in 'approving'. Fixed: the fallback must land on
        registration_unknown with NO id recorded (recoverable via
        force-resolve), never propagate a raw exception, and leave a
        conditional_registration_conflict audit trail naming the real
        (unrecorded) ID."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_result={"conditionalOrderId": "cond-shared"}
        )
        service = self._make_service(fetcher)

        proposal_a = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        result_a = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_a["proposal_uuid"], confirm=True)
        self.assertEqual(result_a["status"], "approved")
        self.assertEqual(result_a["toss_conditional_order_id"], "cond-shared")

        proposal_b = service.create_proposal(
            account_id=self.account_id,
            symbol="000660.KS",
            side="sell",
            trigger_price=70000,
            limit_price=69000,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        # Fetcher is still configured to return the SAME conditionalOrderId
        # -- models "this exact ID is already owned by another proposal at
        # write time" without depending on exactly how that came to be; the
        # unique index is what actually fires here, not a mock.
        result_b = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal_b["proposal_uuid"], confirm=True)

        self.assertEqual(result_b["status"], "registration_unknown")
        self.assertIsNone(result_b["toss_conditional_order_id"])

        # A's own registration is completely untouched by B's conflict.
        stored_a = self.repo.get_conditional_order_proposal(proposal_a["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored_a.status, "approved")
        self.assertEqual(stored_a.toss_conditional_order_id, "cond-shared")

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal_b["proposal_uuid"])
        conflicts = [a for a in audits if a.event == "conditional_registration_conflict"]
        self.assertEqual(len(conflicts), 1, f"expected exactly one conflict audit, got audits={[a.event for a in audits]}")
        conflict = conflicts[0]
        self.assertEqual(conflict.error_code, "owned-by-another-proposal")
        # The real POST id IS recorded on the conflict audit row (for
        # operator visibility) even though it is never written onto B's
        # proposal row itself (asserted via result_b above).
        self.assertEqual(conflict.toss_order_id, "cond-shared")
        import json as _json

        detail = _json.loads(conflict.detail)
        self.assertEqual(detail["post_outcome"], "approved")
        self.assertEqual(detail["post_conditional_order_id"], "cond-shared")
        self.assertEqual(detail["local_status_after"], "registration_unknown")

        # Recoverable -- registration_unknown is never a dead end.
        resolved = service.force_resolve_proposal(
            account_id=self.account_id,
            proposal_uuid=proposal_b["proposal_uuid"],
            confirm=True,
            reason="Confirmed via Toss app: cond-shared belongs to a different registration.",
        )
        self.assertEqual(resolved["status"], "registration_failed")

    def test_normal_convergence_logs_debug_and_appends_no_conflict_audit(self) -> None:
        """R2b (3rd-round review, minor): the ordinary convergence path
        (POST outcome recorded exactly as requested, no race, no conflict)
        must leave a DEBUG-level trace and must never append a
        conditional_registration_conflict audit event."""
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(place_conditional_order_result={"conditionalOrderId": "cond-normal"})
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        with self.assertLogs("src.services.portfolio_conditional_order_service", level="DEBUG") as cm:
            result = service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        self.assertEqual(result["status"], "approved")
        self.assertTrue(
            any("converged normally" in line for line in cm.output),
            f"expected a 'converged normally' DEBUG log line, got: {cm.output}",
        )

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        conflicts = [a for a in audits if a.event == "conditional_registration_conflict"]
        self.assertEqual(len(conflicts), 0)


class ConditionalOrderForceResolveTestCase(_ConditionalOrderTestBase):
    """Codex review major 3 / design spec §7: the authenticated,
    reason-required manual escape hatch for a permanently
    'registration_unknown' proposal."""

    def _make_registration_unknown_proposal(self) -> Dict[str, Any]:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        return proposal, service, fetcher

    def test_force_resolve_happy_path_releases_reservation(self) -> None:
        proposal, service, _fetcher = self._make_registration_unknown_proposal()
        proposal_uuid = proposal["proposal_uuid"]

        before = self.repo.sum_daily_reserved_and_executed_amount_krw(
            self.account_id, kst_date=datetime.now().date()
        )
        self.assertGreater(before, 0.0)

        result = service.force_resolve_proposal(
            account_id=self.account_id,
            proposal_uuid=proposal_uuid,
            confirm=True,
            reason="Confirmed via Toss app: no matching conditional order exists.",
        )
        self.assertEqual(result["status"], "registration_failed")

        after = self.repo.sum_daily_reserved_and_executed_amount_krw(
            self.account_id, kst_date=datetime.now().date()
        )
        self.assertEqual(after, 0.0)

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal_uuid)
        self.assertEqual(audits[-1].event, "cond_force_resolved")
        import json as _json

        self.assertIn("Confirmed via Toss app", _json.loads(audits[-1].detail)["reason"])

    def test_force_resolve_requires_confirm(self) -> None:
        proposal, service, _fetcher = self._make_registration_unknown_proposal()
        with self.assertRaises(ConfirmRequiredError):
            service.force_resolve_proposal(
                account_id=self.account_id,
                proposal_uuid=proposal["proposal_uuid"],
                confirm=False,
                reason="anything",
            )

    def test_force_resolve_requires_nonempty_reason(self) -> None:
        proposal, service, _fetcher = self._make_registration_unknown_proposal()
        with self.assertRaises(ValueError):
            service.force_resolve_proposal(
                account_id=self.account_id,
                proposal_uuid=proposal["proposal_uuid"],
                confirm=True,
                reason="   ",
            )

    def test_force_resolve_rejected_from_wrong_status(self) -> None:
        self._set_live(False)
        fetcher = FakeConditionalTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        # Still 'pending' — force-resolve only ever applies to
        # 'registration_unknown'.
        with self.assertRaises(ConditionalProposalNotForceResolvableError):
            service.force_resolve_proposal(
                account_id=self.account_id,
                proposal_uuid=proposal["proposal_uuid"],
                confirm=True,
                reason="anything",
            )


class ConditionalOrderCancelTestCase(_ConditionalOrderTestBase):
    def test_cancel_pending_never_calls_toss(self) -> None:
        self._set_live(False)
        fetcher = FakeConditionalTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        result = service.cancel_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(result["status"], "canceled")
        self.assertEqual(fetcher.cancel_conditional_order_calls, [])

    def test_cancel_approved_calls_toss_and_transitions_to_toss_canceled(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(place_conditional_order_result={"conditionalOrderId": "cond-99"})
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        result = service.cancel_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(result["status"], "toss_canceled")
        self.assertEqual(fetcher.cancel_conditional_order_calls, [("555", "cond-99")])

    def test_cancel_refused_while_registration_unknown(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(
            place_conditional_order_raise=TossOrderRejectedError(status_code=409, code="request-in-progress", message="x")
        )
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)
        with self.assertRaises(ConditionalProposalNotReconcilableError):
            service.cancel_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"])


class ConditionalOrderSyncTestCase(_ConditionalOrderTestBase):
    def test_list_with_lazy_refresh_observes_completed_status(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(place_conditional_order_result={"conditionalOrderId": "cond-7"})
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        fetcher._get_conditional_order_result = {"conditionalOrderId": "cond-7", "status": "COMPLETED"}
        rows = service.list_conditional_orders_with_lazy_refresh(account_id=self.account_id)
        matched = [r for r in rows if r["proposal_uuid"] == proposal["proposal_uuid"]][0]
        self.assertEqual(matched["status"], "triggered_completed")
        self.assertEqual(matched["toss_status"], "COMPLETED")

    def test_sync_proposals_reports_checked_and_updated_counts(self) -> None:
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(place_conditional_order_result={"conditionalOrderId": "cond-8"})
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        service.approve_proposal(account_id=self.account_id, proposal_uuid=proposal["proposal_uuid"], confirm=True)

        fetcher._get_conditional_order_result = {"conditionalOrderId": "cond-8", "status": "PAUSED"}
        result = service.sync_proposals(account_id=self.account_id)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["updated"], 1)
        stored = self.repo.get_conditional_order_proposal(proposal["proposal_uuid"], account_id=self.account_id)
        self.assertEqual(stored.status, "paused")


class ConditionalOrderCrossTypeDailyCapTestCase(_ConditionalOrderTestBase):
    """The load-bearing bidirectional daily-cap regression (design spec §3
    "한도 산입": "Phase 3 v3의 일일 한도 합산 로직에 조건주문 미확정분 합류") —
    a conditional-order reservation must count against, and be blocked by,
    the exact same daily ceiling as a Phase 3 plain-order reservation, in
    both directions."""

    def test_conditional_reservation_blocks_phase3_order(self) -> None:
        os.environ["TOSS_ORDER_DAILY_MAX_AMOUNT_KRW"] = "100000"
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(place_conditional_order_result={"conditionalOrderId": "cond-x"})
        cond_service = self._make_service(fetcher)
        order_service = PortfolioOrderService(portfolio_service=self.portfolio_service, repo=self.repo, fetcher=fetcher)

        cond_proposal = cond_service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=70000,
            limit_price=70000,
            quantity=1,  # 70,000 KRW
            expire_date=self._near_expire_date(),
        )
        cond_service.approve_proposal(account_id=self.account_id, proposal_uuid=cond_proposal["proposal_uuid"], confirm=True)

        # 70,000 (conditional, approved/held) + 70,000 (this new order) = 140,000 > 100,000 daily
        # cap — already visible at the Phase 3 best-effort create-time check,
        # proving the conditional reservation folds into Phase 3's own sum.
        with self.assertRaises(OrderLimitExceededError) as ctx:
            order_service.create_proposal(account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000)
        self.assertEqual(ctx.exception.limit_type, "daily")
        self.assertEqual(fetcher.place_order_calls, [])

    def test_phase3_reservation_blocks_conditional_registration(self) -> None:
        os.environ["TOSS_ORDER_DAILY_MAX_AMOUNT_KRW"] = "100000"
        self._set_live(True)
        fetcher = FakeConditionalTossFetcher(place_conditional_order_result={"conditionalOrderId": "cond-y"})
        cond_service = self._make_service(fetcher)
        order_service = PortfolioOrderService(portfolio_service=self.portfolio_service, repo=self.repo, fetcher=fetcher)

        order_proposal = order_service.create_proposal(
            account_id=self.account_id, symbol="005930.KS", side="buy", quantity=1, price=70000
        )
        order_service.execute_proposal(account_id=self.account_id, proposal_uuid=order_proposal["proposal_uuid"], confirm=True)

        # 70,000 (Phase 3, executed) + 70,000 (this conditional proposal) =
        # 140,000 > 100,000 daily cap — already visible at the Phase 4
        # best-effort create-time check, proving the Phase 3 reservation
        # folds into the conditional-order service's own sum.
        with self.assertRaises(OrderLimitExceededError) as ctx:
            cond_service.create_proposal(
                account_id=self.account_id,
                symbol="005930.KS",
                side="sell",
                trigger_price=70000,
                limit_price=70000,
                quantity=1,
                expire_date=self._near_expire_date(),
            )
        self.assertEqual(ctx.exception.limit_type, "daily")
        self.assertEqual(fetcher.place_conditional_order_calls, [])


class ConditionalOrderAuditAppendOnlyTestCase(_ConditionalOrderTestBase):
    def test_cond_events_are_append_only(self) -> None:
        self._set_live(False)
        fetcher = FakeConditionalTossFetcher()
        service = self._make_service(fetcher)
        proposal = service.create_proposal(
            account_id=self.account_id,
            symbol="005930.KS",
            side="sell",
            trigger_price=65000,
            limit_price=64500,
            quantity=1,
            expire_date=self._near_expire_date(),
        )
        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal["proposal_uuid"])
        self.assertEqual(audits[0].event, "cond_proposed")

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


class ConditionalOrderApiTestCase(unittest.TestCase):
    """API-level: every Phase 4 endpoint — reads included (Codex BLOCK
    review major 2) — requires ADMIN_AUTH_ENABLED=true + a verified session
    via _require_order_auth. Mirrors
    tests/test_portfolio_order_service.py::PortfolioOrderApiTestCase."""

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
        self.db_path = self.data_dir / "portfolio_conditional_order_api_test.db"
        self._write_env(auth_enabled=False)
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()

        from api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(static_dir=self.data_dir / "empty-static")
        self.client = TestClient(app)
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

    def test_all_nine_conditional_order_endpoints_403_when_auth_disabled(self) -> None:
        dummy_uuid = "does-not-exist"
        responses = {
            "create": self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/proposals",
                json={
                    "symbol": "005930.KS",
                    "side": "sell",
                    "trigger_price": 65000,
                    "limit_price": 64500,
                    "quantity": 1,
                    "expire_date": "2026-07-26",
                },
            ),
            "list_proposals": self.client.get(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/proposals"
            ),
            "get_proposal": self.client.get(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/proposals/{dummy_uuid}"
            ),
            "approve": self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/proposals/{dummy_uuid}/approve",
                json={"confirm": True},
            ),
            "reconcile": self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/proposals/{dummy_uuid}/reconcile"
            ),
            "cancel": self.client.delete(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/proposals/{dummy_uuid}"
            ),
            "observe_list": self.client.get(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders"
            ),
            "sync": self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/sync"
            ),
            "force_resolve": self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/proposals/{dummy_uuid}/force-resolve",
                json={"confirm": True, "reason": "test"},
            ),
        }

        for label, resp in responses.items():
            self.assertEqual(resp.status_code, 403, f"{label}: {resp.text}")
            self.assertEqual(resp.json().get("error"), "order-auth-required", label)

    def test_create_approve_dry_run_round_trip_with_verified_session(self) -> None:
        fetcher = FakeConditionalTossFetcher()
        expire_date = (_now_kst_naive().date() + timedelta(days=1)).isoformat()
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ), patch("src.services.portfolio_order_service.TossFetcher") as mock_cls:
            mock_cls.has_configured_credentials.return_value = True
            mock_cls.return_value = fetcher
            mock_cls.is_order_live_enabled.return_value = False
            self.client.cookies.set("dsa_session", "any-non-empty-value")

            create_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/proposals",
                json={
                    "symbol": "005930.KS",
                    "side": "sell",
                    "trigger_price": 65000,
                    "limit_price": 64500,
                    "quantity": 1,
                    "expire_date": expire_date,
                },
            )
            self.assertEqual(create_resp.status_code, 200, create_resp.text)
            proposal_uuid = create_resp.json()["proposal_uuid"]

            approve_resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/proposals/{proposal_uuid}/approve",
                json={"confirm": True},
            )
            self.assertEqual(approve_resp.status_code, 200, approve_resp.text)
            self.assertEqual(approve_resp.json()["status"], "dry_run_approved")

    def test_oco_type_field_rejected_with_422(self) -> None:
        with patch("api.v1.endpoints.portfolio.is_auth_enabled", return_value=True), patch(
            "api.v1.endpoints.portfolio.verify_session", return_value=True
        ):
            self.client.cookies.set("dsa_session", "any-non-empty-value")
            resp = self.client.post(
                f"/api/v1/portfolio/links/{self.account_id}/conditional-orders/proposals",
                json={
                    "symbol": "005930.KS",
                    "side": "sell",
                    "trigger_price": 65000,
                    "limit_price": 64500,
                    "quantity": 1,
                    "expire_date": "2026-07-26",
                    "type": "OCO",
                },
            )
            self.assertEqual(resp.status_code, 422, resp.text)

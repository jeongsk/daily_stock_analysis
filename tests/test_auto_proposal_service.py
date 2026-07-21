# -*- coding: utf-8 -*-
"""Offline tests for the Toss defensive-signal auto-proposal batch generator
(Phase 5 — design spec
docs/superpowers/specs/2026-07-20-toss-auto-proposal-phase5-design.md).

Highest-priority coverage (design spec §6 "검증 계획"):

- Safety invariant: the generator never calls execute_proposal/
  approve_proposal (Toss write) under any code path — only create_proposal
  (draft). This is spied on directly, not inferred.
- Reverse-mapping reuse: held positions with no active defensive signal
  produce nothing; kr holdings are included (the deliberate market-scope
  widening documented in portfolio_defensive_signals).
- Filters: confidence (None and below-threshold), plan_quality
  (unknown/minimal), malformed stop_loss.
- Sizing: sell -> full held quantity, reduce -> floor(held/2), reduce with a
  1-share holding -> skip (floors to 0).
- Order-type routing: stop_loss present -> Phase 4 conditional STOP with
  slippage applied to both trigger legs; stop_loss absent -> Phase 3 LIMIT
  sell priced off a live quote, slippage applied; quote unavailable -> skip
  (fail-closed, not a fabricated price).
- Idempotency (v6, Codex adversarial review F1+F2): the DB key is the
  composite ``(account_id, source_signal_id, generation_date)`` — a same-day
  batch re-run produces zero new proposals via BOTH distinct paths (the
  active-proposal dedup short-circuit, and separately the actual DB-level
  composite-unique-index IntegrityError path once the first proposal reaches
  a terminal state so the dedup check no longer short-circuits — Codex
  review M4). Two different accounts holding the same signal (F1) both get a
  proposal in the *same* run. A signal whose earlier proposal genuinely
  TTL-expired (real elapsed time, not a hand-set 'expired' status — F2's own
  reviewed counterexample) is free to produce a new proposal on the *next*
  day's ``generation_date``, but a same-day retry after expiry/cancellation
  is still suppressed (intended: a canceled/expired proposal today does not
  silently reappear hours later within the same batch day).
- Migration swap: a DB carrying the pre-v6 single-column
  ``uix_..._source_signal`` index (a real upgrade path, not a fresh DB) has
  that index replaced — dropped, not left in place — by the v6 composite
  index on the next ``DatabaseManager.get_instance()``.
- Activation gating: PHASE5_AUTO_PROPOSAL_ENABLED unset, zero Toss-linked
  accounts, and (Codex review B1) a missing/invalid idempotency unique index
  are all a fail-closed no-op.
- Per-signal isolation (Codex review M1): any exception anywhere in
  per-signal processing (not just create_proposal) skips only that signal.
- Pre-batch sync (Codex review M2): sync_linked_account is attempted for
  every Toss-linked account before reading positions; a sync failure for one
  account never blocks the batch.
- TTL parity (Codex review M3): a TTL-expired pending proposal nobody polled
  does not count as "active" for the dedup check.
- F4 (Codex adversarial review): an immediate-sell proposal is priced off a
  freshness-verified quote (timestamped, within
  PHASE5_QUOTE_MAX_AGE_SECONDS) — stale/untimed quotes skip fail-closed, not
  a fabricated price. ``execute_proposal`` additionally re-confirms an
  auto-generated proposal's price at execute time, gated strictly on
  ``generation_source == 'auto'`` (a manual proposal's execute path is
  unaffected) — refusing execution on a materially drifted price.
- Real over-sell backstop: ``execute_proposal``'s existing sellable-quantity
  re-check (unmodified, generic to every proposal regardless of
  ``generation_source``) is pinned here for an auto-generated proposal
  specifically, since the idempotency key above is an accuracy/UX property,
  not the actual paranoia backstop against over-selling.

TossFetcher and the realtime-quote provider are always faked here — this
suite makes no real HTTP/network calls. ``sync_linked_account`` is exercised
via its natural missing-credentials failure path (no TOSS_CLIENT_ID/SECRET in
the test env), which fails before any network call — also offline.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy import inspect as sa_inspect

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from api.app import create_app
from fastapi.testclient import TestClient
from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.auto_proposal_service import (
    _CONDITIONAL_STOP_EXECUTION_RISK_NOTE,
    AutoProposalService,
    format_batch_summary,
    run_phase5_auto_proposal_batch,
)
from src.services.decision_signal_service import DecisionSignalService
from src.services.portfolio_broker_sync_service import PortfolioBrokerSyncService
from src.services.portfolio_conditional_order_service import PortfolioConditionalOrderService
from src.services.portfolio_order_service import (
    InsufficientSellableQuantityError,
    PortfolioOrderService,
    StalePriceReconfirmationRequiredError,
)
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager, PortfolioOrderProposal


class _FakeTossFetcher:
    """Deterministic stand-in for TossFetcher — mirrors
    tests/test_portfolio_order_service.py::FakeOrderTossFetcher, trimmed to
    what create_proposal's own validation calls need. ``execute``/``approve``
    (place_order) is spied so the safety-invariant test can assert zero
    calls."""

    def __init__(self, *, buying_power: float = 10_000_000.0, sellable_quantity: float = 1_000_000.0) -> None:
        self.buying_power = buying_power
        self.sellable_quantity = sellable_quantity
        self.place_order_calls: List[Any] = []
        self.cancel_order_calls: List[Any] = []

    def get_buying_power(self, account_seq: Any, currency: str) -> float:
        return self.buying_power

    def get_sellable_quantity(self, account_seq: Any, symbol: str) -> float:
        return self.sellable_quantity

    def place_order(self, account_seq: Any, **kwargs: Any) -> Dict[str, Any]:
        self.place_order_calls.append((account_seq, kwargs))
        return {"orderId": "toss-order-should-never-happen"}

    def cancel_order(self, account_seq: Any, order_id: str) -> Dict[str, Any]:
        self.cancel_order_calls.append((account_seq, order_id))
        return {"orderId": order_id}

    def get_order(self, account_seq: Any, order_id: str) -> Dict[str, Any]:
        return {"orderId": order_id, "status": "FILLED"}


class AutoProposalServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"
        self.db_path = Path(self.temp_dir.name) / "auto_proposal_test.db"
        self._write_env()
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()

        self.db = DatabaseManager.get_instance()
        self.portfolio_service = PortfolioService()
        self.repo = PortfolioRepository()
        self.decision_signal_service = DecisionSignalService(portfolio_repo=self.repo)

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

        self.fetcher = _FakeTossFetcher()
        self.order_service = PortfolioOrderService(
            portfolio_service=self.portfolio_service, repo=self.repo, fetcher=self.fetcher
        )
        self.conditional_order_service = PortfolioConditionalOrderService(
            portfolio_service=self.portfolio_service, repo=self.repo, fetcher=self.fetcher
        )
        # Snapshot/quote calls resolve through data_provider.base.DataFetcherManager
        # regardless of caller — patched for the lifetime of each test so both
        # the portfolio-snapshot position pricing and this module's own
        # immediate-sell reference-price lookup are deterministic and offline.
        self._quote_patcher = patch("data_provider.base.DataFetcherManager")
        self._mock_fetcher_manager_cls = self._quote_patcher.start()
        self._set_quote_price(100.0)

    def tearDown(self) -> None:
        self._quote_patcher.stop()
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("PHASE5_AUTO_PROPOSAL_ENABLED", None)
        os.environ.pop("PHASE5_MIN_CONFIDENCE", None)
        os.environ.pop("PHASE5_SELL_SLIPPAGE_BPS", None)
        os.environ.pop("PHASE5_EXECUTE_PRICE_DRIFT_BPS", None)
        self.temp_dir.cleanup()

    def _write_env(self, extra_lines: Optional[List[str]] = None) -> None:
        lines = [
            "STOCK_LIST=600519",
            "GEMINI_API_KEY=test",
            "ADMIN_AUTH_ENABLED=false",
            "PHASE5_AUTO_PROPOSAL_ENABLED=true",
            f"DATABASE_PATH={self.db_path}",
        ]
        lines.extend(extra_lines or [])
        self.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _epoch() -> datetime:
        return datetime(2026, 1, 1, 0, 0, 0)

    def _set_quote_price(self, price: Optional[float], *, age_seconds: float = 5.0) -> None:
        """``age_seconds`` (F4, Codex adversarial review): explicitly sets
        ``provider_timestamp``/``stale_seconds`` (not left to MagicMock's
        auto-vivified attributes, whose default ``__float__``/``__str__``
        magic-method behavior would otherwise make
        ``_fetch_fresh_reference_quote`` accidentally "work" without ever
        exercising the real freshness check) — default 5s is comfortably
        under ``PHASE5_QUOTE_MAX_AGE_SECONDS``'s default 600s. Use
        ``_set_quote_price_stale``/``_set_quote_price_no_timestamp`` for the
        fail-closed cases."""
        if price is None:
            self._mock_fetcher_manager_cls.return_value.get_realtime_quote.return_value = None
        else:
            # ``source=None`` is required, not cosmetic: an unset MagicMock
            # attribute auto-vivifies into a nested MagicMock rather than
            # None, and _fetch_realtime_position_price's
            # ``getattr(source, "value", None) or str(source)`` would then
            # smuggle a non-JSON-serializable MagicMock into the snapshot
            # payload (get_portfolio_snapshot persists it via json.dumps).
            self._mock_fetcher_manager_cls.return_value.get_realtime_quote.return_value = MagicMock(
                price=price,
                source=None,
                provider_timestamp="2026-01-01T00:00:00+00:00",
                stale_seconds=age_seconds,
            )

    def _set_quote_price_stale(self, price: float, *, age_seconds: float) -> None:
        """A quote with a real timestamp, but older than
        PHASE5_QUOTE_MAX_AGE_SECONDS — must fail closed, not "the last known
        price"."""
        self._set_quote_price(price, age_seconds=age_seconds)

    def _set_quote_price_no_timestamp(self, price: float) -> None:
        """A quote whose provider never reported a market timestamp at all —
        must fail closed regardless of PHASE5_QUOTE_MAX_AGE_SECONDS (design
        spec F4a "타임스탬프 없음 시 fail-closed skip")."""
        self._mock_fetcher_manager_cls.return_value.get_realtime_quote.return_value = MagicMock(
            price=price, source=None, provider_timestamp=None, stale_seconds=None
        )

    def _make_auto_service(self) -> AutoProposalService:
        return AutoProposalService(
            repo=self.repo,
            portfolio_service=self.portfolio_service,
            decision_signal_service=self.decision_signal_service,
            order_service=self.order_service,
            conditional_order_service=self.conditional_order_service,
            config=Config.get_instance(),
        )

    def _create_position(
        self, symbol: str, *, quantity: float = 10, price: float = 100.0, account_id: Optional[int] = None
    ) -> None:
        self.portfolio_service.record_trade(
            account_id=account_id if account_id is not None else self.account_id,
            symbol=symbol,
            trade_date=date(2026, 1, 1),
            side="buy",
            quantity=quantity,
            price=price,
            market="kr",
            currency="KRW",
        )

    def _create_signal(self, stock_code: str, action: str, **overrides: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stock_code": stock_code,
            "stock_name": stock_code,
            "market": "kr",
            "source_type": "manual",
            "trace_id": f"phase5-{stock_code}-{action}-{len(overrides)}",
            "trigger_source": "api",
            "action": action,
            "reason": f"{stock_code} {action} reason",
            "status": "active",
            "confidence": 0.8,
            "plan_quality": "complete",
        }
        payload.update(overrides)
        return self.decision_signal_service.create_signal(payload)["item"]

    # ------------------------------------------------------------------
    # Safety invariant (highest priority — design spec §6)
    # ------------------------------------------------------------------
    def test_never_calls_execute_or_approve_or_place_order(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", stop_loss=90.0)
        self._create_position("035420", quantity=10)
        self._create_signal("035420", "sell")  # no stop_loss -> immediate LIMIT path

        with patch.object(PortfolioOrderService, "execute_proposal") as spy_execute, \
                patch.object(PortfolioConditionalOrderService, "approve_proposal") as spy_approve:
            service = self._make_auto_service()
            result = service.run_batch()

        self.assertEqual(spy_execute.call_count, 0)
        self.assertEqual(spy_approve.call_count, 0)
        self.assertEqual(self.fetcher.place_order_calls, [])
        self.assertEqual(result.generated_count, 2)

        # Every created proposal must actually be 'pending' (draft), never executed.
        pending_orders = self.order_service.list_proposals(account_id=self.account_id)
        pending_conditionals = self.conditional_order_service.list_proposals(account_id=self.account_id)
        statuses = {p["status"] for p in pending_orders} | {p["status"] for p in pending_conditionals}
        self.assertEqual(statuses, {"pending"})

    # ------------------------------------------------------------------
    # Activation gating
    # ------------------------------------------------------------------
    def test_disabled_flag_is_noop(self) -> None:
        # Explicit empty override, not pop(): the .env file on disk (written
        # by _write_env) still has PHASE5_AUTO_PROPOSAL_ENABLED=true, and
        # dotenv loading does not override an os.environ key that already
        # exists — popping it would let the file value silently reappear.
        os.environ["PHASE5_AUTO_PROPOSAL_ENABLED"] = ""
        Config.reset_instance()
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")

        result = run_phase5_auto_proposal_batch(config=Config.get_instance(), service=self._make_auto_service())
        self.assertIsNone(result)
        self.assertEqual(self.order_service.list_proposals(account_id=self.account_id), [])

    def test_no_toss_linked_accounts_is_noop(self) -> None:
        self.repo.deactivate_broker_link(self.account_id)
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")

        result = run_phase5_auto_proposal_batch(config=Config.get_instance(), service=self._make_auto_service())
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    def test_confidence_none_is_skipped(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", confidence=None)
        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("confidence", result.skipped[0]["reason"])

    def test_confidence_below_threshold_is_skipped(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", confidence=0.4)
        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 0)
        self.assertEqual(len(result.skipped), 1)

    def test_plan_quality_unknown_is_skipped(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", plan_quality="unknown")
        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 0)

    def test_plan_quality_minimal_is_skipped(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", plan_quality="minimal")
        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 0)

    def test_alert_action_produces_no_proposal_but_is_counted(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "alert")
        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 0)
        self.assertEqual(result.alert_count, 1)
        self.assertEqual(len(result.skipped), 0)

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------
    def test_sell_action_sizes_full_held_quantity(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")
        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 1)
        self.assertEqual(result.generated[0]["quantity"], 10.0)

    def test_reduce_action_sizes_floor_half(self) -> None:
        self._create_position("005930", quantity=11)
        self._create_signal("005930", "reduce")
        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 1)
        self.assertEqual(result.generated[0]["quantity"], 5.0)

    def test_reduce_action_with_one_share_is_skipped(self) -> None:
        self._create_position("005930", quantity=1)
        self._create_signal("005930", "reduce")
        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("<= 0", result.skipped[0]["reason"])

    # ------------------------------------------------------------------
    # Order-type routing
    # ------------------------------------------------------------------
    def test_stop_loss_present_creates_conditional_stop_with_slippage(self) -> None:
        os.environ["PHASE5_SELL_SLIPPAGE_BPS"] = "100"  # 1%
        Config.reset_instance()
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", stop_loss=90.0)

        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 1)
        self.assertEqual(result.generated[0]["order_kind"], "conditional")

        rows = self.conditional_order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trigger_price"], 90.0)
        self.assertAlmostEqual(rows[0]["limit_price"], 90.0 * 0.99, places=6)
        self.assertEqual(rows[0]["generation_source"], "auto")

    def test_no_stop_loss_creates_plain_limit_priced_off_live_quote(self) -> None:
        os.environ["PHASE5_SELL_SLIPPAGE_BPS"] = "100"  # 1%
        Config.reset_instance()
        self._set_quote_price(71000.0)
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")

        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 1)
        self.assertEqual(result.generated[0]["order_kind"], "plain")

        rows = self.order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["price"], 71000.0 * 0.99, places=2)
        self.assertEqual(rows[0]["generation_source"], "auto")

    def test_no_stop_loss_and_quote_unavailable_is_skipped_fail_closed(self) -> None:
        self._set_quote_price(None)
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")

        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 0)
        self.assertEqual(len(result.skipped), 1)

    def test_malformed_stop_loss_is_skipped(self) -> None:
        # DecisionSignalService itself validates stop_loss as numeric at
        # write time, so exercise the generator's own defensive parse via a
        # hand-crafted match instead of going through create_signal.
        self._create_position("005930", quantity=10)
        signal = self._create_signal("005930", "sell", stop_loss=90.0)
        signal["stop_loss"] = "not-a-number"
        service = self._make_auto_service()
        from src.services import auto_proposal_service as mod

        match = {
            "account_id": self.account_id,
            "symbol": "005930",
            "market": "kr",
            "signal_stock_code": "005930",
            "signal": signal,
        }
        batch_result = mod.AutoProposalBatchResult()
        service._process_match(
            match,
            min_confidence=0.6,
            slippage=0.0,
            now=self._epoch(),
            generation_date=self._epoch().date(),
            result=batch_result,
        )
        self.assertEqual(batch_result.generated_count, 0)
        self.assertEqual(len(batch_result.skipped), 1)
        self.assertIn("not numeric", batch_result.skipped[0]["reason"])

    # ------------------------------------------------------------------
    # F4a — quote freshness at generation time (Codex adversarial review)
    # ------------------------------------------------------------------
    def test_stale_quote_beyond_max_age_is_skipped_fail_closed(self) -> None:
        self._create_position("005930", quantity=10)
        self._set_quote_price_stale(100.0, age_seconds=900.0)  # default max age is 600s
        self._create_signal("005930", "sell")

        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 0)
        self.assertEqual(len(result.skipped), 1)

    def test_quote_without_provider_timestamp_is_skipped_fail_closed(self) -> None:
        self._create_position("005930", quantity=10)
        self._set_quote_price_no_timestamp(100.0)
        self._create_signal("005930", "sell")

        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 0)
        self.assertEqual(len(result.skipped), 1)

    def test_plain_proposal_audit_detail_records_quote_metadata(self) -> None:
        """F4b: the proposal's own 'proposed' audit event records the quote
        it was priced off of (source/timestamp/age), not just the batch
        summary — a human reviewing the proposal later can see how current
        the price was at generation time."""
        self._create_position("005930", quantity=10)
        self._set_quote_price(71000.0, age_seconds=12.0)
        self._create_signal("005930", "sell")

        result = self._make_auto_service().run_batch()
        proposal_uuid = result.generated[0]["proposal_uuid"]

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal_uuid)
        proposed = next(a for a in audits if a.event == "proposed")
        detail = json.loads(proposed.detail)
        self.assertEqual(detail["quote_age_seconds"], 12)
        self.assertIn("quote_provider_timestamp", detail)

    # ------------------------------------------------------------------
    # F3 — conditional stop-loss "accept-and-disclose" (Codex adversarial
    # review): the gap-down/non-execution residual risk is never claimed to
    # be solved by the slippage collar, only disclosed on the proposal's own
    # audit trail and the batch summary.
    # ------------------------------------------------------------------
    def test_conditional_stop_proposal_audit_detail_carries_risk_disclosure(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", stop_loss=90.0)

        result = self._make_auto_service().run_batch()
        proposal_uuid = result.generated[0]["proposal_uuid"]

        audits = self.repo.list_order_audits(self.account_id, proposal_uuid=proposal_uuid)
        proposed = next(a for a in audits if a.event == "cond_proposed")
        detail = json.loads(proposed.detail)
        self.assertIn("execution_risk_disclosure", detail)
        self.assertEqual(detail["execution_risk_disclosure"], _CONDITIONAL_STOP_EXECUTION_RISK_NOTE)

    def test_batch_summary_includes_conditional_stop_risk_disclosure_when_generated(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", stop_loss=90.0)

        result = self._make_auto_service().run_batch()
        summary = format_batch_summary(result)
        self.assertIn(_CONDITIONAL_STOP_EXECUTION_RISK_NOTE, summary)

    def test_batch_summary_omits_risk_disclosure_when_no_conditional_generated(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")  # no stop_loss -> plain path only

        result = self._make_auto_service().run_batch()
        summary = format_batch_summary(result)
        self.assertNotIn(_CONDITIONAL_STOP_EXECUTION_RISK_NOTE, summary)

    def test_conditional_proposal_payload_exposes_risk_disclosure_for_auto_generated(self) -> None:
        """F3 follow-up (coordinator-confirmed, second review round): the
        risk disclosure must be visible on the same serialized payload the
        approve/list API returns inline — not only in the audit trail and
        batch notification — since the moment of approval is when the human
        actually accepts the residual risk."""
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", stop_loss=90.0)

        self._make_auto_service().run_batch()

        rows = self.conditional_order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["generation_source"], "auto")
        self.assertEqual(rows[0]["execution_risk_disclosure"], _CONDITIONAL_STOP_EXECUTION_RISK_NOTE)

    def test_conditional_proposal_approve_payload_exposes_risk_disclosure_for_auto_generated(self) -> None:
        """F3 follow-up: the disclosure must survive the actual approve call
        (the moment the human accepts the residual risk), not just be
        visible on a pre-approval list/get read."""
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", stop_loss=90.0)
        result = self._make_auto_service().run_batch()
        proposal_uuid = result.generated[0]["proposal_uuid"]

        approved = self.conditional_order_service.approve_proposal(
            account_id=self.account_id, proposal_uuid=proposal_uuid, confirm=True
        )
        self.assertEqual(approved["generation_source"], "auto")
        self.assertEqual(approved["execution_risk_disclosure"], _CONDITIONAL_STOP_EXECUTION_RISK_NOTE)

    def test_manual_conditional_proposal_payload_omits_risk_disclosure(self) -> None:
        """A human creating a conditional order manually is explicitly
        configuring the STOP/LIMIT leg themselves; the disclosure field
        stays null so it's clear this note was not injected for this
        proposal (distinguishing it from an auto-generated one)."""
        self._create_position("005930", quantity=10)

        created = self.conditional_order_service.create_proposal(
            account_id=self.account_id,
            symbol="005930",
            side="sell",
            trigger_price=90.0,
            limit_price=89.0,
            quantity=5,
            expire_date=date.today() + timedelta(days=1),
        )
        self.assertEqual(created["generation_source"], "manual")
        self.assertIsNone(created["execution_risk_disclosure"])

        fetched = self.conditional_order_service.get_proposal(
            account_id=self.account_id, proposal_uuid=created["proposal_uuid"]
        )
        self.assertIsNone(fetched["execution_risk_disclosure"])

    def test_plain_order_proposal_payload_has_no_risk_disclosure_key(self) -> None:
        """Plain (Phase 3) order proposals are a structurally different
        model/schema and must not carry the conditional-order-only
        disclosure field at all."""
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")  # no stop_loss -> plain path only

        result = self._make_auto_service().run_batch()
        self.assertNotIn("execution_risk_disclosure", result.generated[0])

    # ------------------------------------------------------------------
    # F4c — execute-time price reconfirm, auto-only (Codex adversarial
    # review): the immediate-sell path's residual control, since a human is
    # present at execute time to react. Never applies to a manual proposal.
    # ------------------------------------------------------------------
    def test_execute_time_reconfirm_no_fresh_quote_refuses_execution(self) -> None:
        self._create_position("005930", quantity=10)
        self._set_quote_price(100.0)
        self._create_signal("005930", "sell")
        result = self._make_auto_service().run_batch()
        proposal_uuid = result.generated[0]["proposal_uuid"]

        self._set_quote_price(None)  # quote now unobtainable at execute time
        with self.assertRaises(StalePriceReconfirmationRequiredError):
            self.order_service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid, confirm=True)

        proposals = self.order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(proposals[0]["status"], "failed")

    def test_execute_time_reconfirm_drifted_price_refuses_execution(self) -> None:
        self._create_position("005930", quantity=10)
        self._set_quote_price(100.0)
        self._create_signal("005930", "sell")
        result = self._make_auto_service().run_batch()
        proposal_uuid = result.generated[0]["proposal_uuid"]

        # Market cratered since generation -> drift far exceeds the default
        # 200bps PHASE5_EXECUTE_PRICE_DRIFT_BPS threshold.
        self._set_quote_price(50.0)
        with self.assertRaises(StalePriceReconfirmationRequiredError):
            self.order_service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid, confirm=True)

        proposals = self.order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(proposals[0]["status"], "failed")

    def test_execute_time_reconfirm_passes_with_fresh_stable_quote(self) -> None:
        self._create_position("005930", quantity=10)
        self._set_quote_price(100.0)
        self._create_signal("005930", "sell")
        result = self._make_auto_service().run_batch()
        proposal_uuid = result.generated[0]["proposal_uuid"]

        # Quote unchanged at execute time -> reconfirm passes, execution proceeds (dry-run).
        executed = self.order_service.execute_proposal(
            account_id=self.account_id, proposal_uuid=proposal_uuid, confirm=True
        )
        self.assertEqual(executed["status"], "dry_run_executed")

    def test_execute_time_reconfirm_boundary_drift_exactly_at_threshold_refuses_execution(self) -> None:
        """Codex re-review R3: the design spec and .env.example document
        ``PHASE5_EXECUTE_PRICE_DRIFT_BPS`` as an 'at least' (이상) threshold
        — a drift exactly equal to it must still refuse execution. A plain
        `>` comparison would silently let this exact-boundary case through
        (the bug Codex flagged). Slippage is pinned to 0 and the threshold
        to a power-of-two-friendly 2500bps (25%) so the arithmetic lands on
        the boundary with bit-for-bit precision rather than an epsilon that
        could round to either side of it (an arbitrary bps value like the
        200bps default does not reproduce reliably in binary floating
        point)."""
        os.environ["PHASE5_SELL_SLIPPAGE_BPS"] = "0"
        os.environ["PHASE5_EXECUTE_PRICE_DRIFT_BPS"] = "2500"
        Config.reset_instance()
        self._create_position("005930", quantity=10)
        self._set_quote_price(100.0)
        self._create_signal("005930", "sell")
        result = self._make_auto_service().run_batch()
        proposal_uuid = result.generated[0]["proposal_uuid"]
        stored_price = self.order_service.list_proposals(account_id=self.account_id)[0]["price"]
        self.assertEqual(stored_price, 100.0)  # slippage=0 -> stored price == quote price exactly

        slippage = self.order_service._phase5_sell_slippage_bps() / 10000.0
        threshold = self.order_service._execute_price_drift_bps() / 10000.0
        self.assertEqual(slippage, 0.0)
        self.assertEqual(threshold, 0.25)

        # recomputed_limit = 125.0 * (1 - 0) = 125.0; drift = |125-100|/100 = 0.25 exactly.
        self._set_quote_price(125.0)

        with self.assertRaises(StalePriceReconfirmationRequiredError):
            self.order_service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid, confirm=True)

        proposals = self.order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(proposals[0]["status"], "failed")

    def test_execute_time_reconfirm_only_applies_to_auto_generated_proposals(self) -> None:
        self._create_position("005930", quantity=10)
        self._set_quote_price(100.0)
        manual = self.order_service.create_proposal(
            account_id=self.account_id, symbol="005930", side="sell", quantity=1, order_type="LIMIT", price=95.0
        )
        # If the auto-only reconfirm ran for a manual proposal, this would
        # raise StalePriceReconfirmationRequiredError instead of executing.
        self._set_quote_price(None)
        executed = self.order_service.execute_proposal(
            account_id=self.account_id, proposal_uuid=manual["proposal_uuid"], confirm=True
        )
        self.assertEqual(executed["status"], "dry_run_executed")

    # ------------------------------------------------------------------
    # Real over-sell backstop (advisor): the idempotency key above is an
    # accuracy/UX property, not the actual paranoia guard against
    # over-selling — execute_proposal's/approve_proposal's existing
    # sellable-quantity re-check (unmodified, generic to every proposal) is
    # what protects against that, and it must apply identically to an
    # auto-generated proposal.
    # ------------------------------------------------------------------
    def test_execute_time_sellable_recheck_applies_to_auto_generated_plain_proposal(self) -> None:
        self._create_position("005930", quantity=10)
        self._set_quote_price(100.0)
        self._create_signal("005930", "sell")
        result = self._make_auto_service().run_batch()
        proposal_uuid = result.generated[0]["proposal_uuid"]

        # Position sold elsewhere (e.g. manually on the Toss app) since the
        # proposal was generated.
        self.fetcher.sellable_quantity = 0.0
        with self.assertRaises(InsufficientSellableQuantityError):
            self.order_service.execute_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid, confirm=True)

        proposals = self.order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(proposals[0]["status"], "failed")

    def test_approve_time_sellable_recheck_applies_to_auto_generated_conditional_proposal(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", stop_loss=90.0)
        result = self._make_auto_service().run_batch()
        proposal_uuid = result.generated[0]["proposal_uuid"]

        self.fetcher.sellable_quantity = 0.0
        with self.assertRaises(InsufficientSellableQuantityError):
            self.conditional_order_service.approve_proposal(
                account_id=self.account_id, proposal_uuid=proposal_uuid, confirm=True
            )

    # ------------------------------------------------------------------
    # Idempotency / dedup
    # ------------------------------------------------------------------
    def test_batch_rerun_same_day_is_idempotent(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", stop_loss=90.0)

        service = self._make_auto_service()
        first = service.run_batch()
        self.assertEqual(first.generated_count, 1)

        second = service.run_batch()
        self.assertEqual(second.generated_count, 0)
        self.assertEqual(len(second.skipped), 1)

        rows = self.conditional_order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(len(rows), 1)

    def test_existing_active_manual_proposal_blocks_auto_generation(self) -> None:
        self._create_position("005930", quantity=10)
        self._set_quote_price(100.0)
        # A pre-existing manual proposal for the same account/symbol/side.
        self.order_service.create_proposal(
            account_id=self.account_id, symbol="005930", side="sell", quantity=1, order_type="LIMIT", price=95.0
        )
        self._create_signal("005930", "sell")

        result = self._make_auto_service().run_batch()
        self.assertEqual(result.generated_count, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("active proposal", result.skipped[0]["reason"])

    def _create_second_toss_account(self) -> int:
        created = self.portfolio_service.create_account(
            name="Toss KR 2", broker="toss", market="kr", base_currency="KRW"
        )
        account_id = int(created["id"])
        self.repo.create_broker_link(
            account_id=account_id,
            provider="toss",
            external_account_seq="556",
            external_account_no="9876543210",
            linked_at=self._epoch(),
            snapshot_boundary_at=self._epoch(),
            last_synced_at=self._epoch(),
            active=True,
        )
        return account_id

    def test_two_toss_linked_accounts_holding_same_signal_both_get_proposals(self) -> None:
        """F1 (Codex adversarial review): the old global source_signal_id key
        let only the first account's insert succeed for the same signal
        identity, silently dropping the second account's defensive proposal.
        The v6 composite (account_id, source_signal_id, generation_date) key
        must let every account with a matching held position get its own
        proposal in the same batch run."""
        second_account_id = self._create_second_toss_account()
        self._create_position("005930", quantity=10)  # self.account_id
        self._create_position("005930", quantity=6, account_id=second_account_id)
        self._create_signal("005930", "sell")  # one shared signal identity

        result = self._make_auto_service().run_batch()

        self.assertEqual(result.generated_count, 2)
        self.assertEqual(len(result.skipped), 0)
        account_ids = {item["account_id"] for item in result.generated}
        self.assertEqual(account_ids, {self.account_id, second_account_id})
        quantities_by_account = {item["account_id"]: item["quantity"] for item in result.generated}
        self.assertEqual(quantities_by_account[self.account_id], 10.0)
        self.assertEqual(quantities_by_account[second_account_id], 6.0)

        self.assertEqual(len(self.order_service.list_proposals(account_id=self.account_id)), 1)
        self.assertEqual(len(self.order_service.list_proposals(account_id=second_account_id)), 1)

    def test_next_day_regeneration_after_real_ttl_expiry_is_allowed(self) -> None:
        """F2 (Codex adversarial review) — the exact reviewed counterexample:
        genuine TTL elapse (``expires_at`` moved to the past), ``status``
        left ``'pending'`` (never hand-set to ``'expired'`` — a status-based
        partial index would have incorrectly kept blocking this forever,
        which is precisely why the architect's convergence contract rejected
        that design). A later batch computing a *different*
        ``generation_date`` must be free to produce a new proposal for the
        same still-active signal; a same-day retry (already covered by
        ``test_batch_rerun_same_day_is_idempotent`` and the
        dedup-bypass/IntegrityError tests below) stays suppressed."""
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")

        service = self._make_auto_service()
        first = service.run_batch()
        self.assertEqual(first.generated_count, 1)
        first_proposal_uuid = first.generated[0]["proposal_uuid"]

        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioOrderProposal).where(PortfolioOrderProposal.proposal_uuid == first_proposal_uuid)
            ).scalar_one()
            self.assertEqual(row.status, "pending")  # never hand-set to 'expired'
            first_generation_date = row.generation_date
            row.expires_at = datetime(2020, 1, 1)  # genuine TTL elapse, real time in the past
            session.commit()

        next_day = datetime.combine(first_generation_date, datetime.min.time()) + timedelta(days=1, hours=9)
        with patch("src.services.auto_proposal_service._now_kst_naive", return_value=next_day):
            second = service.run_batch()

        self.assertEqual(second.generated_count, 1)
        self.assertEqual(len(second.skipped), 0)

        rows = self.order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(len(rows), 2)
        generation_dates = {row["generation_date"] for row in rows}
        self.assertEqual(len(generation_dates), 2)
        self.assertIn(first_generation_date.isoformat(), generation_dates)
        self.assertIn(next_day.date().isoformat(), generation_dates)

    # ------------------------------------------------------------------
    # generation_source additive columns / API filter plumbing
    # ------------------------------------------------------------------
    def test_manual_proposal_defaults_to_manual_generation_source(self) -> None:
        self._create_position("005930", quantity=10)
        data = self.order_service.create_proposal(
            account_id=self.account_id, symbol="005930", side="sell", quantity=1, order_type="LIMIT", price=95.0
        )
        self.assertEqual(data["generation_source"], "manual")
        self.assertIsNone(data["source_signal_id"])

    def test_list_proposals_generation_source_filter(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")
        self._make_auto_service().run_batch()
        self.order_service.create_proposal(
            account_id=self.account_id, symbol="005930", side="buy", quantity=1, order_type="LIMIT", price=95.0
        )

        auto_only = self.order_service.list_proposals(account_id=self.account_id, generation_source="auto")
        manual_only = self.order_service.list_proposals(account_id=self.account_id, generation_source="manual")
        self.assertEqual(len(auto_only), 1)
        self.assertEqual(len(manual_only), 1)

    def test_api_list_proposals_rejects_invalid_generation_source_with_422(self) -> None:
        client = TestClient(create_app(static_dir=Path(self.temp_dir.name) / "empty-static"))
        response = client.get(
            f"/api/v1/portfolio/links/{self.account_id}/orders/proposals",
            params={"generation_source": "bogus"},
        )
        self.assertEqual(response.status_code, 422)

    # ------------------------------------------------------------------
    # Codex review B1 — fail-closed on a missing idempotency index
    # ------------------------------------------------------------------
    def test_missing_source_signal_unique_index_refuses_to_run(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")

        with patch.object(PortfolioRepository, "has_idempotency_unique_indexes", return_value=False):
            result = self._make_auto_service().run_batch()

        self.assertEqual(result.generated_count, 0)
        self.assertEqual(len(result.skipped), 0)
        self.assertTrue(result.refused)
        self.assertTrue(result.refused_reason)
        self.assertEqual(self.order_service.list_proposals(account_id=self.account_id), [])
        self.assertEqual(self.conditional_order_service.list_proposals(account_id=self.account_id), [])

    def test_refused_batch_sends_distinct_error_notification_not_the_zero_summary(self) -> None:
        """Codex 2nd-round review minor: a refuse (index missing) must not
        look like a routine 'ran, found nothing' summary."""
        notifier = MagicMock()
        notifier.is_available.return_value = True

        with patch.object(PortfolioRepository, "has_idempotency_unique_indexes", return_value=False):
            result = run_phase5_auto_proposal_batch(
                notifier=notifier, config=Config.get_instance(), service=self._make_auto_service()
            )

        self.assertTrue(result.refused)
        notifier.send.assert_called_once()
        call_kwargs = notifier.send.call_args
        sent_text = call_kwargs[0][0]
        self.assertIn("refused", sent_text.lower())
        self.assertNotIn("0건 생성", sent_text)
        self.assertEqual(call_kwargs.kwargs.get("severity"), "error")

    def test_source_signal_unique_indexes_exist_after_normal_init(self) -> None:
        """Positive counterpart: a freshly-initialized DB (this suite's own
        setUp, which runs the full Phase 3/4 migration chain) always has
        both indexes, so the batch is not permanently fail-closed in the
        common case."""
        self.assertTrue(self.repo.has_idempotency_unique_indexes())

    # ------------------------------------------------------------------
    # Codex 2nd-round review M4 — real on-disk DB scenarios, not mocks,
    # for both the B1-a (init survival) and B1-b (definition, not just
    # name) fixes.
    # ------------------------------------------------------------------
    @contextmanager
    def _temporary_database(self, db_path: Path):
        """Point DatabaseManager/Config at ``db_path`` for the duration of
        the ``with`` block (running the real init/migration chain against
        it), then restore this test case's own DB singleton."""
        original_database_path = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        try:
            yield DatabaseManager.get_instance()
        finally:
            DatabaseManager.reset_instance()
            if original_database_path is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = original_database_path
            Config.reset_instance()
            DatabaseManager.get_instance()

    _LEGACY_ORDER_PROPOSAL_TABLE_SQL = """
        CREATE TABLE portfolio_order_proposals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          account_id INTEGER NOT NULL,
          proposal_uuid VARCHAR(36) NOT NULL UNIQUE,
          symbol VARCHAR(16) NOT NULL,
          storage_symbol VARCHAR(16) NOT NULL,
          market VARCHAR(8) NOT NULL,
          currency VARCHAR(8) NOT NULL,
          side VARCHAR(8) NOT NULL,
          order_type VARCHAR(8) NOT NULL DEFAULT 'LIMIT',
          price FLOAT,
          quantity FLOAT NOT NULL,
          est_amount_krw FLOAT NOT NULL,
          status VARCHAR(24) NOT NULL DEFAULT 'pending',
          toss_order_id VARCHAR(128),
          created_at DATETIME NOT NULL,
          expires_at DATETIME NOT NULL,
          reserved_at DATETIME,
          executed_at DATETIME,
          updated_at DATETIME{extra_columns}
        )
    """

    # v1.1-v5 columns (no generation_date yet) — used to simulate a DB that
    # already ran the pre-v6 migration.
    _V5_EXTRA_COLUMNS = (
        ",\n          generation_source VARCHAR(16) NOT NULL DEFAULT 'manual',"
        "\n          source_signal_id INTEGER"
    )
    # v6 columns (this upgrade's own target shape).
    _V6_EXTRA_COLUMNS = _V5_EXTRA_COLUMNS + ",\n          generation_date DATE"

    def test_legacy_pre_phase5_db_migration_creates_both_indexes(self) -> None:
        """A pre-Phase-5 DB (the two proposal tables exist without
        generation_source/source_signal_id/generation_date at all) must gain
        all three columns and the v6 composite index on the next
        DatabaseManager.get_instance() — this is the actual upgrade path a
        real deployment from *before* Phase 5 shipped goes through, not just
        a freshly-created test DB that never lacked the columns."""
        legacy_db_path = Path(self.temp_dir.name) / "legacy_pre_phase5.db"
        conn = sqlite3.connect(str(legacy_db_path))
        conn.execute(self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(extra_columns=""))
        conn.commit()
        conn.close()

        with self._temporary_database(legacy_db_path) as legacy_db:
            inspector_repo = PortfolioRepository(db_manager=legacy_db)
            self.assertTrue(legacy_db.has_portfolio_proposal_idempotency_unique_indexes())
            self.assertTrue(inspector_repo.has_idempotency_unique_indexes())

    def test_v5_single_column_index_migration_swap_drops_old_creates_composite(self) -> None:
        """advisor-flagged blind spot: a real deployment that already ran the
        v1.1-v5 code has the *old* single-column partial-unique
        ``uix_portfolio_order_proposal_source_signal`` index in place — not
        just a DB that never had any Phase 5 index at all (the legacy test
        above). The v6 migration must actually replace it: the old index is
        gone (``DROP INDEX``, not left dangling underneath the new one) and
        the new composite ``(account_id, source_signal_id,
        generation_date)`` index exists and passes the gate. Also asserts
        the swap is non-destructive: the pre-existing row (which only ever
        satisfied the *stricter* old single-column key) survives untouched
        and does not trip an IntegrityError on the new, strictly looser
        composite key."""
        v5_db_path = Path(self.temp_dir.name) / "v5_single_column_index.db"
        conn = sqlite3.connect(str(v5_db_path))
        conn.execute(self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(extra_columns=self._V5_EXTRA_COLUMNS))
        conn.execute(
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (source_signal_id) WHERE source_signal_id IS NOT NULL"
        )
        conn.execute(
            "INSERT INTO portfolio_order_proposals (account_id, proposal_uuid, symbol, "
            "storage_symbol, market, currency, side, order_type, price, quantity, "
            "est_amount_krw, status, created_at, expires_at, source_signal_id) VALUES "
            "(1, 'pre-existing-uuid', '005930', '005930.KS', 'kr', 'KRW', 'sell', 'LIMIT', "
            "90.0, 5, 450.0, 'pending', '2026-01-01', '2026-01-01', 42)"
        )
        conn.commit()
        conn.close()

        with self._temporary_database(v5_db_path) as v5_db:
            inspector = sa_inspect(v5_db._engine)
            index_names = {index["name"] for index in inspector.get_indexes("portfolio_order_proposals")}
            self.assertNotIn(
                "uix_portfolio_order_proposal_source_signal", index_names,
                "old v1.1-v5 single-column index must be dropped, not left in place",
            )
            self.assertIn("uix_portfolio_order_proposal_account_signal_date", index_names)
            self.assertTrue(v5_db.has_portfolio_proposal_idempotency_unique_indexes())

            # The pre-existing row survived the swap untouched.
            with v5_db.get_session() as session:
                row = session.execute(
                    select(PortfolioOrderProposal).where(
                        PortfolioOrderProposal.proposal_uuid == "pre-existing-uuid"
                    )
                ).scalar_one()
                self.assertEqual(row.source_signal_id, 42)
                self.assertIsNone(row.generation_date)

    def test_same_named_non_unique_index_bypass_is_detected_as_invalid(self) -> None:
        """Codex 2nd-round review M4-b (carried forward to the v6 composite
        index name): seed a same-named-as-the-v6-index but non-unique index
        (the exact B1-b bypass — ``CREATE UNIQUE INDEX IF NOT EXISTS`` is a
        no-op against it since SQLite's ``IF NOT EXISTS`` only checks the
        name) BEFORE Phase 5's own migration runs, then verify the checker
        reports it as missing/invalid using the real on-disk index
        definition — not a mocked return value."""
        bypass_db_path = Path(self.temp_dir.name) / "bypass_index.db"
        conn = sqlite3.connect(str(bypass_db_path))
        conn.execute(self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(extra_columns=self._V6_EXTRA_COLUMNS))
        # The bypass: an index with the exact name DatabaseManager will try
        # to create, but plain (no UNIQUE).
        conn.execute(
            "CREATE INDEX uix_portfolio_order_proposal_account_signal_date "
            "ON portfolio_order_proposals (account_id, source_signal_id, generation_date)"
        )
        conn.commit()
        conn.close()

        with self._temporary_database(bypass_db_path) as bypass_db:
            self.assertFalse(bypass_db.has_portfolio_proposal_idempotency_unique_indexes())
            inspector_repo = PortfolioRepository(db_manager=bypass_db)
            self.assertFalse(inspector_repo.has_idempotency_unique_indexes())

    def test_partial_composite_index_bypass_is_detected_as_invalid(self) -> None:
        """Codex adversarial re-review R1 (2026-07-21): the exact bypass it
        reproduced in-memory. Seed a *partial* unique index — same three
        columns, same order, same name the v6 migration itself would use —
        but restricted to ``WHERE status='approved'``, BEFORE Phase 5's own
        migration runs. ``CREATE UNIQUE INDEX IF NOT EXISTS`` then no-ops
        against it (name-only match, same bypass class as B1-b/M4-b above,
        one level up: this time on the composite index itself). A `pending`
        row sits outside that predicate, so two `pending` rows sharing the
        same (account_id, source_signal_id, generation_date) could insert
        without ever hitting an IntegrityError — the exact same-day
        duplicate this index exists to prevent. The gate must reject this:
        unique+column_names alone is not enough, the matching index must
        also be non-partial."""
        db_path = Path(self.temp_dir.name) / "partial_composite_bypass.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_account_signal_date "
            "ON portfolio_order_proposals (account_id, source_signal_id, generation_date) "
            "WHERE status='approved'",
        )
        with self._temporary_database(db_path) as db:
            # The migration's CREATE ... IF NOT EXISTS no-ops against the
            # pre-seeded same-named partial index (proving this is the same
            # silent-no-op bypass class as the name-only bugs above, not a
            # hypothetical).
            inspector = sa_inspect(db._engine)
            matching = [
                index
                for index in inspector.get_indexes("portfolio_order_proposals")
                if index["name"] == "uix_portfolio_order_proposal_account_signal_date"
            ]
            self.assertEqual(len(matching), 1)
            self.assertTrue(db._sqlite_index_is_partial("portfolio_order_proposals", matching[0]))

            self.assertFalse(db.has_portfolio_proposal_idempotency_unique_indexes())
            inspector_repo = PortfolioRepository(db_manager=db)
            self.assertFalse(inspector_repo.has_idempotency_unique_indexes())

    def test_non_partial_composite_index_alongside_partial_one_is_still_accepted(self) -> None:
        """Positive counterpart: if a non-partial composite index with the
        right shape exists (the actual v6 migration outcome) alongside an
        unrelated partial index someone else created under a different
        name, the gate must still pass — the fix rejects partial *matches*,
        not the mere presence of any partial index anywhere on the table."""
        db_path = Path(self.temp_dir.name) / "partial_and_non_partial_composite.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(extra_columns=self._V6_EXTRA_COLUMNS))
        conn.execute(
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_account_signal_date "
            "ON portfolio_order_proposals (account_id, source_signal_id, generation_date)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX some_other_partial_index "
            "ON portfolio_order_proposals (account_id, source_signal_id, generation_date) "
            "WHERE status='approved'"
        )
        conn.commit()
        conn.close()
        with self._temporary_database(db_path) as db:
            self.assertTrue(db.has_portfolio_proposal_idempotency_unique_indexes())

    # ------------------------------------------------------------------
    # L1 (Codex final re-review, on top of R1): the coordinator's own fix to
    # ``_sqlite_index_is_partial``'s DDL-text fallback (only reachable on a
    # SQLAlchemy version that does not reflect ``dialect_options
    # ['sqlite_where']``) — the fallback's ``\bwhere\b`` search was
    # originally run against the *whole* index DDL, so an index whose own
    # *name* happened to contain the substring "where" (before the column
    # list) was misclassified as partial and fail-closed rejected even
    # though it has no WHERE clause at all. The fix now searches only the
    # tail after the column list's closing ``)``. These tests call
    # ``_sqlite_index_is_partial`` directly with a hand-built index dict
    # whose ``dialect_options`` omits the ``sqlite_where`` key entirely
    # (simulating an older SQLAlchemy that never reflects it), forcing the
    # DDL-fallback branch regardless of which SQLAlchemy version this
    # environment actually runs (2.0.51 here does reflect it, so the
    # fallback is otherwise unreachable through ``get_indexes()`` alone).
    # ------------------------------------------------------------------
    def test_sqlite_index_is_partial_fallback_does_not_false_block_on_where_in_name(self) -> None:
        """(a) A non-partial composite index whose *name* contains the
        standalone word "where" (no actual WHERE clause) must be classified
        non-partial by the fallback — a false positive here would
        fail-closed-reject a perfectly valid migration outcome for no
        reason other than its name. The name uses a quoted identifier with
        "where" as its own space-delimited token (``"legacy where
        index"``) specifically because SQLite/regex word-boundary rules
        mean an underscore-joined name like ``..._where_check`` can never
        trigger the bug being regression-tested here at all (``_`` is a
        word character, so ``\\bwhere\\b`` never matches across an
        underscore) — this is the actual repro shape, not an approximation
        of it."""
        db_path = Path(self.temp_dir.name) / "fallback_where_in_name.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(extra_columns=self._V6_EXTRA_COLUMNS))
        conn.execute(
            'CREATE UNIQUE INDEX "legacy where index" '
            "ON portfolio_order_proposals (account_id, source_signal_id, generation_date)"
        )
        conn.commit()
        conn.close()

        with self._temporary_database(db_path) as db:
            synthetic_index = {
                "name": "legacy where index",
                "unique": True,
                "column_names": ["account_id", "source_signal_id", "generation_date"],
                "dialect_options": {},  # no "sqlite_where" key -> forces the DDL fallback
            }
            self.assertFalse(db._sqlite_index_is_partial("portfolio_order_proposals", synthetic_index))
            # And the full gate (which discovers dialect_options via the
            # real Inspector, not this synthetic dict) also passes, since
            # this SQLAlchemy version's preferred path agrees.
            self.assertTrue(db.has_portfolio_proposal_idempotency_unique_indexes())

    def test_sqlite_index_is_partial_fallback_still_detects_real_partial_predicate(self) -> None:
        """(b) Positive control for the same fallback branch: a genuinely
        partial index (real ``WHERE`` clause after the column list) must
        still be reported as partial — the name-substring fix must not
        have also broken detection of an actual predicate."""
        db_path = Path(self.temp_dir.name) / "fallback_real_partial.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(extra_columns=self._V6_EXTRA_COLUMNS))
        conn.execute(
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_account_signal_date "
            "ON portfolio_order_proposals (account_id, source_signal_id, generation_date) "
            "WHERE status='approved'"
        )
        conn.commit()
        conn.close()

        with self._temporary_database(db_path) as db:
            synthetic_index = {
                "name": "uix_portfolio_order_proposal_account_signal_date",
                "unique": True,
                "column_names": ["account_id", "source_signal_id", "generation_date"],
                "dialect_options": {},  # no "sqlite_where" key -> forces the DDL fallback
            }
            self.assertTrue(db._sqlite_index_is_partial("portfolio_order_proposals", synthetic_index))
            self.assertFalse(db.has_portfolio_proposal_idempotency_unique_indexes())

    def _seed_order_proposal_table_with_index(self, db_path: Path, index_sql: str) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(extra_columns=self._V6_EXTRA_COLUMNS))
        conn.execute(index_sql)
        conn.commit()
        conn.close()

    def test_wrong_column_set_is_detected_as_invalid(self) -> None:
        """A unique index on just ``(source_signal_id, generation_date)``
        (missing ``account_id`` — the exact F1 account-scoping gap this
        composite key exists to close) passes a naive "contains the right
        columns" check but not the exact-list check the gate actually uses.
        Must be rejected."""
        db_path = Path(self.temp_dir.name) / "missing_account_id.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_account_signal_date "
            "ON portfolio_order_proposals (source_signal_id, generation_date)",
        )
        with self._temporary_database(db_path) as db:
            self.assertFalse(db.has_portfolio_proposal_idempotency_unique_indexes())

    def test_wrong_column_order_is_detected_as_invalid(self) -> None:
        """``Inspector.get_indexes()`` returns columns in index-definition
        order — an index built with the same three columns but a different
        order is a structurally different (and, for SQLite, differently
        query-optimized) index, not interchangeable with the one this
        migration creates. Pinning this documents that the gate's column
        list is order-sensitive, not just set-equal."""
        db_path = Path(self.temp_dir.name) / "wrong_column_order.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_account_signal_date "
            "ON portfolio_order_proposals (source_signal_id, account_id, generation_date)",
        )
        with self._temporary_database(db_path) as db:
            self.assertFalse(db.has_portfolio_proposal_idempotency_unique_indexes())

    def test_quoted_identifier_composite_unique_index_is_accepted(self) -> None:
        """A unique index using SQLite's quoted-identifier syntax for every
        column is a perfectly valid, fully-enforcing composite constraint —
        the structural ``get_indexes()`` check (immune to quoting) must
        accept it, mirroring the v3-round quoted-identifier false-negative
        finding that originally motivated moving off DDL-text parsing."""
        db_path = Path(self.temp_dir.name) / "quoted_identifier_composite.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            'CREATE UNIQUE INDEX uix_portfolio_order_proposal_account_signal_date '
            'ON portfolio_order_proposals ("account_id", "source_signal_id", "generation_date")',
        )
        with self._temporary_database(db_path) as db:
            self.assertTrue(db.has_portfolio_proposal_idempotency_unique_indexes())

    def test_correctly_shaped_composite_index_under_a_different_name_is_accepted(self) -> None:
        """The gate is name-independent by design (Codex review history:
        name-only checks are exactly what let a same-named wrong-definition
        index bypass ``CREATE ... IF NOT EXISTS`` in earlier rounds) — any
        unique index with the right column list in the right order counts,
        regardless of what it is named."""
        db_path = Path(self.temp_dir.name) / "differently_named_composite.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX some_other_index_name "
            "ON portfolio_order_proposals (account_id, source_signal_id, generation_date)",
        )
        with self._temporary_database(db_path) as db:
            self.assertTrue(db.has_portfolio_proposal_idempotency_unique_indexes())

    def test_duplicate_composite_key_db_survives_init_and_batch_refuses(self) -> None:
        """Codex 2nd-round review M4-c, adapted to the v6 composite key: a DB
        with two rows sharing the same ``(account_id, source_signal_id,
        generation_date)`` triple (all three equal, not just
        source_signal_id — a genuine composite duplicate is only reachable
        with a matching non-null generation_date on both rows, since NULL
        generation_date rows never collide) is the exact precondition that
        makes SQLite's CREATE UNIQUE INDEX raise IntegrityError (B1-a). Must
        not crash DatabaseManager.get_instance() — reaching the assertions
        below at all is itself proof init survived — and the Phase 5 batch
        must then refuse to run against it end-to-end."""
        dup_db_path = Path(self.temp_dir.name) / "duplicate_composite_key.db"
        conn = sqlite3.connect(str(dup_db_path))
        conn.execute(self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(extra_columns=self._V6_EXTRA_COLUMNS))
        for uid in ("dup-uuid-1", "dup-uuid-2"):
            conn.execute(
                "INSERT INTO portfolio_order_proposals (account_id, proposal_uuid, symbol, "
                "storage_symbol, market, currency, side, order_type, price, quantity, "
                "est_amount_krw, status, created_at, expires_at, source_signal_id, "
                "generation_date) VALUES "
                "(1, ?, '005930', '005930.KS', 'kr', 'KRW', 'sell', 'LIMIT', 90.0, 5, 450.0, "
                "'pending', '2026-01-01', '2026-01-01', 99, '2026-01-01')",
                (uid,),
            )
        conn.commit()
        conn.close()

        with self._temporary_database(dup_db_path) as dup_db:
            # Reaching this line proves DatabaseManager.get_instance() did
            # not raise despite the duplicate rows (B1-a).
            self.assertFalse(dup_db.has_portfolio_proposal_idempotency_unique_indexes())
            repo = PortfolioRepository(db_manager=dup_db)
            auto_service = AutoProposalService(repo=repo, config=Config.get_instance())
            result = auto_service.run_batch()
            self.assertTrue(result.refused)
            self.assertEqual(result.generated_count, 0)

    # ------------------------------------------------------------------
    # Codex review M4 — the actual source_signal_id IntegrityError path
    # ------------------------------------------------------------------
    def test_rerun_after_dedup_bypass_hits_unique_index_integrity_error(self) -> None:
        """The plain 'rerun same day' test above (test_batch_rerun_same_day_
        is_idempotent) only reaches the active-proposal dedup short-circuit,
        since the first proposal is still 'pending'/active — it never
        exercises the source_signal_id unique-index IntegrityError path at
        all. This test cancels the first proposal (a legitimate terminal
        state a real user can reach) so the dedup check no longer
        short-circuits, then re-runs for the identical signal — the second
        create_proposal call must hit the DB's partial unique index (the
        canceled row still holds this source_signal_id) and be caught, not
        silently create a second row for the same signal."""
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell", stop_loss=90.0)

        service = self._make_auto_service()
        first = service.run_batch()
        self.assertEqual(first.generated_count, 1)
        proposal_uuid = first.generated[0]["proposal_uuid"]

        self.conditional_order_service.cancel_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid)

        second = service.run_batch()
        self.assertEqual(second.generated_count, 0)
        self.assertEqual(len(second.skipped), 1)
        self.assertIn("create_proposal failed", second.skipped[0]["reason"])
        self.assertNotIn("active proposal", second.skipped[0]["reason"])

        rows = self.conditional_order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "canceled")

    def test_rerun_after_dedup_bypass_hits_unique_index_integrity_error_plain_order_table(self) -> None:
        """Codex 2nd-round review M4-a: the equivalent IntegrityError path
        for the Phase 3 plain-order table (no stop_loss — the immediate-sell
        LIMIT route), not just the Phase 4 conditional table above."""
        self._create_position("005930", quantity=10)
        self._set_quote_price(100.0)
        self._create_signal("005930", "sell")  # no stop_loss -> plain LIMIT path

        service = self._make_auto_service()
        first = service.run_batch()
        self.assertEqual(first.generated_count, 1)
        self.assertEqual(first.generated[0]["order_kind"], "plain")
        proposal_uuid = first.generated[0]["proposal_uuid"]

        self.order_service.cancel_proposal(account_id=self.account_id, proposal_uuid=proposal_uuid)

        second = service.run_batch()
        self.assertEqual(second.generated_count, 0)
        self.assertEqual(len(second.skipped), 1)
        self.assertIn("create_proposal failed", second.skipped[0]["reason"])
        self.assertNotIn("active proposal", second.skipped[0]["reason"])

        rows = self.order_service.list_proposals(account_id=self.account_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "canceled")

    # ------------------------------------------------------------------
    # Codex review M1 — per-signal isolation extends past create_proposal
    # ------------------------------------------------------------------
    def test_unexpected_exception_outside_create_proposal_only_skips_that_signal(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")
        self._create_position("035420", quantity=10)
        self._create_signal("035420", "sell")

        real_get_quantity = PortfolioRepository.get_cached_position_quantity

        def _flaky_get_quantity(self_repo, **kwargs):
            if kwargs.get("symbol") == "005930":
                raise RuntimeError("simulated DB error outside create_proposal")
            return real_get_quantity(self_repo, **kwargs)

        with patch.object(PortfolioRepository, "get_cached_position_quantity", _flaky_get_quantity):
            result = self._make_auto_service().run_batch()

        self.assertEqual(result.generated_count, 1)
        self.assertEqual(result.generated[0]["symbol"], "035420")
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["symbol"], "005930")
        self.assertIn("unexpected error", result.skipped[0]["reason"])

    # ------------------------------------------------------------------
    # Codex review M2 — pre-batch sync, account-isolated best-effort
    # ------------------------------------------------------------------
    def test_sync_is_attempted_per_toss_linked_account_before_reading_positions(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")

        spy = MagicMock(wraps=self._make_auto_service().broker_sync_service.sync_linked_account)
        service = self._make_auto_service()
        with patch.object(service.broker_sync_service, "sync_linked_account", spy):
            result = service.run_batch()

        spy.assert_called_once_with(self.account_id)
        # Sync fails fast (no TOSS_CLIENT_ID/SECRET in the test env) but the
        # batch still proceeds and generates off the existing ledger state.
        self.assertEqual(result.generated_count, 1)

    def test_sync_failure_for_one_account_does_not_abort_batch(self) -> None:
        self._create_position("005930", quantity=10)
        self._create_signal("005930", "sell")

        service = self._make_auto_service()
        with patch.object(
            service.broker_sync_service, "sync_linked_account", side_effect=RuntimeError("upstream down")
        ):
            result = service.run_batch()

        # Sync failed, but generation still runs off the existing ledger.
        self.assertEqual(result.generated_count, 1)

    # ------------------------------------------------------------------
    # Codex review M3 — TTL-expired pending is not "active"
    # ------------------------------------------------------------------
    def _force_expire_order_proposal(self, proposal_uuid: str) -> None:
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioOrderProposal).where(PortfolioOrderProposal.proposal_uuid == proposal_uuid)
            ).scalar_one()
            row.expires_at = datetime(2020, 1, 1)
            session.commit()

    def test_ttl_expired_pending_manual_proposal_does_not_block_new_proposal(self) -> None:
        self._create_position("005930", quantity=10)
        self._set_quote_price(100.0)
        manual = self.order_service.create_proposal(
            account_id=self.account_id, symbol="005930", side="sell", quantity=1, order_type="LIMIT", price=95.0
        )
        self._force_expire_order_proposal(manual["proposal_uuid"])

        self._create_signal("005930", "sell")
        result = self._make_auto_service().run_batch()

        self.assertEqual(result.generated_count, 1)
        self.assertEqual(len(result.skipped), 0)

    # ------------------------------------------------------------------
    # Minor: always send the batch summary, even when everything is 0
    # ------------------------------------------------------------------
    def test_batch_summary_sent_even_when_nothing_happened(self) -> None:
        # No positions, no signals — the batch runs (flag on, account
        # linked) but has nothing to do.
        notifier = MagicMock()
        notifier.is_available.return_value = True

        result = run_phase5_auto_proposal_batch(
            notifier=notifier, config=Config.get_instance(), service=self._make_auto_service()
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.generated_count, 0)
        notifier.send.assert_called_once()
        sent_text = notifier.send.call_args[0][0]
        self.assertIn("0건", sent_text)


class FormatBatchSummaryTestCase(unittest.TestCase):
    def test_format_includes_counts_and_items(self) -> None:
        from src.services.auto_proposal_service import AutoProposalBatchResult

        result = AutoProposalBatchResult(
            generated=[{"symbol": "005930", "side": "sell", "quantity": 10.0, "order_kind": "plain"}],
            skipped=[{"reason": "x"}],
            alert_count=2,
        )
        text = format_batch_summary(result)
        self.assertIn("1건", text)
        self.assertIn("005930", text)
        self.assertIn("alert 2건", text)


if __name__ == "__main__":
    unittest.main()

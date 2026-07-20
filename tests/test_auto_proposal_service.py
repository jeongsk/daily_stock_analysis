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
- Idempotency: a same-day batch re-run produces zero new proposals via BOTH
  distinct paths — the active-proposal dedup short-circuit (still-pending
  first proposal) AND, separately, the actual DB-level source_signal_id
  unique-index IntegrityError path (first proposal moved to a terminal state
  so the dedup check no longer short-circuits) — Codex review M4: these are
  not the same code path and must both be exercised.
- Activation gating: PHASE5_AUTO_PROPOSAL_ENABLED unset, zero Toss-linked
  accounts, and (Codex review B1) a missing source_signal_id unique index are
  all a fail-closed no-op.
- Per-signal isolation (Codex review M1): any exception anywhere in
  per-signal processing (not just create_proposal) skips only that signal.
- Pre-batch sync (Codex review M2): sync_linked_account is attempted for
  every Toss-linked account before reading positions; a sync failure for one
  account never blocks the batch.
- TTL parity (Codex review M3): a TTL-expired pending proposal nobody polled
  does not count as "active" for the dedup check.

TossFetcher and the realtime-quote provider are always faked here — this
suite makes no real HTTP/network calls. ``sync_linked_account`` is exercised
via its natural missing-credentials failure path (no TOSS_CLIENT_ID/SECRET in
the test env), which fails before any network call — also offline.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from api.app import create_app
from fastapi.testclient import TestClient
from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.auto_proposal_service import (
    AutoProposalService,
    format_batch_summary,
    run_phase5_auto_proposal_batch,
)
from src.services.decision_signal_service import DecisionSignalService
from src.services.portfolio_broker_sync_service import PortfolioBrokerSyncService
from src.services.portfolio_conditional_order_service import PortfolioConditionalOrderService
from src.services.portfolio_order_service import PortfolioOrderService
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

    def _set_quote_price(self, price: Optional[float]) -> None:
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
                price=price, source=None
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

    def _create_position(self, symbol: str, *, quantity: float = 10, price: float = 100.0) -> None:
        self.portfolio_service.record_trade(
            account_id=self.account_id,
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
            match, min_confidence=0.6, slippage=0.0, now=self._epoch(), result=batch_result
        )
        self.assertEqual(batch_result.generated_count, 0)
        self.assertEqual(len(batch_result.skipped), 1)
        self.assertIn("not numeric", batch_result.skipped[0]["reason"])

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

        with patch.object(PortfolioRepository, "has_source_signal_unique_indexes", return_value=False):
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

        with patch.object(PortfolioRepository, "has_source_signal_unique_indexes", return_value=False):
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
        self.assertTrue(self.repo.has_source_signal_unique_indexes())

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

    def test_legacy_pre_phase5_db_migration_creates_both_indexes(self) -> None:
        """A pre-Phase-5 DB (the two proposal tables exist without
        generation_source/source_signal_id) must gain both columns and both
        indexes on the next DatabaseManager.get_instance() — this is the
        actual upgrade path a real deployment goes through, not just a
        freshly-created test DB that never lacked the columns."""
        legacy_db_path = Path(self.temp_dir.name) / "legacy_pre_phase5.db"
        conn = sqlite3.connect(str(legacy_db_path))
        conn.execute(self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(extra_columns=""))
        conn.commit()
        conn.close()

        with self._temporary_database(legacy_db_path) as legacy_db:
            inspector_repo = PortfolioRepository(db_manager=legacy_db)
            self.assertTrue(legacy_db.has_portfolio_proposal_source_signal_unique_indexes())
            self.assertTrue(inspector_repo.has_source_signal_unique_indexes())

    def test_same_named_non_unique_index_bypass_is_detected_as_invalid(self) -> None:
        """Codex 2nd-round review M4-b: seed a same-named but non-unique,
        non-partial index (the exact B1-b bypass — ``CREATE UNIQUE INDEX IF
        NOT EXISTS`` is a no-op against it since SQLite's ``IF NOT EXISTS``
        only checks the name) BEFORE Phase 5's own migration runs, then
        verify the checker reports it as missing/invalid using the real
        on-disk index definition — not a mocked return value."""
        bypass_db_path = Path(self.temp_dir.name) / "bypass_index.db"
        conn = sqlite3.connect(str(bypass_db_path))
        conn.execute(
            self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(
                extra_columns=(
                    ",\n          generation_source VARCHAR(16) NOT NULL DEFAULT 'manual',"
                    "\n          source_signal_id INTEGER"
                )
            )
        )
        # The bypass: an index with the exact name DatabaseManager will try
        # to create, but plain (no UNIQUE) and with no partial predicate.
        conn.execute(
            "CREATE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (source_signal_id)"
        )
        conn.commit()
        conn.close()

        with self._temporary_database(bypass_db_path) as bypass_db:
            self.assertFalse(bypass_db.has_portfolio_proposal_source_signal_unique_indexes())
            inspector_repo = PortfolioRepository(db_manager=bypass_db)
            self.assertFalse(inspector_repo.has_source_signal_unique_indexes())

    def _seed_order_proposal_table_with_index(self, db_path: Path, index_sql: str) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(
                extra_columns=(
                    ",\n          generation_source VARCHAR(16) NOT NULL DEFAULT 'manual',"
                    "\n          source_signal_id INTEGER"
                )
            )
        )
        conn.execute(index_sql)
        conn.commit()
        conn.close()

    def test_regex_bypass_comment_containing_column_name_is_detected_as_invalid(self) -> None:
        """Codex 3rd-round review M4: the previous DDL-regex checker matched
        ``(source_signal_id)`` anywhere in the raw ``sqlite_master.sql``
        text — including inside a SQL comment — so an index that is
        actually UNIQUE on an unrelated column (``quantity``) but merely
        *mentions* ``source_signal_id`` in a trailing comment would have
        been wrongly accepted (fail-open: no real per-signal uniqueness
        constraint in effect). The introspection-based checker uses
        ``get_indexes()``'s parsed ``column_names``, which is immune to
        this — it must reject this index."""
        db_path = Path(self.temp_dir.name) / "comment_bypass.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (quantity) /* (source_signal_id) */",
        )
        with self._temporary_database(db_path) as db:
            self.assertFalse(db.has_portfolio_proposal_source_signal_unique_indexes())

    def test_composite_unique_index_including_source_signal_id_is_detected_as_invalid(self) -> None:
        """Codex 3rd-round review M4: a composite unique index
        ``(quantity, source_signal_id)`` does not enforce "at most one row
        per source_signal_id" on its own (the actual constraint is on the
        pair) — the old regex, which only checked that
        ``(source_signal_id)`` appeared somewhere in the DDL text, would
        have accepted this. ``column_names`` must be exactly
        ``["source_signal_id"]``, so this composite index must be
        rejected."""
        db_path = Path(self.temp_dir.name) / "composite_index.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (quantity, source_signal_id)",
        )
        with self._temporary_database(db_path) as db:
            self.assertFalse(db.has_portfolio_proposal_source_signal_unique_indexes())

    def test_quoted_identifier_unique_index_is_accepted_no_false_negative(self) -> None:
        """Codex 3rd-round review M4: a unique index using SQLite's quoted-
        identifier syntax (``"source_signal_id"``) is a perfectly valid,
        fully-enforcing constraint, but the old regex (which matched the
        bare token ``source_signal_id`` inside unquoted parens) rejected it
        — permanently fail-closing an otherwise-healthy deployment. The
        introspection-based checker must accept it: SQLAlchemy's
        get_indexes() parses the column name independent of quoting."""
        db_path = Path(self.temp_dir.name) / "quoted_identifier.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            'CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal '
            'ON portfolio_order_proposals ("source_signal_id") '
            'WHERE "source_signal_id" IS NOT NULL',
        )
        with self._temporary_database(db_path) as db:
            self.assertTrue(db.has_portfolio_proposal_source_signal_unique_indexes())

    def test_restrictive_partial_predicate_is_detected_as_invalid(self) -> None:
        """Codex 4th-round review B1-b (the exact counterexample Codex
        reproduced against the 3rd-round fix): a unique index on exactly
        ``(source_signal_id)`` passes the structural unique+column check,
        but if its partial predicate is ``source_signal_id > 100`` (or any
        other restriction beyond ``IS NOT NULL``), the constraint only
        applies to rows the predicate admits — ``source_signal_id = 1``
        could repeat freely since it falls outside ``> 100``, violating the
        idempotency contract despite passing a unique+column-only check.
        The checker must inspect the predicate itself and reject this."""
        db_path = Path(self.temp_dir.name) / "restrictive_predicate.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (source_signal_id) "
            "WHERE source_signal_id > 100",
        )
        with self._temporary_database(db_path) as db:
            self.assertFalse(db.has_portfolio_proposal_source_signal_unique_indexes())

    def test_restrictive_predicate_with_in_list_parens_is_detected_as_invalid(self) -> None:
        """Codex 5th-round review: a self-caught fail-open re-occurrence of
        the same class as the 4th-round finding, in a different DDL shape.
        The 5th-round fix (WHERE-tail extraction via the *first* ``)`` in
        the statement, not the *last* one) must still reject a restrictive
        predicate when the predicate itself contains a parenthesis — an
        ``IN (...)`` list here means only source_signal_id 1/2/3 are
        constrained; 5 (or any other non-null value) could repeat freely.
        A last-``)``-based split would have found an empty tail after the
        real last ``)`` and misjudged this as non-partial (safe)."""
        db_path = Path(self.temp_dir.name) / "restrictive_predicate_in_list.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (source_signal_id) "
            "WHERE source_signal_id IN (1,2,3)",
        )
        with self._temporary_database(db_path) as db:
            self.assertFalse(db.has_portfolio_proposal_source_signal_unique_indexes())

    def test_restrictive_predicate_wrapped_in_parens_is_detected_as_invalid(self) -> None:
        """Codex 5th-round review: same fail-open class, predicate wrapped
        in its own parentheses (``WHERE (source_signal_id > 100)``) — also
        must be rejected, not misread as non-partial via a last-``)`` split
        landing inside the predicate's own wrapping parens."""
        db_path = Path(self.temp_dir.name) / "restrictive_predicate_wrapped.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (source_signal_id) "
            "WHERE (source_signal_id > 100)",
        )
        with self._temporary_database(db_path) as db:
            self.assertFalse(db.has_portfolio_proposal_source_signal_unique_indexes())

    def test_restrictive_predicate_with_trailing_paren_clause_is_detected_as_invalid(self) -> None:
        """Codex 5th-round review: restrictive predicate followed by an
        additional parenthesized clause at the very end of the statement
        (``... AND (1=1)``) — the true last ``)`` in the DDL belongs to
        this trailing clause, not the column list, so a last-``)``-based
        split would again find an empty (falsely "non-partial") tail."""
        db_path = Path(self.temp_dir.name) / "restrictive_predicate_trailing_paren.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (source_signal_id) "
            "WHERE source_signal_id > 100 AND (1=1)",
        )
        with self._temporary_database(db_path) as db:
            self.assertFalse(db.has_portfolio_proposal_source_signal_unique_indexes())

    def test_safe_partial_predicate_is_accepted(self) -> None:
        """Codex 4th-round review: the one partial predicate that IS safe
        — ``source_signal_id IS NOT NULL`` — must still pass (this is
        exactly what this file's own migration creates)."""
        db_path = Path(self.temp_dir.name) / "safe_partial_predicate.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (source_signal_id) "
            "WHERE source_signal_id IS NOT NULL",
        )
        with self._temporary_database(db_path) as db:
            self.assertTrue(db.has_portfolio_proposal_source_signal_unique_indexes())

    def test_safe_predicate_written_with_extra_parens_is_pinned_fail_closed(self) -> None:
        """Codex 5th-round review: a semantically-safe predicate written in
        an unusual parenthesized shape (``WHERE (source_signal_id) IS NOT
        NULL``) is not a form this file's own migration ever produces —
        pinning this to False (rather than asserting a specific direction
        is "required") documents the actual current behavior so a future
        change to the normalization logic doesn't silently flip it without
        a test noticing. Fail-closed here is safe by construction (never
        wrongly accepts an unenforced constraint); it just isn't the most
        permissive possible reading of an equivalent predicate."""
        db_path = Path(self.temp_dir.name) / "safe_predicate_extra_parens.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (source_signal_id) "
            "WHERE (source_signal_id) IS NOT NULL",
        )
        with self._temporary_database(db_path) as db:
            self.assertFalse(db.has_portfolio_proposal_source_signal_unique_indexes())

    def test_non_partial_unique_index_is_accepted(self) -> None:
        """Codex 4th-round review: a plain (non-partial, no WHERE clause at
        all) unique index on exactly source_signal_id is also safe — SQLite
        already treats every NULL as distinct from every other NULL, so
        this already guarantees "at most one row per non-null value, NULLs
        unlimited" without needing a predicate at all."""
        db_path = Path(self.temp_dir.name) / "non_partial.db"
        self._seed_order_proposal_table_with_index(
            db_path,
            "CREATE UNIQUE INDEX uix_portfolio_order_proposal_source_signal "
            "ON portfolio_order_proposals (source_signal_id)",
        )
        with self._temporary_database(db_path) as db:
            self.assertTrue(db.has_portfolio_proposal_source_signal_unique_indexes())

    def test_duplicate_source_signal_id_db_survives_init_and_batch_refuses(self) -> None:
        """Codex 2nd-round review M4-c: a DB with two rows sharing the same
        source_signal_id (the exact precondition that makes SQLite's CREATE
        UNIQUE INDEX raise IntegrityError, per B1-a) must not crash
        DatabaseManager.get_instance() — reaching the assertions below at
        all is itself proof init survived — and the Phase 5 batch must then
        refuse to run against it end-to-end (B1-a and B1-b/the fail-closed
        gate composing safely)."""
        dup_db_path = Path(self.temp_dir.name) / "duplicate_source_signal.db"
        conn = sqlite3.connect(str(dup_db_path))
        conn.execute(
            self._LEGACY_ORDER_PROPOSAL_TABLE_SQL.format(
                extra_columns=(
                    ",\n          generation_source VARCHAR(16) NOT NULL DEFAULT 'manual',"
                    "\n          source_signal_id INTEGER"
                )
            )
        )
        for uid in ("dup-uuid-1", "dup-uuid-2"):
            conn.execute(
                "INSERT INTO portfolio_order_proposals (account_id, proposal_uuid, symbol, "
                "storage_symbol, market, currency, side, order_type, price, quantity, "
                "est_amount_krw, status, created_at, expires_at, source_signal_id) VALUES "
                "(1, ?, '005930', '005930.KS', 'kr', 'KRW', 'sell', 'LIMIT', 90.0, 5, 450.0, "
                "'pending', '2026-01-01', '2026-01-01', 99)",
                (uid,),
            )
        conn.commit()
        conn.close()

        with self._temporary_database(dup_db_path) as dup_db:
            # Reaching this line proves DatabaseManager.get_instance() did
            # not raise despite the duplicate rows (B1-a).
            self.assertFalse(dup_db.has_portfolio_proposal_source_signal_unique_indexes())
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

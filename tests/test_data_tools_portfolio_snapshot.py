# -*- coding: utf-8 -*-
"""Contract tests for get_portfolio_snapshot agent tool."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.tools.data_tools import _handle_get_portfolio_snapshot


class _FakePortfolioService:
    def get_portfolio_snapshot(self, **_kwargs):
        return {
            "as_of": "2026-03-15",
            "cost_method": "fifo",
            "currency": "CNY",
            "account_count": 1,
            "total_cash": 10000.0,
            "total_market_value": 50000.0,
            "total_equity": 60000.0,
            "realized_pnl": 1200.0,
            "unrealized_pnl": 800.0,
            "fx_stale": False,
            "accounts": [
                {
                    "account_id": 1,
                    "account_name": "Main",
                    "market": "cn",
                    "base_currency": "CNY",
                    "total_equity": 60000.0,
                    "total_market_value": 50000.0,
                    "total_cash": 10000.0,
                    "realized_pnl": 1200.0,
                    "unrealized_pnl": 800.0,
                    "fx_stale": False,
                    "positions": [
                        {
                            "symbol": "600519",
                            "market": "cn",
                            "currency": "CNY",
                            "quantity": 10.0,
                            "avg_cost": 1500.0,
                            "last_price": 1600.0,
                            "market_value_base": 16000.0,
                        },
                        {
                            "symbol": "000001",
                            "market": "cn",
                            "currency": "CNY",
                            "quantity": 100.0,
                            "avg_cost": 10.0,
                            "last_price": 11.0,
                            "market_value_base": 1100.0,
                        },
                    ],
                }
            ],
        }


class _FakeKrPortfolioService:
    """Pure-KR snapshot: aggregates in KRW with data_quality=ok (informational limitations only)."""

    def get_portfolio_snapshot(self, **_kwargs):
        return {
            "as_of": "2026-03-15",
            "cost_method": "fifo",
            "currency": "KRW",
            "account_count": 1,
            "total_cash": 100000.0,
            "total_market_value": 700000.0,
            "total_equity": 800000.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 699000.0,
            "fx_stale": False,
            "data_quality": "ok",
            "limitations": [
                "realtime_quote_best_effort",
                "sector_and_risk_metrics_limited",
            ],
            "accounts": [
                {
                    "account_id": 1,
                    "account_name": "KR-account",
                    "market": "kr",
                    "base_currency": "KRW",
                    "total_equity": 800000.0,
                    "total_market_value": 700000.0,
                    "total_cash": 100000.0,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 699000.0,
                    "fx_stale": False,
                    "data_quality": "ok",
                    "positions": [
                        {
                            "symbol": "005930.KS",
                            "market": "kr",
                            "currency": "KRW",
                            "quantity": 10.0,
                            "avg_cost": 100.0,
                            "last_price": 70000.0,
                            "market_value_base": 700000.0,
                        },
                    ],
                }
            ],
        }


class _FakeRiskService:
    def __init__(self, **_kwargs):
        pass

    def get_risk_report(self, **_kwargs):
        return {
            "as_of": "2026-03-15",
            "currency": "CNY",
            "cost_method": "fifo",
            "thresholds": {"concentration_alert_pct": 35.0},
            "concentration": {
                "alert": True,
                "top_weight_pct": 40.12,
                "top_positions": [
                    {"symbol": "600519", "weight_pct": 40.12},
                    {"symbol": "000001", "weight_pct": 12.3},
                ],
            },
            "drawdown": {
                "alert": False,
                "max_drawdown_pct": 8.7,
                "current_drawdown_pct": 3.2,
                "fx_stale": False,
            },
            "stop_loss": {
                "near_alert": True,
                "triggered_count": 1,
                "near_count": 2,
                "items": [{"symbol": "600519", "loss_pct": 9.8}],
            },
        }


class TestGetPortfolioSnapshotTool(unittest.TestCase):
    @patch("src.services.portfolio_service.PortfolioService", _FakePortfolioService)
    @patch("src.services.portfolio_risk_service.PortfolioRiskService", _FakeRiskService)
    def test_default_returns_compact_snapshot_and_risk(self) -> None:
        result = _handle_get_portfolio_snapshot(account_id=1)
        self.assertEqual(result["status"], "ok")
        self.assertIn("snapshot", result)
        self.assertIn("risk", result)
        self.assertEqual(result["risk"]["status"], "ok")

        account = result["snapshot"]["accounts"][0]
        self.assertIn("top_positions", account)
        self.assertNotIn("positions", account)
        self.assertEqual(account["position_count"], 2)
        self.assertEqual(account["top_positions"][0]["symbol"], "600519")

    @patch("src.services.portfolio_service.PortfolioService", _FakePortfolioService)
    @patch("src.services.portfolio_risk_service.PortfolioRiskService", _FakeRiskService)
    def test_include_positions_and_disable_risk(self) -> None:
        result = _handle_get_portfolio_snapshot(
            account_id=1,
            include_positions=True,
            include_risk=False,
            as_of="2026-03-15",
        )
        self.assertEqual(result["status"], "ok")
        account = result["snapshot"]["accounts"][0]
        self.assertIn("positions", account)
        self.assertNotIn("risk", result)

        invalid = _handle_get_portfolio_snapshot(as_of="2026/03/15")
        self.assertIn("error", invalid)
        self.assertIn("YYYY-MM-DD", invalid["error"])

    @patch("src.services.portfolio_service.PortfolioService", _FakeKrPortfolioService)
    @patch("src.services.portfolio_risk_service.PortfolioRiskService", _FakeRiskService)
    def test_kr_snapshot_exposes_krw_and_ok_quality_to_llm(self) -> None:
        # Contract: a pure-KR snapshot reaches the LLM as KRW with data_quality=ok
        # (no misleading `partial`). Guards the KR valuation-accuracy fix end-to-end.
        result = _handle_get_portfolio_snapshot(account_id=1)
        self.assertEqual(result["status"], "ok")
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["currency"], "KRW")
        self.assertEqual(snapshot["data_quality"], "ok")
        self.assertEqual(snapshot["accounts"][0]["base_currency"], "KRW")


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Regression tests for the process_single_stock entry-path code resolution.

A bare 6-digit KR base code (e.g. 000660) collides with the same-numbered CN
A-share. Scheduled run()/CLI paths do not pre-resolve codes the way the API/bot
paths do, so process_single_stock must restore the JP/KR suffix itself before
dispatching, or the whole run analyzes the wrong (Chinese) company.
"""

from datetime import date
import unittest
from unittest.mock import MagicMock, patch

from src.core.pipeline import StockAnalysisPipeline


class _StopRun(Exception):
    """Sentinel used to abort run() right at the observation point."""


class PipelineCodeResolutionTestCase(unittest.TestCase):
    def _make_pipeline(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline._emit_progress = MagicMock()
        pipeline._resolve_resume_target_date = MagicMock(return_value=date(2026, 7, 20))
        pipeline.query_id = "q-test"
        pipeline.trace_id = "t-test"
        pipeline.query_source = None
        # skip_analysis short-circuits after fetch; fetch is the observation point.
        pipeline.fetch_and_save_stock_data = MagicMock(return_value=(True, None))
        return pipeline

    def _run(self, pipeline, code):
        with patch("src.services.history_loader.set_frozen_target_date", return_value=None), \
             patch("src.services.history_loader.reset_frozen_target_date"), \
             patch("src.core.pipeline.get_current_diagnostic_context", return_value=object()):
            return StockAnalysisPipeline.process_single_stock(
                pipeline, code, skip_analysis=True
            )

    def test_bare_kr_base_code_restored_to_ks_suffix(self):
        """000660 -> 000660.KS so the KR stock is not analyzed as CN *ST南华."""
        pipeline = self._make_pipeline()

        self._run(pipeline, "000660")

        dispatched = pipeline.fetch_and_save_stock_data.call_args.args[0]
        self.assertEqual(dispatched, "000660.KS")

    def test_already_suffixed_code_is_idempotent(self):
        """Already-resolved API/bot inputs pass through unchanged."""
        pipeline = self._make_pipeline()

        self._run(pipeline, "000660.KS")

        dispatched = pipeline.fetch_and_save_stock_data.call_args.args[0]
        self.assertEqual(dispatched, "000660.KS")

    def test_cn_code_left_untouched(self):
        """Genuine CN A-share codes must not be rerouted to KR."""
        pipeline = self._make_pipeline()

        self._run(pipeline, "600519")

        dispatched = pipeline.fetch_and_save_stock_data.call_args.args[0]
        self.assertEqual(dispatched, "600519")


class RunEntryPrefetchResolutionTestCase(unittest.TestCase):
    """run() 的三条预取路径必须先拿到解析后的代码。

    process_single_stock 的解析发生在预取之后，所以只有它是不够的：裸 KR 代码
    仍会被 prefetch_* 送进 A 股专用数据源（2026-07-24 调度线程 wedge 的触发路径），
    并把同号 A 股的名称写进名称缓存。
    """

    def _make_pipeline(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.max_workers = 1
        pipeline.fetcher_manager = MagicMock()
        return pipeline

    def test_resolve_entry_stock_codes_handles_kr_cn_and_suffixed_inputs(self):
        pipeline = self._make_pipeline()

        resolved = StockAnalysisPipeline._resolve_entry_stock_codes(
            pipeline, ["000660", "000660.KS", "600519", "AAPL"]
        )

        self.assertEqual(resolved, ["000660.KS", "000660.KS", "600519", "AAPL"])

    def test_stock_name_prefetch_receives_resolved_codes(self):
        pipeline = self._make_pipeline()
        pipeline.fetcher_manager.prefetch_stock_names.side_effect = _StopRun

        with self.assertRaises(_StopRun):
            StockAnalysisPipeline.run(pipeline, stock_codes=["000660", "600519"])

        called = pipeline.fetcher_manager.prefetch_stock_names.call_args.args[0]
        self.assertEqual(called, ["000660.KS", "600519"])

    def test_daily_kline_prefetch_receives_resolved_codes(self):
        """5 只以上才会走批量预取分支，这里覆盖该分支的入参。"""
        pipeline = self._make_pipeline()
        pipeline.fetcher_manager.prefetch_daily_klines.side_effect = _StopRun

        with self.assertRaises(_StopRun):
            StockAnalysisPipeline.run(
                pipeline,
                stock_codes=["000660", "005930", "600519", "AAPL", "TSLA"],
            )

        called = pipeline.fetcher_manager.prefetch_daily_klines.call_args.args[0]
        self.assertEqual(called, ["000660.KS", "005930.KS", "600519", "AAPL", "TSLA"])

    def test_realtime_prefetch_receives_resolved_codes(self):
        pipeline = self._make_pipeline()
        pipeline.fetcher_manager.prefetch_daily_klines.return_value = 0
        pipeline.fetcher_manager.prefetch_realtime_quotes.side_effect = _StopRun

        with self.assertRaises(_StopRun):
            StockAnalysisPipeline.run(
                pipeline,
                stock_codes=["000660", "005930", "600519", "AAPL", "TSLA"],
            )

        called = pipeline.fetcher_manager.prefetch_realtime_quotes.call_args.args[0]
        self.assertEqual(called, ["000660.KS", "005930.KS", "600519", "AAPL", "TSLA"])


if __name__ == "__main__":
    unittest.main()

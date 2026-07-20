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


if __name__ == "__main__":
    unittest.main()

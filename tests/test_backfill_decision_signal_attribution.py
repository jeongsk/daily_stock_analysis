# -*- coding: utf-8 -*-
"""Offline tests for scripts/backfill_decision_signal_attribution.py (Task 3b).

Covers the deferred metadata backfill for pre-capture DecisionSignal rows:
dry-run makes no writes, --apply backfills accurately from the linked
analysis_history report, existing signal_attribution values are never
overwritten, and rows without a usable source are skipped and left NULL.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from backfill_decision_signal_attribution import run  # noqa: E402
from src.config import Config  # noqa: E402
from src.storage import AnalysisHistory, DatabaseManager, DecisionSignalRecord  # noqa: E402


VALID_ATTRIBUTION = {
    "technical_indicators": 40,
    "news_sentiment": 25,
    "fundamentals": 20,
    "market_conditions": 15,
    "strongest_bullish_signal": "MA golden cross",
    "strongest_bearish_signal": None,
}


def _dashboard_raw_result(attribution):
    return json.dumps({"dashboard": {"signal_attribution": attribution}})


class TestBackfillDecisionSignalAttribution(unittest.TestCase):
    def setUp(self):
        DatabaseManager.reset_instance()
        self._temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._temp_dir.name, "backfill.sqlite")
        self.db = DatabaseManager(db_url=f"sqlite:///{db_path}")

    def tearDown(self):
        DatabaseManager.reset_instance()
        Config.reset_instance()
        self._temp_dir.cleanup()

    def _make_signal(self, session, **overrides):
        fields = dict(
            stock_code="600519",
            market="cn",
            source_type="analysis",
            trigger_source="system",
            action="buy",
            metadata_json=json.dumps({"decision_profile": "balanced"}),
        )
        fields.update(overrides)
        row = DecisionSignalRecord(**fields)
        session.add(row)
        session.flush()
        return row

    def _make_history(self, session, raw_result):
        row = AnalysisHistory(code="600519", raw_result=raw_result)
        session.add(row)
        session.flush()
        return row

    def test_dry_run_makes_no_changes(self):
        with self.db.get_session() as session:
            history = self._make_history(session, _dashboard_raw_result(VALID_ATTRIBUTION))
            signal = self._make_signal(session, source_report_id=history.id)
            session.commit()
            signal_id = signal.id

        stats = run(apply=False)

        self.assertEqual(stats.scanned, 1)
        self.assertEqual(stats.backfillable, 1)
        self.assertEqual(stats.updated, 0)

        with self.db.get_session() as session:
            row = session.get(DecisionSignalRecord, signal_id)
            metadata = json.loads(row.metadata_json)
            self.assertNotIn("signal_attribution", metadata)
            self.assertEqual(metadata["decision_profile"], "balanced")

    def test_apply_backfills_attribution_accurately(self):
        with self.db.get_session() as session:
            history = self._make_history(session, _dashboard_raw_result(VALID_ATTRIBUTION))
            signal = self._make_signal(session, source_report_id=history.id)
            session.commit()
            signal_id = signal.id

        with self.db.get_session() as session:
            original_updated_at = session.get(DecisionSignalRecord, signal_id).updated_at

        stats = run(apply=True)

        self.assertEqual(stats.backfillable, 1)
        self.assertEqual(stats.updated, 1)

        with self.db.get_session() as session:
            row = session.get(DecisionSignalRecord, signal_id)
            metadata = json.loads(row.metadata_json)
            self.assertEqual(metadata["signal_attribution"], VALID_ATTRIBUTION)
            # Pre-existing metadata keys must survive untouched.
            self.assertEqual(metadata["decision_profile"], "balanced")
            # Additive-only backfill must not touch updated_at (raw UPDATE,
            # not ORM attribute assignment, bypasses the onupdate hook).
            self.assertEqual(row.updated_at, original_updated_at)

    def test_existing_attribution_is_never_overwritten(self):
        existing_attribution = {
            "technical_indicators": 10,
            "news_sentiment": 10,
            "fundamentals": 10,
            "market_conditions": 70,
            "strongest_bullish_signal": None,
            "strongest_bearish_signal": None,
        }
        with self.db.get_session() as session:
            history = self._make_history(session, _dashboard_raw_result(VALID_ATTRIBUTION))
            signal = self._make_signal(
                session,
                source_report_id=history.id,
                metadata_json=json.dumps({"signal_attribution": existing_attribution}),
            )
            session.commit()
            signal_id = signal.id

        stats = run(apply=True)

        self.assertEqual(stats.already_attributed, 1)
        self.assertEqual(stats.backfillable, 0)
        self.assertEqual(stats.updated, 0)

        with self.db.get_session() as session:
            row = session.get(DecisionSignalRecord, signal_id)
            metadata = json.loads(row.metadata_json)
            # Still the original (weaker) values -- never replaced by the
            # source report's attribution even though it differs.
            self.assertEqual(metadata["signal_attribution"], existing_attribution)

    def test_missing_or_unusable_source_is_skipped_and_left_null(self):
        with self.db.get_session() as session:
            # 1) no source_report_id at all.
            self._make_signal(session, source_report_id=None)
            # 2) source_report_id points at a row that does not exist.
            self._make_signal(session, source_report_id=999999)
            # 3) source report exists but raw_result is malformed JSON.
            broken_history = self._make_history(session, "{not-json")
            self._make_signal(session, source_report_id=broken_history.id)
            # 4) source report exists with valid JSON but no usable dashboard attribution.
            empty_history = self._make_history(session, json.dumps({"dashboard": {}}))
            self._make_signal(session, source_report_id=empty_history.id)
            # 5) the signal's own metadata_json is malformed.
            self._make_signal(session, metadata_json="{not-json", source_report_id=None)
            session.commit()
            signal_ids = [
                row.id
                for row in session.query(DecisionSignalRecord).order_by(DecisionSignalRecord.id).all()
            ]

        stats = run(apply=True)

        self.assertEqual(stats.scanned, 5)
        self.assertEqual(stats.no_source_report_id, 1)
        self.assertEqual(stats.source_report_missing, 1)
        self.assertEqual(stats.source_raw_result_invalid, 1)
        self.assertEqual(stats.source_attribution_unavailable, 1)
        self.assertEqual(stats.invalid_metadata, 1)
        self.assertEqual(stats.backfillable, 0)
        self.assertEqual(stats.updated, 0)

        with self.db.get_session() as session:
            rows = [session.get(DecisionSignalRecord, sid) for sid in signal_ids]
            for row in rows:
                if row.metadata_json in (None, "{not-json"):
                    continue
                metadata = json.loads(row.metadata_json)
                self.assertNotIn("signal_attribution", metadata)


if __name__ == "__main__":
    unittest.main()

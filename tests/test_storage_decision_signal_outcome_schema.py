# -*- coding: utf-8 -*-
"""decision_signal_outcomes.dominant_attribution 스키마·마이그레이션 계약. 오프라인.

계약(스펙 D3, 계획 Task 4):
  - 신규 DB(create_all): 컬럼 + ix_decision_signal_outcome_stats_attribution 존재
  - 기존 DB(컬럼 없는 레거시 테이블): 부팅 시 ALTER로 컬럼·인덱스 추가
  - 마이그레이션 재실행 idempotent(duplicate column 무시)
"""

import os
import sys
import unittest

from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.storage import DatabaseManager, DecisionSignalOutcomeRecord


def _fresh_manager(tmp_path: str) -> DatabaseManager:
    DatabaseManager.reset_instance()
    return DatabaseManager(db_url=f"sqlite:///{tmp_path}")


class TestDominantAttributionSchema(unittest.TestCase):
    def setUp(self) -> None:
        DatabaseManager.reset_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def _columns_and_indexes(self, engine):
        inspector = inspect(engine)
        table = DecisionSignalOutcomeRecord.__tablename__
        columns = {c["name"] for c in inspector.get_columns(table)}
        indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes(table)}
        return columns, indexes

    def test_fresh_database_has_column_and_index(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            manager = _fresh_manager(os.path.join(tmp, "fresh.db"))
            columns, indexes = self._columns_and_indexes(manager._engine)
            self.assertIn("dominant_attribution", columns)
            self.assertEqual(
                indexes.get("ix_decision_signal_outcome_stats_attribution"),
                ["engine_version", "dominant_attribution", "horizon"],
            )

    def test_legacy_database_is_migrated_on_boot(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "legacy.db")
            # 레거시 상태 시뮬레이션: 컬럼 없는 outcome 테이블을 직접 생성
            legacy_engine = create_engine(f"sqlite:///{db_path}")
            with legacy_engine.begin() as connection:
                connection.execute(text(
                    "CREATE TABLE decision_signal_outcomes ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "signal_id INTEGER NOT NULL, "
                    "horizon VARCHAR(16) NOT NULL, "
                    "engine_version VARCHAR(32) NOT NULL, "
                    "eval_status VARCHAR(24) NOT NULL DEFAULT 'unable', "
                    "holding_state VARCHAR(16) NOT NULL DEFAULT 'unknown')"
                ))
                connection.execute(text(
                    "INSERT INTO decision_signal_outcomes "
                    "(signal_id, horizon, engine_version) "
                    "VALUES (1, '1d', 'decision-signal-v1')"
                ))
            legacy_engine.dispose()

            manager = _fresh_manager(db_path)
            columns, indexes = self._columns_and_indexes(manager._engine)
            self.assertIn("dominant_attribution", columns)
            self.assertIn("ix_decision_signal_outcome_stats_attribution", indexes)
            with manager._engine.connect() as connection:
                value = connection.execute(text(
                    "SELECT dominant_attribution FROM decision_signal_outcomes WHERE signal_id = 1"
                )).scalar_one()
            self.assertIsNone(value)  # 백필 없음 — 기존 행은 NULL 유지

    def test_migration_is_idempotent(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            manager = _fresh_manager(os.path.join(tmp, "idem.db"))
            # 두 번째 실행이 duplicate column/index로 실패하지 않아야 한다
            manager._ensure_decision_signal_outcome_attribution_schema()
            columns, _ = self._columns_and_indexes(manager._engine)
            self.assertIn("dominant_attribution", columns)


if __name__ == "__main__":
    unittest.main()

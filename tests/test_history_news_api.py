# -*- coding: utf-8 -*-
"""Integration tests for the history news endpoint with merge + provenance.

Covers HistoryService.resolve_and_get_news merge wiring (fail-open, opt-out)
and the GET /api/v1/history/{record_id}/news additive field serialization.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from src.config import Config
from src.services.history_service import HistoryService
from src.storage import AnalysisHistory, DatabaseManager, IntelligenceItem


class ResolveAndGetNewsMergeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self._temp_dir.name, "svc.db")
        Config._instance = None
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config._instance = None
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _record(self, *, code="AAPL", report_type="detailed", created_at=None):
        return SimpleNamespace(
            id=1,
            query_id="q-1",
            code=code,
            name="Test",
            report_type=report_type,
            created_at=created_at or datetime.now(),
            raw_result=None,
            context_snapshot=None,
        )


    def test_existing_stored_polluted_pool_item_displays_clean_and_skips_translation(self) -> None:
        svc = HistoryService()
        created = datetime.now()
        polluted = (
            '&lt;p style=&quot;font-family:Malgun Gothic&quot;&gt;'
            '한국은행은 최근 국내외 경제상황을 평가하였다.&lt;/p&gt;'
        )
        with self.db.get_session() as session:
            record = AnalysisHistory(
                query_id="q-polluted",
                code="005930.KS",
                name="Samsung",
                report_type="detailed",
                raw_result='{"report_language":"ko"}',
                created_at=created,
            )
            session.add(record)
            session.flush()
            record_id = int(record.id)
            session.add(IntelligenceItem(
                source_name="Bank of Korea Press Releases",
                source_type="rss",
                title="경제상황 평가(2026.7월)",
                summary=polluted,
                url="https://www.bok.or.kr/portal/bbs/P0000559/view.do?nttId=1",
                source="Bank of Korea Press Releases",
                published_at=created,
                fetched_at=created,
                scope_type="market",
                scope_value="__dsa_null_scope__",
                market="kr",
            ))
            session.commit()

        result = svc.resolve_and_get_news(str(record_id), limit=8)

        self.assertEqual(len(result), 1)
        item = result[0]
        self.assertEqual(item["title"], "경제상황 평가(2026.7월)")
        self.assertEqual(item["snippet"], "한국은행은 최근 국내외 경제상황을 평가하였다.")
        self.assertEqual(item["provenance"], "pool")
        self.assertEqual(item["translation_status"], "original")
        self.assertEqual(item["source_language"], "ko")
        self.assertNotIn("original_snippet", item)
        self.assertNotIn("&lt;p", item["snippet"])
        self.assertNotIn("style=", item["snippet"])

        with self.db.get_session() as session:
            stored = session.execute(select(IntelligenceItem).limit(1)).scalar_one()
            self.assertIn("&lt;p", stored.summary)

    def test_merge_is_applied_to_direct_items(self) -> None:
        svc = HistoryService()
        record = self._record()
        direct = [SimpleNamespace(
            title="Direct", snippet="s", url="https://a/1",
            source="search", provider="test", published_date=None, fetched_at=None,
        )]

        with patch.object(svc, "_resolve_record", return_value=record), \
             patch.object(svc.db, "get_news_intel_by_query_id", return_value=direct), \
             patch("src.services.history_service.NewsCardMerger") as MergerCls:
            merger = MergerCls.return_value
            merger.merge_for_report.return_value = [
                {"title": "Direct", "snippet": "s", "url": "https://a/1",
                 "provenance": "direct", "source_type": "search"},
                {"title": "Pool", "snippet": "p", "url": "https://b/2",
                 "provenance": "pool", "source_type": "rss",
                 "source": "Nasdaq", "published_at": "2026-07-17T10:00:00"},
            ]
            result = svc.resolve_and_get_news("1", limit=10)

        self.assertEqual(len(result), 2)
        merger.merge_for_report.assert_called_once()
        direct_arg = merger.merge_for_report.call_args.kwargs["direct_items"][0]
        self.assertEqual(direct_arg["provenance"], "direct")
        self.assertEqual(direct_arg["source_type"], "search")
        # Provenance metadata flows through.
        self.assertEqual(result[1]["provenance"], "pool")
        self.assertEqual(result[1]["source"], "Nasdaq")

    def test_merge_failure_falls_back_to_direct(self) -> None:
        svc = HistoryService()
        record = self._record()
        direct = [SimpleNamespace(
            title="Direct", snippet="s", url="https://a/1",
            source="search", provider="test", published_date=None, fetched_at=None,
        )]

        with patch.object(svc, "_resolve_record", return_value=record), \
             patch.object(svc.db, "get_news_intel_by_query_id", return_value=direct), \
             patch("src.services.history_service.NewsCardMerger") as MergerCls:
            MergerCls.return_value.merge_for_report.side_effect = RuntimeError("boom")
            result = svc.resolve_and_get_news("1", limit=10)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Direct")
        self.assertEqual(result[0]["provenance"], "direct")

    def test_record_not_found_returns_empty(self) -> None:
        svc = HistoryService()
        with patch.object(svc, "_resolve_record", return_value=None):
            self.assertEqual(svc.resolve_and_get_news("missing", limit=10), [])


class NewsEndpointSerializationTestCase(unittest.TestCase):
    """Verify the additive fields round-trip through the NewsIntelItem model."""

    def test_direct_item_serializes_minimal_fields(self) -> None:
        from api.v1.schemas.history import NewsIntelItem

        item = NewsIntelItem(title="t", snippet="s", url="https://a/1")
        dumped = item.model_dump(exclude_none=True)
        self.assertEqual(dumped["title"], "t")
        self.assertEqual(dumped["url"], "https://a/1")
        # Additive fields are optional and absent when not provided.
        self.assertNotIn("provenance", dumped)
        self.assertNotIn("translation_status", dumped)

    def test_merged_translated_item_serializes_all_fields(self) -> None:
        from api.v1.schemas.history import NewsIntelItem

        item = NewsIntelItem(
            title="한국어 번역",
            snippet="요약",
            url="https://a/1",
            original_title="English title",
            original_snippet="English snippet",
            translation_status="translated",
            source_language="en",
            provenance="pool",
            source="Nasdaq Stocks Feed",
            source_type="rss",
            published_at="2026-07-17T10:00:00",
        )
        dumped = item.model_dump()
        self.assertEqual(dumped["translation_status"], "translated")
        self.assertEqual(dumped["provenance"], "pool")
        self.assertEqual(dumped["source_type"], "rss")
        self.assertEqual(dumped["original_title"], "English title")


if __name__ == "__main__":
    unittest.main()

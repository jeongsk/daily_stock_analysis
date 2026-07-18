# -*- coding: utf-8 -*-
"""Tests for NewsCardMerger (pool-to-card merge) and list_items_for_report."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.config import Config
from src.repositories.intelligence_repo import IntelligenceRepository
from src.services.news_merge_service import (
    NewsCardMerger,
    canonical_url,
    content_hash,
    normalize_text_for_hash,
)
from src.storage import DatabaseManager, IntelligenceItem, IntelligenceSource


def _pool_row(
    *,
    title: str,
    url: str,
    summary: str = "",
    source_name: str = "Feed",
    source_type: str = "rss",
    scope_type: str = "market",
    scope_value: str = "__dsa_null_scope__",
    market: str = "us",
    published_at: datetime | None = None,
    fetched_at: datetime | None = None,
    row_id: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id,
        title=title,
        summary=summary,
        url=url,
        source_name=source_name,
        source=source_name,
        source_type=source_type,
        scope_type=scope_type,
        scope_value=scope_value,
        market=market,
        published_at=published_at,
        fetched_at=fetched_at or datetime.now(),
    )


class NewsCardMergerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self._temp_dir.name, "merge.db")
        Config._instance = None
        DatabaseManager.reset_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config._instance = None
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _merger(self, repo=None, **config_overrides) -> NewsCardMerger:
        cfg = dict(
            news_card_merge_intel_enabled=True,
            news_max_age_days=3,
            news_strategy_profile="short",
            market_review_region="cn",
        )
        cfg.update(config_overrides)
        return NewsCardMerger(intel_repo=repo, config=SimpleNamespace(**cfg))

    def test_direct_items_are_preferred_and_reserved(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="AAPL", report_type="detailed", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        direct = [
            {"title": "Direct A", "snippet": "s", "url": "https://x.direct/a"},
            {"title": "Direct B", "snippet": "s", "url": "https://x.direct/b"},
        ]
        # Pool returns many rows but direct must be reserved first.
        repo = MagicMock()
        repo.list_items_for_report.return_value = [
            _pool_row(title=f"Pool {i}", url=f"https://x.pool/{i}", market="us", row_id=i)
            for i in range(20)
        ]
        merger = self._merger(repo=repo)
        result = merger.merge_for_report(record=record, direct_items=direct, limit=8)

        titles = [item["title"] for item in result]
        self.assertEqual(titles[:2], ["Direct A", "Direct B"])
        self.assertLessEqual(len(result), 8)
        self.assertTrue(all(item["provenance"] in {"direct", "pool"} for item in result))

    def test_per_source_cap_prevents_swamping(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="AAPL", report_type="detailed", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        # 10 rows all from the same broad feed (Fed) — cap is 3.
        repo = MagicMock()
        repo.list_items_for_report.return_value = [
            _pool_row(
                title=f"Fed {i}", url=f"https://fed/{i}",
                source_name="Federal Reserve All Press Releases",
                market="us", row_id=i,
            )
            for i in range(10)
        ]
        merger = self._merger(repo=repo)
        result = merger.merge_for_report(record=record, direct_items=[], limit=20)
        fed_count = sum(
            1 for item in result
            if item.get("source") == "Federal Reserve All Press Releases"
        )
        self.assertLessEqual(fed_count, 3)

    def test_pool_query_oversamples_before_per_source_cap_to_avoid_starvation(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="AAPL", report_type="detailed", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        repo = MagicMock()

        def fake_query(*, scope_type, start_at, end_at, market=None, scope_value=None, limit=50):
            self.assertGreater(limit, 6)
            rows = [
                _pool_row(
                    title=f"Fed {i}", url=f"https://fed/{i}",
                    source_name="Federal Reserve All Press Releases",
                    market="us", published_at=created - timedelta(minutes=i), row_id=i,
                )
                for i in range(6)
            ]
            rows.extend(
                _pool_row(
                    title=f"Nasdaq {i}", url=f"https://nasdaq/{i}",
                    source_name="Nasdaq Stocks Feed",
                    market="us", published_at=created - timedelta(minutes=6 + i), row_id=6 + i,
                )
                for i in range(3)
            )
            return rows[:limit]

        repo.list_items_for_report.side_effect = fake_query
        merger = self._merger(repo=repo)
        result = merger.merge_for_report(record=record, direct_items=[], limit=6)

        titles = [item["title"] for item in result]
        self.assertEqual(titles, ["Fed 0", "Fed 1", "Fed 2", "Nasdaq 0", "Nasdaq 1", "Nasdaq 2"])
        self.assertEqual(len(result), 6)


    def test_existing_stored_encoded_html_pool_item_is_sanitized_in_card_payload(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="005930.KS", report_type="detailed", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        polluted = (
            '&lt;p style=&quot;font-family:Malgun Gothic&quot;&gt;'
            '한국은행은 최근 국내외 경제상황을 평가하였다.&lt;/p&gt;'
        )
        repo = MagicMock()
        repo.list_items_for_report.return_value = [
            _pool_row(
                title="경제상황 평가(2026.7월)",
                summary=polluted,
                url="https://www.bok.or.kr/portal/bbs/P0000559/view.do?nttId=1",
                source_name="Bank of Korea Press Releases",
                market="kr",
                published_at=created,
                row_id=1,
            ),
        ]
        result = self._merger(repo=repo).merge_for_report(record=record, direct_items=[], limit=8)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["snippet"], "한국은행은 최근 국내외 경제상황을 평가하였다.")
        self.assertEqual(result[0]["provenance"], "pool")
        self.assertEqual(result[0]["source_type"], "rss")
        self.assertNotIn("&lt;p", result[0]["snippet"])
        self.assertNotIn("style=", result[0]["snippet"])

    def test_cross_store_dedup_canonical_url_direct_wins(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="AAPL", report_type="detailed", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        direct = [{"title": "Direct", "snippet": "s", "url": "https://Example.com/news/1/"}]
        repo = MagicMock()
        repo.list_items_for_report.return_value = [
            _pool_row(title="Pool dup", url="https://example.com/news/1#frag", market="us", row_id=1),
            _pool_row(title="Pool unique", url="https://example.com/unique", market="us", row_id=2),
        ]
        merger = self._merger(repo=repo)
        result = merger.merge_for_report(record=record, direct_items=direct, limit=10)
        titles = [item["title"] for item in result]
        self.assertIn("Direct", titles)
        self.assertNotIn("Pool dup", titles)  # deduped by canonical URL
        self.assertIn("Pool unique", titles)
        # Direct item kept (wins the tie).
        direct_item = next(item for item in result if item["title"] == "Direct")
        self.assertEqual(direct_item["provenance"], "direct")

    def test_cross_store_dedup_title_hash(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="AAPL", report_type="detailed", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        direct = [{"title": "Fed Cuts Rates", "snippet": "rates", "url": "https://a/1"}]
        repo = MagicMock()
        repo.list_items_for_report.return_value = [
            # Same normalized title, different snippet and URL.
            _pool_row(title="  fed  cuts rates ", summary="different", url="https://b/2", market="us", row_id=1),
        ]
        merger = self._merger(repo=repo)
        result = merger.merge_for_report(record=record, direct_items=direct, limit=10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provenance"], "direct")

    def test_time_window_anchored_to_created_at(self) -> None:
        # Historical report from 40 days ago: items outside the window are excluded.
        created = datetime.now() - timedelta(days=40)
        record = SimpleNamespace(
            code="AAPL", report_type="detailed", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        repo = MagicMock()
        # Capture the bounds passed to list_items_for_report.
        captured: dict = {}
        def fake_query(*, scope_type, start_at, end_at, scope_value=None, market=None, limit=50):
            captured["start_at"] = start_at
            captured["end_at"] = end_at
            return []
        repo.list_items_for_report.side_effect = fake_query
        merger = self._merger(repo=repo)
        merger.merge_for_report(record=record, direct_items=[], limit=10)
        # lookback (short=3d) anchored at created_at, forward +1d.
        self.assertLess(captured["start_at"], created)
        self.assertGreater(captured["end_at"], created)
        # End must not drift to "now" (40 days later).
        self.assertLess(captured["end_at"], datetime.now() - timedelta(days=30))

    def test_fail_open_on_pool_error(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="AAPL", report_type="detailed", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        direct = [{"title": "Direct", "snippet": "s", "url": "https://a/1"}]
        repo = MagicMock()
        repo.list_items_for_report.side_effect = RuntimeError("db down")
        merger = self._merger(repo=repo)
        result = merger.merge_for_report(record=record, direct_items=direct, limit=10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Direct")

    def test_opt_out_returns_direct_only(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="AAPL", report_type="detailed", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        direct = [{"title": "Direct", "snippet": "s", "url": "https://a/1"}]
        repo = MagicMock()
        repo.list_items_for_report.return_value = [
            _pool_row(title="Pool", url="https://b/2", market="us", row_id=1),
        ]
        merger = self._merger(repo=repo, news_card_merge_intel_enabled=False)
        result = merger.merge_for_report(record=record, direct_items=direct, limit=10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provenance"], "direct")
        repo.list_items_for_report.assert_not_called()

    def test_market_review_region_from_overview(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="MARKET", report_type="market_review", created_at=created,
            context_snapshot={
                "analysis_context_pack_overview": {
                    "subject": {"code": "MARKET", "market": "kr"},
                },
            },
            raw_result=None,
        )
        repo = MagicMock()
        captured_markets: list = []
        def fake_query(*, scope_type, start_at, end_at, market=None, scope_value=None, limit=50):
            captured_markets.append(market)
            return []
        repo.list_items_for_report.side_effect = fake_query
        merger = self._merger(repo=repo)
        merger.merge_for_report(record=record, direct_items=[], limit=10)
        self.assertIn("kr", captured_markets)

    def test_market_review_region_falls_back_to_config(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="MARKET", report_type="market_review", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        repo = MagicMock()
        captured_markets: list = []
        def fake_query(*, scope_type, start_at, end_at, market=None, scope_value=None, limit=50):
            captured_markets.append(market)
            return []
        repo.list_items_for_report.side_effect = fake_query
        merger = self._merger(repo=repo, market_review_region="us")
        merger.merge_for_report(record=record, direct_items=[], limit=10)
        self.assertTrue(captured_markets)  # config fallback produced a region

    def test_pool_items_carry_provenance_and_metadata(self) -> None:
        created = datetime.now()
        record = SimpleNamespace(
            code="AAPL", report_type="detailed", created_at=created,
            context_snapshot=None, raw_result=None,
        )
        repo = MagicMock()
        pub = created - timedelta(hours=2)
        repo.list_items_for_report.return_value = [
            _pool_row(
                title="Pool", summary="ctx", url="https://b/2",
                source_name="Nasdaq Stocks Feed", source_type="rss",
                market="us", published_at=pub, row_id=7,
            ),
        ]
        merger = self._merger(repo=repo)
        result = merger.merge_for_report(record=record, direct_items=[], limit=10)
        self.assertEqual(len(result), 1)
        item = result[0]
        self.assertEqual(item["provenance"], "pool")
        self.assertEqual(item["source"], "Nasdaq Stocks Feed")
        self.assertEqual(item["source_type"], "rss")
        self.assertEqual(item["published_at"], pub.isoformat())

    def test_missing_created_at_fails_open_with_direct_only(self) -> None:
        record = SimpleNamespace(
            code="AAPL", report_type="detailed", created_at=None,
            context_snapshot=None, raw_result=None,
        )
        direct = [{"title": "Direct", "snippet": "s", "url": "https://a/1"}]
        repo = MagicMock()
        merger = self._merger(repo=repo)
        result = merger.merge_for_report(record=record, direct_items=direct, limit=10)
        self.assertEqual(len(result), 1)
        repo.list_items_for_report.assert_not_called()


class NormalizeAndHashTestCase(unittest.TestCase):
    def test_normalize_collapses_whitespace_and_case(self) -> None:
        self.assertEqual(normalize_text_for_hash("  Fed  Cuts "), normalize_text_for_hash("fed cuts"))
        # NFKC equivalence (fullwidth -> ascii).
        self.assertEqual(normalize_text_for_hash("ＡＢＣ"), normalize_text_for_hash("ABC"))

    def test_canonical_url_normalizes_host_scheme_query_and_fragment(self) -> None:
        self.assertEqual(
            canonical_url("HTTPS://Example.com/news/1/"),
            canonical_url("https://example.com/news/1"),
        )
        self.assertEqual(
            canonical_url("https://example.com/news/1?b=2&a=1#frag"),
            canonical_url("https://example.com/news/1?a=1&b=2"),
        )
        self.assertNotEqual(
            canonical_url("https://example.com/news/1?a=1"),
            canonical_url("https://example.com/news/1?a=2"),
        )
        # Path case is significant (URLs are case-sensitive in path).
        self.assertNotEqual(
            canonical_url("https://example.com/A"),
            canonical_url("https://example.com/a"),
        )
        self.assertEqual(canonical_url("no-url:intel:abc"), "")
        self.assertEqual(canonical_url("mailto:x@y.com"), "")

    def test_content_hash_pair_level(self) -> None:
        self.assertEqual(
            content_hash("Title", "Snippet A"),
            content_hash("title", "snippet a"),
        )
        self.assertNotEqual(
            content_hash("Title", "A"),
            content_hash("Title", "B"),
        )


class ListItemsForReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self._temp_dir.name, "repo.db")
        Config._instance = None
        DatabaseManager.reset_instance()
        self.repo = IntelligenceRepository()
        self.now = datetime.now()
        # Seed one source + items across time/scope.
        self.source = self.repo.create_source({
            "name": "test-feed",
            "source_type": "rss",
            "url": "https://news.example.com/rss.xml",
            "enabled": True,
            "scope_type": "market",
            "market": "us",
        })

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config._instance = None
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _add_item(self, *, title, url, scope_type="market", scope_value=None, market="us",
                  published_at, source_id=None):
        self.repo.upsert_items([{
            "source_id": source_id or self.source.id,
            "source_name": "test-feed",
            "source_type": "rss",
            "title": title,
            "summary": "",
            "url": url,
            "source": "test-feed",
            "published_at": published_at,
            "fetched_at": published_at,
            "scope_type": scope_type,
            "scope_value": scope_value or "__dsa_null_scope__",
            "market": market,
            "raw_payload": "{}",
        }])

    def test_explicit_time_bounds_exclude_outside_window(self) -> None:
        anchor = self.now - timedelta(days=10)
        self._add_item(title="in-window", url="https://a/1", published_at=anchor - timedelta(hours=1))
        self._add_item(title="too-old", url="https://a/2", published_at=anchor - timedelta(days=30))
        self._add_item(title="too-new", url="https://a/3", published_at=anchor + timedelta(days=5))
        rows = self.repo.list_items_for_report(
            scope_type="market", market="us",
            start_at=anchor - timedelta(days=3), end_at=anchor + timedelta(days=1),
            limit=50,
        )
        titles = {row.title for row in rows}
        self.assertIn("in-window", titles)
        self.assertNotIn("too-old", titles)
        self.assertNotIn("too-new", titles)

    def test_coalesces_fetched_at_when_published_missing(self) -> None:
        anchor = self.now - timedelta(days=10)
        # published_at None -> fetched_at drives the window.
        self._add_item(title="fetched-only", url="https://a/4", published_at=None)
        # Manually set fetched_at via the row already inserted (fetched_at = published_at when provided above).
        # For this item published_at was None at insert; fetched_at defaults to now, so it is outside the old window.
        rows = self.repo.list_items_for_report(
            scope_type="market", market="us",
            start_at=self.now - timedelta(days=1), end_at=self.now + timedelta(days=1),
            limit=50,
        )
        titles = {row.title for row in rows}
        self.assertIn("fetched-only", titles)

    def test_scope_and_market_filters(self) -> None:
        anchor = self.now - timedelta(days=1)
        self._add_item(title="us-market", url="https://a/u", market="us", published_at=anchor)
        self._add_item(title="cn-market", url="https://a/c", market="cn", published_at=anchor)
        rows = self.repo.list_items_for_report(
            scope_type="market", market="us",
            start_at=anchor - timedelta(days=1), end_at=anchor + timedelta(days=1),
            limit=50,
        )
        titles = {row.title for row in rows}
        self.assertIn("us-market", titles)
        self.assertNotIn("cn-market", titles)


if __name__ == "__main__":
    unittest.main()

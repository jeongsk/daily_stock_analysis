# -*- coding: utf-8 -*-
"""Tests for Korean-only news translation cache and fail-open semantics."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from src.config import Config
from src.llm.generation_backend import GenerationResult
from src.services.news_merge_service import content_hash, normalize_text_for_hash
from src.services.news_translation_service import NewsTranslationService
from src.storage import DatabaseManager, NewsTranslationCache

class FakeBackend:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def generate(self, prompt, generation_config, **kwargs):
        self.calls += 1
        validator = kwargs.get("response_validator")
        if validator:
            validator(self.text)
        return GenerationResult(
            text=self.text,
            model="fake-model",
            provider="fake",
            backend="fake",
            usage={},
        )

class NewsTranslationServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self._temp_dir.name, "translation.db")
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _service(self, backend=None, **config_overrides) -> NewsTranslationService:
        cfg = dict(news_translation_unavailable_ttl_hours=24)
        cfg.update(config_overrides)
        return NewsTranslationService(
            db_manager=self.db,
            config=SimpleNamespace(**cfg),
            generation_backend=backend,
        )

    def test_non_ko_reports_are_skipped_and_preserve_metadata(self) -> None:
        item = {"title": "Fed cuts rates", "snippet": "Markets rally", "url": "https://a/1", "provenance": "pool"}
        result = self._service().translate_items([item], "en")
        self.assertEqual(result[0]["translation_status"], "skipped")
        self.assertEqual(result[0]["source_language"], "en")
        self.assertEqual(result[0]["provenance"], "pool")

    def test_korean_items_are_original_without_duplicate_original_fields(self) -> None:
        item = {"title": "삼성전자 실적 발표", "snippet": "시장이 주목", "url": "https://a/1"}
        result = self._service().translate_items([item], "ko")
        self.assertEqual(result[0]["translation_status"], "original")
        self.assertEqual(result[0]["source_language"], "ko")
        self.assertNotIn("original_title", result[0])
        self.assertNotIn("original_snippet", result[0])


    def test_sanitized_bok_korean_item_remains_original_not_translated(self) -> None:
        item = {
            "title": "경제상황 평가(2026.7월)",
            "snippet": "한국은행은 최근 국내외 경제상황을 평가하였다.",
            "url": "https://www.bok.or.kr/portal/bbs/P0000559/view.do?nttId=1",
            "provenance": "pool",
            "source_type": "rss",
        }
        backend = FakeBackend('[{"id":0,"title":"should not call","snippet":"should not call"}]')

        result = self._service(backend=backend).translate_items([item], "ko")

        self.assertEqual(result[0]["translation_status"], "original")
        self.assertEqual(result[0]["source_language"], "ko")
        self.assertNotIn("original_title", result[0])
        self.assertNotIn("original_snippet", result[0])
        self.assertEqual(backend.calls, 0)

    def test_successful_translation_keeps_original_and_caches_pair(self) -> None:
        backend = FakeBackend('[{"id":0,"title":"연준 금리 인하","snippet":"시장이 상승했습니다"}]')
        item = {"title": "Fed cuts rates", "snippet": "Markets rally", "url": "https://a/1"}
        service = self._service(backend=backend)
        result = service.translate_items([item], "ko")

        self.assertEqual(result[0]["title"], "연준 금리 인하")
        self.assertEqual(result[0]["original_title"], "Fed cuts rates")
        self.assertEqual(result[0]["translation_status"], "translated")
        self.assertEqual(backend.calls, 1)

        # Same normalized pair should hit cache and avoid another LLM call.
        result2 = service.translate_items([
            {"title": "  fed  cuts rates ", "snippet": "markets rally", "url": "https://a/2"}
        ], "ko")
        self.assertEqual(result2[0]["title"], "연준 금리 인하")
        self.assertEqual(backend.calls, 1)

    def test_default_litellm_backend_uses_existing_analyzer_completion_callable(self) -> None:
        calls = []

        class FakeAnalyzer:
            def __init__(self, *, config):
                self.config = config

            def _call_litellm_impl(self, prompt, generation_config, **kwargs):
                calls.append((prompt, generation_config, kwargs))
                validator = kwargs.get("response_validator")
                text = '[{"id":0,"title":"연준 금리 인하","snippet":"시장이 상승했습니다"}]'
                if validator:
                    validator(text)
                return text, "openai/gpt-test", {"provider": "openai"}

        cfg = SimpleNamespace(
            generation_backend="",
            news_translation_unavailable_ttl_hours=24,
            litellm_model="openai/gpt-test",
            litellm_fallback_models=[],
            llm_model_list=[],
        )
        with patch("src.analyzer.GeminiAnalyzer", FakeAnalyzer):
            service = NewsTranslationService(db_manager=self.db, config=cfg)
            result = service.translate_items([
                {"title": "Fed cuts rates", "snippet": "Markets rally", "url": "https://a/1"}
            ], "ko")

        self.assertEqual(result[0]["translation_status"], "translated")
        self.assertEqual(result[0]["title"], "연준 금리 인하")
        self.assertEqual(len(calls), 1)

    def test_valid_cached_translation_allows_empty_original_snippet(self) -> None:
        hash_value = content_hash("Fed cuts rates", "")
        with self.db.get_session() as session:
            session.add(NewsTranslationCache(
                content_hash=hash_value,
                target_language="ko",
                source_language="en",
                translated_title="연준 금리 인하",
                translated_snippet="",
                translation_status="translated",
                updated_at=datetime.now(),
            ))
            session.commit()

        result = self._service(backend=FakeBackend("[]")).translate_items([
            {"title": "Fed cuts rates", "snippet": "", "url": "https://a/1"}
        ], "ko")

        self.assertEqual(result[0]["translation_status"], "translated")
        self.assertEqual(result[0]["snippet"], "")
        self.assertEqual(result[0]["original_snippet"], "")

    def test_cached_translation_missing_snippet_retries_when_original_snippet_non_empty(self) -> None:
        hash_value = content_hash("Fed cuts rates", "Markets rally")
        with self.db.get_session() as session:
            session.add(NewsTranslationCache(
                content_hash=hash_value,
                target_language="ko",
                source_language="en",
                translated_title="연준 금리 인하",
                translated_snippet="",
                translation_status="translated",
                updated_at=datetime.now(),
            ))
            session.commit()
        backend = FakeBackend('[{"id":0,"title":"연준 금리 인하","snippet":"시장이 상승했습니다"}]')

        result = self._service(backend=backend).translate_items([
            {"title": "Fed cuts rates", "snippet": "Markets rally", "url": "https://a/1"}
        ], "ko")

        self.assertEqual(result[0]["translation_status"], "translated")
        self.assertEqual(result[0]["snippet"], "시장이 상승했습니다")
        self.assertEqual(backend.calls, 1)

    def test_malformed_response_fails_open_and_caches_unavailable(self) -> None:
        backend = FakeBackend('{"not":"an array"}')
        item = {"title": "Fed cuts rates", "snippet": "Markets rally", "url": "https://a/1"}
        result = self._service(backend=backend).translate_items([item], "ko")
        self.assertEqual(result[0]["title"], "Fed cuts rates")
        self.assertEqual(result[0]["translation_status"], "unavailable")
        with self.db.get_session() as session:
            row = session.query(NewsTranslationCache).filter_by(
                content_hash=content_hash("Fed cuts rates", "Markets rally"),
                target_language="ko",
            ).one_or_none()
        self.assertIsNotNone(row)
        self.assertEqual(row.translation_status, "unavailable")

    def test_hanzi_leak_fails_open(self) -> None:
        backend = FakeBackend('[{"id":0,"title":"降息","snippet":"市场上涨"}]')
        item = {"title": "Fed cuts rates", "snippet": "Markets rally", "url": "https://a/1"}
        result = self._service(backend=backend).translate_items([item], "ko")
        self.assertEqual(result[0]["translation_status"], "unavailable")
        self.assertEqual(result[0]["title"], "Fed cuts rates")

    def test_stale_unavailable_cache_retries(self) -> None:
        stale_hash = content_hash("Fed cuts rates", "Markets rally")
        with self.db.get_session() as session:
            session.add(NewsTranslationCache(
                content_hash=stale_hash,
                target_language="ko",
                source_language="en",
                translation_status="unavailable",
                updated_at=datetime.now() - timedelta(hours=48),
            ))
            session.commit()
        backend = FakeBackend('[{"id":0,"title":"연준 금리 인하","snippet":"시장이 상승했습니다"}]')
        result = self._service(backend=backend, news_translation_unavailable_ttl_hours=1).translate_items([
            {"title": "Fed cuts rates", "snippet": "Markets rally", "url": "https://a/1"}
        ], "ko")
        self.assertEqual(result[0]["translation_status"], "translated")
        self.assertEqual(backend.calls, 1)

    def test_normalize_text_for_hash_is_shared_nfkc_whitespace_casefold(self) -> None:
        self.assertEqual(normalize_text_for_hash(" Ｆｅｄ\nCuts  "), normalize_text_for_hash("fed cuts"))

if __name__ == "__main__":
    unittest.main()

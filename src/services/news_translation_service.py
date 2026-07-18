# -*- coding: utf-8 -*-
"""Korean-only lazy batch translation for the report-page news card.

Operates on the merged news items produced by :class:`NewsCardMerger` (or any
``title``/``snippet`` dicts). Only ``target_language == "ko"`` triggers
translation; other languages pass through with ``translation_status="skipped"``.

Translation reuses the existing configured ``GenerationBackend`` (no new
provider/model/base URL). Results are persisted in ``news_translation_cache``
keyed by the normalized content hash + target language. Every failure
(timeout, malformed response, schema violation, Hanzi leak, backend not
configured) fails open per-item: the original text is kept and
``translation_status="unavailable"`` is returned.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config import Config, get_config
from src.report_language import detect_text_language, has_disallowed_report_script
from src.services.news_merge_service import content_hash
from src.storage import DatabaseManager, NewsTranslationCache

logger = logging.getLogger(__name__)

_KO_RATIO_THRESHOLD = 0.15
_BATCH_LIMIT = 20
_GENERATION_CONFIG = {"temperature": 0.2, "timeout": 30}
_TRANSLATION_SYSTEM_PROMPT = (
    "You are a faithful news translator. Translate each JSON object's 'title' "
    "and 'snippet' into Korean. Preserve the JSON structure, the array order, "
    "and each object's 'id' field. Do not summarize, add, or omit content. "
    "If a field is already Korean or empty, return it unchanged. "
    "Output only a JSON array of objects with 'id', 'title' and 'snippet'."
)


class NewsTranslationService:
    """Korean-only news translation with persistent cache and fail-open."""

    _batch_lock = threading.Lock()

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        config: Optional[Config] = None,
        generation_backend: Optional[Any] = None,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.config = config or get_config()
        self._generation_backend = generation_backend

    # ------------------------------------------------------------------ public

    def translate_items(
        self,
        items: List[Dict[str, Any]],
        target_language: str,
    ) -> List[Dict[str, Any]]:
        """Return ``items`` with ko translation applied (fail-open, additive)."""
        if not items:
            return list(items)

        if (target_language or "").strip().lower() != "ko":
            # Non-ko reports: no translation, just tag skipped.
            return [self._tag_skipped(item) for item in items]

        translated: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []  # items needing translation/lookup

        for item in items:
            enriched = dict(item)
            combined = self._combined_text(item)
            source_lang = detect_text_language(combined, ko_threshold=_KO_RATIO_THRESHOLD)
            enriched["source_language"] = source_lang
            if source_lang == "ko":
                enriched["translation_status"] = "original"
                # Already Korean: no duplication of the original (spec requirement 5).
                enriched.pop("original_title", None)
                enriched.pop("original_snippet", None)
                translated.append(enriched)
                continue
            # Non-ko: try cache first.
            cached = self._cache_get(content_hash(item.get("title", ""), item.get("snippet", "")), "ko")
            if cached is not None:
                if self._cache_is_fresh(cached) and self._apply_cache(enriched, cached):
                    translated.append(enriched)
                else:
                    pending.append(enriched)
                    translated.append(enriched)  # placeholder; updated in-place below
            else:
                pending.append(enriched)
                translated.append(enriched)  # placeholder; updated in-place below

        if pending:
            self._translate_batch_inplace(pending)

        return translated

    # ------------------------------------------------------------------ batch

    def _translate_batch_inplace(self, items: List[Dict[str, Any]]) -> None:
        """Translate a batch of non-ko items in place (fail-open per item)."""
        for chunk in _chunked(items, _BATCH_LIMIT):
            try:
                self._translate_chunk(chunk)
            except Exception as exc:  # whole-chunk failure -> all unavailable
                logger.warning(
                    "news translation chunk failed (fail-open): %s",
                    _safe_error(exc),
                )
                for item in chunk:
                    self._mark_unavailable(item)
                    self._cache_upsert_unavailable(item)

    def _translate_chunk(self, items: List[Dict[str, Any]]) -> None:
        payload = [
            {"id": idx, "title": item.get("title", ""), "snippet": item.get("snippet", "")}
            for idx, item in enumerate(items)
        ]
        prompt = json.dumps(payload, ensure_ascii=False)

        def _validator(text: str) -> None:
            parsed = _parse_json_array(text)
            if not isinstance(parsed, list) or len(parsed) != len(items):
                raise ValueError("translation response shape mismatch")
            seen_ids = {entry.get("id") for entry in parsed if isinstance(entry, dict)}
            expected_ids = set(range(len(items)))
            if seen_ids != expected_ids:
                raise ValueError("translation response id set mismatch")

        backend = self._resolve_backend()
        if backend is None:
            for item in items:
                self._mark_unavailable(item)
                self._cache_upsert_unavailable(item)
            return

        result = backend.generate(
            prompt,
            dict(_GENERATION_CONFIG),
            system_prompt=_TRANSLATION_SYSTEM_PROMPT,
            response_validator=_validator,
            audit_context={
                "feature": "news_translation",
                "target_language": "ko",
                "batch_size": len(items),
            },
        )
        parsed = _parse_json_array(result.text)
        by_id: Dict[int, Dict[str, Any]] = {
            int(entry.get("id")): entry
            for entry in parsed
            if isinstance(entry, dict) and entry.get("id") is not None
        }
        model_used = getattr(result, "model", "") or getattr(result, "backend", "")
        for idx, item in enumerate(items):
            entry = by_id.get(idx)
            translated_title = (entry or {}).get("title")
            translated_snippet = (entry or {}).get("snippet")
            if not self._valid_translated_pair(
                item.get("title", ""),
                item.get("snippet", ""),
                translated_title,
                translated_snippet,
            ):
                self._mark_unavailable(item)
                self._cache_upsert_unavailable(item)
                continue
            # Hanzi leak guard: a Korean card must not render Hanji from the model.
            if has_disallowed_report_script("ko", translated_title) or has_disallowed_report_script(
                "ko", translated_snippet
            ):
                self._mark_unavailable(item)
                self._cache_upsert_unavailable(item)
                continue
            # Success: keep original, surface Korean first via title/snippet.
            item["original_title"] = item.get("title")
            item["original_snippet"] = item.get("snippet")
            item["title"] = translated_title
            item["snippet"] = translated_snippet
            item["translation_status"] = "translated"
            self._cache_upsert_translated(item, translated_title, translated_snippet, model_used)

    # ------------------------------------------------------------------ backend

    def _resolve_backend(self) -> Optional[Any]:
        if self._generation_backend is not None:
            return self._generation_backend
        try:
            from src.llm.backend_factory import create_generation_backend
            from src.llm.backend_registry import LITELLM_BACKEND_ID, resolve_generation_backend_id
            from src.llm.generation_backend import GenerationError

            try:
                backend_id = resolve_generation_backend_id(self.config)
            except GenerationError:
                return None
            kwargs: Dict[str, Any] = {"config": self.config}
            if backend_id == LITELLM_BACKEND_ID:
                completion_callable = self._resolve_litellm_completion_callable()
                if completion_callable is None:
                    return None
                kwargs["litellm_completion_callable"] = completion_callable
            self._generation_backend = create_generation_backend(backend_id, **kwargs)
            return self._generation_backend
        except GenerationError:
            return None
        except Exception as exc:
            logger.warning("news translation backend resolution failed: %s", _safe_error(exc))
            return None

    def _resolve_litellm_completion_callable(self) -> Optional[Any]:
        """Return the existing analyzer LiteLLM completion seam for backend factory."""
        try:
            from src.analyzer import GeminiAnalyzer

            analyzer = GeminiAnalyzer(config=self.config)
            completion_callable = getattr(analyzer, "_call_litellm_impl", None)
            return completion_callable if callable(completion_callable) else None
        except Exception as exc:
            logger.warning(
                "news translation LiteLLM completion callable resolution failed: %s",
                _safe_error(exc),
            )
            return None

    # ------------------------------------------------------------------ cache

    def _cache_get(self, hash_value: str, target_language: str) -> Optional[NewsTranslationCache]:
        try:
            with self.db.get_session() as session:
                return session.execute(
                    select(NewsTranslationCache).where(
                        (NewsTranslationCache.content_hash == hash_value)
                        & (NewsTranslationCache.target_language == target_language)
                    ).limit(1)
                ).scalar_one_or_none()
        except Exception as exc:
            logger.warning("news translation cache read failed (continuing): %s", _safe_error(exc))
            return None

    def _cache_is_fresh(self, row: NewsTranslationCache) -> bool:
        if getattr(row, "translation_status", None) != "unavailable":
            return True
        ttl_hours = int(getattr(self.config, "news_translation_unavailable_ttl_hours", 24) or 24)
        updated = getattr(row, "updated_at", None) or getattr(row, "created_at", None)
        if not isinstance(updated, datetime):
            return True
        return updated >= datetime.now() - timedelta(hours=max(1, ttl_hours))

    def _cache_upsert(
        self,
        *,
        hash_value: str,
        target_language: str,
        source_language: Optional[str],
        translated_title: Optional[str],
        translated_snippet: Optional[str],
        status: str,
        model_used: Optional[str],
    ) -> None:
        fields = {
            "content_hash": hash_value,
            "target_language": target_language,
            "source_language": source_language,
            "translated_title": translated_title[:600] if translated_title is not None else None,
            "translated_snippet": translated_snippet,
            "translation_status": status,
            "model_used": model_used,
            "updated_at": datetime.now(),
        }
        try:
            with self.db.get_session() as session:
                existing = session.execute(
                    select(NewsTranslationCache).where(
                        (NewsTranslationCache.content_hash == hash_value)
                        & (NewsTranslationCache.target_language == target_language)
                    ).limit(1)
                ).scalar_one_or_none()
                if existing is None:
                    session.add(NewsTranslationCache(**fields))
                else:
                    for key, value in fields.items():
                        if key == "content_hash" or key == "target_language":
                            continue
                        setattr(existing, key, value)
                session.commit()
        except IntegrityError:
            # Concurrent insert of the same (hash, lang) — ignore, read on next pass.
            try:
                session.rollback()
            except Exception:
                pass
        except Exception as exc:
            logger.warning("news translation cache write failed (continuing): %s", _safe_error(exc))

    def _cache_upsert_translated(
        self, item: Dict[str, Any], title: str, snippet: str, model_used: Optional[str]
    ) -> None:
        self._cache_upsert(
            hash_value=content_hash(item.get("original_title", ""), item.get("original_snippet", "")),
            target_language="ko",
            source_language=item.get("source_language"),
            translated_title=title,
            translated_snippet=snippet,
            status="translated",
            model_used=model_used,
        )

    def _cache_upsert_unavailable(self, item: Dict[str, Any]) -> None:
        self._cache_upsert(
            hash_value=content_hash(item.get("title", ""), item.get("snippet", "")),
            target_language="ko",
            source_language=item.get("source_language"),
            translated_title=None,
            translated_snippet=None,
            status="unavailable",
            model_used=None,
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _combined_text(item: Dict[str, Any]) -> str:
        return f"{item.get('title', '')}\n\n{item.get('snippet', '')}"

    @staticmethod
    def _tag_skipped(item: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(item)
        combined = f"{item.get('title', '')}\n\n{item.get('snippet', '')}"
        enriched["source_language"] = detect_text_language(combined, ko_threshold=_KO_RATIO_THRESHOLD)
        enriched["translation_status"] = "skipped"
        return enriched

    @staticmethod
    def _apply_cache(item: Dict[str, Any], row: NewsTranslationCache) -> bool:
        status = getattr(row, "translation_status", None)
        if status == "translated":
            translated_title = getattr(row, "translated_title", None)
            translated_snippet = getattr(row, "translated_snippet", None)
            if not NewsTranslationService._valid_translated_pair(
                item.get("title", ""),
                item.get("snippet", ""),
                translated_title,
                translated_snippet,
            ):
                return False
            item["original_title"] = item.get("title")
            item["original_snippet"] = item.get("snippet")
            item["title"] = translated_title
            item["snippet"] = translated_snippet
            item["translation_status"] = "translated"
            return True
        item["translation_status"] = status or "unavailable"
        return True

    @staticmethod
    def _valid_translated_pair(
        original_title: Any,
        original_snippet: Any,
        translated_title: Any,
        translated_snippet: Any,
    ) -> bool:
        if not isinstance(translated_title, str) or not isinstance(translated_snippet, str):
            return False
        if str(original_title or "") and not translated_title:
            return False
        if str(original_snippet or "") and not translated_snippet:
            return False
        return True

    @staticmethod
    def _mark_unavailable(item: Dict[str, Any]) -> None:
        item["translation_status"] = "unavailable"
        # title/snippet stay as the original (fail-open).


def _chunked(items: List[Any], size: int) -> List[List[Any]]:
    size = max(1, int(size))
    return [items[i:i + size] for i in range(0, len(items), size)]


def _parse_json_array(text: str) -> Any:
    raw = (text or "").strip()
    # Tolerate stray code fences / leading prose.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def _safe_error(exc: Exception) -> str:
    return str(exc)[:300]

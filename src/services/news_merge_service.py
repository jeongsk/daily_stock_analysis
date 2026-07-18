# -*- coding: utf-8 -*-
"""Deterministic direct-preferred merge of intelligence pool items into the
report-page related-news card.

This module is a read-path post-processor: it combines direct query-linked
``news_intel`` items (already fetched by :class:`HistoryService`) with relevant
``intelligence_items`` pool rows for the same historical report, then applies
cross-store dedup, deterministic tier ranking, per-source/per-pool caps and
provenance metadata. It never mutates stored data and fails open (returns the
direct items unchanged) on any pool-query error.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.config import Config, get_config, resolve_news_window_days
from src.repositories.intelligence_repo import IntelligenceRepository
from src.utils.sanitize import normalize_html_plain_text

logger = logging.getLogger(__name__)

# Deterministic caps (see docs/superpowers/specs/2026-07-18-... §7.2 / D19).
_PER_SOURCE_CAP = 3
_PER_POOL_MARKET_CAP = 6
_POOL_QUERY_OVERSAMPLE_LIMIT = min(100, _PER_POOL_MARKET_CAP * _PER_SOURCE_CAP)
_FORWARD_WINDOW = timedelta(days=1)
_VALID_MERGE_MARKETS = {"cn", "hk", "us", "jp", "kr", "tw", "global"}


def normalize_text_for_hash(text: Any) -> str:
    """Normalize text so the same headline normalizes to one cache/dedup key.

    Shared by the merge dedup (title hash) and the translation cache hash so
    both stores use a single source of truth (NFKC -> lowercase -> collapse
    whitespace -> strip).
    """
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def content_hash(title: str, snippet: str) -> str:
    """SHA-256 hex of the normalized (title + '\\n\\n' + snippet) pair."""
    raw = normalize_text_for_hash(title) + "\n\n" + normalize_text_for_hash(snippet)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def title_hash(title: str) -> str:
    """SHA-256 hex of a normalized title for cross-store news dedup."""
    return hashlib.sha256(normalize_text_for_hash(title).encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str:
    """Canonicalize a URL for cross-store dedup (scheme/host lower, no fragment,
    trailing slash normalized). Returns "" for non-http(s) / placeholder urls.
    """
    raw = (url or "").strip()
    if not raw or raw.startswith("no-url:intel:"):
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    # Drop fragment; sort query parameters for stability without erasing
    # semantically meaningful query strings.
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


class NewsCardMerger:
    """Merge direct news_intel items with relevant intelligence_items pool rows."""

    def __init__(
        self,
        intel_repo: Optional[IntelligenceRepository] = None,
        config: Optional[Config] = None,
    ) -> None:
        self.repo = intel_repo or IntelligenceRepository()
        self.config = config or get_config()

    def merge_for_report(
        self,
        *,
        record: Any,
        direct_items: Sequence[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Return up to ``limit`` merged items (direct preferred, fail-open).

        ``record`` exposes ``code``/``report_type``/``created_at`` and optional
        ``context_snapshot``/``raw_result`` (parsed dicts or JSON strings).
        ``direct_items`` are the title/snippet/url dicts from
        :meth:`HistoryService.get_news_intel`.
        """
        created_at = getattr(record, "created_at", None)
        if not isinstance(created_at, datetime):
            # Without an analysis timestamp we cannot anchor a historical window;
            # fail open with direct items only (still tagged with provenance).
            return self._tag_direct_only(direct_items, limit)

        # 1) Tag direct items (T1) — always included first, reserve-aware.
        direct = [
            self._tag_direct(item)
            for item in direct_items
            if isinstance(item, dict) and (item.get("title") or item.get("url"))
        ][: max(0, int(limit))]

        pool_rows: List[Any] = []
        if getattr(self.config, "news_card_merge_intel_enabled", True):
            try:
                pool_rows = self._fetch_pool_rows(record, created_at)
            except Exception as exc:  # fail-open: pool errors never break the card
                logger.warning(
                    "NewsCardMerger pool fetch failed (fail-open, returning direct only): %s",
                    _safe_error(exc),
                )
                pool_rows = []
        else:
            # opt-out: behave like v1 (direct only).
            return self._finalize(direct, [], limit, created_at)

        # 2) Normalize pool rows into tagged dicts (T2 symbol-scope, T3 market-scope).
        pool_items = [self._tag_pool(row) for row in pool_rows]

        return self._finalize(direct, pool_items, limit, created_at)

    # ------------------------------------------------------------------ helpers

    def _fetch_pool_rows(self, record: Any, created_at: datetime) -> List[Any]:
        lookback_days = resolve_news_window_days(
            getattr(self.config, "news_max_age_days", 3),
            getattr(self.config, "news_strategy_profile", "short"),
        )
        start_at = created_at - timedelta(days=max(1, lookback_days))
        end_at = created_at + _FORWARD_WINDOW

        report_type = str(getattr(record, "report_type", "") or "")
        code = str(getattr(record, "code", "") or "").strip()

        rows: List[Any] = []
        if report_type == "market_review":
            for region in self._market_review_regions(record):
                rows.extend(
                    self.repo.list_items_for_report(
                        scope_type="market",
                        market=region,
                        start_at=start_at,
                        end_at=end_at,
                        limit=_POOL_QUERY_OVERSAMPLE_LIMIT,
                    )
                )
            return rows

        market = _infer_market(code)
        symbol = _canonical_symbol(code)
        if symbol and market:
            rows.extend(
                self.repo.list_items_for_report(
                    scope_type="symbol",
                    scope_value=symbol,
                    market=market,
                    start_at=start_at,
                    end_at=end_at,
                    limit=_POOL_QUERY_OVERSAMPLE_LIMIT,
                )
            )
        if market:
            rows.extend(
                self.repo.list_items_for_report(
                    scope_type="market",
                    market=market,
                    start_at=start_at,
                    end_at=end_at,
                    limit=_POOL_QUERY_OVERSAMPLE_LIMIT,
                )
            )
        return rows

    def _market_review_regions(self, record: Any) -> List[str]:
        """Derive review regions deterministically from the persisted payload.

        Order: (1) analysis_context_pack_overview.subject.market (single),
        (2) any market_review payload ``region``/``markets`` keys in the stored
        snapshot/raw_result, (3) read-time ``market_review_region`` config via
        the existing ``_resolve_market_review_regions`` helper. Empty/unknown
        -> caller fails open (no T3 rows).
        """
        regions: List[str] = []
        for blob in (_parsed(getattr(record, "context_snapshot", None)),
                     _parsed(getattr(record, "raw_result", None))):
            if not isinstance(blob, dict):
                continue
            overview = blob.get("analysis_context_pack_overview")
            if isinstance(overview, dict):
                subject = overview.get("subject")
                if isinstance(subject, dict):
                    market = str(subject.get("market") or "").strip().lower()
                    if market in _VALID_MERGE_MARKETS:
                        regions.append(market)
            payload_regions = _extract_payload_regions(blob)
            for region in payload_regions:
                if region in _VALID_MERGE_MARKETS:
                    regions.append(region)
            # Also handle a top-level market_review payload nested under raw_result.
            for candidate in (blob.get("market_review_payload"), blob.get("market_review")):
                payload_regions = _extract_payload_regions(candidate)
                for region in payload_regions:
                    if region in _VALID_MERGE_MARKETS:
                        regions.append(region)

        if not regions:
            raw_region = getattr(self.config, "market_review_region", None)
            try:
                from src.core.market_review import _resolve_market_review_regions

                regions = list(_resolve_market_review_regions(raw_region))
            except Exception:
                regions = [str(raw_region).strip().lower()] if raw_region else []

        # De-dup preserving order.
        seen: set = set()
        ordered: List[str] = []
        for region in regions:
            if region and region not in seen:
                seen.add(region)
                ordered.append(region)
        return ordered

    @staticmethod
    def _tag_direct(item: Dict[str, Any]) -> Dict[str, Any]:
        tagged = dict(item)
        tagged.setdefault("provenance", "direct")
        tagged.setdefault("source_type", "search")
        tagged.setdefault("source", item.get("source") or "search")
        return tagged

    @staticmethod
    def _tag_pool(row: Any) -> Dict[str, Any]:
        title = normalize_html_plain_text(getattr(row, "title", "") or "")
        snippet = normalize_html_plain_text(
            getattr(row, "summary", "") or getattr(row, "snippet", "") or ""
        )
        url = str(getattr(row, "url", "") or "")
        source_name = str(getattr(row, "source_name", "") or getattr(row, "source", "") or "")
        published_at = getattr(row, "published_at", None) or getattr(row, "fetched_at", None)
        scope_type = str(getattr(row, "scope_type", "") or "market").lower()
        return {
            "title": title,
            "snippet": snippet,
            "url": url,
            "provenance": "pool",
            "source": source_name or getattr(row, "source", "") or "",
            "source_type": str(getattr(row, "source_type", "") or "rss"),
            "published_at": _iso(published_at),
            "_published_dt": published_at if isinstance(published_at, datetime) else None,
            "_tier": 1 if scope_type == "symbol" else 2,
            "_is_market_pool": scope_type == "market",
            "_source_key": (source_name or url or title)[:120],
            "_row_id": int(getattr(row, "id", 0) or 0),
        }

    def _tag_direct_only(self, direct_items: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        direct = [self._tag_direct(item) for item in direct_items if isinstance(item, dict)]
        return self._finalize(direct, [], limit, None)

    def _finalize(
        self,
        direct: List[Dict[str, Any]],
        pool: List[Dict[str, Any]],
        limit: int,
        created_at: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        limit = max(0, min(int(limit), 100))
        # Mark tiers: direct=0, pool symbol-scope=1, pool market-scope=2.
        for item in direct:
            item.setdefault("_tier", 0)
        for item in pool:
            item.setdefault("_tier", 2)

        # Cross-store dedup: canonical URL, then title hash. Direct wins ties
        # (processed first -> occupies the seen sets first).
        seen_urls: set = set()
        seen_hashes: set = set()
        merged: List[Dict[str, Any]] = []

        def consider(item: Dict[str, Any]) -> None:
            url_key = canonical_url(item.get("url", ""))
            hash_key = title_hash(item.get("title", ""))
            if url_key and url_key in seen_urls:
                return
            if hash_key and hash_key in seen_hashes:
                return
            if url_key:
                seen_urls.add(url_key)
            if hash_key:
                seen_hashes.add(hash_key)
            merged.append(item)

        # Direct reserve: keep up to `limit` direct first.
        reserved_direct = direct[:limit]
        for item in reserved_direct:
            consider(item)

        # Rank pool deterministically: tier asc, |published_at - created_at| asc
        # (None published_at -> treated as created_at / distance 0), id asc.
        def pool_sort_key(item: Dict[str, Any]):
            published_dt = item.get("_published_dt")
            if created_at is not None and isinstance(published_dt, datetime):
                distance = abs((published_dt - created_at).total_seconds())
            else:
                distance = 0.0
            return (int(item.get("_tier", 2)), distance, int(item.get("_row_id", 0) or 0))

        ranked_pool = sorted(pool, key=pool_sort_key)

        # Apply per-source and market-pool caps while filling remaining slots.
        source_counts: Dict[str, int] = {}
        pool_market_count = 0
        remaining = limit - len(merged)
        for item in ranked_pool:
            if remaining <= 0:
                break
            source_key = str(item.get("_source_key") or "")
            if source_counts.get(source_key, 0) >= _PER_SOURCE_CAP:
                continue
            is_market_pool = bool(item.get("_is_market_pool"))
            if is_market_pool and pool_market_count >= _PER_POOL_MARKET_CAP:
                continue
            # Dedup check against the running merged set.
            url_key = canonical_url(item.get("url", ""))
            hash_key = title_hash(item.get("title", ""))
            if url_key and url_key in seen_urls:
                continue
            if hash_key and hash_key in seen_hashes:
                continue
            merged.append(item)
            if url_key:
                seen_urls.add(url_key)
            if hash_key:
                seen_hashes.add(hash_key)
            source_counts[source_key] = source_counts.get(source_key, 0) + 1
            if is_market_pool:
                pool_market_count += 1
            remaining -= 1

        return self._strip_internal(merged[:limit])

    @staticmethod
    def _strip_internal(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cleaned: List[Dict[str, Any]] = []
        for item in items:
            stripped = {
                k: v for k, v in item.items()
                if not k.startswith("_")
            }
            cleaned.append(stripped)
        return cleaned


def _infer_market(code: str) -> Optional[str]:
    """Reuse the existing market inference helper (single source of truth)."""
    try:
        from src.services.decision_signal_reassess_service import _infer_market as _impl

        return _impl(code)
    except Exception:
        return None


def _canonical_symbol(code: str) -> Optional[str]:
    try:
        from data_provider.base import canonical_stock_code

        symbol = canonical_stock_code(code or "")
        return symbol or None
    except Exception:
        return code or None


def _extract_payload_regions(blob: Any) -> List[str]:
    if not isinstance(blob, dict):
        return []
    regions: List[str] = []
    region = blob.get("region")
    if isinstance(region, str) and region.strip():
        regions.append(region.strip().lower())
    markets = blob.get("markets")
    if isinstance(markets, dict):
        for key in markets.keys():
            if isinstance(key, str) and key.strip():
                regions.append(key.strip().lower())
    return regions


def _parsed(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        import json

        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    return text[:300]

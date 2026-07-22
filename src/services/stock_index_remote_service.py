# -*- coding: utf-8 -*-
"""Best-effort remote cache for the generated stock autocomplete index."""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STOCK_INDEX_REMOTE_URL = (
    "https://raw.githubusercontent.com/ZhuLinsen/daily_stock_analysis/"
    "main/apps/dsa-web/public/stocks.index.json"
)
DEFAULT_STOCK_INDEX_CACHE_PATH = REPO_ROOT / "data" / "cache" / "stocks.index.json"
# Committed, curated index — the regression-guard floor (design spec
# docs/superpowers/specs/2026-07-22-stock-index-remote-regression-guard-design.md
# §2). Read-only: this module never writes to it.
DEFAULT_STOCK_INDEX_BASELINE_PATH = REPO_ROOT / "apps" / "dsa-web" / "public" / "stocks.index.json"
# The committed index ships in two possible bundled locations, in preference
# order, and the regression-guard baseline must resolve to whichever one
# actually exists — never the remote cache. A dev checkout has
# apps/dsa-web/public/; the deployed analyzer image copies only static/
# (apps/dsa-web is a web-frontend source dir, not shipped into the backend
# image), so hardcoding apps/dsa-web/public alone makes the baseline "missing"
# in the container and degrades the guard to a permanent conservative-reject
# plus a noisy ERROR on every load.
STOCK_INDEX_BASELINE_CANDIDATE_PATHS = (
    REPO_ROOT / "apps" / "dsa-web" / "public" / "stocks.index.json",
    REPO_ROOT / "static" / "stocks.index.json",
)
DEFAULT_STOCK_INDEX_REMOTE_TTL_HOURS = 48
DEFAULT_STOCK_INDEX_REMOTE_TIMEOUT_SECONDS = 10
DEFAULT_STOCK_INDEX_REMOTE_MAX_FAILURES = 3
# Below this fraction of the committed baseline's per-market item count, a
# candidate index is considered a regression for that market (design spec §2).
DEFAULT_STOCK_INDEX_REMOTE_MIN_MARKET_RATIO = 0.8
SUPPORTED_STOCK_INDEX_MARKETS = {"CN", "HK", "US", "BSE", "JP", "KR"}

_REMOTE_REFRESH_LOCK = Lock()
_REMOTE_FAILURE_LOCK = Lock()
_REMOTE_CONSECUTIVE_FAILURES = 0
_REMOTE_SUPPRESS_UNTIL = 0.0


@dataclass(frozen=True)
class RemoteStockIndexSettings:
    """Runtime settings for remote stock-index refresh."""

    enabled: bool = True
    url: str = DEFAULT_STOCK_INDEX_REMOTE_URL
    ttl_hours: int = DEFAULT_STOCK_INDEX_REMOTE_TTL_HOURS
    timeout_seconds: int = DEFAULT_STOCK_INDEX_REMOTE_TIMEOUT_SECONDS
    cache_path: Path = DEFAULT_STOCK_INDEX_CACHE_PATH
    min_market_ratio: float = DEFAULT_STOCK_INDEX_REMOTE_MIN_MARKET_RATIO


@dataclass(frozen=True)
class RemoteStockIndexResult:
    """Outcome of a best-effort refresh attempt."""

    cache_path: Optional[Path]
    refreshed: bool = False
    skipped: bool = False
    error: Optional[str] = None


def settings_from_config(config: Any) -> RemoteStockIndexSettings:
    """Build remote stock-index settings from the application config object."""
    configured_url = str(getattr(config, "stock_index_remote_url", "") or "").strip()
    configured_ratio = getattr(config, "stock_index_remote_min_market_ratio", None)
    try:
        min_market_ratio = (
            float(configured_ratio)
            if configured_ratio is not None
            else DEFAULT_STOCK_INDEX_REMOTE_MIN_MARKET_RATIO
        )
    except (TypeError, ValueError):
        min_market_ratio = DEFAULT_STOCK_INDEX_REMOTE_MIN_MARKET_RATIO

    return RemoteStockIndexSettings(
        enabled=bool(getattr(config, "stock_index_remote_update_enabled", True)),
        url=configured_url or DEFAULT_STOCK_INDEX_REMOTE_URL,
        ttl_hours=DEFAULT_STOCK_INDEX_REMOTE_TTL_HOURS,
        timeout_seconds=DEFAULT_STOCK_INDEX_REMOTE_TIMEOUT_SECONDS,
        min_market_ratio=min_market_ratio,
    )


def get_remote_stock_index_cache_path() -> Path:
    """Return the canonical on-disk cache path for remote stock index data."""
    return DEFAULT_STOCK_INDEX_CACHE_PATH


def get_stock_index_baseline_path() -> Path:
    """Return the on-disk path for the committed baseline index used as the
    regression-guard floor — never the (possibly polluted) remote cache.

    Resolves to the first **existing** bundled candidate
    (``STOCK_INDEX_BASELINE_CANDIDATE_PATHS``): ``apps/dsa-web/public/`` in a
    dev checkout, ``static/`` in the deployed backend image. If none exist,
    returns the canonical default so the caller's "missing baseline -> reject
    conservatively" path (and its ERROR log) still identifies a concrete path.
    Read-only from this module's perspective.
    """
    for candidate in STOCK_INDEX_BASELINE_CANDIDATE_PATHS:
        if candidate.is_file():
            return candidate
    return DEFAULT_STOCK_INDEX_BASELINE_PATH


def parse_stock_index_remote_min_market_ratio(value: Optional[str]) -> float:
    """Parse ``STOCK_INDEX_REMOTE_MIN_MARKET_RATIO``: must be finite and in
    ``(0, 1]`` (design spec §2/§4). Unlike a clamp, any invalid value (blank
    aside, which just means "unset") is an ERROR-logged misconfiguration that
    forces the safe default wholesale — a ratio of exactly ``0`` would make
    the regression check never trigger (``incoming < 0`` is never true for a
    non-negative count), silently defeating the guard, so ``0`` is rejected
    just like negative/NaN/>1 values.
    """
    if value is None or not str(value).strip():
        return DEFAULT_STOCK_INDEX_REMOTE_MIN_MARKET_RATIO
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        logger.error(
            "STOCK_INDEX_REMOTE_MIN_MARKET_RATIO=%r is not a valid number; forcing default %s",
            value,
            DEFAULT_STOCK_INDEX_REMOTE_MIN_MARKET_RATIO,
        )
        return DEFAULT_STOCK_INDEX_REMOTE_MIN_MARKET_RATIO
    if not math.isfinite(parsed) or parsed <= 0 or parsed > 1:
        logger.error(
            "STOCK_INDEX_REMOTE_MIN_MARKET_RATIO=%r must be a finite number within (0, 1]; forcing default %s",
            value,
            DEFAULT_STOCK_INDEX_REMOTE_MIN_MARKET_RATIO,
        )
        return DEFAULT_STOCK_INDEX_REMOTE_MIN_MARKET_RATIO
    return parsed


def get_stock_index_min_market_ratio() -> float:
    """Resolve the min-market-ratio directly from the environment.

    Used independently by the loader's read-path regression gate (which has
    no ``Config`` object threaded through its module-level lazy caches) and by
    ``settings_from_config``'s write-path guard, so a single parsing/
    validation implementation governs both.
    """
    return parse_stock_index_remote_min_market_ratio(os.getenv("STOCK_INDEX_REMOTE_MIN_MARKET_RATIO"))


def per_market_counts(payload: list) -> dict[str, int]:
    """Return the number of items per ``market`` field in a stock-index payload.

    Tolerant of malformed rows (skips them) since this is used both on
    already-validated payloads and on best-effort regression checks.
    """
    counts: dict[str, int] = {}
    for item in payload:
        if not isinstance(item, list) or len(item) < 7:
            continue
        market = str(item[6] or "").strip().upper()
        if not market:
            continue
        counts[market] = counts.get(market, 0) + 1
    return counts


def regressing_markets(
    incoming_counts: dict[str, int],
    baseline_counts: dict[str, int],
    ratio: float,
) -> dict[str, tuple[int, int]]:
    """Return ``{market: (incoming, baseline)}`` for every baseline market
    where ``incoming < ratio * baseline`` (design spec §2/§4).

    A market present in ``incoming`` but absent from ``baseline`` is a new
    market addition, never a regression. A market absent from ``incoming``
    (count 0) but present in ``baseline`` is the most severe regression case
    (a whole market silently dropped).
    """
    offending: dict[str, tuple[int, int]] = {}
    for market, baseline_count in baseline_counts.items():
        if baseline_count <= 0:
            continue
        incoming_count = incoming_counts.get(market, 0)
        if incoming_count < ratio * baseline_count:
            offending[market] = (incoming_count, baseline_count)
    return offending


def payload_regresses_market(
    incoming_counts: dict[str, int],
    baseline_counts: dict[str, int],
    ratio: float,
) -> bool:
    """Convenience boolean wrapper around :func:`regressing_markets`."""
    return bool(regressing_markets(incoming_counts, baseline_counts, ratio))


def assert_no_market_regression(
    payload: list,
    baseline_counts: dict[str, int],
    ratio: float,
) -> None:
    """Raise ``ValueError`` if ``payload`` regresses any baseline market
    below ``ratio`` (write-path guard, design spec §2/§3). The message
    embeds per-market before/after counts so the caller's failure-log line
    surfaces them without a second log call.
    """
    incoming_counts = per_market_counts(payload)
    offending = regressing_markets(incoming_counts, baseline_counts, ratio)
    if offending:
        details = ", ".join(
            f"{market}: incoming={incoming} baseline={baseline}"
            for market, (incoming, baseline) in sorted(offending.items())
        )
        raise ValueError(
            f"remote stock index regresses market(s) below {ratio:.2f}x committed baseline: {details}"
        )


def _load_baseline_market_counts_for_write(baseline_path: Path) -> dict[str, int]:
    """Load and count the committed baseline for the write-path guard.

    Any failure to read/parse the baseline is a conservative rejection
    (design spec §4 "커밋 기준선 파일 부재/파싱 실패 → 보수적으로 원격 거부"),
    ERROR-logged distinctly from the generic best-effort WARNING the caller
    already emits for refresh failures.
    """
    try:
        baseline_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        validate_stock_index_payload(baseline_payload)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.error(
            "[stock-index] committed baseline index unavailable/invalid at %s; "
            "rejecting remote refresh conservatively: %s",
            baseline_path,
            exc,
        )
        raise ValueError(f"committed baseline index unavailable at {baseline_path}: {exc}") from exc
    return per_market_counts(baseline_payload)


def is_remote_stock_index_cache_fresh(
    cache_path: Path = DEFAULT_STOCK_INDEX_CACHE_PATH,
    *,
    ttl_hours: int = DEFAULT_STOCK_INDEX_REMOTE_TTL_HOURS,
    now: Optional[float] = None,
) -> bool:
    """Return whether the remote cache exists and is still inside its TTL."""
    if ttl_hours <= 0 or not cache_path.is_file():
        return False

    current_time = time.time() if now is None else now
    try:
        age_seconds = current_time - cache_path.stat().st_mtime
    except OSError:
        return False
    return age_seconds < ttl_hours * 3600


def validate_stock_index_payload(
    payload: Any,
    *,
    min_items: int = 100,
) -> list[list[Any]]:
    """Validate the compressed ``stocks.index.json`` wire format."""
    if not isinstance(payload, list):
        raise ValueError("stock index payload must be a list")
    if len(payload) < min_items:
        raise ValueError(f"stock index payload is unexpectedly small: {len(payload)}")

    for index, item in enumerate(payload):
        if not isinstance(item, list) or len(item) < 10:
            raise ValueError(f"stock index item {index} is not a compressed tuple")

        (
            canonical_code,
            display_code,
            name,
            _pinyin,
            _abbr,
            aliases,
            market,
            asset_type,
            active,
            popularity,
        ) = item[:10]
        if not all(isinstance(value, str) and value.strip() for value in (canonical_code, display_code, name)):
            raise ValueError(f"stock index item {index} is missing code or name")
        if not isinstance(aliases, list):
            raise ValueError(f"stock index item {index} aliases must be a list")
        if market not in SUPPORTED_STOCK_INDEX_MARKETS:
            raise ValueError(f"stock index item {index} has unsupported market: {market!r}")
        if asset_type not in {"stock", "index", "etf"}:
            raise ValueError(f"stock index item {index} has unsupported asset type: {asset_type!r}")
        if not isinstance(active, bool):
            raise ValueError(f"stock index item {index} active flag must be boolean")
        if (
            isinstance(popularity, bool)
            or not isinstance(popularity, (int, float))
            or not math.isfinite(float(popularity))
        ):
            raise ValueError(f"stock index item {index} popularity must be a finite number")

    return payload


def is_valid_remote_stock_index_file(cache_path: Path = DEFAULT_STOCK_INDEX_CACHE_PATH) -> bool:
    """Return whether a cached remote stock-index file is still usable."""
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        validate_stock_index_payload(payload)
        return True
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("[stock-index] cached remote index is invalid: %s", exc)
        return False


def _download_remote_stock_index(settings: RemoteStockIndexSettings) -> tuple[bytes, list]:
    response = requests.get(settings.url, timeout=settings.timeout_seconds)
    response.raise_for_status()

    content = response.content
    payload = json.loads(content.decode("utf-8"))
    validate_stock_index_payload(payload)
    return content, payload


def _atomic_write(cache_path: Path, content: bytes) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_bytes(content)
        os.replace(temp_path, cache_path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _clear_backend_stock_index_cache() -> None:
    try:
        from src.data.stock_index_loader import clear_stock_index_cache

        clear_stock_index_cache()
    except Exception as exc:  # noqa: BLE001 - cache clearing must not break refresh.
        logger.warning("[stock-index] remote index refreshed but backend cache clear failed: %s", exc)


def _reset_remote_failure_state() -> None:
    global _REMOTE_CONSECUTIVE_FAILURES, _REMOTE_SUPPRESS_UNTIL
    with _REMOTE_FAILURE_LOCK:
        _REMOTE_CONSECUTIVE_FAILURES = 0
        _REMOTE_SUPPRESS_UNTIL = 0.0


def _remote_refresh_suppressed(now: float) -> bool:
    global _REMOTE_CONSECUTIVE_FAILURES, _REMOTE_SUPPRESS_UNTIL
    with _REMOTE_FAILURE_LOCK:
        if _REMOTE_CONSECUTIVE_FAILURES < DEFAULT_STOCK_INDEX_REMOTE_MAX_FAILURES:
            return False
        if now < _REMOTE_SUPPRESS_UNTIL:
            return True
        _REMOTE_CONSECUTIVE_FAILURES = 0
        _REMOTE_SUPPRESS_UNTIL = 0.0
        return False


def _record_remote_failure(now: float, ttl_hours: int) -> int:
    global _REMOTE_CONSECUTIVE_FAILURES, _REMOTE_SUPPRESS_UNTIL
    with _REMOTE_FAILURE_LOCK:
        _REMOTE_CONSECUTIVE_FAILURES += 1
        if _REMOTE_CONSECUTIVE_FAILURES >= DEFAULT_STOCK_INDEX_REMOTE_MAX_FAILURES:
            _REMOTE_SUPPRESS_UNTIL = now + max(ttl_hours, 1) * 3600
        return _REMOTE_CONSECUTIVE_FAILURES


def refresh_remote_stock_index_cache(settings: RemoteStockIndexSettings) -> RemoteStockIndexResult:
    """Refresh the remote stock index cache without breaking callers on failure."""
    cache_path = settings.cache_path
    if not settings.enabled:
        return RemoteStockIndexResult(cache_path=cache_path if cache_path.is_file() else None, skipped=True)

    current_time = time.time()
    if is_remote_stock_index_cache_fresh(cache_path, ttl_hours=settings.ttl_hours, now=current_time):
        if is_valid_remote_stock_index_file(cache_path):
            _reset_remote_failure_state()
            return RemoteStockIndexResult(cache_path=cache_path, skipped=True)

    if _remote_refresh_suppressed(current_time):
        return RemoteStockIndexResult(
            cache_path=cache_path if is_valid_remote_stock_index_file(cache_path) else None,
            skipped=True,
            error="remote update temporarily suppressed after repeated failures",
        )

    if not _REMOTE_REFRESH_LOCK.acquire(blocking=False):
        return RemoteStockIndexResult(
            cache_path=cache_path if is_valid_remote_stock_index_file(cache_path) else None,
            skipped=True,
        )

    try:
        if is_remote_stock_index_cache_fresh(cache_path, ttl_hours=settings.ttl_hours):
            if is_valid_remote_stock_index_file(cache_path):
                _reset_remote_failure_state()
                return RemoteStockIndexResult(cache_path=cache_path, skipped=True)

        content, payload = _download_remote_stock_index(settings)
        baseline_counts = _load_baseline_market_counts_for_write(get_stock_index_baseline_path())
        assert_no_market_regression(payload, baseline_counts, settings.min_market_ratio)
        _atomic_write(cache_path, content)
        _clear_backend_stock_index_cache()
        _reset_remote_failure_state()
        logger.info("[stock-index] remote index refreshed: %s", cache_path)
        return RemoteStockIndexResult(cache_path=cache_path, refreshed=True)
    except Exception as exc:  # noqa: BLE001 - remote refresh is best-effort by design.
        message = str(exc)
        failures = _record_remote_failure(current_time, settings.ttl_hours)
        logger.warning(
            "[stock-index] remote update failed (%d/%d), using local fallback: %s",
            failures,
            DEFAULT_STOCK_INDEX_REMOTE_MAX_FAILURES,
            message,
        )
        if is_valid_remote_stock_index_file(cache_path):
            return RemoteStockIndexResult(cache_path=cache_path, error=message)
        return RemoteStockIndexResult(cache_path=None, error=message)
    finally:
        _REMOTE_REFRESH_LOCK.release()


def _reset_remote_stock_index_state_for_tests() -> None:
    _reset_remote_failure_state()

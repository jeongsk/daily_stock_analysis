"""Read-only World Monitor boundary: health probe and market-review event sync.

Design:
- docs/superpowers/specs/2026-07-27-worldmonitor-self-hosting-design.md (status)
- docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md (events)

The event sync deliberately lives here rather than in a new module: base-URL
normalization, the timeout policy, and diagnostic sanitization already exist on
this boundary and must not be reimplemented alongside it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

import httpx

from src.config import Config, get_config
from src.repositories.world_event_repo import PROMPT_SCOPES, WorldEventRepository
from src.services.run_diagnostics import sanitize_diagnostic_text
from src.services.worldmonitor_events import (
    CATEGORIES,
    CATEGORY_CONFLICT,
    CATEGORY_ENDPOINTS,
    CATEGORY_ENERGY,
    CATEGORY_OUTAGE,
    normalize_acled_event,
    normalize_energy_disruption,
    normalize_internet_outage,
)
from src.storage import WorldEvent

logger = logging.getLogger(__name__)

WorldMonitorStatusName = Literal[
    "disabled", "healthy", "degraded", "unreachable", "misconfigured"
]

FreshnessState = Literal["fresh", "stale", "unavailable", "unverified"]

# 每个类别的响应数组字段名与归一化函数。
_CATEGORY_SPECS = {
    CATEGORY_CONFLICT: ("events", normalize_acled_event),
    CATEGORY_OUTAGE: ("outages", normalize_internet_outage),
    CATEGORY_ENERGY: ("events", normalize_energy_disruption),
}


@dataclass(frozen=True)
class WorldMonitorStatus:
    status: WorldMonitorStatusName
    base_url: Optional[str] = None
    detail: Optional[str] = None

    def to_dict(self) -> dict[str, Optional[str]]:
        return {
            "status": self.status,
            "base_url": self.base_url,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CategorySyncOutcome:
    """单个类别一次同步的结果。"""

    category: str
    status: Literal["ok", "error", "skipped"]
    event_count: int = 0
    upstream_unavailable: bool = False
    error: Optional[str] = None


@dataclass(frozen=True)
class SyncResult:
    performed: bool
    outcomes: Dict[str, CategorySyncOutcome] = field(default_factory=dict)
    budget_exhausted: bool = False


@dataclass(frozen=True)
class CategoryFreshness:
    """某个类别在提示词里可以声称到什么程度。

    ``can_claim_no_events`` 与 ``state`` 分开，是因为"同步成功"并不等于"这个类别
    真的能产出事件"（设计 §7.1）。
    """

    category: str
    state: FreshnessState
    last_success_at: Optional[datetime] = None
    last_nonempty_at: Optional[datetime] = None
    can_claim_no_events: bool = False
    detail: Optional[str] = None


class WorldMonitorService:
    """Probe World Monitor without affecting the stock-analysis liveness contract."""

    def __init__(
        self,
        config: Optional[Config] = None,
        repo: Optional[WorldEventRepository] = None,
    ):
        self.config = config or get_config()
        self._repo = repo
        self._last_sync_at: Optional[datetime] = None

    @property
    def repo(self) -> WorldEventRepository:
        if self._repo is None:
            self._repo = WorldEventRepository()
        return self._repo

    # ------------------------------------------------------------------
    # 状态探测（self-hosting 阶段既有行为，保持不变）
    # ------------------------------------------------------------------

    def get_status(self) -> WorldMonitorStatus:
        if not self.config.worldmonitor_enabled:
            return WorldMonitorStatus(status="disabled")

        base_url = self._resolve_base_url()
        if base_url is None:
            return WorldMonitorStatus(
                status="misconfigured",
                detail="WORLDMONITOR_BASE_URL must be an HTTP(S) origin without credentials",
            )

        try:
            response = httpx.get(
                f"{base_url}/api/health",
                params={"compact": "1"},
                timeout=self._timeout(),
                follow_redirects=False,
            )
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            detail = sanitize_diagnostic_text(str(exc), max_length=200) or "connection failed"
            return WorldMonitorStatus(
                status="unreachable",
                base_url=base_url,
                detail=detail,
            )

        overall = str(payload.get("overall") or payload.get("status") or "").lower() if isinstance(payload, dict) else ""
        if response.status_code == 200 and overall in {"healthy", "ok"}:
            return WorldMonitorStatus(status="healthy", base_url=base_url)
        return WorldMonitorStatus(
            status="degraded",
            base_url=base_url,
            detail=f"health response status={response.status_code} overall={overall or 'unknown'}",
        )

    # ------------------------------------------------------------------
    # 事件同步
    # ------------------------------------------------------------------

    def sync_events(self, *, now: Optional[datetime] = None) -> SyncResult:
        """在市场复盘之前同步一次事件。

        全程 fail-open：任何异常都不向复盘链路传播（设计 §6）。单个类别失败只影响
        该类别，其余继续。
        """
        now = now or datetime.now()

        if not (self.config.worldmonitor_enabled and self.config.worldmonitor_events_enabled):
            return SyncResult(performed=False)

        if self._within_cooldown(now):
            return SyncResult(performed=False)

        base_url = self._resolve_base_url()
        if base_url is None:
            logger.warning("[WorldMonitor] event sync skipped: base URL is misconfigured")
            return SyncResult(performed=False)

        started = time.monotonic()
        budget = self.config.worldmonitor_sync_budget_seconds
        outcomes: Dict[str, CategorySyncOutcome] = {}
        budget_exhausted = False

        for category in CATEGORIES:
            # 整次同步共用一个截止时间，而不是给每个类别单独设超时：后者的最坏
            # 延迟会随类别数线性增长，让复盘开始时间变得不可预测（设计 §6.1）。
            if time.monotonic() - started >= budget:
                budget_exhausted = True
                outcomes[category] = CategorySyncOutcome(
                    category=category, status="skipped", error="sync budget exhausted"
                )
                continue

            outcome = self._sync_category(category, base_url=base_url, now=now)
            outcomes[category] = outcome
            self.repo.record_sync(
                category=category,
                status=outcome.status,
                synced_at=now,
                event_count=outcome.event_count,
                error=outcome.error,
            )

        self._last_sync_at = now

        if any(outcome.status == "ok" for outcome in outcomes.values()):
            try:
                self.repo.prune(
                    now=now, retention_days=self.config.worldmonitor_event_retention_days
                )
            except Exception as exc:  # pragma: no cover - 清理失败不应影响复盘
                logger.warning("[WorldMonitor] retention prune failed: %s", exc)

        return SyncResult(
            performed=True, outcomes=outcomes, budget_exhausted=budget_exhausted
        )

    def _sync_category(
        self, category: str, *, base_url: str, now: datetime
    ) -> CategorySyncOutcome:
        endpoint = CATEGORY_ENDPOINTS[category]
        array_key, normalizer = _CATEGORY_SPECS[category]

        try:
            response = httpx.get(
                f"{base_url}{endpoint}",
                timeout=self._timeout(),
                follow_redirects=False,
            )
            if response.status_code != 200:
                return CategorySyncOutcome(
                    category=category,
                    status="error",
                    error=f"upstream status {response.status_code}",
                )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            detail = sanitize_diagnostic_text(str(exc), max_length=200) or "request failed"
            return CategorySyncOutcome(category=category, status="error", error=detail)
        except Exception as exc:  # pragma: no cover - fail-open 兜底
            detail = sanitize_diagnostic_text(str(exc), max_length=200) or "unexpected failure"
            logger.warning("[WorldMonitor] unexpected sync failure category=%s", category)
            return CategorySyncOutcome(category=category, status="error", error=detail)

        if not isinstance(payload, dict):
            return CategorySyncOutcome(
                category=category, status="error", error="unexpected response shape"
            )

        # 只有 list-energy-disruptions 会明确告诉我们"上游没数据"与"确实没事件"
        # 的区别，这个信号不能丢（设计 §3.1）。
        upstream_unavailable = bool(payload.get("upstreamUnavailable"))

        raw_items = payload.get(array_key) or []
        if not isinstance(raw_items, list):
            raw_items = []

        normalized: List[Dict[str, Any]] = []
        for raw in raw_items:
            item = normalizer(raw)
            if item is not None:
                normalized.append(item)

        try:
            stored = self.repo.upsert_events(normalized, collected_at=now)
        except Exception as exc:  # pragma: no cover - 写库失败不应打断复盘
            detail = sanitize_diagnostic_text(str(exc), max_length=200) or "persist failed"
            logger.warning("[WorldMonitor] event persist failed category=%s", category)
            return CategorySyncOutcome(category=category, status="error", error=detail)

        if upstream_unavailable:
            return CategorySyncOutcome(
                category=category,
                status="error",
                event_count=stored,
                upstream_unavailable=True,
                error="upstream reported unavailable",
            )

        return CategorySyncOutcome(
            category=category, status="ok", event_count=stored
        )

    # ------------------------------------------------------------------
    # 新鲜度
    # ------------------------------------------------------------------

    def get_freshness(self, category: str, *, now: Optional[datetime] = None) -> CategoryFreshness:
        """判定某个类别在提示词里能声称到什么程度（设计 §7、§7.1）。"""
        now = now or datetime.now()
        try:
            state_row = self.repo.get_sync_state(category)
        except Exception:  # pragma: no cover - 读状态失败按"无法确认"处理
            state_row = None

        if state_row is None or state_row.last_success_at is None:
            return CategoryFreshness(
                category=category,
                state="unavailable",
                detail=(state_row.last_error if state_row else None),
            )

        last_success = state_row.last_success_at
        last_nonempty = state_row.last_nonempty_at

        # "确实没有事件"这句话，只有在该类别于回溯窗口内真的产出过事件时才能说。
        # 上游缺 API key 时会长期返回 200 + 空数组，仅看成功时刻会把它读成
        # "无事件"，这正是 §3.1 要防的虚假信心。
        lookback_start = now - timedelta(days=self.config.worldmonitor_event_lookback_days)
        proven = last_nonempty is not None and last_nonempty >= lookback_start

        age = (now - last_success).total_seconds()
        if age > self.config.worldmonitor_event_stale_after_seconds:
            return CategoryFreshness(
                category=category,
                state="stale",
                last_success_at=last_success,
                last_nonempty_at=last_nonempty,
                can_claim_no_events=False,
                detail=state_row.last_error,
            )

        if not proven:
            return CategoryFreshness(
                category=category,
                state="unverified",
                last_success_at=last_success,
                last_nonempty_at=last_nonempty,
                can_claim_no_events=False,
                detail=state_row.last_error,
            )

        return CategoryFreshness(
            category=category,
            state="fresh",
            last_success_at=last_success,
            last_nonempty_at=last_nonempty,
            can_claim_no_events=True,
        )

    def get_all_freshness(self, *, now: Optional[datetime] = None) -> Dict[str, CategoryFreshness]:
        now = now or datetime.now()
        return {category: self.get_freshness(category, now=now) for category in CATEGORIES}

    # ------------------------------------------------------------------
    # 提示词取数
    # ------------------------------------------------------------------

    def get_events_for_prompt(
        self, *, market: str, now: Optional[datetime] = None
    ) -> Dict[str, List[WorldEvent]]:
        """按类别取要注入提示词的事件。

        每个类别单独设上限，而不是设一个全局上限：冲突事件数量级最大，全局上限
        会让它把其它类别挤出提示词（设计 §9）。
        """
        now = now or datetime.now()
        limit = self.config.worldmonitor_event_prompt_limit
        results: Dict[str, List[WorldEvent]] = {}
        for category in CATEGORIES:
            try:
                results[category] = self.repo.list_events(
                    now=now,
                    categories=[category],
                    scopes=list(PROMPT_SCOPES),
                    markets=[market] if market else None,
                    lookback_days=self.config.worldmonitor_event_lookback_days,
                    limit=limit,
                )
            except Exception as exc:  # pragma: no cover - 取数失败不应打断复盘
                logger.warning(
                    "[WorldMonitor] prompt event query failed category=%s error=%s",
                    category,
                    exc,
                )
                results[category] = []
        return results

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _within_cooldown(self, now: datetime) -> bool:
        if self._last_sync_at is None:
            return False
        elapsed = (now - self._last_sync_at).total_seconds()
        return 0 <= elapsed < self.config.worldmonitor_sync_cooldown_seconds

    def _resolve_base_url(self) -> Optional[str]:
        base_url = self.config.worldmonitor_base_url.strip().rstrip("/")
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        return base_url

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.config.worldmonitor_connect_timeout_seconds,
            read=self.config.worldmonitor_read_timeout_seconds,
            write=self.config.worldmonitor_read_timeout_seconds,
            pool=self.config.worldmonitor_connect_timeout_seconds,
        )

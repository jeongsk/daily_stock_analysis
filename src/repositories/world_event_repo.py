# -*- coding: utf-8 -*-
"""World Monitor 归一化事件的存取层。

设计: docs/superpowers/specs/2026-07-29-worldmonitor-market-review-events-design.md

沿用 ``IntelligenceRepository`` 的会话使用方式，不新建平行实现。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from src.storage import DatabaseManager, WorldEvent, WorldEventSyncState

logger = logging.getLogger(__name__)

# 提示词与查询默认作用域：unmapped 表示"不知道发生地"，与 global 语义相反，
# 默认不进入提示词（设计 §8.1）。
PROMPT_SCOPES = ("market", "global")


class WorldEventRepository:
    """World Monitor 事件与同步状态的 DB 访问层。"""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def upsert_events(
        self,
        events: Iterable[Dict[str, Any]],
        *,
        collected_at: datetime,
    ) -> int:
        """写入或更新归一化事件，返回实际落库的条数（新增 + 更新）。

        重复同步以 ``(category, external_id)`` 去重。更新时刷新 ``ended_at`` /
        ``severity_rank`` 等会变化的字段，但 ``occurred_at`` 与 ``collected_at``
        保持首次观测值不变 —— 首次观测时刻是审计与回测重放的基准，不能被后续
        同步改写（设计 §4）。

        注意返回值把"更新"也算进去，与 ``IntelligenceRepository.upsert_items``
        只数新增不同。这里的计数会喂给 ``record_sync(event_count=...)``，回答的是
        "上游这次给出事件了吗"，而不是"有没有新事件"。若只数新增，一组稳定不变的
        事件在第二次同步后就会被判成"上游产不出事件"，从而错误地进入
        ``unverified``（设计 §7.1）。
        """
        stored = 0
        with self.db.get_session() as session:
            for payload in events:
                occurred_at = payload.get("occurred_at")
                if not isinstance(occurred_at, datetime):
                    continue
                # 发生时刻晚于采集时刻的记录直接丢弃：尚未发生的事件既会污染当天
                # 复盘，也会成为后续回测的未来信息泄漏源（设计 §4.1）。
                if occurred_at > collected_at:
                    logger.debug(
                        "[WorldEvent] skip future-dated event category=%s external_id=%s",
                        payload.get("category"),
                        payload.get("external_id"),
                    )
                    continue

                category = payload.get("category")
                external_id = payload.get("external_id")
                if not category or not external_id:
                    continue

                row = session.execute(
                    select(WorldEvent).where(
                        and_(
                            WorldEvent.category == category,
                            WorldEvent.external_id == external_id,
                        )
                    ).limit(1)
                ).scalar_one_or_none()

                if row is None:
                    # 与 IntelligenceRepository.upsert_items 同样的写法：select 与
                    # insert 之间存在竞态窗口，唯一约束冲突时只丢这一条，不能让
                    # 整批提交失败。
                    try:
                        with session.begin_nested():
                            session.add(
                                WorldEvent(
                                    category=category,
                                    source=payload.get("source") or "worldmonitor",
                                    source_endpoint=payload.get("source_endpoint"),
                                    external_id=external_id,
                                    title=payload.get("title") or "",
                                    summary=payload.get("summary"),
                                    url=payload.get("url"),
                                    occurred_at=occurred_at,
                                    ended_at=payload.get("ended_at"),
                                    collected_at=collected_at,
                                    countries=_encode_json_list(payload.get("countries")),
                                    markets=_encode_json_list(payload.get("markets")),
                                    scope=payload.get("scope") or "unmapped",
                                    severity_rank=int(payload.get("severity_rank") or 0),
                                    raw_payload=_encode_json(payload.get("raw_payload")),
                                )
                            )
                            session.flush()
                    except IntegrityError:
                        continue
                else:
                    row.title = payload.get("title") or row.title
                    row.summary = payload.get("summary")
                    row.url = payload.get("url")
                    row.ended_at = payload.get("ended_at")
                    row.countries = _encode_json_list(payload.get("countries"))
                    row.markets = _encode_json_list(payload.get("markets"))
                    row.scope = payload.get("scope") or row.scope
                    row.severity_rank = int(payload.get("severity_rank") or 0)
                    row.raw_payload = _encode_json(payload.get("raw_payload"))
                stored += 1
            session.commit()
        return stored

    def list_events(
        self,
        *,
        now: datetime,
        categories: Optional[Sequence[str]] = None,
        scopes: Optional[Sequence[str]] = None,
        markets: Optional[Sequence[str]] = None,
        lookback_days: int = 30,
        limit: Optional[int] = None,
    ) -> List[WorldEvent]:
        """按回溯窗口取事件，进行中的优先、其次严重度、再次发生时刻。

        上界 ``occurred_at <= now`` 是对 §4.1 的第二道防线：即使某行在写入之后
        因时钟回拨而变成"未来事件"，读取侧也不会把它送进提示词。
        """
        window_start = now - timedelta(days=max(1, lookback_days))
        conditions = [
            WorldEvent.occurred_at >= window_start,
            WorldEvent.occurred_at <= now,
        ]
        if categories:
            conditions.append(WorldEvent.category.in_(list(categories)))
        if scopes:
            conditions.append(WorldEvent.scope.in_(list(scopes)))

        if markets:
            # 市场过滤下推到 SQL。这里的谓词并不选择性强（global 事件按设计要进
            # 所有市场），所以留在 Python 侧过滤等于把整个回溯窗口的行都实例化 ——
            # 每行都带 raw_payload 大文本，而这段代码跑在复盘开始之前的内联路径上。
            # markets 存为 JSON 文本数组，用带引号的 LIKE 匹配整个元素，避免
            # 子串误命中（`"kr"` 不会匹配到别的市场码）。
            conditions.append(
                or_(
                    WorldEvent.scope == "global",
                    *[WorldEvent.markets.like(f'%"{market}"%') for market in markets],
                )
            )

        with self.db.get_session() as session:
            stmt = (
                select(WorldEvent)
                .where(and_(*conditions))
                .order_by(
                    # NULL ended_at（进行中）排前面
                    WorldEvent.ended_at.is_(None).desc(),
                    WorldEvent.severity_rank.desc(),
                    WorldEvent.occurred_at.desc(),
                )
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = list(session.execute(stmt).scalars().all())
            for row in rows:
                session.expunge(row)
            return rows

    def prune(self, *, now: datetime, retention_days: int) -> int:
        """删除超出保留期的事件，按发生时刻判定，返回删除条数。"""
        cutoff = now - timedelta(days=max(1, retention_days))
        with self.db.get_session() as session:
            result = session.execute(
                delete(WorldEvent).where(WorldEvent.occurred_at < cutoff)
            )
            session.commit()
            return int(result.rowcount or 0)

    def count_by_scope(self, scope: str) -> int:
        with self.db.get_session() as session:
            return int(
                session.execute(
                    select(func.count(WorldEvent.id)).where(WorldEvent.scope == scope)
                ).scalar_one()
                or 0
            )

    def count_events(
        self,
        *,
        now: datetime,
        category: Optional[str] = None,
        lookback_days: int = 30,
    ) -> int:
        """回溯窗口内的条数。诊断只要一个整数，不该把整窗口的行读进内存。"""
        window_start = now - timedelta(days=max(1, lookback_days))
        conditions = [
            WorldEvent.occurred_at >= window_start,
            WorldEvent.occurred_at <= now,
        ]
        if category:
            conditions.append(WorldEvent.category == category)
        with self.db.get_session() as session:
            return int(
                session.execute(
                    select(func.count(WorldEvent.id)).where(and_(*conditions))
                ).scalar_one()
                or 0
            )

    # ------------------------------------------------------------------
    # 同步状态
    # ------------------------------------------------------------------

    def record_sync(
        self,
        *,
        category: str,
        status: str,
        synced_at: datetime,
        event_count: int,
        error: Optional[str] = None,
        upstream_unavailable: bool = False,
    ) -> None:
        """记录一次同步结果。

        只有成功才推进 ``last_success_at``；只有成功且拿到非空结果才推进
        ``last_nonempty_at``。后者是 §7.1 判定 ``unverified`` 的唯一依据 ——
        没有它，"上游永远返回空数组"会被读成"确实没有事件"。

        ``last_attempt_at`` 无论成败都推进，供冷却判断使用；
        ``last_upstream_unavailable`` 单独保存，因为"最近成功过但现在上游挂了"
        用时间戳表达不出来。
        """
        with self.db.get_session() as session:
            row = session.execute(
                select(WorldEventSyncState)
                .where(WorldEventSyncState.category == category)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = WorldEventSyncState(category=category)
                session.add(row)

            row.last_status = status
            row.last_error = error
            row.last_attempt_at = synced_at
            row.last_upstream_unavailable = bool(upstream_unavailable)
            if status == "ok":
                row.last_success_at = synced_at
                if event_count > 0:
                    row.last_nonempty_at = synced_at
            session.commit()

    def get_last_attempt_at(self) -> Optional[datetime]:
        """所有类别里最近一次同步尝试的时刻，用于跨实例的冷却判断。"""
        with self.db.get_session() as session:
            values = session.execute(
                select(WorldEventSyncState.last_attempt_at)
            ).scalars().all()
            stamps = [v for v in values if isinstance(v, datetime)]
            return max(stamps) if stamps else None

    def get_sync_state(self, category: str) -> Optional[WorldEventSyncState]:
        with self.db.get_session() as session:
            row = session.execute(
                select(WorldEventSyncState)
                .where(WorldEventSyncState.category == category)
                .limit(1)
            ).scalar_one_or_none()
            if row is not None:
                session.expunge(row)
            return row


def _encode_json_list(value: Any) -> str:
    if not value:
        return "[]"
    if isinstance(value, (list, tuple, set)):
        return json.dumps([str(item) for item in value], ensure_ascii=False)
    return json.dumps([str(value)], ensure_ascii=False)


def _encode_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None

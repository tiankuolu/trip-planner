"""外部接口结果缓存（高德 POI / 天气 / 酒店）

为什么值得做：同一个城市同一种偏好的搜索会反复打高德，既慢又逼近配额；
一次运行 4 步里有 3 步是外部调用，缓存命中率是能拿数字说话的优化点。

缓存放数据库而不是进程内字典，是因为 M2 之后执行发生在 worker 进程里，
API 进程和 worker 进程要看到同一份缓存和同一份命中计数。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from sqlalchemy import delete, func, select

from ..clock import utcnow
from ..db import session_scope
from ..models.tables import CacheCounter, CacheEntry

DEFAULT_TTL_SECONDS = {
    "poi": 3600,     # 景点搜索结果，一小时
    "hotel": 1800,   # 酒店，半小时
    "weather": 1800,  # 天气，半小时（预报本来就是低频更新）
}


def _now() -> datetime:
    return utcnow()


def _bump(name: str, delta: int = 1) -> None:
    with session_scope() as session:
        counter = session.get(CacheCounter, name)
        if counter is None:
            session.add(CacheCounter(name=name, value=delta))
        else:
            counter.value += delta


def cache_get(kind: str, key: str) -> Optional[Dict[str, Any]]:
    """命中返回值，未命中返回 None（两者都会计数）"""
    with session_scope() as session:
        entry = session.get(CacheEntry, key)
        if entry is not None and entry.expires_at > _now():
            _bump("hits")
            try:
                return json.loads(entry.value_json)
            except json.JSONDecodeError:
                return None

    _bump("misses")
    return None


def cache_set(kind: str, key: str, value: Dict[str, Any], ttl_seconds: Optional[int] = None) -> None:
    ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_SECONDS.get(kind, 600)
    payload = json.dumps(value, ensure_ascii=False, default=str)
    with session_scope() as session:
        entry = session.get(CacheEntry, key)
        if entry is None:
            session.add(
                CacheEntry(
                    key=key,
                    kind=kind,
                    value_json=payload,
                    expires_at=_now() + timedelta(seconds=ttl),
                )
            )
        else:
            entry.kind = kind
            entry.value_json = payload
            entry.expires_at = _now() + timedelta(seconds=ttl)


def cached(
    kind: str,
    key: str,
    producer: Callable[[], Dict[str, Any]],
    ttl_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """读缓存 → 未命中就调 producer 并写回

    producer 抛异常时不写缓存（失败结果不能缓存，否则一次网络抖动就被固化）。
    """
    hit = cache_get(kind, key)
    if hit is not None:
        return hit
    value = producer()
    cache_set(kind, key, value, ttl_seconds)
    return value


def purge_expired() -> int:
    with session_scope() as session:
        result = session.execute(delete(CacheEntry).where(CacheEntry.expires_at <= _now()))
        return int(result.rowcount or 0)


def cache_stats() -> Dict[str, Any]:
    with session_scope() as session:
        counters = {
            row.name: int(row.value) for row in session.scalars(select(CacheCounter)).all()
        }
        entries = int(session.scalar(select(func.count()).select_from(CacheEntry)) or 0)
        alive = int(
            session.scalar(
                select(func.count()).select_from(CacheEntry).where(CacheEntry.expires_at > _now())
            )
            or 0
        )
    hits = counters.get("hits", 0)
    misses = counters.get("misses", 0)
    total = hits + misses
    return {
        "hits": hits,
        "misses": misses,
        "hit_ratio": round(hits / total, 4) if total else 0.0,
        "entries": entries,
        "entries_alive": alive,
    }


def reset_counters() -> None:
    with session_scope() as session:
        session.execute(delete(CacheCounter))

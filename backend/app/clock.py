"""统一时钟

全项目只用**朴素 UTC**（naive UTC）：
SQLite 的 DATETIME 不带时区，写进去再读出来就是 naive；
如果代码里混用 aware/naive，第一次比较就会 `can't compare offset-naive and offset-aware`。
所以入口就统一，不给自己留坑。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def utcnow() -> datetime:
    """朴素 UTC，用于所有入库时间字段"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value: Optional[datetime]) -> Optional[str]:
    """给 API 输出用；数据库里读出来的可能是 str（原生 SQL 路径）"""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value)
    return text or None


def minutes_ago(minutes: int) -> datetime:
    return utcnow() - timedelta(minutes=minutes)

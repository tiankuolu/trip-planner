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
    """给 API 输出用：**必须带时区标记**

    库里存的是朴素 UTC，如果直接 isoformat() 出去，浏览器会按本地时间解析，
    结果就是"已运行 28800 秒"这种 8 小时偏差。所以这里补一个 Z。
    原生 SQL 路径读回来的可能是字符串，那就原样返回。
    """
    if value is None:
        return None
    if not hasattr(value, "isoformat"):
        text = str(value)
        return text or None
    if getattr(value, "tzinfo", None) is None:
        return value.isoformat() + "Z"
    return value.isoformat()


def minutes_ago(minutes: int) -> datetime:
    return utcnow() - timedelta(minutes=minutes)

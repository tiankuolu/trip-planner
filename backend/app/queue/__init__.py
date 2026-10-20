"""队列工厂

优先级：REDIS_URL 存在就用 Redis（多机），否则用 SQLite（本机零依赖）。
两条路径实现同一个 `Queue` 接口，业务代码不感知。
"""

from __future__ import annotations

import os
from typing import Optional

from .base import DEAD, DONE, LEASED, READY, Job, Queue
from .sqlite_queue import SqliteQueue, backoff_seconds

__all__ = ["Queue", "Job", "READY", "LEASED", "DONE", "DEAD", "SqliteQueue", "get_queue", "reset_queue", "backoff_seconds"]

_queue: Optional[Queue] = None


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        redis_url = os.getenv("REDIS_URL", "").strip()
        if redis_url:
            from .redis_queue import RedisQueue

            _queue = RedisQueue(redis_url, max_attempts=int(os.getenv("QUEUE_MAX_ATTEMPTS", "3")))
        else:
            _queue = SqliteQueue(max_attempts=int(os.getenv("QUEUE_MAX_ATTEMPTS", "3")))
    return _queue


def reset_queue() -> None:
    global _queue
    _queue = None

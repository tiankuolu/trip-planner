"""SQLite 队列实现

核心是**一条 UPDATE ... RETURNING 完成"挑一个 + 租走它"**：
SQLite 3.35+ 支持 RETURNING，单语句天然原子，不需要自己拼 BEGIN IMMEDIATE，
也不会出现"两个 worker 同时领到同一个作业"。

租约（lease）不是 ack：worker 领走后作业被锁住 lease_seconds，
到期还没 ack 就说明进程没了，`reap_expired()` 会把它捡回来重试。
"收到消息"和"任务真的做完了"是两件事——这是队列最容易考的点。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..clock import iso, utcnow
from ..db import get_engine
from .base import DEAD, LEASED, READY, Job, Queue

BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0


def backoff_seconds(attempts: int) -> float:
    """指数退避：2s, 4s, 8s ... 封顶 60s"""
    return min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))


class SqliteQueue(Queue):
    def __init__(self, engine: Optional[Engine] = None, max_attempts: int = 3) -> None:
        self.engine = engine or get_engine()
        self.max_attempts = max_attempts
        if self.engine.dialect.name != "sqlite":
            raise RuntimeError(
                "SqliteQueue 只用于 SQLite；MySQL 部署请配置 REDIS_URL 走 RedisQueue"
            )

    # ------------------------------------------------------------------ 入队
    def enqueue(self, task_id: str, delay_seconds: float = 0.0) -> int:
        now = utcnow()
        available = now + timedelta(seconds=max(0.0, delay_seconds))
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO jobs (task_id, state, attempts, max_attempts,
                                      available_at, created_at, updated_at)
                    VALUES (:task_id, :state, 0, :max_attempts, :available_at, :now, :now)
                    RETURNING id
                    """
                ),
                {
                    "task_id": task_id,
                    "state": READY,
                    "max_attempts": self.max_attempts,
                    "available_at": available,
                    "now": now,
                },
            ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ 领取
    def reserve(self, worker_id: str, lease_seconds: int = 120) -> Optional[Job]:
        now = utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    UPDATE jobs
                       SET state = :leased,
                           worker_id = :worker_id,
                           lease_expires_at = :lease_until,
                           updated_at = :now
                     WHERE id = (
                           SELECT id FROM jobs
                            WHERE state = :ready AND available_at <= :now
                            ORDER BY available_at, id
                            LIMIT 1
                     )
                 RETURNING id, task_id, attempts, max_attempts
                    """
                ),
                {
                    "leased": LEASED,
                    "ready": READY,
                    "worker_id": worker_id,
                    "lease_until": lease_until,
                    "now": now,
                },
            ).fetchone()
        if row is None:
            return None
        return Job(
            id=int(row[0]),
            task_id=str(row[1]),
            attempts=int(row[2]),
            max_attempts=int(row[3]),
            worker_id=worker_id,
        )

    # ------------------------------------------------------------------ 收尾
    def ack(self, job_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE jobs SET state = :done, lease_expires_at = NULL,
                                    worker_id = NULL, updated_at = :now
                     WHERE id = :id
                    """
                ),
                {"done": "done", "now": utcnow(), "id": job_id},
            )

    def fail(self, job_id: int, error: str, retryable: bool = True) -> str:
        now = utcnow()
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT attempts, max_attempts FROM jobs WHERE id = :id"), {"id": job_id}
            ).fetchone()
            if row is None:
                return DEAD
            attempts, max_attempts = int(row[0]), int(row[1])
            next_attempt = attempts + 1
            exhausted = next_attempt >= max_attempts
            new_state = READY if (retryable and not exhausted) else DEAD
            delay = 0.0 if not retryable else backoff_seconds(next_attempt)

            conn.execute(
                text(
                    """
                    UPDATE jobs
                       SET state = :state,
                           attempts = :attempts,
                           available_at = :available_at,
                           lease_expires_at = NULL,
                           worker_id = NULL,
                           last_error = :error,
                           updated_at = :now
                     WHERE id = :id
                    """
                ),
                {
                    "state": new_state,
                    "attempts": next_attempt,
                    "available_at": now + timedelta(seconds=delay),
                    "error": (error or "")[:1000],
                    "now": now,
                    "id": job_id,
                },
            )
        return new_state

    # ------------------------------------------------------------------ 回收
    def reap_expired(self) -> int:
        """租约过期的作业：算一次失败尝试，重排或进死信"""
        now = utcnow()
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    UPDATE jobs
                       SET attempts = attempts + 1,
                           state = CASE WHEN attempts + 1 >= max_attempts THEN :dead ELSE :ready END,
                           available_at = :now,
                           lease_expires_at = NULL,
                           worker_id = NULL,
                           last_error = COALESCE(last_error, '租约过期：worker 可能已崩溃'),
                           updated_at = :now
                     WHERE state = :leased AND lease_expires_at IS NOT NULL
                       AND lease_expires_at < :now
                 RETURNING id
                    """
                ),
                {"dead": DEAD, "ready": READY, "leased": LEASED, "now": now},
            ).fetchall()
        return len(rows)

    # ------------------------------------------------------------------ 统计
    def stats(self) -> Dict[str, int]:
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT state, COUNT(*) FROM jobs GROUP BY state")).fetchall()
        counts = {"ready": 0, "leased": 0, "done": 0, "dead": 0}
        for state, count in rows:
            counts[str(state)] = int(count)
        return counts

    def dead_letters(self, limit: int = 20) -> list[Dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, task_id, attempts, last_error, updated_at
                      FROM jobs WHERE state = 'dead'
                      ORDER BY updated_at DESC LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).fetchall()
        return [
            {
                "job_id": int(r[0]),
                "task_id": str(r[1]),
                "attempts": int(r[2]),
                "last_error": r[3],
                "updated_at": iso(r[4]),
            }
            for r in rows
        ]

"""Redis 队列实现（与 SqliteQueue 同一套语义）

⚠️ 本机没有 Docker/Redis，这份实现**尚未在本机验证**（见 docs/jd-fit.md）。
保留它的理由是：SQLite 队列解决"本机能跑"，Redis 队列解决"多机部署"，
接口一致，换 broker 不改业务代码。等有 Docker 环境时按同一组用例回归即可。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .base import DEAD, DONE, LEASED, READY, Job, Queue
from .sqlite_queue import backoff_seconds

KEY_READY = "tripplanner:jobs:ready"  # zset: job_id -> available_at(score)
KEY_LEASED = "tripplanner:jobs:leased"  # zset: job_id -> lease_expires_at(score)
KEY_JOB = "tripplanner:job:{}"  # hash: job 元数据
KEY_DEAD = "tripplanner:jobs:dead"  # list


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


class RedisQueue(Queue):
    """基于 sorted set 的租约队列

    - ready / leased 各一个 zset，score 是时间戳，天然支持"到点才可见"和"租约过期"；
    - 领取用 ZPOPMIN 的原子性保证不会双领（配合 ZADD 归还）。
    """

    def __init__(self, url: str, max_attempts: int = 3, prefix: str = "") -> None:
        import redis  # 延迟导入：没装这个包时，SQLite 路径不受影响

        self.client = redis.Redis.from_url(url, decode_responses=True)
        self.max_attempts = max_attempts
        self.prefix = prefix

    # ------------------------------------------------------------------ 内部
    def _job_key(self, job_id: str) -> str:
        return KEY_JOB.format(job_id)

    def _create_job(self, task_id: str, available_at: float) -> str:
        job_id = uuid.uuid4().hex[:16]
        self.client.hset(
            self._job_key(job_id),
            mapping={
                "task_id": task_id,
                "attempts": 0,
                "max_attempts": self.max_attempts,
                "created_at": _now_ts(),
            },
        )
        self.client.zadd(KEY_READY, {job_id: available_at})
        return job_id

    # ------------------------------------------------------------------ 接口
    def enqueue(self, task_id: str, delay_seconds: float = 0.0) -> int:
        job_id = self._create_job(task_id, _now_ts() + max(0.0, delay_seconds))
        return int(job_id[:8], 16)

    def reserve(self, worker_id: str, lease_seconds: int = 120) -> Optional[Job]:
        now = _now_ts()
        while True:
            candidates = self.client.zrangebyscore(KEY_READY, "-inf", now, start=0, num=1)
            if not candidates:
                return None
            job_id = candidates[0]
            # ZREM 的返回值决定谁抢到：只有一个 worker 能拿到 1
            if self.client.zrem(KEY_READY, job_id) != 1:
                continue
            self.client.zadd(KEY_LEASED, {job_id: now + lease_seconds})
            meta = self.client.hgetall(self._job_key(job_id))
            if not meta:
                self.client.zrem(KEY_LEASED, job_id)
                continue
            return Job(
                id=int(job_id[:8], 16),
                task_id=str(meta.get("task_id", "")),
                attempts=int(meta.get("attempts", 0)),
                max_attempts=int(meta.get("max_attempts", self.max_attempts)),
                worker_id=worker_id,
            )

    def ack(self, job_id: int) -> None:
        job_id_hex = self._find_key(job_id)
        self.client.zrem(KEY_LEASED, job_id_hex)
        if job_id_hex:
            self.client.hset(self._job_key(job_id_hex), "state", DONE)

    def fail(self, job_id: int, error: str, retryable: bool = True) -> str:
        job_id_hex = self._find_key(job_id)
        if not job_id_hex:
            return DEAD
        attempts = int(self.client.hget(self._job_key(job_id_hex), "attempts") or 0) + 1
        max_attempts = int(
            self.client.hget(self._job_key(job_id_hex), "max_attempts") or self.max_attempts
        )
        self.client.hset(
            self._job_key(job_id_hex),
            mapping={"attempts": attempts, "last_error": (error or "")[:1000], "state": LEASED},
        )
        self.client.zrem(KEY_LEASED, job_id_hex)

        if retryable and attempts < max_attempts:
            self.client.zadd(
                KEY_READY, {job_id_hex: _now_ts() + backoff_seconds(attempts)}
            )
            self.client.hset(self._job_key(job_id_hex), "state", READY)
            return READY

        self.client.hset(self._job_key(job_id_hex), "state", DEAD)
        self.client.rpush(KEY_DEAD, job_id_hex)
        return DEAD

    def reap_expired(self) -> int:
        now = _now_ts()
        expired = self.client.zrangebyscore(KEY_LEASED, "-inf", now)
        for job_id in expired:
            self.client.zrem(KEY_LEASED, job_id)
            self.client.hincrby(self._job_key(job_id), "attempts", 1)
            attempts = int(self.client.hget(self._job_key(job_id), "attempts") or 1)
            max_attempts = int(self.client.hget(self._job_key(job_id), "max_attempts") or 3)
            if attempts >= max_attempts:
                self.client.hset(self._job_key(job_id), "state", DEAD)
                self.client.rpush(KEY_DEAD, job_id)
            else:
                self.client.hset(self._job_key(job_id), "state", READY)
                self.client.zadd(KEY_READY, {job_id: now})
        return len(expired)

    def stats(self) -> Dict[str, int]:
        return {
            READY: int(self.client.zcard(KEY_READY)),
            LEASED: int(self.client.zcard(KEY_LEASED)),
            DEAD: int(self.client.llen(KEY_DEAD)),
            DONE: 0,  # redis 版不保存已完成作业（交给数据库审计）
        }

    # ------------------------------------------------------------------ 工具
    def _find_key(self, job_id: int) -> Optional[str]:
        for job_id_hex in self.client.zrange(KEY_LEASED, 0, -1):
            if int(job_id_hex[:8], 16) == job_id:
                for ready in self.client.zrange(KEY_READY, 0, -1):
                    if ready == job_id_hex:
                        return job_id_hex
                return job_id_hex
        for ready in self.client.zrange(KEY_READY, 0, -1):
            if int(ready[:8], 16) == job_id:
                return ready
        return None

"""队列接口

为什么要抽象： broker 是选型，语义才是 JD 要看的东西——
入队、租约（可见性超时）、重试退避、死信、跨进程领取。
本机没有 Docker/Redis，所以默认实现是 SQLite；Redis 版本按同一接口另写一份。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

READY = "ready"
LEASED = "leased"
DONE = "done"
DEAD = "dead"


@dataclass
class Job:
    """一次"把某个任务跑掉"的作业"""

    id: int
    task_id: str
    attempts: int
    max_attempts: int
    worker_id: Optional[str] = None

    @property
    def is_final_attempt(self) -> bool:
        return self.attempts + 1 >= self.max_attempts


class Queue(ABC):
    """队列契约"""

    @abstractmethod
    def enqueue(self, task_id: str, delay_seconds: float = 0.0) -> int:
        """入队，返回 job id"""

    @abstractmethod
    def reserve(self, worker_id: str, lease_seconds: int = 120) -> Optional[Job]:
        """领取一个可执行作业；没有就返回 None"""

    @abstractmethod
    def ack(self, job_id: int) -> None:
        """标记完成"""

    @abstractmethod
    def fail(self, job_id: int, error: str, retryable: bool = True) -> str:
        """标记失败：可重试则退避重排，否则进死信。返回新状态"""

    @abstractmethod
    def reap_expired(self) -> int:
        """回收租约过期的作业（worker 崩了），返回处理条数"""

    @abstractmethod
    def stats(self) -> Dict[str, int]:
        """各状态计数"""

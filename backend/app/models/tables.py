"""ORM 模型：任务、任务步骤、任务事件

三张表回答三个问题：
- `tasks`      这次请求现在是什么状态？（状态机落在 status 上）
- `task_steps` 编排的每一步做了什么、花了多久、调了哪个工具？
- `task_events` 前端 SSE 要按 seq 增量补发，重连时用 Last-Event-ID 对齐。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Task(Base):
    """一次旅行生成任务"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("task"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="pending")

    city: Mapped[str] = mapped_column(String(64), nullable=False)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    end_date: Mapped[str] = mapped_column(String(10), nullable=False)
    travel_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)

    # 幂等键：同一 key 重复提交只产生一个任务
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, unique=True)

    plan_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    data_sources_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    error_code: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_usage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    queued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    steps: Mapped[list["TaskStep"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskStep.id"
    )
    events: Mapped[list["TaskEvent"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskEvent.seq"
    )

    __table_args__ = (Index("ix_tasks_status_created", "status", "created_at"),)


class TaskStep(Base):
    """编排中一步的执行记录（对应 trace 的一条）"""

    __tablename__ = "task_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="succeeded")
    tool: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    arguments_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    item_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mode: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[Task] = relationship(back_populates="steps")


class TaskEvent(Base):
    """任务事件流，SSE 按 seq 增量推送、断线用 Last-Event-ID 续传"""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[Task] = relationship(back_populates="events")

    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_task_events_task_seq"),)

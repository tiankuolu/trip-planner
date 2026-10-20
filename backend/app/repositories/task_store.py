"""仓储层：任务的状态机与读写

把"状态能不能跳"这件事集中在一个地方，路由和 worker 都不许直接改 status。
非法跃迁抛 `InvalidTransition`，而不是悄悄写进去——否则排查时状态历史就是一团浆糊。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.schemas import TripRequest
from ..models.tables import Task, TaskEvent, TaskStep, utcnow

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELED = "canceled"

TERMINAL = {SUCCEEDED, FAILED, CANCELED}

# 允许的状态跃迁：running 回 pending 是为了重试，failed 回 pending 是人工重跑
ALLOWED_TRANSITIONS: Dict[str, set[str]] = {
    PENDING: {RUNNING, CANCELED},
    RUNNING: {SUCCEEDED, FAILED, CANCELED, PENDING},
    FAILED: {PENDING},
    SUCCEEDED: set(),
    CANCELED: set(),
}


class InvalidTransition(RuntimeError):
    """非法状态跃迁"""


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def loads(raw: Optional[str], fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


# ------------------------------------------------------------------ 创建


def create_task(
    session: Session,
    request: TripRequest,
    payload: Dict[str, Any],
    idempotency_key: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Tuple[Task, bool]:
    """创建任务；命中幂等键时返回已有任务（第二个返回值 False 表示复用）"""
    if idempotency_key:
        existing = get_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            return existing, False

    task = Task(
        status=PENDING,
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        travel_days=request.travel_days,
        request_json=dumps(payload),
        idempotency_key=idempotency_key,
        request_id=request_id,
        queued_at=utcnow(),
    )
    session.add(task)
    try:
        session.flush()
    except IntegrityError:
        # 并发下两个请求同时命中同一幂等键：唯一约束兜底，回退到已有任务
        session.rollback()
        existing = get_by_idempotency_key(session, idempotency_key) if idempotency_key else None
        if existing is None:
            raise
        return existing, False

    append_event(session, task, "task.created", {"status": PENDING, "city": task.city})
    return task, True


def get_task(session: Session, task_id: str) -> Optional[Task]:
    return session.get(Task, task_id)


def get_by_idempotency_key(session: Session, key: str) -> Optional[Task]:
    return session.scalar(select(Task).where(Task.idempotency_key == key))


def list_tasks(
    session: Session, status: Optional[str] = None, limit: int = 20
) -> Sequence[Task]:
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Task.status == status)
    return session.scalars(stmt).all()


# ------------------------------------------------------------------ 状态


def transition(
    session: Session,
    task: Task,
    target: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> Task:
    """按状态机跃迁；非法跃迁直接抛错"""
    current = task.status
    if current == target:
        return task
    if not can_transition(current, target):
        raise InvalidTransition(f"非法状态跃迁: {current} -> {target}")

    task.status = target
    if target == RUNNING:
        task.started_at = utcnow()
        task.attempt_count += 1
    elif target in TERMINAL:
        task.ended_at = utcnow()
    elif target == PENDING:
        task.queued_at = utcnow()

    if error_code:
        task.error_code = error_code
    if error_message:
        task.error_message = error_message[:2000]

    append_event(
        session,
        task,
        f"task.{target}",
        {
            "from": current,
            "to": target,
            "attempt": task.attempt_count,
            "error": error_message[:300] if error_message else None,
        },
    )
    return task


def request_cancel(session: Session, task: Task) -> Task:
    """协作式取消：标记 + 立即置终态（worker 在下个检查点看到标记会退出）"""
    if task.status in TERMINAL:
        return task
    task.cancel_requested = True
    append_event(session, task, "task.cancel_requested", {"status": task.status})
    return transition(session, task, CANCELED, error_code="canceled_by_user", error_message="用户取消")


def is_cancel_requested(session: Session, task_id: str) -> bool:
    value = session.scalar(select(Task.cancel_requested).where(Task.id == task_id))
    return bool(value)


# ------------------------------------------------------------------ 步骤


def append_step(session: Session, task: Task, step: Dict[str, Any]) -> TaskStep:
    record = TaskStep(
        task_id=task.id,
        step=str(step.get("step", "unknown")),
        status="succeeded" if step.get("ok", True) else "failed",
        tool=step.get("tool"),
        arguments_json=dumps(step.get("arguments")) if step.get("arguments") else None,
        item_count=step.get("item_count"),
        latency_ms=int(step.get("latency_ms") or 0),
        mode=step.get("mode"),
        detail=(step.get("detail") or "")[:2000] or None,
    )
    session.add(record)
    session.flush()
    append_event(
        session,
        task,
        "task.step",
        {
            "step": record.step,
            "status": record.status,
            "tool": record.tool,
            "item_count": record.item_count,
            "latency_ms": record.latency_ms,
        },
    )
    return record


# ------------------------------------------------------------------ 事件


def next_seq(session: Session, task_id: str) -> int:
    current = session.scalar(
        select(func.coalesce(func.max(TaskEvent.seq), 0)).where(TaskEvent.task_id == task_id)
    )
    return int(current or 0) + 1


def append_event(
    session: Session, task: Task, event_type: str, payload: Dict[str, Any]
) -> TaskEvent:
    event = TaskEvent(
        task_id=task.id,
        seq=next_seq(session, task.id),
        type=event_type,
        payload_json=dumps(payload),
    )
    session.add(event)
    session.flush()
    return event


def list_events(session: Session, task_id: str, after_seq: int = 0) -> List[TaskEvent]:
    stmt = (
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id, TaskEvent.seq > after_seq)
        .order_by(TaskEvent.seq)
    )
    return list(session.scalars(stmt).all())


# ------------------------------------------------------------------ 序列化


def serialize(task: Task) -> Dict[str, Any]:
    """给 HTTP 层用的任务视图"""
    payload: Dict[str, Any] = {
        "task_id": task.id,
        "status": task.status,
        "city": task.city,
        "start_date": task.start_date,
        "end_date": task.end_date,
        "travel_days": task.travel_days,
        "degraded": task.degraded,
        "warnings": loads(task.warnings_json, []),
        "data_sources": loads(task.data_sources_json, {}),
        "error": (
            {"code": task.error_code, "message": task.error_message}
            if task.error_code or task.error_message
            else None
        ),
        "attempt_count": task.attempt_count,
        "elapsed_ms": task.elapsed_ms,
        "token_usage": task.token_usage,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "ended_at": task.ended_at.isoformat() if task.ended_at else None,
        "steps": [
            {
                "step": s.step,
                "status": s.status,
                "tool": s.tool,
                "arguments": loads(s.arguments_json, None),
                "item_count": s.item_count,
                "latency_ms": s.latency_ms,
                "mode": s.mode,
                "detail": s.detail,
            }
            for s in (task.steps or [])
        ],
    }
    plan = loads(task.plan_json, None)
    if plan is not None:
        payload["plan"] = plan
    return payload

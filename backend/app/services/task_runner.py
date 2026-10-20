"""任务执行器

一个 `execute(task_id)` 就是"把库里这个任务跑完"：加载请求 → 置 running → 跑编排
→ 每步落库并推事件 → 置 succeeded / failed / canceled。

为什么按 task_id 重新加载而不是传对象进来：M2 之后执行发生在另一个进程（worker）里，
唯一能共享的就是数据库。现在按 id 加载，换成 worker 时一行不改。
"""

from __future__ import annotations

import time
import traceback
from typing import Callable, Optional

from ..agents.trip_planner_agent import (
    MultiAgentTripPlanner,
    PlanGenerationError,
    TaskCanceled,
    get_trip_planner_agent,
)
from ..db import session_scope
from ..models.schemas import TripRequest
from ..repositories import task_store as store


class TaskRunner:
    """执行器：不持有任务状态，状态只在库里"""

    def __init__(
        self,
        planner_factory: Callable[[], MultiAgentTripPlanner] = get_trip_planner_agent,
    ) -> None:
        self.planner_factory = planner_factory

    # ------------------------------------------------------------------ 入口
    def execute(self, task_id: str) -> None:
        payload, should_run = self._claim(task_id)
        if not should_run:
            return

        try:
            request = TripRequest(**payload)
        except Exception as exc:  # noqa: BLE001 - 参数坏了就是坏请求
            self._fail(task_id, "invalid_request", f"请求参数非法: {exc}")
            return

        started = time.perf_counter()
        try:
            planner = self.planner_factory()
            result = planner.plan_trip(
                request,
                on_step=lambda entry: self._record_step(task_id, entry),
                should_cancel=lambda: self._cancel_requested(task_id),
            )
        except TaskCanceled:
            self._mark(task_id, store.CANCELED, error_code="canceled_by_user", message="任务已被用户取消")
            return
        except PlanGenerationError as exc:
            self._fail(task_id, "plan_generation_failed", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 未知异常也要落库，不能只打日志
            traceback.print_exc()
            self._fail(task_id, "internal_error", str(exc))
            return

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._succeed(task_id, result, elapsed_ms)

    # ------------------------------------------------------------------ 阶段
    def _claim(self, task_id: str):
        """抢占任务：置 running 并返回请求体；不该跑就返回 (None, False)"""
        with session_scope() as session:
            task = store.get_task(session, task_id)
            if task is None:
                return None, False
            if task.status in store.TERMINAL:
                return None, False
            if task.cancel_requested:
                store.transition(session, task, store.CANCELED, error_code="canceled_by_user")
                return None, False
            store.transition(session, task, store.RUNNING)
            return store.loads(task.request_json, {}), True

    def _record_step(self, task_id: str, entry: dict) -> None:
        with session_scope() as session:
            task = store.get_task(session, task_id)
            if task is None:
                return
            store.append_step(session, task, entry)

    def _cancel_requested(self, task_id: str) -> bool:
        with session_scope() as session:
            return store.is_cancel_requested(session, task_id)

    def _mark(self, task_id: str, status: str, error_code: str = "", message: str = "") -> None:
        with session_scope() as session:
            task = store.get_task(session, task_id)
            if task is None or task.status in store.TERMINAL:
                return
            store.transition(
                session,
                task,
                status,
                error_code=error_code or None,
                error_message=message or None,
            )

    def _fail(self, task_id: str, code: str, message: str) -> None:
        self._mark(task_id, store.FAILED, error_code=code, message=message)

    def _succeed(self, task_id: str, result, elapsed_ms: int) -> None:
        with session_scope() as session:
            task = store.get_task(session, task_id)
            if task is None:
                return

            # 用户在执行过程中点了取消：结果仍保留，但状态尊重取消
            if task.cancel_requested:
                if task.status not in store.TERMINAL:
                    store.transition(
                        session,
                        task,
                        store.CANCELED,
                        error_code="canceled_by_user",
                        error_message="用户在执行过程中取消",
                    )
                return

            if task.status in store.TERMINAL:
                return

            task.plan_json = store.dumps(result.plan.model_dump())
            task.warnings_json = store.dumps(result.warnings)
            task.data_sources_json = store.dumps(result.sources)
            task.degraded = bool(result.degraded)
            task.elapsed_ms = elapsed_ms
            task.token_usage = int(getattr(result, "token_usage", 0) or 0)
            store.transition(session, task, store.SUCCEEDED)


_runner: Optional[TaskRunner] = None


def get_task_runner() -> TaskRunner:
    global _runner
    if _runner is None:
        _runner = TaskRunner()
    return _runner


def reset_task_runner() -> None:
    global _runner
    _runner = None

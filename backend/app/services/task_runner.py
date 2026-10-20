"""任务执行器

一个 `execute(task_id)` 就是"把库里这个任务跑完"：加载请求 → 置 running → 跑编排
→ 每步落库并推事件 → 置 succeeded / failed / canceled。

为什么按 task_id 重新加载而不是传对象进来：M2 之后执行发生在另一个进程（worker）里，
唯一能共享的就是数据库。现在按 id 加载，换成 worker 时一行不改。
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from ..agents.trip_planner_agent import (
    MultiAgentTripPlanner,
    PlanGenerationError,
    TaskCanceled,
    get_trip_planner_agent,
)
from ..db import session_scope
from ..models.schemas import TripRequest
from ..observability import bind_task, log
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
        payload, should_run, request_id = self._claim(task_id)
        if not should_run:
            return

        # request_id 从库里读回来：这样后台线程/独立 worker 的日志和最初那次 HTTP 请求是同一个 id
        with bind_task(task_id, request_id):
            self._run(task_id, payload)

    def _run(self, task_id: str, payload: dict) -> None:
        try:
            request = TripRequest(**payload)
        except Exception as exc:  # noqa: BLE001 - 参数坏了就是坏请求
            self._fail(task_id, "invalid_request", f"请求参数非法: {exc}")
            return

        log.info("task.started", city=request.city, days=request.travel_days)
        started = time.perf_counter()
        try:
            planner = self.planner_factory()
            result = planner.plan_trip(
                request,
                on_step=lambda entry: self._record_step(task_id, entry),
                should_cancel=lambda: self._cancel_requested(task_id),
            )
        except TaskCanceled:
            log.info("task.canceled")
            self._mark(task_id, store.CANCELED, error_code="canceled_by_user", message="任务已被用户取消")
            return
        except PlanGenerationError as exc:
            log.error("task.failed", code="plan_generation_failed", error=str(exc))
            self._fail(task_id, "plan_generation_failed", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 未知异常也要落库，不能只打日志
            log.exception("task.crashed")
            self._fail(task_id, "internal_error", str(exc))
            return

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._succeed(task_id, result, elapsed_ms)
        log.info(
            "task.succeeded",
            elapsed_ms=elapsed_ms,
            tokens=int(getattr(result, "token_usage", 0) or 0),
            degraded=bool(result.degraded),
            warnings=len(result.warnings),
        )

    # ------------------------------------------------------------------ 阶段
    def _claim(self, task_id: str):
        """抢占任务：置 running 并返回 (请求体, 是否执行, request_id)"""
        with session_scope() as session:
            task = store.get_task(session, task_id)
            if task is None:
                return None, False, None
            if task.status in store.TERMINAL:
                return None, False, None
            if task.cancel_requested:
                store.transition(session, task, store.CANCELED, error_code="canceled_by_user")
                return None, False, None

            if task.status == store.RUNNING:
                # worker 崩溃后重新领到同一个任务：这是"恢复"，不是首次执行
                task.attempt_count += 1
                store.append_event(
                    session, task, "task.resumed", {"attempt": task.attempt_count}
                )
                log.warning("task.resumed", attempt=task.attempt_count)
            else:
                store.transition(session, task, store.RUNNING)
            return store.loads(task.request_json, {}), True, task.request_id

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

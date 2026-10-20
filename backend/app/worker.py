"""worker 进程：从队列领作业，跑完 ack，失败按重试策略重排

独立进程的意义（也是 JD「任务调度」真正的考点）：
API 进程只负责收请求和查状态，生成这种 20 秒级的重活交给 worker，
worker 崩了不会影响接口，任务也不会丢——它还在库里，租约到期会被重新领取。

用法：
    python -m app.worker              # 常驻
    python -m app.worker --once       # 只处理一轮（脚本/测试用）
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Optional

from . import config  # noqa: F401  触发 load_dotenv()
from .db import create_all, session_scope
from .models.tables import Task
from .queue import Job, get_queue
from .repositories import task_store as store
from .services.task_runner import TaskRunner

# 这些错误值得重试（LLM 超时、上游 5xx），参数错误重试没有意义
RETRYABLE_CODES = {"plan_generation_failed", "internal_error"}
TERMINAL = {store.SUCCEEDED, store.FAILED, store.CANCELED}


def _task_state(task_id: str) -> tuple[Optional[str], Optional[str]]:
    with session_scope() as session:
        task: Optional[Task] = store.get_task(session, task_id)
        if task is None:
            return None, None
        return task.status, task.error_code


def settle(job: Job, runner: TaskRunner) -> str:
    """跑一个作业并决定 ack 还是重排"""
    runner.execute(job.task_id)
    status, error_code = _task_state(job.task_id)

    if status is None:
        get_queue().ack(job.id)
        return "acked:任务已不存在"

    if status == store.FAILED and error_code in RETRYABLE_CODES:
        next_state = get_queue().fail(job.id, f"task failed: {error_code}", retryable=True)
        return f"{next_state}:任务失败({error_code})，已按退避重排"

    get_queue().ack(job.id)
    return f"acked:{status}"


def run_once(worker_id: Optional[str] = None) -> bool:
    """处理一个作业；没有可做的返回 False"""
    queue = get_queue()
    worker_id = worker_id or f"worker-{os.getpid()}"
    job = queue.reserve(worker_id, lease_seconds=int(os.getenv("WORKER_LEASE_SECONDS", "120")))
    if job is None:
        return False

    runner = TaskRunner()
    result = settle(job, runner)
    print(f"🧹 {worker_id} job={job.id} task={job.task_id} -> {result}", flush=True)
    return True


def serve(poll_interval: float = 0.5) -> None:
    queue = get_queue()
    worker_id = f"worker-{os.getpid()}"
    print(f"👷 {worker_id} 启动，队列: {type(queue).__name__}", flush=True)

    stopping = False

    def _stop(signum, _frame):  # noqa: ANN001
        nonlocal stopping
        print(f"\n收到信号 {signum}，处理完当前作业后退出...", flush=True)
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    idle_rounds = 0
    while not stopping:
        reaped = queue.reap_expired()
        if reaped:
            print(f"♻️  回收了 {reaped} 个租约过期的作业", flush=True)

        if run_once(worker_id):
            idle_rounds = 0
            continue

        idle_rounds += 1
        time.sleep(poll_interval)

    print(f"👋 {worker_id} 已退出", flush=True)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="旅行规划任务 worker")
    parser.add_argument("--once", action="store_true", help="只处理一轮就退出")
    args = parser.parse_args(argv)

    create_all()  # 首次启动自动建表（含 jobs）

    if args.once:
        did_work = run_once()
        print("没有可处理的作业" if not did_work else "处理了一个作业", flush=True)
        sys.exit(0 if did_work else 2)

    serve()


if __name__ == "__main__":
    main()

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
from .observability import bind_task, configure_logging, log
from .queue import Job, get_queue
from .repositories import task_store as store
from .services.task_runner import TaskRunner

# 这些错误值得重试（LLM 超时、上游 5xx），参数错误重试没有意义
RETRYABLE_CODES = {"plan_generation_failed", "internal_error"}
TERMINAL = {store.SUCCEEDED, store.FAILED, store.CANCELED}


def _task_state(task_id: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    with session_scope() as session:
        task: Optional[Task] = store.get_task(session, task_id)
        if task is None:
            return None, None, None
        return task.status, task.error_code, task.request_id


def settle(job: Job, runner: TaskRunner) -> str:
    """跑一个作业并决定 ack 还是重排"""
    runner.execute(job.task_id)
    status, error_code, request_id = _task_state(job.task_id)

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

    with bind_task(job.task_id):
        runner = TaskRunner()
        result = settle(job, runner)
        log.info("job.settled", worker=worker_id, job_id=job.id, result=result)
    return True


def serve(poll_interval: float = 0.5) -> None:
    queue = get_queue()
    worker_id = f"worker-{os.getpid()}"
    log.info("worker.started", worker=worker_id, queue=type(queue).__name__)

    stopping = False

    def _stop(signum, _frame):  # noqa: ANN001
        nonlocal stopping
        log.info("worker.stop_signal", signal=signum)
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    while not stopping:
        reaped = queue.reap_expired()
        if reaped:
            log.info("queue.reaped", count=reaped)

        if run_once(worker_id):
            continue

        time.sleep(poll_interval)

    log.info("worker.exited", worker=worker_id)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="旅行规划任务 worker")
    parser.add_argument("--once", action="store_true", help="只处理一轮就退出")
    args = parser.parse_args(argv)

    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    create_all()  # 首次启动自动建表（含 jobs）

    if args.once:
        did_work = run_once()
        log.info("worker.once", did_work=did_work)
        sys.exit(0 if did_work else 2)

    serve()


if __name__ == "__main__":
    main()

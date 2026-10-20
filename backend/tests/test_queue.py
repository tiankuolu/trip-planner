"""SQLite 队列测试

对应验收矩阵 M2「队列」一行，重点是四件事：
- 领取是原子的，同一个作业不会被两个 worker 同时领走；
- 租约过期（worker 崩了）能回收，且算一次失败尝试；
- 失败按指数退避重排，不是立刻重试打爆上游；
- 重试次数耗尽进死信，不会被无声吞掉。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.queue.sqlite_queue import SqliteQueue, backoff_seconds


def _make_task(session, trip_request, suffix: str) -> str:
    from app.models.schemas import TripRequest
    from app.repositories import task_store as store

    request = TripRequest(**{**trip_request.model_dump(), "city": f"城市{suffix}"})
    task, _ = store.create_task(session, request, request.model_dump())
    session.commit()
    return task.id


def test_enqueue_then_reserve_then_ack(queue, session, trip_request):
    task_id = _make_task(session, trip_request, "A")

    queue.enqueue(task_id)
    assert queue.stats()["ready"] == 1

    job = queue.reserve("w-1", lease_seconds=60)
    assert job is not None
    assert job.task_id == task_id
    assert queue.stats()["leased"] == 1
    assert queue.stats()["ready"] == 0

    queue.ack(job.id)
    assert queue.stats()["done"] == 1


def test_reserve_is_atomic_one_job_per_worker(queue, session, trip_request):
    """只有一份作业时，两个 worker 竞争只会有一个人拿到"""
    task_id = _make_task(session, trip_request, "B1")
    queue.enqueue(task_id)

    first = queue.reserve("w-1", lease_seconds=60)
    second = queue.reserve("w-2", lease_seconds=60)

    assert first is not None
    assert second is None


def test_delayed_job_is_not_visible_immediately(queue, session, trip_request):
    task_id = _make_task(session, trip_request, "C")

    queue.enqueue(task_id, delay_seconds=30)

    assert queue.reserve("w-1") is None


def test_lease_expiry_requeues_job_and_counts_attempt(queue, session, trip_request):
    task_id = _make_task(session, trip_request, "D")
    queue.enqueue(task_id)

    job = queue.reserve("w-1", lease_seconds=-5)  # 直接给一个已过期的租约，模拟 worker 崩了
    assert job is not None
    assert queue.stats()["leased"] == 1

    reaped = queue.reap_expired()

    assert reaped == 1
    assert queue.stats()["ready"] == 1

    again = queue.reserve("w-2", lease_seconds=60)
    assert again is not None
    assert again.attempts == 1  # 被回收算一次尝试


def test_failure_applies_backoff_before_retry(queue, session, trip_request):
    task_id = _make_task(session, trip_request, "E")
    queue.enqueue(task_id)
    job = queue.reserve("w-1")

    state = queue.fail(job.id, "上游超时", retryable=True)

    assert state == "ready"
    # 退避期间不可见
    assert queue.reserve("w-2") is None
    # 把 available_at 拨回现在，模拟退避结束
    _force_available_now(queue, job.id)
    assert queue.reserve("w-2") is not None


def test_retries_exhausted_go_to_dead_letter(queue, session, trip_request):
    task_id = _make_task(session, trip_request, "F")
    queue.enqueue(task_id)

    for _ in range(3):
        job = queue.reserve("w-1")
        assert job is not None
        state = queue.fail(job.id, "一直失败", retryable=True)
        _force_available_now(queue, job.id)

    assert state == "dead"
    assert queue.stats()["dead"] == 1

    dead = queue.dead_letters()
    assert dead[0]["task_id"] == task_id
    assert dead[0]["attempts"] == 3
    assert "一直失败" in dead[0]["last_error"]


def test_non_retryable_failure_goes_straight_to_dead(queue, session, trip_request):
    task_id = _make_task(session, trip_request, "G")
    queue.enqueue(task_id)
    job = queue.reserve("w-1")

    state = queue.fail(job.id, "参数非法", retryable=False)

    assert state == "dead"
    assert queue.stats()["dead"] == 1


def test_backoff_is_exponential_and_capped():
    assert backoff_seconds(1) == 2.0
    assert backoff_seconds(2) == 4.0
    assert backoff_seconds(3) == 8.0
    assert backoff_seconds(10) == 60.0


def _force_available_now(queue: SqliteQueue, job_id: int) -> None:
    from sqlalchemy import text

    with queue.engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET available_at = :now WHERE id = :id"),
            {"now": datetime.now(timezone.utc) - timedelta(seconds=1), "id": job_id},
        )

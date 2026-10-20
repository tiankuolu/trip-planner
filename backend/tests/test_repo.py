"""任务仓储与状态机测试

验收矩阵里这几行的证据就是这些用例：
- 状态维护：state_machine 相关
- 幂等：duplicate_submit_creates_one_task
- 进度推送的数据基础：events 的 seq 递增与 after_seq 续传
"""

import pytest

from app.repositories import task_store as store


def _payload(request):
    return request.model_dump()


def test_create_task_is_pending_with_created_event(session, trip_request):
    task, created = store.create_task(session, trip_request, _payload(trip_request))

    assert created is True
    assert task.status == store.PENDING
    assert task.city == "杭州"

    events = store.list_events(session, task.id)
    assert [e.type for e in events] == ["task.created"]
    assert events[0].seq == 1


def test_duplicate_submit_with_same_key_creates_one_task(session, trip_request):
    first, created_first = store.create_task(
        session, trip_request, _payload(trip_request), idempotency_key="k-1"
    )
    second, created_second = store.create_task(
        session, trip_request, _payload(trip_request), idempotency_key="k-1"
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert len(store.list_tasks(session)) == 1


def test_transition_pending_to_running_sets_timestamps(session, trip_request):
    task, _ = store.create_task(session, trip_request, _payload(trip_request))

    store.transition(session, task, store.RUNNING)

    assert task.status == store.RUNNING
    assert task.started_at is not None
    assert task.attempt_count == 1
    assert [e.type for e in store.list_events(session, task.id)] == [
        "task.created",
        "task.running",
    ]


def test_state_machine_rejects_invalid_transition(session, trip_request):
    task, _ = store.create_task(session, trip_request, _payload(trip_request))

    with pytest.raises(store.InvalidTransition):
        store.transition(session, task, store.SUCCEEDED)

    assert task.status == store.PENDING


def test_terminal_states_are_final(session, trip_request):
    task, _ = store.create_task(session, trip_request, _payload(trip_request))
    store.transition(session, task, store.RUNNING)
    store.transition(session, task, store.SUCCEEDED)

    for target in (store.RUNNING, store.FAILED, store.CANCELED, store.PENDING):
        with pytest.raises(store.InvalidTransition):
            store.transition(session, task, target)


def test_cancel_pending_task(session, trip_request):
    task, _ = store.create_task(session, trip_request, _payload(trip_request))

    store.request_cancel(session, task)

    assert task.status == store.CANCELED
    assert task.cancel_requested is True
    assert store.is_cancel_requested(session, task.id) is True
    assert task.error_code == "canceled_by_user"


def test_cancel_after_success_is_noop(session, trip_request):
    task, _ = store.create_task(session, trip_request, _payload(trip_request))
    store.transition(session, task, store.RUNNING)
    store.transition(session, task, store.SUCCEEDED)

    store.request_cancel(session, task)

    assert task.status == store.SUCCEEDED


def test_append_step_increments_event_seq(session, trip_request):
    task, _ = store.create_task(session, trip_request, _payload(trip_request))

    store.append_step(
        session,
        task,
        {
            "step": "景点搜索",
            "ok": True,
            "tool": "amap_search_poi",
            "arguments": {"keywords": "博物馆", "city": "杭州"},
            "item_count": 10,
            "latency_ms": 8030,
            "mode": "function_call",
        },
    )
    store.append_step(session, task, {"step": "天气查询", "ok": False, "detail": "超时"})

    assert [s.status for s in task.steps] == ["succeeded", "failed"]
    seqs = [e.seq for e in store.list_events(session, task.id)]
    assert seqs == [1, 2, 3]


def test_list_events_after_seq_supports_last_event_id(session, trip_request):
    task, _ = store.create_task(session, trip_request, _payload(trip_request))
    store.transition(session, task, store.RUNNING)
    store.transition(session, task, store.SUCCEEDED)

    all_events = store.list_events(session, task.id)
    tail = store.list_events(session, task.id, after_seq=2)

    assert len(all_events) == 3
    assert [e.seq for e in tail] == [3]


def test_serialize_includes_steps_and_plan(session, trip_request):
    task, _ = store.create_task(session, trip_request, _payload(trip_request))
    store.append_step(session, task, {"step": "景点搜索", "item_count": 3})
    task.plan_json = store.dumps({"city": "杭州", "days": []})
    session.flush()

    data = store.serialize(task)

    assert data["task_id"] == task.id
    assert data["status"] == store.PENDING
    assert data["steps"][0]["step"] == "景点搜索"
    assert data["plan"] == {"city": "杭州", "days": []}


def test_serialized_timestamps_carry_timezone(session, trip_request):
    """库里存朴素 UTC，出 API 必须带 Z，否则前端会按本地时间解析（差 8 小时）"""
    from datetime import datetime

    task, _ = store.create_task(session, trip_request, _payload(trip_request))
    store.transition(session, task, store.RUNNING)
    session.flush()

    data = store.serialize(task)

    assert data["created_at"].endswith("Z")
    assert data["started_at"].endswith("Z")
    parsed = datetime.fromisoformat(data["started_at"])
    assert parsed.tzinfo is not None

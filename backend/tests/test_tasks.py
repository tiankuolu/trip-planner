"""任务化接口测试

覆盖验收矩阵里这几行：
- 任务调度：202 + task_id + 状态查询
- 状态维护：状态机驱动，steps 落库
- 幂等：同 key 只产生一个任务
- 取消：pending 直接取消；running 只打标记，由检查点退出
- 进度推送：SSE 事件序列 + done 收尾

全部用假 planner，不联网、不花钱。
"""

from __future__ import annotations

import pytest

from app.agents.trip_planner_agent import PlanGenerationError, PlanResult, TaskCanceled
from app.models.schemas import DayPlan, TripPlan
from app.repositories import task_store as store
from app.services.task_runner import TaskRunner


class FakePlanner:
    """可控的假编排器：立刻返回，或按需失败/自取消"""

    def __init__(self, *, steps: int = 2, fail: bool = False, cancel_via=None) -> None:
        self.steps = steps
        self.fail = fail
        self.cancel_via = cancel_via

    def plan_trip(self, request, on_step=None, should_cancel=None):
        for index in range(self.steps):
            if should_cancel is not None and should_cancel():
                raise TaskCanceled("用户取消")
            if on_step is not None:
                on_step(
                    {
                        "step": f"步骤{index}",
                        "ok": True,
                        "tool": "amap_search_poi",
                        "arguments": {"keywords": "博物馆", "city": request.city},
                        "item_count": 3,
                        "latency_ms": 5,
                        "mode": "function_call",
                        "detail": "fake",
                    }
                )
            if self.cancel_via is not None:
                self.cancel_via()
                self.cancel_via = None

        if self.fail:
            raise PlanGenerationError("模型抽风了")

        plan = TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=[DayPlan(date=request.start_date, day_index=0, description="假行程")],
            overall_suggestions="假的",
        )
        return PlanResult(
            plan=plan,
            warnings=[],
            trace=[],
            sources={"attractions": 3},
            elapsed_ms=10,
            token_usage=42,
        )


@pytest.fixture()
def client(db_url, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.main import app
    from app.db import create_all

    create_all()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def use_planner(monkeypatch):
    """把路由里的 runner 换成注入假 planner 的 runner"""

    def _apply(planner):
        import app.api.routes.trip as trip_routes

        runner = TaskRunner(planner_factory=lambda: planner)
        monkeypatch.setattr(trip_routes, "get_task_runner", lambda: runner)
        return runner

    return _apply


BODY = {
    "city": "杭州",
    "start_date": "2026-10-21",
    "end_date": "2026-10-23",
    "travel_days": 3,
    "transportation": "公共交通",
    "accommodation": "经济型酒店",
    "preferences": ["历史文化"],
}


# ------------------------------------------------------------------ 提交


def test_submit_returns_202_with_task_id(client, use_planner):
    use_planner(FakePlanner())

    response = client.post("/api/trip/plan", json=BODY)

    assert response.status_code == 202
    payload = response.json()
    assert payload["task_id"].startswith("task_")
    assert payload["status"] in {"pending", store.SUCCEEDED}
    assert payload["status_url"].endswith(payload["task_id"])
    assert payload["events_url"].endswith("/events")
    assert payload["deduplicated"] is False


def test_task_succeeds_and_records_steps(client, use_planner):
    use_planner(FakePlanner(steps=3))
    task_id = client.post("/api/trip/plan", json=BODY).json()["task_id"]

    detail = client.get(f"/api/trip/tasks/{task_id}").json()

    assert detail["status"] == store.SUCCEEDED
    assert [s["step"] for s in detail["steps"]] == ["步骤0", "步骤1", "步骤2"]
    assert detail["steps"][0]["tool"] == "amap_search_poi"
    assert detail["plan"]["city"] == "杭州"
    assert detail["token_usage"] == 42
    assert detail["ended_at"] is not None


def test_duplicate_submit_with_same_key_creates_one_task(client, use_planner):
    use_planner(FakePlanner())
    headers = {"Idempotency-Key": "flow-abc"}

    first = client.post("/api/trip/plan", json=BODY, headers=headers).json()
    second = client.post("/api/trip/plan", json=BODY, headers=headers).json()

    assert first["task_id"] == second["task_id"]
    assert second["deduplicated"] is True
    assert len(client.get("/api/trip/tasks").json()) == 1


def test_planner_failure_marks_task_failed(client, use_planner):
    use_planner(FakePlanner(fail=True))
    task_id = client.post("/api/trip/plan", json=BODY).json()["task_id"]

    detail = client.get(f"/api/trip/tasks/{task_id}").json()

    assert detail["status"] == store.FAILED
    assert detail["error"]["code"] == "plan_generation_failed"
    assert "模型抽风了" in detail["error"]["message"]


# ------------------------------------------------------------------ 取消


def test_cancel_pending_task_immediately(client, session):
    from app.models.schemas import TripRequest

    request = TripRequest(**BODY)
    task, _ = store.create_task(session, request, BODY)
    session.commit()

    response = client.post(f"/api/trip/tasks/{task.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == store.CANCELED


def test_cancel_running_task_only_flags(client, session):
    from app.models.schemas import TripRequest

    request = TripRequest(**BODY)
    task, _ = store.create_task(session, request, BODY)
    store.transition(session, task, store.RUNNING)
    session.commit()

    response = client.post(f"/api/trip/tasks/{task.id}/cancel")

    assert response.json()["status"] == store.RUNNING
    session.refresh(task)
    assert task.cancel_requested is True


def test_runner_stops_at_checkpoint_when_cancelled(db_url, session):
    """执行中取消：on_step 里打标记，下一个检查点抛 TaskCanceled，任务落 canceled"""
    from app.models.schemas import TripRequest

    request = TripRequest(**BODY)
    task, _ = store.create_task(session, request, BODY)
    session.commit()

    def cancel_now():
        with __import__("app.db", fromlist=["session_scope"]).session_scope() as s:
            target = store.get_task(s, task.id)
            store.request_cancel(s, target)

    runner = TaskRunner(planner_factory=lambda: FakePlanner(steps=5, cancel_via=cancel_now))
    runner.execute(task.id)

    session.refresh(task)
    assert task.status == store.CANCELED
    assert task.error_code == "canceled_by_user"
    assert len(task.steps) == 1


def test_404_for_unknown_task(client):
    assert client.get("/api/trip/tasks/task_不存在").status_code == 404
    assert client.post("/api/trip/tasks/task_不存在/cancel").status_code == 404


# ------------------------------------------------------------------ SSE


def test_sse_stream_emits_events_and_done(client, use_planner):
    use_planner(FakePlanner(steps=2))
    task_id = client.post("/api/trip/plan", json=BODY).json()["task_id"]

    events = []
    with client.stream("GET", f"/api/trip/tasks/{task_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: "))

    assert events[0] == "task.created"
    assert "task.step" in events
    assert "task.succeeded" in events
    assert events[-1] == "done"


def test_sse_resume_from_last_event_id(client, use_planner, session):
    use_planner(FakePlanner(steps=1))
    task_id = client.post("/api/trip/plan", json=BODY).json()["task_id"]

    with session_scope() as s:
        total = len(store.list_events(s, task_id))

    events = []
    with client.stream(
        "GET",
        f"/api/trip/tasks/{task_id}/events",
        headers={"Last-Event-ID": str(total - 1)},
    ) as response:
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: "))

    # 只剩最后一条事件 + done
    assert len(events) == 2
    assert events[-1] == "done"


from app.db import session_scope  # noqa: E402  (测试里延迟导入会触发循环，这里放末尾)

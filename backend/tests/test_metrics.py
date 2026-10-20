"""指标测试

验收矩阵 M3「指标」一行的证据：
- /metrics 里能读到 P50/P95、失败率、每步耗时、token 用量；
- 分位数算法本身是对的（用已知数据算出来的值对得上）。
"""

from __future__ import annotations

from app.api.routes.metrics import _percentile, render_metrics


def _seed(session):
    from app.models.schemas import TripRequest
    from app.repositories import task_store as store

    request = TripRequest(
        city="杭州",
        start_date="2026-10-21",
        end_date="2026-10-23",
        travel_days=3,
        transportation="公共交通",
        accommodation="经济型酒店",
    )

    elapsed_values = [1000, 2000, 3000, 4000, 10000]
    for index, elapsed in enumerate(elapsed_values):
        task, _ = store.create_task(session, request, request.model_dump())
        store.transition(session, task, store.RUNNING)
        store.append_step(
            session,
            task,
            {"step": "景点搜索", "ok": True, "tool": "amap_search_poi", "latency_ms": 1000 + index * 100},
        )
        store.transition(session, task, store.SUCCEEDED)
        task.elapsed_ms = elapsed
        task.token_usage = 100

    failed, _ = store.create_task(session, request, request.model_dump())
    store.transition(session, failed, store.RUNNING)
    store.transition(session, failed, store.FAILED, error_code="internal_error")
    session.commit()


def test_percentile_basic():
    assert _percentile([], 0.5) == 0.0
    assert _percentile([7], 0.95) == 7.0
    assert _percentile([1, 2, 3, 4, 5], 0.5) == 3.0
    assert _percentile([1, 2, 3, 4, 5], 1.0) == 5.0


def test_metrics_contains_latency_failure_tokens_and_steps(database, session):
    _seed(session)

    text = render_metrics()

    assert 'tasks_total{status="succeeded"} 5' in text
    assert 'tasks_total{status="failed"} 1' in text
    assert 'task_duration_ms{quantile="0.5"}' in text
    assert 'task_duration_ms{quantile="0.95"}' in text
    # 5 条成功任务，1 条失败 → 1/6
    assert "task_failure_ratio 0.1667" in text
    assert "task_tokens_total 500" in text
    assert 'task_step_duration_ms{step="景点搜索"}' in text
    assert 'task_step_count{step="景点搜索"} 5' in text
    assert "cache_hit_ratio" in text
    assert "queue_depth" in text


def test_metrics_survives_empty_database(database):
    text = render_metrics()

    assert 'task_duration_ms{quantile="0.5"} 0' in text
    assert "task_failure_ratio 0.0" in text
    assert "task_tokens_total 0" in text

"""/metrics：Prometheus 文本格式

为什么用 Prometheus 文本而不是随便返一段 JSON：这是运维侧的标准接口，
`prometheus_client` 抓、Grafana 画，也方便 grep 做验收（测试里就是这么断言的）。

覆盖：队列深度、任务分布与延迟分位数、失败率、每步耗时、token 用量、缓存命中率。
"""

from __future__ import annotations

from typing import Iterable, List

from fastapi import APIRouter, Response
from sqlalchemy import func, select

from ...db import session_scope
from ...models.tables import Task, TaskStep
from ...queue import get_queue
from ...repositories.task_store import SUCCEEDED
from ...services.cache import cache_stats

router = APIRouter(tags=["可观测"])


def _percentile(values: Iterable[int], quantile: float) -> float:
    """最近秩法；样本为 0 时返回 0，不抛异常（指标接口不该因为没数据就 500）"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    index = max(0, min(len(ordered) - 1, int(round(quantile * (len(ordered) - 1)))))
    return float(ordered[index])


def _task_stats() -> dict:
    with session_scope() as session:
        rows = session.execute(select(Task.status, func.count()).group_by(Task.status)).all()
        elapsed = list(session.scalars(select(Task.elapsed_ms).where(Task.status == SUCCEEDED)))
        tokens = int(session.scalar(select(func.coalesce(func.sum(Task.token_usage), 0))) or 0)

    counts = {str(status): int(count) for status, count in rows}
    total = sum(counts.values()) or 0
    failed = counts.get("failed", 0)
    return {
        "counts": counts,
        "total": total,
        "failure_ratio": round(failed / total, 4) if total else 0.0,
        "p50": _percentile(elapsed, 0.50),
        "p95": _percentile(elapsed, 0.95),
        "tokens": tokens,
    }


def _step_stats() -> List[tuple[str, int, float]]:
    with session_scope() as session:
        rows = session.execute(
            select(TaskStep.step, func.count(), func.avg(TaskStep.latency_ms))
            .group_by(TaskStep.step)
            .order_by(func.avg(TaskStep.latency_ms).desc())
        ).all()
    return [(str(step), int(count), float(avg or 0)) for step, count, avg in rows]


def render_metrics() -> str:
    lines: List[str] = ["# metrics by trip-planner"]

    queue = get_queue()
    stats = queue.stats()
    lines.append("# HELP queue_depth 队列中各状态的作业数")
    lines.append("# TYPE queue_depth gauge")
    for state, value in stats.items():
        lines.append(f'queue_depth{{state="{state}"}} {value}')

    lines.append("# HELP queue_dead_letters_total 死信作业数")
    lines.append("# TYPE queue_dead_letters_total counter")
    lines.append(f"queue_dead_letters_total {stats.get('dead', 0)}")

    tasks = _task_stats()
    lines.append("# HELP tasks_total 按状态统计的任务数")
    lines.append("# TYPE tasks_total gauge")
    for status, value in tasks["counts"].items():
        lines.append(f'tasks_total{{status="{status}"}} {value}')

    lines.append("# HELP task_duration_ms 已完成任务的耗时分位数")
    lines.append("# TYPE task_duration_ms gauge")
    lines.append(f'task_duration_ms{{quantile="0.5"}} {tasks["p50"]:.0f}')
    lines.append(f'task_duration_ms{{quantile="0.95"}} {tasks["p95"]:.0f}')

    lines.append("# HELP task_failure_ratio 失败任务占比")
    lines.append("# TYPE task_failure_ratio gauge")
    lines.append(f"task_failure_ratio {tasks['failure_ratio']}")

    lines.append("# HELP task_tokens_total 累计 token 用量")
    lines.append("# TYPE task_tokens_total counter")
    lines.append(f"task_tokens_total {tasks['tokens']}")

    lines.append("# HELP task_step_duration_ms 各编排步骤的平均耗时")
    lines.append("# TYPE task_step_duration_ms gauge")
    lines.append("# HELP task_step_count 各编排步骤的执行次数")
    lines.append("# TYPE task_step_count counter")
    for step, count, avg in _step_stats():
        safe = step.replace('"', "")
        lines.append(f'task_step_duration_ms{{step="{safe}"}} {avg:.0f}')
        lines.append(f'task_step_count{{step="{safe}"}} {count}')

    cache = cache_stats()
    lines.append("# HELP cache_hits_total 缓存命中次数")
    lines.append("# TYPE cache_hits_total counter")
    lines.append(f"cache_hits_total {cache['hits']}")
    lines.append("# HELP cache_misses_total 缓存未命中次数")
    lines.append("# TYPE cache_misses_total counter")
    lines.append(f"cache_misses_total {cache['misses']}")
    lines.append("# HELP cache_hit_ratio 缓存命中率")
    lines.append("# TYPE cache_hit_ratio gauge")
    lines.append(f"cache_hit_ratio {cache['hit_ratio']}")
    lines.append("# HELP cache_entries 缓存条目数")
    lines.append("# TYPE cache_entries gauge")
    lines.append(f"cache_entries {cache['entries']}")

    return "\n".join(lines) + "\n"


@router.get("/metrics", summary="Prometheus 指标", include_in_schema=False)
def metrics():
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")


@router.get("/api/metrics/json", summary="指标（JSON，便于前端/脚本读取）")
def metrics_json():
    tasks = _task_stats()
    return {
        "queue": get_queue().stats(),
        "tasks": {
            **tasks,
            "steps": [
                {"step": step, "count": count, "avg_latency_ms": round(avg, 1)}
                for step, count, avg in _step_stats()
            ],
        },
        "cache": cache_stats(),
    }

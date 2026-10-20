"""/metrics：Prometheus 文本格式

为什么用 Prometheus 文本而不是随便返一段 JSON：这是运维侧的标准接口，
`prometheus_client` 抓、Grafana 画，也方便用 grep 做验收（测试里就是这么断言的）。

M2 阶段先暴露队列与缓存；M3 补上任务延迟分位数、失败率、每步耗时与 token 用量。
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import func, select

from ...db import session_scope
from ...models.tables import Task
from ...queue import get_queue
from ...services.cache import cache_stats

router = APIRouter(tags=["可观测"])


def _task_counts() -> dict[str, int]:
    with session_scope() as session:
        rows = session.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    return {str(status): int(count) for status, count in rows}


def render_metrics() -> str:
    lines: list[str] = []

    queue = get_queue()
    stats = queue.stats()
    lines.append("# HELP queue_depth 队列中各状态的作业数")
    lines.append("# TYPE queue_depth gauge")
    for state, value in stats.items():
        lines.append(f'queue_depth{{state="{state}"}} {value}')

    dead = stats.get("dead", 0)
    lines.append("# HELP queue_dead_letters_total 死信作业数")
    lines.append("# TYPE queue_dead_letters_total counter")
    lines.append(f"queue_dead_letters_total {dead}")

    counts = _task_counts()
    lines.append("# HELP tasks_total 按状态统计的任务数")
    lines.append("# TYPE tasks_total gauge")
    for status, value in counts.items():
        lines.append(f'tasks_total{{status="{status}"}} {value}')

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
    return {
        "queue": get_queue().stats(),
        "tasks": _task_counts(),
        "cache": cache_stats(),
    }

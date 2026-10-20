"""任务查询、SSE 事件流、取消

设计取舍：SSE 用"轮询数据库"实现，而不是 Redis pub/sub。
理由是本机没有 Redis（见 docs/jd-fit.md 运行时约束），而事件已经落库；
400ms 轮询对单人/小规模完全够用，且天然支持断线重连（Last-Event-ID 直接映射到 seq）。
M2 之后如果有 Redis，可以换成 pub/sub 推送，接口不变。
"""

from __future__ import annotations

import json
import time
from typing import Iterator, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from ...db import session_scope
from ...models.schemas import TaskDetailResponse
from ...repositories import task_store as store

router = APIRouter(prefix="/trip/tasks", tags=["任务"])

POLL_INTERVAL_SECONDS = 0.4


def _load(task_id: str):
    with session_scope() as session:
        task = store.get_task(session, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        return store.serialize(task)


@router.get("", response_model=List[TaskDetailResponse], summary="任务列表")
def list_tasks(
    status: Optional[str] = Query(None, description="按状态过滤"),
    limit: int = Query(20, ge=1, le=100),
):
    with session_scope() as session:
        return [store.serialize(t) for t in store.list_tasks(session, status, limit)]


@router.get("/{task_id}", response_model=TaskDetailResponse, summary="查询任务状态与结果")
def get_task(task_id: str):
    return _load(task_id)


@router.post("/{task_id}/cancel", response_model=TaskDetailResponse, summary="取消任务")
def cancel_task(task_id: str):
    with session_scope() as session:
        task = store.get_task(session, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        if task.status in store.TERMINAL:
            return store.serialize(task)
        store.request_cancel(session, task)
        return store.serialize(task)


@router.get(
    "/{task_id}/events",
    summary="任务事件流（SSE）",
    description="推送 task.created / task.running / task.step / task.succeeded 等事件；"
    "断线重连时带上 Last-Event-ID 即可续传",
)
def stream_events(
    task_id: str,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    after: int = Query(0, ge=0, description="从哪个 seq 之后开始（Last-Event-ID 的等价参数）"),
    timeout_s: int = Query(300, ge=5, le=900, description="最长挂多久"),
):
    start_seq = after
    if last_event_id:
        try:
            start_seq = max(start_seq, int(last_event_id))
        except ValueError:
            pass

    def generator() -> Iterator[str]:
        cursor = start_seq
        deadline = time.monotonic() + timeout_s

        # 先确认任务存在，避免空流挂在 404 上
        with session_scope() as session:
            if store.get_task(session, task_id) is None:
                yield _sse(None, "error", {"message": "任务不存在"})
                return

        while True:
            with session_scope() as session:
                task = store.get_task(session, task_id)
                if task is None:
                    yield _sse(None, "error", {"message": "任务不存在"})
                    return
                status = task.status
                events = store.list_events(session, task_id, cursor)
                pending_events = [
                    (e.seq, e.type, store.loads(e.payload_json, {})) for e in events
                ]

            for seq, event_type, payload in pending_events:
                cursor = seq
                yield _sse(seq, event_type, {"seq": seq, "type": event_type, "payload": payload})

            if status in store.TERMINAL and not pending_events:
                yield _sse(cursor, "done", {"status": status})
                return

            if time.monotonic() > deadline:
                yield _sse(cursor, "timeout", {"status": status, "hint": "带 Last-Event-ID 重连可续传"})
                return

            time.sleep(POLL_INTERVAL_SECONDS)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _sse(seq: Optional[int], event: str, payload: dict) -> str:
    lines = []
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"

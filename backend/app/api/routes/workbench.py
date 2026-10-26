from pathlib import Path

from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException

from ...db import session_scope
from ...repositories import task_store as store

router = APIRouter(prefix="/api/workbench", tags=["React 工作台"])
PROJECT = Path(__file__).resolve().parents[4]


@router.get("/config")
def config():
    values = dotenv_values(PROJECT / "frontend" / ".env")
    return {"map_key": values.get("VITE_AMAP_WEB_JS_KEY", ""), "map_security_code": values.get("VITE_AMAP_SECURITY_JS_CODE", "")}


@router.get("/tasks/{task_id}/timeline")
def timeline(task_id: str):
    with session_scope() as session:
        if store.get_task(session, task_id) is None:
            raise HTTPException(404, "任务不存在")
        return [{"seq": event.seq, "type": event.type, "payload": store.loads(event.payload_json, {})} for event in store.list_events(session, task_id, 0)]


@router.get("/tasks/{task_id}/request")
def request(task_id: str):
    with session_scope() as session:
        task = store.get_task(session, task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        return store.loads(task.request_json, {})

"""结构化日志与请求上下文

三件事：
1. 日志是 JSON 行（loguru serialize），字段可被 grep/jq/日志系统直接消费；
2. `request_id` / `task_id` 放在 ContextVar 里，任何模块打日志都会自动带上；
3. 同一次 run 的所有日志能串起来：HTTP 请求分配 request_id 并写进 tasks 表，
   runner 从表里读回来设进上下文——所以后台线程/独立 worker 里的日志也带同一个 id。

为什么不用 print：print 没有级别、没有字段、没法按 request_id 过滤，出问题时只能翻屏。
"""

from __future__ import annotations

import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any, Optional

from loguru import logger

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
task_id_var: ContextVar[str] = ContextVar("task_id", default="-")
step_var: ContextVar[str] = ContextVar("step", default="-")

_record_patcher_installed = False


def _patch(record: dict[str, Any]) -> None:
    """把上下文变量塞进每条日志的 extra"""
    extra = record["extra"]
    extra.setdefault("request_id", request_id_var.get())
    extra.setdefault("task_id", task_id_var.get())
    extra.setdefault("step", step_var.get())


def configure_logging(level: Optional[str] = None, json_lines: Optional[bool] = None) -> None:
    """应用启动时调一次；重复调用安全"""
    global _record_patcher_installed

    level = level or os.getenv("LOG_LEVEL", "INFO")
    if json_lines is None:
        json_lines = os.getenv("LOG_JSON", "1") != "0"

    logger.remove()
    logger.configure(patcher=_patch)
    _record_patcher_installed = True
    logger.add(
        sys.stdout,
        level=level.upper(),
        serialize=json_lines,
        backtrace=False,
        diagnose=False,
        enqueue=False,
        format="{time:YYYY-MM-DDTHH:mm:ss.SSSZ} | {level: <8} | {message}",
    )

    # 可选落盘（便于 grep/接日志采集；默认不写文件）
    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        from pathlib import Path

        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            level=level.upper(),
            serialize=json_lines,
            backtrace=False,
            diagnose=False,
            enqueue=False,
            rotation="10 MB",
            retention=5,
            encoding="utf-8",
        )


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


class _Ctx:
    """上下文管理器：临时绑定某个 id，退出时恢复原值"""

    def __init__(self, var: ContextVar[str], value: str) -> None:
        self.var = var
        self.value = value
        self.token: Any = None

    def __enter__(self) -> "_Ctx":
        self.token = self.var.set(self.value)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.var.reset(self.token)


def bind_request(request_id: str) -> _Ctx:
    return _Ctx(request_id_var, request_id or "-")


def bind_task(task_id: str, request_id: Optional[str] = None) -> _Ctx:
    """进入任务上下文；request_id 从库里读回来，保证跨进程也能串起来"""
    if request_id:
        request_id_var.set(request_id)
    return _Ctx(task_id_var, task_id)


def bind_step(step: str) -> _Ctx:
    return _Ctx(step_var, step)


# 统一入口：业务代码从这里拿 logger，方便以后换实现
log = logger

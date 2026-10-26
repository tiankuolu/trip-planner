"""FastAPI 主应用"""

import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

# Windows 控制台默认 GBK，强制 UTF-8 避免日志里的中文/emoji 崩掉
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import get_settings, print_config, validate_config
from ..db import create_all, get_database_url
from ..observability import bind_request, configure_logging, log, new_request_id
from .routes import map as map_routes
from .routes import metrics, poi, tasks, trip, workbench

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动/关闭事件"""
    log.info("app.starting", app=settings.app_name, version=settings.app_version)
    print_config()

    try:
        validate_config()
        log.info("config.validated")
    except ValueError as exc:
        log.error("config.invalid", error=str(exc))
        raise

    # 建表（幂等）。本地开发不必手动 alembic；生产发布走 `alembic upgrade head`
    create_all()
    db_url = get_database_url()
    log.info("db.ready", database="sqlite" if db_url.startswith("sqlite") else "external")
    log.info("app.ready", docs="http://localhost:8000/docs", health="/api/trip/health")

    yield

    log.info("app.stopping")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "基于多 Agent 编排的旅行规划助手：景点/天气/酒店数据来自高德 Web 服务，"
        "工具调用由本地执行并回填坐标；生成走异步任务，支持查询/取消/SSE 进度。"
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """给每个请求分配 request_id，并记录一条带耗时的访问日志"""
    request_id = request.headers.get("X-Request-Id") or new_request_id()
    status_code = 500
    started = time.perf_counter()

    with bind_request(request_id):
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=status_code,
                elapsed_ms=elapsed_ms,
            )


app.include_router(trip.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(poi.router, prefix="/api")
app.include_router(map_routes.router, prefix="/api")
app.include_router(metrics.router)
app.include_router(workbench.router)
FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend-react" / "dist"
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="react-assets")


@app.get("/")
def root():
    return FileResponse(FRONTEND_DIST / "index.html")


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version,
    }

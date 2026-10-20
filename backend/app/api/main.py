"""FastAPI 主应用"""

import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Windows 控制台默认 GBK，强制 UTF-8 避免打印 emoji 时崩溃
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings, print_config, validate_config
from ..db import create_all, get_database_url
from .routes import map as map_routes
from .routes import metrics, poi, tasks, trip

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动/关闭事件（替代已废弃的 on_event）"""
    print("\n" + "=" * 60)
    print(f"🚀 {settings.app_name} v{settings.app_version}")
    print("=" * 60)
    print_config()

    try:
        validate_config()
        print("\n✅ 配置验证通过")
    except ValueError as exc:
        print(f"\n❌ 配置验证失败:\n{exc}")
        raise

    # 建表（幂等）。本地开发不必先手动 alembic；生产发布走 `alembic upgrade head`
    create_all()
    db = get_database_url()
    print(f"🗄️  数据库: {'sqlite' if db.startswith('sqlite') else db.split(':')[0]}")

    print("\n" + "=" * 60)
    print("📚 API文档: http://localhost:8000/docs")
    print("🔧 工具自检: http://localhost:8000/api/trip/health")
    print("=" * 60 + "\n")

    yield

    print("\n" + "=" * 60)
    print("👋 应用正在关闭...")
    print("=" * 60 + "\n")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "基于多 Agent 编排的旅行规划助手：景点/天气/酒店数据来自高德 Web 服务，"
        "工具调用由本地执行并回填坐标。"
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
)

app.include_router(trip.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(poi.router, prefix="/api")
app.include_router(map_routes.router, prefix="/api")
app.include_router(metrics.router)


@app.get("/")
def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
        "redoc": "/redoc",
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version,
    }

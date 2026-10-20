"""数据库连接与会话管理

默认 SQLite（本机可跑、可测），通过 `DATABASE_URL` 可切 MySQL：
    DATABASE_URL=mysql+pymysql://user:pass@127.0.0.1:3306/tripplanner

SQLite 的两个必要设置：WAL（并发读写）+ foreign_keys（外键真的生效）。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BACKEND_DIR / "data" / "app.db"


class Base(DeclarativeBase):
    """所有 ORM 模型的基类"""


def get_database_url() -> str:
    """读取 DATABASE_URL，缺省落到 backend/data/app.db"""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{DEFAULT_DB_PATH.as_posix()}"


_engine: Optional[Engine] = None
_engine_url: Optional[str] = None
_session_factory: Optional[sessionmaker[Session]] = None


def get_engine() -> Engine:
    """按当前 DATABASE_URL 返回引擎（URL 变化时自动重建）"""
    global _engine, _engine_url, _session_factory

    url = get_database_url()
    if _engine is not None and _engine_url == url:
        return _engine

    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False, "timeout": 15} if is_sqlite else {},
    )

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()

    _engine = engine
    _engine_url = url
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务边界：正常提交，异常回滚"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    """建表（测试与本地首次启动用；生产走 alembic upgrade head）"""
    from . import models  # noqa: F401  确保模型已注册到 Base.metadata

    Base.metadata.create_all(get_engine())


def reset_engine() -> None:
    """丢弃缓存的引擎（测试切换 DATABASE_URL 时用）"""
    global _engine, _engine_url, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None
    _session_factory = None

"""pytest 公共装置：每个测试用独立的 SQLite 文件，互不污染"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def _skip_config_validation(monkeypatch):
    """测试环境没有真实 key：跳过启动时的强校验（LLM 警告仍会打印）"""
    monkeypatch.setenv("SKIP_CONFIG_VALIDATION", "1")


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    """独立的临时库；测试结束后引擎丢弃"""
    from app.db import reset_engine

    url = f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    reset_engine()
    yield url
    reset_engine()


@pytest.fixture()
def database(db_url):
    """建好表的空库；同时清掉队列单例，避免跨测试复用旧引擎"""
    from app.db import create_all
    from app.queue import reset_queue

    reset_queue()
    create_all()
    yield db_url
    reset_queue()


@pytest.fixture()
def session(database):
    """已建表、自动关事务的会话"""
    from app.db import get_session_factory

    s = get_session_factory()()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture()
def queue(database):
    from app.queue import SqliteQueue

    return SqliteQueue(max_attempts=3)


@pytest.fixture()
def trip_request():
    from app.models.schemas import TripRequest

    return TripRequest(
        city="杭州",
        start_date="2026-10-21",
        end_date="2026-10-23",
        travel_days=3,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["历史文化"],
    )

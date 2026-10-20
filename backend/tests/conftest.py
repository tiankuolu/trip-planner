"""pytest 公共装置：每个测试用独立的 SQLite 文件，互不污染"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


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
def session(db_url):
    """已建表、自动关事务的会话"""
    from app.db import create_all, get_session_factory

    create_all()
    s = get_session_factory()()
    try:
        yield s
        s.commit()
    finally:
        s.close()


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

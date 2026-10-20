"""缓存测试

验收矩阵 M2「缓存」一行的证据：命中率是真实统计出来的，不是编的。
"""

from __future__ import annotations

from app.services import cache


def test_roundtrip(database):
    cache.cache_set("poi", "poi:杭州:博物馆", {"count": 2})

    assert cache.cache_get("poi", "poi:杭州:博物馆") == {"count": 2}


def test_expired_entry_is_a_miss(database):
    cache.cache_set("poi", "poi:杭州:公园", {"count": 1}, ttl_seconds=-1)

    assert cache.cache_get("poi", "poi:杭州:公园") is None


def test_cached_calls_producer_once(database):
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return {"count": 3}

    first = cache.cached("hotel", "hotel:杭州:汉庭:", producer)
    second = cache.cached("hotel", "hotel:杭州:汉庭:", producer)

    assert first == second == {"count": 3}
    assert calls["n"] == 1

    stats = cache.cache_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_ratio"] == 0.5


def test_producer_exception_is_not_cached(database):
    def broken():
        raise RuntimeError("高德挂了")

    try:
        cache.cached("weather", "weather:杭州", broken)
    except RuntimeError:
        pass

    assert cache.cache_get("weather", "weather:杭州") is None


def test_purge_expired(database):
    cache.cache_set("poi", "k1", {"a": 1}, ttl_seconds=-1)
    cache.cache_set("poi", "k2", {"a": 2}, ttl_seconds=600)

    removed = cache.purge_expired()

    assert removed == 1
    assert cache.cache_stats()["entries"] == 1

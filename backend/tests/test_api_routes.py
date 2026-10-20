"""HTTP 层测试：路由、错误语义、request_id 贯穿、Metrics 出口

用假 AmapService，不联网。
"""

from __future__ import annotations

import pytest

from app.models.schemas import Location, POIInfo, WeatherInfo


class FakeAmap:
    def __init__(self) -> None:
        self.key = "fake-key"

    def search_poi(self, keywords, city, citylimit=True, types=None, offset=10):
        return [
            POIInfo(
                id="B0FFFAKE",
                name=f"{city}{keywords}1",
                type="风景名胜",
                address="某路 1 号",
                location=Location(longitude=120.15, latitude=30.25),
            )
        ]

    def get_weather(self, city):
        return [
            WeatherInfo(
                date="2026-10-21",
                day_weather="晴",
                night_weather="多云",
                day_temp=25,
                night_temp=15,
                wind_direction="东风",
                wind_power="1-3",
            )
        ]

    def plan_route_by_coords(self, *args, city="", route_type="transit", **kwargs):
        from app.models.schemas import RouteInfo

        return RouteInfo(distance=1200, duration=600, route_type=route_type, description="测试路线")

    def plan_route(self, *args, **kwargs):
        return self.plan_route_by_coords(*args, **kwargs)

    def search_poi_photos(self, name, city, limit=5):
        return ["http://example.com/a.jpg"]

    def get_poi_detail(self, poi_id):
        return {"id": poi_id, "name": "断桥残雪"}


@pytest.fixture()
def client(database, monkeypatch):
    from fastapi.testclient import TestClient

    import app.api.routes.map as map_routes
    import app.api.routes.poi as poi_routes
    from app.api.main import app

    fake = FakeAmap()
    monkeypatch.setattr(map_routes, "get_amap_service", lambda: fake)
    monkeypatch.setattr(poi_routes, "get_amap_service", lambda: fake)

    with TestClient(app) as test_client:
        yield test_client


def test_request_id_header_and_access_log(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["X-Request-Id"].startswith("req_")


def test_custom_request_id_is_echoed(client):
    response = client.get("/health", headers={"X-Request-Id": "req_custom123"})

    assert response.headers["X-Request-Id"] == "req_custom123"


def test_map_poi_returns_real_shape(client):
    response = client.get("/api/map/poi", params={"keywords": "西湖", "city": "杭州"})

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["data"][0]["name"] == "杭州西湖1"
    assert body["data"][0]["location"]["longitude"] == 120.15


def test_map_weather_returns_forecast(client):
    body = client.get("/api/map/weather", params={"city": "杭州"}).json()

    assert body["success"] is True
    assert body["data"][0]["day_weather"] == "晴"


def test_map_route_between(client):
    body = client.get(
        "/api/map/route-between",
        params={
            "origin_lng": 120.1,
            "origin_lat": 30.2,
            "destination_lng": 120.2,
            "destination_lat": 30.3,
            "city": "杭州",
            "route_type": "walking",
        },
    ).json()

    assert body["success"] is True
    assert body["data"]["route_type"] == "walking"


def test_poi_photo(client):
    body = client.get("/api/poi/photo", params={"name": "断桥残雪", "city": "杭州"}).json()

    assert body["success"] is True
    assert body["data"]["photo_url"].startswith("http")


def test_metrics_endpoint_is_prometheus_text(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "cache_hit_ratio" in response.text


def test_metrics_json_endpoint_shape(client):
    body = client.get("/api/metrics/json").json()

    assert set(body) == {"queue", "tasks", "cache"}
    assert "failure_ratio" in body["tasks"]
    assert "hit_ratio" in body["cache"]


def test_validation_error_on_bad_submit(client):
    response = client.post("/api/trip/plan", json={"city": "杭州"})

    assert response.status_code == 422

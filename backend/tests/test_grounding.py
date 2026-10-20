"""坐标回填的匹配逻辑测试（纯函数，不联网、不调模型）

这几个用例对应线上真实踩到的坑：
- "西湖" 不能匹配到 "西湖博物馆"（短名包含会误伤）；
- 同一连锁的不同分店不能互相匹配（否则会把 A 店的地址安到 B 店头上）。
"""


import pytest

from app.agents.trip_planner_agent import _best_match, _distance_km, _normalize_name


def test_exact_name_wins():
    candidates = [{"name": "西湖博物馆"}, {"name": "西湖"}]
    assert _best_match("西湖", candidates)["name"] == "西湖"


def test_short_name_does_not_match_longer_poi():
    candidates = [{"name": "西湖博物馆"}]
    assert _best_match("西湖", candidates) is None


def test_branch_name_matches_with_padding():
    candidates = [{"name": "浙江省博物馆(孤山馆区)"}]
    assert _best_match("浙江省博物馆", candidates)["name"] == "浙江省博物馆(孤山馆区)"


def test_different_hotel_branches_do_not_match():
    candidates = [{"name": "汉庭酒店(杭州文新地铁站店)"}]
    assert _best_match("汉庭酒店(杭州西湖仁和路店)", candidates, min_similarity=0.75) is None


def test_normalize_strips_brackets_but_keeps_branch():
    # 括号符号去掉，但括号里的分店/馆区信息必须保留
    assert _normalize_name("杭州博物馆（孤山馆区）") == "杭州博物馆孤山馆区"


def test_distance_between_xihu_and_xiaoheshan():
    # 西湖 ~ 萧山，直线距离 15 公里以上
    distance = _distance_km(
        {"longitude": 120.1508, "latitude": 30.2590},
        {"longitude": 120.2640, "latitude": 30.1790},
    )
    assert 10 < distance < 20


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("90", 90), ("90分钟", 90), ("1.5小时", 90), (None, 120)],
)
def test_minutes_parsing_applies_to_attraction(raw, expected):
    from app.models.schemas import Attraction

    attraction = Attraction(name="测试景点", visit_duration=raw)
    assert attraction.visit_duration == expected


def test_ticket_price_tolerates_free_text():
    from app.models.schemas import Attraction

    assert Attraction(name="免费景点", ticket_price="免费").ticket_price == 0
    assert Attraction(name="收费景点", ticket_price="¥60").ticket_price == 60

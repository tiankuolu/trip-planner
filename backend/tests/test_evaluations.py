import copy
import json
from pathlib import Path

import pytest

from app.models.schemas import TripPlan
from app.services.amap_service import AmapError
from evaluations.benchmark import build, load
from evaluations.environment import FrozenTools, SnapshotMiss
from evaluations.graders import (
    aggregate,
    budget_check,
    grade,
    grade_constraints,
    grade_pois,
    grade_tools,
)
from evaluations.io import digest, read
from evaluations.models import Constraint, Experiment
from evaluations.report import summarize

DATA = Path(__file__).resolve().parents[1] / "evaluations" / "data"


@pytest.fixture
def snapshot():
    return read(DATA / "snapshot.json")


@pytest.fixture
def task():
    return build()[0]


@pytest.fixture
def plan(task, snapshot):
    poi = snapshot["cities"][task.request.city]["pois"][0]
    return TripPlan(city=task.request.city, start_date=task.request.start_date, end_date=task.request.end_date,
                    days=[{"date": task.request.start_date, "attractions": [{"name": poi["name"], "poi_id": poi["id"], "location": poi["location"]}]}],
                    overall_suggestions="评分器输入契约验证").model_dump()


@pytest.mark.parametrize("statuses,expected", [(["pass"], "pass"), (["unverifiable"], "unverifiable"), (["fail", "unverifiable"], "fail"), ([], "unverifiable")])
def test_aggregation(statuses, expected):
    assert aggregate(statuses) == expected


def test_benchmark_contract():
    tasks = build()
    assert len(tasks) == 100
    assert len({t.task_id for t in tasks}) == 100
    assert len({t.category for t in tasks}) == 10
    assert {t.fault["kind"] for t in tasks if t.fault} == {"timeout", "weather_error", "hotel_empty", "poi_empty", "invalid_fields", "network"}
    assert read(DATA / "benchmark.json") == [t.model_dump() for t in tasks]
    with pytest.raises(ValueError):
        load([tasks[0].model_dump(), tasks[0].model_dump()])


def test_snapshot_content_hash(snapshot):
    assert snapshot["content_hash"] == digest({k: v for k, v in snapshot.items() if k != "content_hash"})
    assert snapshot["provenance"]["kind"] == "recorded_api"
    assert len(snapshot["recordings"]) == 12


def test_valid_core(task, plan, snapshot):
    result = grade(task, {"plan": plan, "raw_plan": plan, "tool_calls": []}, snapshot)
    assert result["task_success"] == "pass"


@pytest.mark.parametrize("kind", ["city", "dates", "daily_count"])
def test_hard_constraints_fail(task, plan, snapshot, kind):
    if kind == "city":
        plan["city"] = "杭州"
    elif kind == "dates":
        plan["days"][0]["date"] = "2026-12-31"
    else:
        plan["days"][0]["attractions"] *= 4
    checks = grade_constraints(task, plan, plan, snapshot)
    assert next(c for c in checks if c["name"] == kind)["status"] == "fail"


def test_empty_plan_fails(task, snapshot):
    assert grade(task, {"plan": None}, snapshot)["task_success"] == "fail"


def test_unknown_poi_fails_catalog_validity(plan, snapshot):
    plan["days"][0]["attractions"][0]["name"] = "未收录地点"
    assert grade_pois(plan, snapshot)[0]["status"] == "fail"


def test_changed_coordinate_fails(plan, snapshot):
    plan["days"][0]["attractions"][0]["location"]["latitude"] = 0
    assert grade_pois(plan, snapshot)[0]["status"] == "fail"


def test_untrusted_snapshot_never_passes(plan, snapshot):
    snapshot["provenance"]["kind"] = "analytical_input"
    assert grade_pois(plan, snapshot)[0]["status"] == "unverifiable"


@pytest.mark.parametrize("kind,value", [("budget", 1000), ("travelers", 2), ("opening", True), ("visit_window", ["09:00", "17:00"])])
def test_missing_evidence_unknown(task, plan, snapshot, kind, value):
    task.constraints.append(Constraint(kind=kind, value=value, source="contract", rule="缺少证据必须无法验证"))
    assert grade(task, {"plan": plan, "raw_plan": plan}, snapshot)["task_success"] == "unverifiable"


def test_declared_budget_over_limit_fails(plan, snapshot):
    plan["budget"] = {"total": 2000}
    assert budget_check(plan, {}, snapshot, 1000) == "fail"


def test_required_poi_absence(task, plan, snapshot):
    task.constraints.append(Constraint(kind="required_pois", value=["未收录地点"], source="contract", rule="必须出现"))
    assert grade_constraints(task, plan, {}, snapshot)[-1]["status"] == "fail"


def test_infeasible_response():
    task = next(t for t in build() if t.category == "infeasible")
    raw = {"infeasible": True, "reason": "每天至少4个与每天最多1个互相冲突", "days": []}
    assert grade_constraints(task, None, raw, {})[0]["status"] == "pass"
    assert grade_constraints(task, None, {**raw, "reason": "无法规划"}, {})[0]["status"] == "fail"


@pytest.mark.parametrize("args,selected", [({"city": "杭州", "keywords": "景点"}, "amap_search_poi"), ({"city": "北京"}, "amap_search_poi"), ({"city": "北京", "keywords": "景点"}, "unknown")])
def test_tool_selection_and_arguments(task, args, selected):
    checks = grade_tools([{"tool": "amap_search_poi", "arguments": args, "selected_tool": selected}], task)
    assert any(c["status"] == "fail" for c in checks)


@pytest.mark.parametrize("kind", ["timeout", "network", "weather_error"])
def test_fault_exception_and_recovery(snapshot, kind):
    tools = FrozenTools(snapshot, {"kind": kind, "tool": "amap_search_poi", "occurrences": [1]})
    with pytest.raises(AmapError, match=f"injected:{kind}"):
        tools("amap_search_poi", {"city": "北京", "keywords": "景点"})
    assert tools("amap_search_poi", {"city": "北京", "keywords": "景点"})["count"] > 0
    assert len(tools.calls) == 2
    assert tools.calls[0]["error"]
    assert tools.calls[1]["error"] is None


@pytest.mark.parametrize("kind,tool,key", [("hotel_empty", "amap_search_hotel", "hotels"), ("poi_empty", "amap_search_poi", "pois")])
def test_fault_empty(snapshot, kind, tool, key):
    tools = FrozenTools(snapshot, {"kind": kind, "tool": tool, "occurrences": [1]})
    assert tools(tool, {"city": "北京", "keywords": "景点"})[key] == []
    assert tools(tool, {"city": "北京", "keywords": "景点"})[key]


def test_fault_invalid_fields(snapshot):
    tools = FrozenTools(snapshot, {"kind": "invalid_fields", "tool": "amap_search_poi", "occurrences": [1]})
    response = tools("amap_search_poi", {"city": "北京", "keywords": "景点"})
    with pytest.raises(ValueError):
        int(response["count"])
    assert tools.calls[0]["invalid_response"]


def test_isolation_and_copy(snapshot):
    original = digest(snapshot)
    first, second = FrozenTools(snapshot), FrozenTools(snapshot)
    response = first("amap_search_poi", {"city": "北京", "keywords": "景点"})
    response["pois"][0]["name"] = "调用方修改"
    assert second("amap_search_poi", {"city": "北京", "keywords": "景点"})["pois"][0]["name"] != "调用方修改"
    assert digest(snapshot) == original
    assert first.calls[0]["response"]["pois"][0]["name"] != "调用方修改"


def test_snapshot_miss_fails_closed(snapshot):
    with pytest.raises(SnapshotMiss):
        FrozenTools(snapshot)("amap_get_weather", {"city": "未录制城市"})


def test_summary_denominators(task, plan, snapshot):
    base = {"plan": plan, "raw_plan": plan, "tool_calls": [], "latency_ms": 10, "usage": {"total_tokens": None}, "cost": None}
    base["grades"] = grade(task, base, snapshot)
    unknown = copy.deepcopy(base)
    unknown["grades"]["task_success"] = "unverifiable"
    stats = summarize([base, unknown])
    assert stats["verifiable_samples"] == stats["unverifiable_samples"] == 1
    assert stats["task_success_rate"] == 1
    assert stats["success_fraction_all"] == 0.5
    assert stats["recovery_rate"] is None
    assert stats["token_usage"] is None
    assert stats["cost"] is None


def test_no_fault_exposure_no_recovery(task, plan, snapshot):
    task.fault = {"kind": "timeout", "tool": "amap_search_poi", "occurrences": [1]}
    result = grade(task, {"plan": plan, "raw_plan": plan}, snapshot)
    assert not result["robustness"]["fault_exposed"]
    assert not result["robustness"]["recovered"]


def test_config_rejects_partial_prices():
    with pytest.raises(ValueError):
        Experiment(input_cost_per_million=1)


def test_all_data_json_is_portable():
    for path in DATA.glob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("opened,expected", [(True, "pass"), (False, "fail")])
def test_opening_evidence_contract(task, plan, snapshot, opened, expected):
    attraction = plan["days"][0]["attractions"][0]
    snapshot["evidence"]["opening"] = {attraction["poi_id"]: {task.request.start_date: {"open": opened, "source": "unit-test:opening-boolean-contract"}}}
    task.constraints.append(Constraint(kind="opening", value=True, source="contract", rule="依据开放布尔值"))
    assert grade_constraints(task, plan, {}, snapshot)[-1]["status"] == expected


@pytest.mark.parametrize("start,end,expected", [("10:00", "11:00", "pass"), ("08:00", "10:00", "fail"), ("11:00", "10:00", "fail")])
def test_visit_time_contract(task, plan, snapshot, start, end, expected):
    attraction = plan["days"][0]["attractions"][0]
    attraction["visit_duration"] = 60
    raw = copy.deepcopy(plan)
    raw["days"][0]["attractions"][0].update(start_time=start, end_time=end)
    snapshot["evidence"]["opening"] = {attraction["poi_id"]: {task.request.start_date: {"open": True, "hours": [["09:00", "17:00"]], "source": "unit-test:time-interval-contract"}}}
    task.constraints.append(Constraint(kind="visit_window", value=["09:00", "17:00"], source="contract", rule="时间区间包含关系"))
    assert grade_constraints(task, plan, raw, snapshot)[-1]["status"] == expected


@pytest.mark.parametrize("limit,expected", [(100, "pass"), (99, "fail")])
def test_budget_arithmetic_contract(task, plan, snapshot, limit, expected):
    attraction = plan["days"][0]["attractions"][0]
    snapshot["evidence"]["prices"] = {task.request.start_date: {"tickets": {attraction["poi_id"]: 20}, "meals_per_person": 20, "transport_per_person": 10, "source": "unit-test:2-times-50-arithmetic"}}
    assert budget_check(plan, {"travelers": 2, "rooms": 1}, snapshot, limit) == expected


def test_real_recorded_agent_execution(task, snapshot):
    from evaluations.runner import execute

    recorded = read(DATA / "recordings" / "basic-01-A.json")
    replay = execute(task, snapshot, Experiment(), "A", replay=recorded["llm_calls"])
    assert replay["plan"] == recorded["plan"]
    assert replay["grades"]["task_success"] == recorded["grades"]["task_success"]
    assert replay["grades"]["constraints"] == recorded["grades"]["constraints"]
    assert replay["grades"]["poi_validity"] == recorded["grades"]["poi_validity"]
    assert replay["usage"]["total_tokens"] is None
    assert replay["mode"] == "recorded_model_replay"


@pytest.mark.parametrize("arguments", [[], None, {"city": "北京", "keywords": 3}])
def test_malformed_arguments_are_graded(task, arguments):
    checks = grade_tools([{"tool": "amap_search_poi", "arguments": arguments}], task)
    assert next(c for c in checks if c["name"].startswith("arguments:"))["status"] == "fail"


def test_malformed_final_plan_is_failure(task, snapshot):
    assert grade(task, {"plan": {"days": "invalid"}}, snapshot)["task_success"] == "fail"


def test_actual_process_timeout(task, snapshot, tmp_path):
    from evaluations.runner import isolated

    result = isolated(task, snapshot, Experiment(timeout_seconds=0.001), "A", tmp_path / "timeout.json")
    assert result["error"]["type"] == "TimeoutError"
    assert result["grades"]["task_success"] == "fail"
    assert "timeout" in result["grades"]["errors"]
    assert result["usage"]["total_tokens"] is None

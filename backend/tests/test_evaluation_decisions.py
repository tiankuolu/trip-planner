import copy
from pathlib import Path

import pytest

from evaluations.benchmark import build
from evaluations.decisions import contradiction, judge_decision, parse_decision
from evaluations.environment import FrozenTools
from evaluations.graders import grade, opening_check
from evaluations.io import read
from evaluations.models import Experiment
from evaluations.runner import execute

DATA = Path(__file__).resolve().parents[1] / "evaluations" / "data"


@pytest.mark.parametrize("strategy", ["B", "C"])
def test_contradiction_preflight_executes_without_model(strategy):
    task = next(t for t in build() if t.category == "infeasible")
    run = execute(task, read(DATA / "snapshot.json"), Experiment(), strategy, replay=[])
    assert run["grades"]["task_success"] == "pass"
    assert run["decision"]["source"] == "preflight"
    assert run["error"] is None
    assert run["llm_calls"] == run["tool_calls"] == []
    assert run["usage"]["total_tokens"] == 0


def test_wrong_conflict_rejected():
    task = next(t for t in build() if t.category == "infeasible")
    decision = contradiction(task)
    decision["conflicts"][0]["min"] = 3
    assert not judge_decision(task, decision, {}, [])


def test_clarification_requires_exact_search_and_absent_evidence():
    snapshot = read(DATA / "snapshot.json")
    task = next(t for t in build() if t.expected_response == "clarification")
    name = next(c.value[0] for c in task.constraints if c.kind == "required_pois")
    decision = {"response_type": "clarification", "reason": "目录查询未找到对应地点", "question": "请确认名称或补充地址。", "missing_pois": [name], "conflicts": [], "source": "preflight"}
    tools = FrozenTools(snapshot)
    assert not judge_decision(task, decision, snapshot, tools.calls)
    tools("amap_search_poi", {"city": task.request.city, "keywords": name})
    assert judge_decision(task, decision, snapshot, tools.calls)
    assert grade(task, {"decision": decision, "tool_calls": tools.calls}, snapshot)["task_success"] == "pass"
    wrong = copy.deepcopy(decision)
    wrong["missing_pois"] = ["故宫博物院"]
    assert not judge_decision(task, wrong, snapshot, tools.calls)


def test_arbitrary_refusal_does_not_pass_normal_task():
    task = build()[0]
    decision = {"response_type": "clarification", "reason": "不想规划", "question": "换个要求？", "missing_pois": [], "conflicts": [], "source": "model"}
    assert grade(task, {"decision": decision}, read(DATA / "snapshot.json"))["task_success"] == "fail"


def test_legacy_refusal_requires_explicit_relationships():
    assert parse_decision({"infeasible": True, "reason": "4和1", "days": []}) is None
    assert parse_decision({"infeasible": True, "reason": "每天至少4个且最多1个有冲突", "days": []})["response_type"] == "infeasible"
    assert parse_decision({"infeasible": True, "reason": "每天至少4个且最多1个有冲突", "days": [{}]}) is None


def test_time_violation_is_known_without_opening_evidence():
    plan = {"city": "北京", "days": [{"date": "2026-10-26", "attractions": [{"name": "故宫博物院", "visit_duration": 120}]}]}
    raw = copy.deepcopy(plan)
    raw["days"][0]["attractions"][0].update(start_time="08:00", end_time="10:00")
    assert opening_check(plan, raw, read(DATA / "snapshot.json"), ["09:00", "17:00"]) == "fail"
    raw["days"][0]["attractions"][0].update(start_time="10:00", end_time="11:00")
    assert opening_check(plan, raw, read(DATA / "snapshot.json"), ["09:00", "17:00"]) == "fail"


def test_continuous_fault_does_not_clear_on_second_attempt():
    from app.services.amap_service import AmapError

    snapshot = read(DATA / "snapshot.json")
    tools = FrozenTools(snapshot, {"kind": "network", "tool": "amap_search_poi", "occurrences": [1, 2, 3]})
    for _ in range(3):
        with pytest.raises(AmapError):
            tools("amap_search_poi", {"city": "北京", "keywords": "景点"})
    assert tools("amap_search_poi", {"city": "北京", "keywords": "景点"})["count"] > 0


def test_unsupported_budget_refusal_rejected_before_delivery():
    from app.agents.trip_planner_agent import PlanResult
    from evaluations.agent import ConstraintError, ReplayLLM, StrategyPlanner

    task = next(t for t in build() if t.category == "budget")
    snapshot = read(DATA / "snapshot.json")
    planner = StrategyPlanner(task, snapshot, "C", ReplayLLM([], Experiment(), "C"), FrozenTools(snapshot))
    raw = {"response_type": "infeasible", "reason": "预算太低", "question": "增加预算？", "missing_pois": [], "conflicts": []}
    with pytest.raises(ConstraintError, match="缺少可验证证据"):
        planner._validate(raw, task.request, PlanResult(plan=None))


def test_model_cannot_claim_preflight_origin():
    raw = {"response_type": "infeasible", "reason": "数量冲突", "question": "修改数量？", "source": "preflight", "conflicts": [{"kind": "daily_count", "min": 4, "max": 1}]}
    assert parse_decision(raw)["source"] == "model"

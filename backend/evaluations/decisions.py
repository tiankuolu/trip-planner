import re

from pydantic import ValidationError

from .models import Decision


class DecisionResponse(RuntimeError):
    def __init__(self, decision):
        self.decision = Decision.model_validate(decision).model_dump()
        super().__init__(self.decision["reason"])


def parse_decision(raw):
    if not isinstance(raw, dict) or raw.get("days"):
        return None
    if raw.get("infeasible") is True and isinstance(raw.get("reason"), str):
        minimum = re.search(r"至少\s*(\d+)", raw["reason"])
        maximum = re.search(r"最多\s*(\d+)", raw["reason"])
        if minimum and maximum:
            return Decision(response_type="infeasible", reason=raw["reason"], question="请修改每天景点数量的要求。",
                            conflicts=[{"kind": "daily_count", "min": int(minimum[1]), "max": int(maximum[1])}]).model_dump()
    if raw.get("response_type") not in {"infeasible", "clarification"}:
        return None
    try:
        decision = Decision.model_validate(raw).model_dump()
        decision["source"] = "model"
        return decision
    except ValidationError:
        return None


def contradiction(task):
    for constraint in task.constraints:
        if constraint.kind == "infeasible" and constraint.value["min"] > constraint.value["max"]:
            minimum, maximum = constraint.value["min"], constraint.value["max"]
            return Decision(response_type="infeasible", reason=f"每天至少{minimum}个景点与每天最多{maximum}个景点无法同时满足。",
                            question="请修改每天景点数量的要求。", conflicts=[{"kind": "daily_count", "min": minimum, "max": maximum}], source="preflight").model_dump()
    return None


def judge_decision(task, decision, snapshot, calls):
    expected = task.expected_response
    if expected == "plan" and any(c.kind == "infeasible" for c in task.constraints):
        expected = "infeasible"
    if not decision or decision["response_type"] != expected:
        return False
    if expected == "infeasible":
        target = next((c.value for c in task.constraints if c.kind == "infeasible"), None)
        return bool(target and target["min"] > target["max"] and any(
            c.get("kind") == "daily_count" and c.get("min") == target["min"] and c.get("max") == target["max"]
            for c in decision["conflicts"]))
    required = next((c.value for c in task.constraints if c.kind == "required_pois"), [])
    names = {name for p in snapshot["cities"].get(task.request.city, {}).get("pois", []) for name in [p["name"], *p.get("aliases", [])]}
    missing = {name for name in required if name not in names}
    searched = {c["arguments"].get("keywords") for c in calls if c["tool"] == "amap_search_poi"
                and c["arguments"].get("city") == task.request.city and not c.get("error") and not c.get("invalid_response")}
    return bool(missing and set(decision["missing_pois"]) == missing and missing <= searched)

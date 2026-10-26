from datetime import date, time

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.agents.tools import TOOLS
from app.models.schemas import TripPlan

from .decisions import judge_decision, parse_decision
from .models import Check


def aggregate(statuses):
    statuses = list(statuses)
    if "fail" in statuses:
        return "fail"
    if not statuses or "unverifiable" in statuses:
        return "unverifiable"
    return "pass"


def check(name, state, source, detail=None):
    status = state if isinstance(state, str) else "pass" if state else "fail"
    return Check(name=name, status=status, source=source, detail=detail).model_dump()


def catalog(snapshot, city):
    return snapshot["cities"].get(city, {}).get("pois", [])


def match(attraction, pois):
    return next((p for p in pois if attraction.get("name") in [p["name"], *p.get("aliases", [])]
                 and (not attraction.get("poi_id") or attraction["poi_id"] == p["id"])), None)


def grade_pois(plan, snapshot):
    checks = []
    trusted = snapshot.get("provenance", {}).get("kind") == "recorded_api"
    for day in (plan or {}).get("days", []):
        for attraction in day.get("attractions", []):
            poi = match(attraction, catalog(snapshot, plan["city"]))
            status = "unverifiable" if not trusted else "pass" if poi else "fail"
            if poi and attraction.get("location"):
                loc = attraction["location"]
                if any(abs(loc[k] - poi["location"][k]) > 0.001 for k in ["longitude", "latitude"]):
                    status = "fail"
            checks.append(check(attraction["name"], status, "snapshot.cities.pois", {"date": day["date"], "matched_id": poi["id"] if poi else None}))
    return checks


def opening_check(plan, raw, snapshot, window=None):
    statuses = []
    evidence = snapshot.get("evidence", {}).get("opening", {})
    for day in plan["days"]:
        for attraction in day["attractions"]:
            interval = None
            if window is not None:
                source_day = next((d for d in raw.get("days", []) if d.get("date") == day["date"]), {})
                visit = next((a for a in source_day.get("attractions", []) if a.get("name") == attraction["name"]), {})
                if not all(visit.get(k) for k in ["start_time", "end_time"]):
                    statuses.append("unverifiable")
                    continue
                try:
                    start, end = time.fromisoformat(visit["start_time"]), time.fromisoformat(visit["end_time"])
                except (ValueError, TypeError):
                    statuses.append("fail")
                    continue
                minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
                if not (time.fromisoformat(window[0]) <= start < end <= time.fromisoformat(window[1])) or minutes < attraction.get("visit_duration", 0):
                    statuses.append("fail")
                    continue
                interval = start, end
            poi = match(attraction, catalog(snapshot, plan["city"]))
            record = evidence.get(poi["id"] if poi else "", {}).get(day["date"])
            if not record or not record.get("source"):
                statuses.append("unverifiable")
                continue
            if not record["open"]:
                statuses.append("fail")
                continue
            if window is None:
                statuses.append("pass")
                continue
            if not record.get("hours"):
                statuses.append("unverifiable")
                continue
            start, end = interval
            opened = any(time.fromisoformat(a) <= start < end <= time.fromisoformat(b) for a, b in record["hours"])
            statuses.append("pass" if opened else "fail")
    return aggregate(statuses)


def budget_check(plan, raw, snapshot, limit):
    budget = plan.get("budget")
    if budget and (budget["total"] > limit or budget["total"] < 0):
        return "fail"
    prices = snapshot.get("evidence", {}).get("prices", {})
    people, rooms = raw.get("travelers"), raw.get("rooms")
    if not isinstance(people, int) or people < 1 or not isinstance(rooms, int) or rooms < 1:
        return "unverifiable"
    total = 0
    for index, day in enumerate(plan["days"]):
        quotes = prices.get(day["date"], {})
        if not quotes.get("source"):
            return "unverifiable"
        for attraction in day["attractions"]:
            poi = match(attraction, catalog(snapshot, plan["city"]))
            price = quotes.get("tickets", {}).get(poi["id"] if poi else "")
            if price is None:
                return "unverifiable"
            total += price * people
        if index < len(plan["days"]) - 1:
            price = quotes.get("hotels", {}).get((day.get("hotel") or {}).get("name"))
            if price is None:
                return "unverifiable"
            total += price * rooms
        for kind in ["meals_per_person", "transport_per_person"]:
            if quotes.get(kind) is None:
                return "unverifiable"
            total += quotes[kind] * people
    return "pass" if total <= limit else "fail"


def grade_constraints(task, plan, raw, snapshot):
    checks = []
    for constraint in task.constraints:
        if not constraint.hard:
            continue
        kind, value = constraint.kind, constraint.value
        state = "fail"
        if kind == "infeasible":
            reason = str(raw.get("reason", ""))
            state = raw.get("infeasible") is True and not raw.get("days") and all(str(value[k]) in reason for k in ["min", "max"])
        elif plan:
            days = plan["days"]
            attractions = [a for d in days for a in d["attractions"]]
            if kind == "city":
                state = plan["city"] == value
            elif kind == "dates":
                state = [d["date"] for d in days] == value and plan["start_date"] == value[0] and plan["end_date"] == value[-1]
            elif kind == "daily_count":
                state = bool(days) and all(value[0] <= len(d["attractions"]) <= value[1] and len({a["name"] for a in d["attractions"]}) == len(d["attractions"]) for d in days)
            elif kind == "required_pois":
                names = {a["name"] for a in attractions}
                state = all(name in names or any(name in p.get("aliases", []) and p["name"] in names for p in catalog(snapshot, plan["city"])) for name in value)
            elif kind == "travelers":
                state = raw["travelers"] == value if "travelers" in raw else "unverifiable"
            elif kind == "budget":
                state = budget_check(plan, raw, snapshot, value)
            elif kind in {"opening", "visit_window"}:
                state = opening_check(plan, raw, snapshot, value if kind == "visit_window" else None)
            elif kind == "hotel":
                state = all(d.get("hotel") and any(h["name"] == d["hotel"]["name"] for h in snapshot["cities"][plan["city"]]["hotels"]) for d in days[:-1])
            elif kind == "weather":
                forecasts = snapshot["cities"][plan["city"]]["forecasts"]
                state = "unverifiable" if any(d["date"] not in {w["date"] for w in forecasts} for d in days) else all(w in forecasts for w in plan["weather_info"])
        checks.append(check(kind, state, constraint.source, {"expected": value, "rule": constraint.rule}))
    return checks


def grade_tools(calls, task):
    checks = []
    for index, call in enumerate(calls):
        name, args = call["tool"], call["arguments"]
        known = name in TOOLS
        selected = call.get("selected_tool", name)
        checks.append(check(f"selection:{index}", known and selected == name, f"tool_calls[{index}]", selected))
        errors = list(Draft202012Validator(TOOLS[name]["schema"]["function"]["parameters"]).iter_errors(args)) if known else []
        valid = known and isinstance(args, dict) and not errors and args.get("city") == task.request.city
        if name == "amap_search_poi":
            valid = valid and isinstance(args.get("keywords"), str) and bool(args["keywords"].strip())
        checks.append(check(f"arguments:{index}", valid, f"tool_calls[{index}].arguments", [e.message for e in errors]))
        checks.append(check(f"execution:{index}", not call.get("error") and not call.get("invalid_response"), f"tool_calls[{index}].response", call.get("error")))
    return checks


def grade(task, run, snapshot):
    plan, raw = run.get("plan") or run.get("candidate_plan"), run.get("raw_plan") or {}
    malformed = False
    if not isinstance(raw, dict):
        raw = {}
        malformed = True
    if plan:
        try:
            TripPlan.model_validate(plan)
            for day in plan["days"]:
                date.fromisoformat(day["date"])
        except (ValidationError, ValueError, TypeError, KeyError):
            plan = None
            malformed = True
    constraints = grade_constraints(task, plan, raw, snapshot)
    pois = grade_pois(plan, snapshot)
    tools = grade_tools(run.get("tool_calls", []), task)
    decision = run.get("decision") or parse_decision(raw)
    if decision is not None:
        justified = not plan and not raw.get("days") and judge_decision(task, decision, snapshot, run.get("tool_calls", []))
        constraints = [check("response_policy", justified, "task.expected_response + decision + tool trace", decision)]
    refusal = any(c.kind == "infeasible" for c in task.constraints)
    valid = bool(plan and plan["days"] and all(d["attractions"] for d in plan["days"]))
    success = aggregate([c["status"] for c in constraints] + ([] if refusal else ["pass" if valid else "fail", aggregate(c["status"] for c in pois)]))
    if decision is not None:
        success = "pass" if justified and not run.get("error") else "fail"
    elif task.expected_response == "clarification":
        success = "fail"
    if run.get("error") and not refusal:
        success = "unverifiable" if run["error"].get("type") == "SnapshotMiss" else "fail"
    if malformed:
        success = "fail"
    faults = [c for c in run.get("tool_calls", []) if c.get("fault")]
    recovered = bool(faults and success == "pass")
    retried = any(c.get("recovery_attempt", False) for c in run.get("tool_calls", []))
    errors = set()
    if any(c["status"] == "fail" for c in constraints):
        errors.add("constraint_violation")
    if any(c["status"] == "fail" for c in pois):
        errors.add("invalid_poi")
    for c in tools:
        if c["status"] == "fail" and c["name"].startswith(("selection:", "arguments:")):
            errors.add("tool_selection_error" if c["name"].startswith("selection:") else "argument_error")
    if not valid and not ((refusal or decision is not None) and success == "pass"):
        errors.add("planning_failure")
    if faults and success == "fail":
        errors.add("recovery_failure")
    if any(c.get("fault") == "timeout" for c in faults) or (run.get("error") or {}).get("type") == "TimeoutError":
        errors.add("timeout")
    if success == "unverifiable" or any(c["status"] == "unverifiable" for c in constraints + pois):
        errors.add("unverifiable")
    return {"task_success": success, "constraints": constraints, "poi_validity": pois, "tool_calls": tools,
            "response_type": decision["response_type"] if decision else "infeasible" if refusal and success == "pass" else "plan",
            "robustness": {"fault_exposed": bool(faults), "recovered": recovered, "recovery_status": success if faults else "not_applicable", "strategy_retry_observed": retried, "infrastructure_retries": 0},
            "errors": sorted(errors)}

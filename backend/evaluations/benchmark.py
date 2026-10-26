from datetime import date, timedelta

from .models import Constraint, Task


def build(snapshot="amap-frozen-v1"):
    tasks = []
    categories = ["basic", "multi_day", "budget", "required_pois", "daily_count", "opening", "infeasible", "faults", "time_window", "people"]
    for category in categories:
        for variant in range(10):
            city = ["北京", "杭州"][variant % 2]
            landmark = {"北京": "故宫博物院", "杭州": "西湖"}[city]
            start = date(2026, 10, 26) + timedelta(days=variant)
            days = 1 if category == "basic" else 2 + variant % 3
            end = start + timedelta(days=days - 1)
            constraints = [
                Constraint(kind="city", value=city, source="final_plan.city", rule="等于请求城市"),
                Constraint(kind="dates", value=[(start + timedelta(days=n)).isoformat() for n in range(days)], source="final_plan.days[].date", rule="日期依次等于完整请求日期集合"),
                Constraint(kind="daily_count", value=[1, 3], source="final_plan.days[].attractions", rule="每天有1至3个不重复景点"),
            ]
            extra = "每天安排1至3个不同景点。"
            expected_response = "plan"
            if days > 1:
                constraints.append(Constraint(kind="hotel", value=True, source="final_plan.days[].hotel + snapshot.cities.hotels", rule="每个住宿晚对应的酒店必须匹配冻结目录"))
            fault = None
            if category == "budget":
                amount = [300, 800, 1500, 2500, 4000][variant // 2]
                constraints.append(Constraint(kind="budget", value=amount, source="snapshot.evidence.prices + raw_plan.travelers + final_plan", rule="按日期、人数和房间数计算所有费用；任何缺失报价为无法验证"))
                extra += f"全部费用合计不超过{amount}元。"
            if category in {"required_pois", "opening", "time_window"}:
                if category == "required_pois" and variant >= 8:
                    landmark = "不存在的星际旅行博物馆"
                    expected_response = "clarification"
                constraints.append(Constraint(kind="required_pois", value=[landmark], source="final_plan.attractions + snapshot POI", rule="必选地点精确匹配冻结目录名称或有证据的别名"))
                extra += f"必须游览{landmark}。"
                if expected_response == "clarification":
                    extra += "如果查询不到，请说明查询范围并询问我是否需要更正名称，不要编造地点或替换成其他地点。"
            if category == "daily_count":
                count = 1 + variant % 4
                constraints[2].value = [count, count]
                constraints[2].rule = f"每天恰好有{count}个不重复景点"
                extra = f"每天必须安排{count}个不同景点。"
            if category == "opening":
                constraints.append(Constraint(kind="opening", value=True, source="snapshot.evidence.opening + final_plan.days", rule="每个到访日期必须有开放证据并处于开放日期内"))
                extra += "所有景点必须在到访日期开放。"
            if category == "time_window":
                constraints.append(Constraint(kind="visit_window", value=["09:00", "17:00"], source="raw_plan.days[].attractions[].start_time/end_time + snapshot.evidence.opening", rule="明确到访时间，处于用户时间范围及官方开放时间内"))
                extra += "仅在09:00至17:00游览，请提供每个景点的开始时间和结束时间。"
            if category == "people":
                people = 2 + variant
                constraints.append(Constraint(kind="travelers", value=people, source="raw_plan.travelers", rule="显式人数等于用户人数"))
                extra += f"共有{people}位成人，请在JSON的travelers字段注明人数。"
            if category == "infeasible":
                expected_response = "infeasible"
                extra = "每天必须至少安排4个景点，同时每天最多安排1个景点。若无法同时满足，请明确说明冲突。"
                constraints = [Constraint(kind="infeasible", value={"min": 4, "max": 1}, source="request contradiction + raw_plan.infeasible/reason", rule="标记infeasible=true并说明每天至少4个和最多1个之间的冲突；不得提供假装满足的行程")]
            if category == "faults":
                kinds = ["timeout", "weather_error", "hotel_empty", "poi_empty", "invalid_fields", "network", "timeout", "hotel_empty", "poi_empty", "network"]
                kind = kinds[variant]
                tool = {"weather_error": "amap_get_weather", "hotel_empty": "amap_search_hotel"}.get(kind, "amap_search_poi")
                fault = {"kind": kind, "tool": tool, "occurrences": [1]}
                if variant >= 6:
                    fault["occurrences"] = [1, 2, 3]
            query = f"请规划{city}{days}日旅行，日期从{start}至{end}，公共交通、经济型住宿。{extra}希望路线舒适、景点类型丰富。"
            tasks.append(Task(task_id=f"{category}-{variant + 1:02d}", category=category,
                              difficulty="easy" if category == "basic" else "hard" if category in {"infeasible", "faults", "time_window"} else "medium",
                              user_query=query, request={"city": city, "start_date": str(start), "end_date": str(end), "travel_days": days,
                              "transportation": "公共交通", "accommodation": "经济型酒店", "preferences": [], "free_text_input": query},
                              constraints=constraints, soft_preferences=["路线舒适", "景点多样性"], environment_snapshot=snapshot, fault=fault,
                              expected_response=expected_response))
    return tasks


def load(data):
    tasks = [Task.model_validate(row) for row in data]
    if len({t.task_id for t in tasks}) != len(tasks):
        raise ValueError("task_id 重复")
    return tasks

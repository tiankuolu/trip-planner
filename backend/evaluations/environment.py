import copy
import time
from collections import Counter

from app.services.amap_service import AmapError


class SnapshotMiss(RuntimeError):
    pass


class FrozenTools:
    def __init__(self, snapshot, fault=None):
        self.snapshot = copy.deepcopy(snapshot)
        self.fault = fault
        self.calls = []
        self.counts = Counter()
        self.recovery_attempt = False
        self.checkpoint = lambda: None
        self.selected_tool = lambda: "unobserved"

    def __call__(self, name, arguments):
        started = time.perf_counter()
        self.counts[name] += 1
        row = {"tool": name, "arguments": copy.deepcopy(arguments), "response": None, "error": None,
               "occurrence": self.counts[name], "recovery_attempt": self.recovery_attempt, "selected_tool": self.selected_tool()}
        self.calls.append(row)
        try:
            data = self._lookup(name, arguments)
            fault = self.fault
            if fault and name == fault["tool"] and self.counts[name] in fault["occurrences"]:
                row["fault"] = fault["kind"]
                if fault["kind"] in {"timeout", "network", "weather_error"}:
                    raise AmapError(f"injected:{fault['kind']}")
                if fault["kind"] == "invalid_fields":
                    data = {"count": "invalid", "pois": [{"name": None}]}
                    row["invalid_response"] = True
                else:
                    key = "hotels" if fault["kind"] == "hotel_empty" else "pois"
                    data = {"count": 0, key: []}
            row["response"] = copy.deepcopy(data)
            return data
        except Exception as exc:
            row["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            row["latency_ms"] = (time.perf_counter() - started) * 1000
            self.checkpoint()

    def _lookup(self, name, args):
        city = args.get("city", "").removesuffix("市")
        if city not in self.snapshot["cities"]:
            raise SnapshotMiss(f"冻结目录没有城市：{city}")
        data = self.snapshot["cities"][city]
        if name == "amap_search_poi":
            keyword = args.get("keywords", "")
            pois = data["pois"]
            if keyword != "景点":
                pois = [p for p in pois if keyword in p["name"] or keyword in p.get("type", "") or keyword in p.get("aliases", [])]
            return {"city": city, "keywords": keyword, "count": len(pois), "pois": copy.deepcopy(pois)}
        if name == "amap_search_hotel":
            hotels = data["hotels"]
            if args.get("brand"):
                hotels = [h for h in hotels if args["brand"] in h["name"]]
            if args.get("near"):
                raise SnapshotMiss("冻结目录不包含地标距离检索证据")
            return {"city": city, "count": len(hotels), "hotels": copy.deepcopy(hotels)}
        if name == "amap_get_weather":
            return {"city": city, "count": len(data["forecasts"]), "forecasts": copy.deepcopy(data["forecasts"])}
        raise SnapshotMiss(f"冻结目录没有工具：{name}")

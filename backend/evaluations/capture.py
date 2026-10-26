from datetime import datetime, timezone

from app.agents.tools import execute_tool

from .io import digest


def capture():
    snapshot = {"snapshot_id": "amap-frozen-v1", "provenance": {"kind": "recorded_api", "provider": "amap", "captured_at": datetime.now(timezone.utc).isoformat(),
                "search_policy": "exact substring in frozen name/type/aliases; 景点 returns full catalog; city scoped; hotel brand substring"},
                "cities": {}, "evidence": {"prices": {}, "opening": {}}, "recordings": []}
    for city, landmark in [("北京", "故宫博物院"), ("杭州", "西湖")]:
        pois = {}
        for keyword in ["景点", "博物馆", "公园", landmark]:
            args = {"city": city, "keywords": keyword}
            response = execute_tool("amap_search_poi", args)
            snapshot["recordings"].append({"tool": "amap_search_poi", "arguments": args, "response": response})
            for poi in response["pois"]:
                pois[poi["id"]] = poi
        hotel = execute_tool("amap_search_hotel", {"city": city})
        weather = execute_tool("amap_get_weather", {"city": city})
        snapshot["recordings"].extend([
            {"tool": "amap_search_hotel", "arguments": {"city": city}, "response": hotel},
            {"tool": "amap_get_weather", "arguments": {"city": city}, "response": weather},
        ])
        snapshot["cities"][city] = {"pois": list(pois.values()), "hotels": hotel["hotels"], "forecasts": weather["forecasts"]}
    snapshot["content_hash"] = digest(snapshot)
    return snapshot

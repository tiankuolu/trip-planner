import html
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

from .io import write


def rows(directory):
    with sqlite3.connect(Path(directory) / "results.sqlite") as db:
        return [json.loads(r[0]) for r in db.execute("SELECT result_json FROM runs ORDER BY rowid")]


def ratio(passed, total):
    return passed / total if total else None


def summarize(results):
    success = Counter(r["grades"]["task_success"] for r in results)
    constraints = Counter(c["status"] for r in results for c in r["grades"]["constraints"])
    pois = Counter(c["status"] for r in results for c in r["grades"]["poi_validity"])
    calls = [c for r in results for c in r["tool_calls"]]
    fault_runs = [r for r in results if r["grades"]["robustness"]["fault_exposed"]]
    verified_fault_runs = [r for r in fault_runs if r["grades"]["task_success"] != "unverifiable"]
    plan_runs = [r for r in results if r.get("expected_response", "plan") == "plan"]
    decision_runs = [r for r in results if r.get("expected_response", "plan") != "plan"]
    error_counts = Counter(e for r in results for e in r["grades"]["errors"])
    tokens = [r["usage"]["total_tokens"] for r in results]
    costs = [r["cost"] for r in results]
    return {"samples": len(results), "verifiable_samples": success["pass"] + success["fail"], "unverifiable_samples": success["unverifiable"],
            "model_invoked_samples": sum(bool(r.get("llm_calls")) for r in results),
            "preflight_only_samples": sum(not r.get("llm_calls") and bool(r.get("decision")) for r in results),
            "task_success_rate": ratio(success["pass"], success["pass"] + success["fail"]),
            "success_fraction_all": ratio(success["pass"], len(results)),
            "constraint_satisfaction_rate": ratio(constraints["pass"], constraints["pass"] + constraints["fail"]),
            "constraint_counts": dict(constraints), "poi_validity_rate": ratio(pois["pass"], pois["pass"] + pois["fail"]), "poi_counts": dict(pois),
            "tool_error_rate": ratio(sum(bool(c.get("error") or c.get("invalid_response")) for c in calls), len(calls)),
            "fault_exposed_samples": len(fault_runs), "recovery_unverifiable_samples": len(fault_runs) - len(verified_fault_runs),
            "recovery_rate": ratio(sum(r["grades"]["robustness"]["recovered"] for r in verified_fault_runs), len(verified_fault_runs)),
            "response_counts": dict(Counter(r["grades"].get("response_type", "plan") for r in results)),
            "decision_sources": dict(Counter((r.get("decision") or {}).get("source", "none") for r in results)),
            "plan_task_success_rate": ratio(sum(r["grades"]["task_success"] == "pass" for r in plan_runs), sum(r["grades"]["task_success"] != "unverifiable" for r in plan_runs)),
            "decision_task_success_rate": ratio(sum(r["grades"]["task_success"] == "pass" for r in decision_runs), len(decision_runs)),
            "average_wall_latency_ms": mean(r.get("wall_latency_ms", r["latency_ms"]) for r in results) if results else None,
            "average_latency_ms": mean(r["latency_ms"] for r in results) if results else None,
            "average_tool_calls": ratio(len(calls), len(results)),
            "token_usage": sum(tokens) if tokens and all(v is not None for v in tokens) else None,
            "token_observed_samples": sum(v is not None for v in tokens),
            "cost": sum(costs) if costs and all(v is not None for v in costs) else None,
            "error_counts": dict(error_counts), "error_proportions": {e: n / len(results) for e, n in error_counts.items()}}


def report(directory):
    directory = Path(directory)
    results = rows(directory)
    groups = defaultdict(list)
    faults = defaultdict(list)
    categories = defaultdict(list)
    for result in results:
        groups[result["strategy"]].append(result)
        fault = result.get("fault") or {}
        pattern = "persistent" if len(fault.get("occurrences", [])) > 1 else "transient"
        faults[f"{result['strategy']}:{fault.get('kind', 'none')}:{pattern}"].append(result)
        categories[f"{result['strategy']}:{result['category']}"].append(result)
    summary = {"mode": sorted({r["mode"] for r in results}), "strategies": {k: summarize(v) for k, v in groups.items()},
               "fault_conditions": {k: summarize(v) for k, v in faults.items()}, "categories": {k: summarize(v) for k, v in categories.items()}, "sample_count": len(results)}
    write(directory / "summary.json", summary)
    columns = ["samples", "model_invoked_samples", "verifiable_samples", "unverifiable_samples", "success_fraction_all", "task_success_rate", "plan_task_success_rate", "decision_task_success_rate", "constraint_satisfaction_rate", "poi_validity_rate", "tool_error_rate", "recovery_rate", "average_latency_ms", "average_wall_latency_ms", "average_tool_calls", "token_usage", "cost"]
    def table(values):
        header = "<tr><th>指标</th>" + "".join(f"<th>{html.escape(key)}</th>" for key in values) + "</tr>"
        body = []
        for key in columns:
            cells = []
            for metrics in values.values():
                value = metrics[key]
                cells.append(f"<td>{'unknown' if value is None else format(value, '.4g') if isinstance(value, float) else value}</td>")
            body.append(f"<tr><th>{html.escape(key)}</th>{''.join(cells)}</tr>")
        return "<div class='scroll'><table>" + header + "".join(body) + "</table></div>"
    traces = []
    for result in results:
        visible = {k: v for k, v in result.items() if k not in {"http_calls", "llm_calls"}}
        traces.append(f"<details data-errors='{html.escape(' '.join(result['grades']['errors']))}'><summary>{html.escape(result['run_id'])} · {result['grades']['task_success']} · {html.escape(', '.join(result['grades']['errors']))}</summary><pre>{html.escape(json.dumps(visible, ensure_ascii=False, indent=2))}</pre></details>")
    markup = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Agent 评测报告</title>
<style>body{font:15px system-ui;margin:36px;color:#203047;background:#f5f7fb}table{border-collapse:collapse;background:white}td,th{padding:12px;border:1px solid #ccd5e0;text-align:left}h1{font-size:28px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#e8eef4;padding:18px}details{margin:12px 0;padding:12px;background:white}.scroll{overflow:auto}select{padding:8px}</style>
<h1>旅行规划 Agent 评测报告</h1><p>真实模型 + 冻结高德工具环境。成功率分母为可验证样本；unknown 表示缺少证据或价格。故障恢复率仅统计实际遇到注入故障的执行。多次重复属于同一任务，不能视作独立任务样本。</p>"""
    markup += table(summary["strategies"]) + "<h2>故障条件</h2>" + table(summary["fault_conditions"])
    markup += "<h2>错误分类</h2><pre>" + html.escape(json.dumps({k: {"counts": v["error_counts"], "proportions": v["error_proportions"]} for k, v in summary["strategies"].items()}, ensure_ascii=False, indent=2)) + "</pre>"
    markup += "<h2>行程、工具轨迹与评分证据</h2><p>完整模型请求与响应保存在证据包中的逐次JSON记录。</p><select id='filter'><option value=''>全部</option>" + "".join(f"<option>{e}</option>" for e in ["constraint_violation", "invalid_poi", "tool_selection_error", "argument_error", "planning_failure", "recovery_failure", "timeout", "unverifiable"]) + "</select>"
    markup += "".join(traces) + "<script>document.querySelector('#filter').onchange=e=>document.querySelectorAll('details').forEach(d=>d.hidden=!!e.target.value&&!d.dataset.errors.split(' ').includes(e.target.value))</script></html>"
    (directory / "report.html").write_text(markup, encoding="utf-8")
    return summary

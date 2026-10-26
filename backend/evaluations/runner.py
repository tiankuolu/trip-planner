import importlib.metadata
import json
import multiprocessing
import platform
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.agents import trip_planner_agent
from app.observability import bind_task

from . import VERSION
from .agent import DECISION_PROMPT, STRICT_PROMPT, RecordedLLM, ReplayLLM, StrategyPlanner
from .decisions import DecisionResponse
from .environment import FrozenTools
from .graders import grade
from .io import digest, read, write
from .models import Experiment, Task


def execute(task, snapshot, config, strategy, replay=None, checkpoint_path=None, planner_factory=StrategyPlanner, llm_factory=None):
    environment = FrozenTools(snapshot, task.fault)
    if llm_factory is not None:
        llm = llm_factory(replay, config, strategy)
    else:
        llm = ReplayLLM(replay, config, strategy) if replay is not None else RecordedLLM(config, strategy)
    result = {"task_id": task.task_id, "strategy": strategy, "category": task.category, "fault": task.fault,
              "plan": None, "error": None, "steps": [], "tool_calls": environment.calls,
              "mode": "recorded_model_replay" if replay is not None else "live_model_frozen_tools"}
    result["decision"] = None
    result["expected_response"] = task.expected_response
    def checkpoint():
        if checkpoint_path is not None:
            write(checkpoint_path, {**result, "llm_calls": llm.calls, "http_calls": llm.http_calls, "raw_plan": llm.raw_plan})

    llm.checkpoint = checkpoint
    environment.checkpoint = checkpoint
    environment.selected_tool = lambda: llm.selected_tool
    started = time.perf_counter()
    planner = planner_factory(task, snapshot, strategy, llm, environment)
    try:
        with bind_task(f"eval-{task.task_id}-{uuid4().hex[:8]}"):
            planned = planner.plan_trip(task.request, on_step=result["steps"].append)
        result["plan"] = planned.plan.model_dump()
        result["warnings"] = planned.warnings
    except DecisionResponse as exc:
        result["decision"] = exc.decision
    except Exception as exc:
        # 实验边界记录失败；异常终止当前 Agent，不生成替代行程。
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if replay is None:
            llm.client.close()
    result["latency_ms"] = (time.perf_counter() - started) * 1000
    result["raw_plan"] = llm.raw_plan
    result["candidate_plan"] = planner.candidate_plan
    result["evidence_gaps"] = planner.evidence_gaps
    result["llm_calls"] = llm.calls
    result["http_calls"] = llm.http_calls
    result["model"] = getattr(llm, "model", "recorded")
    usage_rows = [(c.get("response") or {}).get("usage") for c in llm.http_calls]
    known = (bool(usage_rows) and all(u is not None for u in usage_rows)) or (not llm.calls and bool(result["decision"]))
    result["usage"] = {key: sum(u.get(key, 0) for u in usage_rows) if known else None for key in ["prompt_tokens", "completion_tokens", "total_tokens"]}
    result["cost"] = None
    if known and config.input_cost_per_million is not None:
        result["cost"] = (result["usage"]["prompt_tokens"] * config.input_cost_per_million + result["usage"]["completion_tokens"] * config.output_cost_per_million) / 1_000_000
    result["grades"] = grade(task, result, snapshot)
    return result


def child(task_data, snapshot, config_data, strategy, output, planner_factory, llm_factory):
    result = execute(Task.model_validate(task_data), snapshot, Experiment.model_validate(config_data), strategy,
                     checkpoint_path=output + ".partial", planner_factory=planner_factory, llm_factory=llm_factory)
    write(output, result)


def isolated(task, snapshot, config, strategy, output, planner_factory=StrategyPlanner, llm_factory=None):
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=child, args=(task.model_dump(), snapshot, config.model_dump(), strategy, str(output), planner_factory, llm_factory))
    started = time.perf_counter()
    process.start()
    process.join(config.timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join()
        result = {"task_id": task.task_id, "strategy": strategy, "category": task.category, "fault": task.fault,
                  "expected_response": task.expected_response, "decision": None,
                  "mode": "live_model_frozen_tools", "plan": None, "raw_plan": None, "tool_calls": [], "steps": [], "llm_calls": [],
                  "error": {"type": "TimeoutError", "message": "Agent执行超过实验时间限制；未完成调用费用未知"},
                  "latency_ms": (time.perf_counter() - started) * 1000, "usage": {"total_tokens": None, "prompt_tokens": None, "completion_tokens": None}, "cost": None}
        partial = Path(str(output) + ".partial")
        if partial.exists():
            observed = read(partial)
            for key in ["tool_calls", "steps", "llm_calls", "http_calls", "raw_plan"]:
                result[key] = observed.get(key, result.get(key))
        result["grades"] = grade(task, result, snapshot)
        write(output, result)
        return result
    if process.exitcode:
        raise RuntimeError(f"评测子进程异常终止：{process.exitcode}")
    result = read(output)
    result["wall_latency_ms"] = (time.perf_counter() - started) * 1000
    return result


def run(tasks, snapshot, config, output, selected=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    if snapshot.get("provenance", {}).get("kind") != "recorded_api":
        raise ValueError("真实模型实验需要已录制的外部数据")
    if snapshot.get("content_hash") != digest({k: v for k, v in snapshot.items() if k != "content_hash"}):
        raise ValueError("快照内容校验失败")
    if any(t.environment_snapshot != snapshot["snapshot_id"] for t in tasks):
        raise ValueError("Benchmark和快照ID不一致")
    task_data = [t.model_dump() for t in tasks]
    source = {p.as_posix(): p.read_text(encoding="utf-8") for folder in [Path("evaluations"), Path("app")] for p in sorted(folder.rglob("*.py"))}
    manifest = {"evaluation_version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(), "config": config.model_dump(),
                "benchmark_hash": digest(task_data), "snapshot_hash": digest(snapshot), "source_hash": digest(source),
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "prompt_hash": digest([trip_planner_agent.PLANNER_PROMPT, trip_planner_agent.POI_PARAM_PROMPT, trip_planner_agent.WEATHER_PARAM_PROMPT, trip_planner_agent.HOTEL_PARAM_PROMPT, STRICT_PROMPT, DECISION_PROMPT]),
                "python": platform.python_version(), "dependencies": {p: importlib.metadata.version(p) for p in ["openai", "httpx", "pydantic", "jsonschema"]},
                "execution": "isolated_process_direct_agent", "infrastructure_retries": 0}
    write(output / "manifest.json", manifest)
    write(output / "source.json", source)
    write(output / "benchmark.json", task_data)
    write(output / "snapshot.json", snapshot)
    with sqlite3.connect(output / "results.sqlite") as db:
        db.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, task_id TEXT, strategy TEXT, repeat INTEGER, result_json TEXT)")
        with ThreadPoolExecutor(max_workers=config.parallelism) as pool:
            futures = {}
            for repeat in range(config.repeats):
                for task in tasks:
                    for strategy in config.strategies:
                        if selected is not None and (task.task_id, strategy, repeat) not in selected:
                            continue
                        run_id = f"{task.task_id}-{strategy}-{repeat}"
                        future = pool.submit(isolated, task, snapshot, config, strategy, output / f"{run_id}.json")
                        futures[future] = (run_id, task.task_id, strategy, repeat)
            for future in as_completed(futures):
                run_id, task_id, strategy, repeat = futures[future]
                result = future.result()
                result.update({"run_id": run_id, "repeat": repeat, "agent_version": manifest["source_hash"], "prompt_version": manifest["prompt_hash"]})
                write(output / f"{run_id}.json", result)
                db.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (run_id, task_id, strategy, repeat, json.dumps(result, ensure_ascii=False)))
                db.commit()
                print(f"Completed {run_id}: {result['grades']['task_success']}", flush=True)
    return output

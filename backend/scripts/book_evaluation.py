import argparse
import json
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from evaluations.agent import RecordedLLM
from evaluations.benchmark import load
from evaluations.book_policy import (
    BOOK_COMMIT,
    DEFAULT_DAILY_RULE,
    USER_DAILY_RULE,
    WORKFLOW_PROMPT,
    NoReplanPlanner,
    workflow_llm_factory,
)
from evaluations.io import digest, read, write
from evaluations.models import Experiment
from evaluations.report import report, rows, summarize
from evaluations.runner import execute, isolated
from evaluations.statistics import paired_comparison, repeat_metrics, status_summary


def source_files():
    return {p.as_posix(): p.read_text(encoding="utf-8")
            for folder in [Path("evaluations"), Path("app"), Path("scripts")]
            for p in sorted(folder.rglob("*.py"))}


def analyze(directory, output):
    output.mkdir(parents=True, exist_ok=True)
    recorded = rows(directory)
    if len(recorded) != 600:
        raise ValueError("本次研究要求完整的600次原始执行")
    statistics = {"source_manifest": read(directory / "manifest.json"), "book_commit": BOOK_COMMIT,
                  "repeat_metrics": repeat_metrics(recorded, 2),
                  "comparisons": [paired_comparison(recorded, a, b) for a, b in [("A", "B"), ("B", "C"), ("A", "C")]]}
    write(output / "historical-statistics.json", statistics)
    tasks = {t.task_id: t for t in load(read(directory / "benchmark.json"))}
    config = Experiment.model_validate(statistics["source_manifest"]["config"])
    snapshot = read(directory / "snapshot.json")
    ablations, pairs = [], []
    for original in recorded:
        if original["strategy"] != "C":
            continue
        calls = original.get("llm_calls", [])
        rule_only = (original.get("decision") or {}).get("source") == "preflight" and not calls
        if (not calls and not rule_only) or any("response" not in c for c in calls):
            ablations.append({"run_id": original["run_id"], "status": "unverifiable", "reason": "录制响应不完整"})
            continue
        result = execute(tasks[original["task_id"]], snapshot, config, "C", replay=calls, planner_factory=NoReplanPlanner)
        if result["llm_calls"] != calls[:len(result["llm_calls"])]:
            raise ValueError("消融执行没有保持原始调用前缀")
        message = (result.get("error") or {}).get("message", "")
        if "重放请求" in message or "录制已耗尽" in message:
            raise ValueError(f"消融前缀无法重放：{original['run_id']} {message}")
        result.update({"repeat": original["repeat"], "strategy": "no_replan", "run_id": original["run_id"] + "-no-replan"})
        write(output / "ablation" / f"{result['run_id']}.json", result)
        pairs.extend([original, result])
        ablations.append({"run_id": original["run_id"], "status": "verified_prefix",
                          "original": original["grades"]["task_success"], "without_replan": result["grades"]["task_success"],
                          "used_model_calls": len(result["llm_calls"]), "original_model_calls": len(calls),
                          "replanned": any(s.get("mode") == "agent_replan" for s in original["steps"])})
    stats = {"scope": "conditional_on_recorded_response_prefixes", "checks": ablations,
             "covered": sum(c["status"] == "verified_prefix" for c in ablations),
             "unverifiable_recordings": sum(c["status"] == "unverifiable" for c in ablations),
             "original": status_summary(p["grades"]["task_success"] for p in pairs if p["strategy"] == "C"),
             "without_replan": status_summary(p["grades"]["task_success"] for p in pairs if p["strategy"] == "no_replan"),
             "rescued": sum(c.get("original") == "pass" and c.get("without_replan") == "fail" for c in ablations),
             "lost": sum(c.get("original") == "fail" and c.get("without_replan") == "pass" for c in ablations),
             "omitted_model_calls": sum(c.get("original_model_calls", 0) - c.get("used_model_calls", 0) for c in ablations)}
    write(output / "replan-ablation.json", stats)
    render_research(output)
    print({k: v for k, v in stats.items() if k != "checks"}, flush=True)


def paired_job(task, snapshot, config_data, repeat, order, output, manifest):
    config = Experiment.model_validate({**config_data, "seed": 42 + repeat})
    results = []
    for variant in order:
        run_id = f"{task.task_id}-{variant}-{repeat}"
        result = isolated(task, snapshot, config, "C", output / f"{run_id}.json",
                          llm_factory=workflow_llm_factory if variant == "W" else None)
        result.update({"strategy": variant, "base_strategy": "C", "repeat": repeat, "seed": config.seed,
                       "run_id": run_id, "pair_order": order, "agent_version": manifest["source_hash"],
                       "prompt_version": manifest["workflow_prompt_hash"] if variant == "W" else manifest["control_prompt_hash"]})
        if result.get("model", manifest["model"]) != manifest["model"]:
            raise ValueError("执行模型与实验登记不同")
        write(output / f"{run_id}.json", result)
        print(f"Completed {run_id}: {result['grades']['task_success']}", flush=True)
        results.append(result)
    return results


def trial(directory, output):
    output.mkdir(parents=True, exist_ok=False)
    original_manifest = read(directory / "manifest.json")
    config = Experiment.model_validate({**original_manifest["config"], "repeats": 3, "strategies": ["C"]})
    tasks = [t for t in load(read(directory / "benchmark.json")) if t.category in {"daily_count", "faults"}]
    if len(tasks) != 20:
        raise ValueError("诊断任务数量应为20")
    snapshot = read(directory / "snapshot.json")
    if snapshot.get("content_hash") != digest({k: v for k, v in snapshot.items() if k != "content_hash"}):
        raise ValueError("冻结快照校验失败")
    source = source_files()
    manifest = {"book_commit": BOOK_COMMIT, "created_at": datetime.now(timezone.utc).isoformat(),
                "config": config.model_dump(), "seeds": [42, 43, 44], "variants": ["C", "W"],
                "source_hash": digest(source), "benchmark_hash": digest([t.model_dump() for t in tasks]),
                "snapshot_hash": digest(snapshot), "control_prompt_hash": original_manifest["prompt_hash"],
                "scope": "development_diagnostic_not_unseen_holdout", "primary": "paired_confirmed_success_difference_W_minus_C",
                "ordering": "counterbalanced_within_task_seed_and_shuffled_pairs", "infrastructure_retries": 0,
                "annotation": "both_variants_receive_the_same_human_annotated_hard_constraints",
                "changes": "W replaces conflicting default daily rule and adds workflow/checklist; C recovery budget unchanged"}
    probe = RecordedLLM(config, "C")
    manifest.update({"model": probe.model, "endpoint": probe.base_url,
                     "workflow_prompt_hash": digest([original_manifest["prompt_hash"], DEFAULT_DAILY_RULE, USER_DAILY_RULE, WORKFLOW_PROMPT])})
    probe.client.close()
    jobs = [(task, repeat, ["C", "W"] if (i + repeat) % 2 == 0 else ["W", "C"])
            for repeat in range(3) for i, task in enumerate(tasks)]
    random.Random(20261024).shuffle(jobs)
    manifest["schedule"] = [{"task_id": task.task_id, "repeat": repeat, "order": order} for task, repeat, order in jobs]
    write(output / "manifest.json", manifest)
    write(output / "source.json", source)
    write(output / "benchmark.json", [t.model_dump() for t in tasks])
    write(output / "snapshot.json", snapshot)
    with sqlite3.connect(output / "results.sqlite") as db:
        db.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, task_id TEXT, strategy TEXT, repeat INTEGER, result_json TEXT)")
        with ThreadPoolExecutor(max_workers=config.parallelism) as pool:
            futures = [pool.submit(paired_job, task, snapshot, config.model_dump(), repeat, order, output, manifest)
                       for task, repeat, order in jobs]
            for future in as_completed(futures):
                for result in future.result():
                    db.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (result["run_id"], result["task_id"], result["strategy"],
                                                                      result["repeat"], json.dumps(result, ensure_ascii=False)))
                db.commit()
    finish_trial(output)


def finish_trial(output):
    manifest = read(output / "manifest.json")
    if digest(source_files()) != manifest["source_hash"]:
        raise ValueError("实验源码发生变化，无法验证")
    recorded = rows(output)
    if len(recorded) != 120:
        raise ValueError("实验要求120次完整执行")
    tasks = {t.task_id: t for t in load(read(output / "benchmark.json"))}
    snapshot = read(output / "snapshot.json")
    checks = []
    for row in recorded:
        calls = row.get("llm_calls", [])
        if not calls or any("response" not in c for c in calls):
            checks.append({"run_id": row["run_id"], "status": "unverifiable"})
            continue
        config = Experiment.model_validate({**manifest["config"], "seed": row["seed"]})
        replay = execute(tasks[row["task_id"]], snapshot, config, "C", replay=calls,
                         llm_factory=workflow_llm_factory if row["strategy"] == "W" else None)
        same = all(replay.get(k) == row.get(k) for k in ["plan", "decision", "grades"])
        checks.append({"run_id": row["run_id"], "status": "pass" if same else "fail", "error": replay.get("error")})
    write(output / "replay-verification.json", {"checks": checks, **status_summary(c["status"] for c in checks)})
    if any(c["status"] == "fail" for c in checks):
        raise ValueError("录制重放存在差异")
    summary = report(output)
    statistics = {"repeats": repeat_metrics(recorded, 3), "primary_comparison": paired_comparison(recorded, "C", "W"),
                  "categories": {category: paired_comparison([r for r in recorded if r["category"] == category], "C", "W")
                                 for category in ["daily_count", "faults"]},
                  "metrics": summary["strategies"]}
    write(output / "trial-statistics.json", statistics)
    render_research(output.parent)
    print(statistics["primary_comparison"], flush=True)


def render_research(output):
    historical_path = output / "historical-statistics.json"
    if not historical_path.exists():
        return
    historical, ablation = read(historical_path), read(output / "replan-ablation.json")
    lines = ["# 书籍方法实验：重复可靠性、组件消融与执行清单", "",
             f"书籍固定提交：`{BOOK_COMMIT}`。原始数据为100条任务、三种配置、每条两次，共600次真实模型执行。",
             "", "## 两次运行是否稳定", "", "| 配置 | 至少一次通过 | 两次全部通过 | 两次全通过无法验证 |",
             "| --- | --- | --- | --- |"]
    for name, values in historical["repeat_metrics"].items():
        lines.append(f"| {name} | {values['any_pass']['pass']}/100 | {values['all_pass']['pass']}/100 | {values['all_pass']['unverifiable']}/100 |")
    lines += ["", "这是本次固定低温参数的经验重复成绩，不能解释为独立随机采样的总体可靠性。",
              "", "## 配对差值与证据缺失", ""]
    for comparison in historical["comparisons"]:
        ci = comparison["paired_task_bootstrap_95"]
        cluster = comparison["category_cluster_bootstrap_95"]
        lines.append(f"- {comparison['treatment']} 相对 {comparison['control']}：已确认成功比例差值 {comparison['confirmed_rate_difference']:.1%}；任务配对bootstrap 95%区间 [{ci[0]:.1%}, {ci[1]:.1%}]；分类聚合敏感性区间 [{cluster[0]:.1%}, {cluster[1]:.1%}]。")
    lines += ["", "区间按任务抽样并保留重复记录；分类敏感性使用10个类别整体抽样。任务共享模板，区间的适用范围受到覆盖面的限制。未知状态保持独立，完整计数及识别区间保存在JSON中。多个历史比较仅作探索分析。",
              "", "## 关闭重新规划", "",
              f"200次C记录中，可验证调用前缀 {ablation['covered']} 次，录制不完整 {ablation['unverifiable_recordings']} 次。",
              f"相同可重放记录中，保留重新规划通过 {ablation['original']['pass']} 次，关闭重新规划通过 {ablation['without_replan']['pass']} 次。",
              f"重新规划把已确认失败修复为通过 {ablation['rescued']} 次；已确认通过变为失败 {ablation['lost']} 次；关闭后少执行 {ablation['omitted_model_calls']} 次模型调用。",
              "", "比较限定在已录制响应前缀，保留工具重试与相同提示词。没有发出新的模型请求，离线耗时不代表线上延迟，不能据此推算新的线上平均效果。"]
    trial_path = output / "trial" / "trial-statistics.json"
    if trial_path.exists():
        study = read(trial_path)
        lines += ["", "## 执行清单的真实模型对照", "",
                  "使用每日数量与故障两类20条诊断任务，C与W各运行三次，种子42/43/44，temperature=0。任务与种子配对，运行顺序平衡，共120次真实执行。",
                  "两组接收相同的人工标注条件，工具、快照、模型参数、重试和重新规划预算相同。W调整默认数量规则并加入流程与条件清单；本次对整个提示词改动组合进行比较。",
                  "", "| 配置 | 通过 | 失败 | 无法验证 | 平均耗时 | 平均工具调用 | 已记录Token |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        trial_rows = rows(output / "trial")
        for name in ["C", "W"]:
            selected = [r for r in trial_rows if r["strategy"] == name]
            counts, metrics = status_summary(r["grades"]["task_success"] for r in selected), summarize(selected)
            lines.append(f"| {name} | {counts['pass']}/60 | {counts['fail']}/60 | {counts['unverifiable']}/60 | {metrics['average_latency_ms']/1000:.2f}秒 | {metrics['average_tool_calls']:.2f} | {metrics['token_usage']} |")
        comparison = study["primary_comparison"]
        ci = comparison["paired_task_bootstrap_95"]
        lines += ["", f"预先指定的主要指标：W相对C的已确认成功比例差值 {comparison['confirmed_rate_difference']:.1%}，任务配对bootstrap 95%区间 [{ci[0]:.1%}, {ci[1]:.1%}]。",
                  "", "这20条任务已经用于开发诊断，成绩用于回归分析；无法据此断言对未见任务的泛化效果。零温度与种子参数也不能保证独立随机样本。费用缺少核实单价，保持unknown。每类结果、三次全部通过指标和McNemar检验保存在trial-statistics.json。"]
    lines += ["", "## 复现", "", "在backend目录运行：", "", "```powershell",
              "venv/Scripts/python.exe -m scripts.book_evaluation analyze evaluations/runs/full-20261023-final --output evaluations/reports/book-study-20261024",
              "venv/Scripts/python.exe -m scripts.book_evaluation run evaluations/runs/full-20261023-final --output evaluations/reports/book-study-20261024/trial",
              "```", "", "run命令调用真实模型，需要backend/.env配置；输出目录必须不存在。analyze完全使用已有真实响应重放。"]
    (output / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["analyze", "run", "finish"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "finish":
        finish_trial(args.directory)
    elif args.output is None:
        parser.error("analyze/run需要--output")
    elif args.command == "analyze":
        analyze(args.directory, args.output)
    else:
        trial(args.directory, args.output)


if __name__ == "__main__":
    main()

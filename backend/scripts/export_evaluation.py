import argparse
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evaluations.io import read, write
from evaluations.report import report, rows


def metric(value, percent=False):
    if value is None:
        return "unknown"
    return f"{value:.2%}" if percent else f"{value:.2f}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true", help="更新同一实验的已有报告")
    args = parser.parse_args()
    results = rows(args.directory)
    summary = report(args.directory)
    manifest = read(args.directory / "manifest.json")
    current_source = {p.as_posix(): p.read_text(encoding="utf-8") for folder in [Path("evaluations"), Path("app")] for p in sorted(folder.rglob("*.py"))}
    if current_source != read(args.directory / "source.json"):
        raise ValueError("导出源码与实验源码不同")
    verification = read(args.directory / "replay-verification.json")
    if verification["failed"]:
        raise ValueError("响应重放验证未通过")
    expected = len(read(args.directory / "benchmark.json")) * len(manifest["config"]["strategies"]) * manifest["config"]["repeats"]
    if len(results) != expected:
        raise ValueError("实验尚未完成")
    if args.output.exists():
        if not args.refresh:
            raise FileExistsError("报告目录已存在，更新同一实验时请使用--refresh")
        previous = read(args.output / "manifest.json")
        if any(previous[key] != manifest[key] for key in ["source_hash", "snapshot_hash", "benchmark_hash", "prompt_hash"]):
            raise ValueError("已有报告属于其他实验")
    else:
        args.output.mkdir(parents=True)
    for name in ["report.html", "summary.json", "manifest.json", "replay-verification.json"]:
        shutil.copy2(args.directory / name, args.output / name)
    with zipfile.ZipFile(args.output / "evidence.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in args.directory.rglob("*"):
            interrupted = "interrupted-attempts" in path.relative_to(args.directory).parts
            if path.is_file() and (path.suffix in {".json", ".sqlite"} or (interrupted and path.suffix == ".partial")):
                archive.write(path, path.relative_to(args.directory).as_posix())
        for folder in [Path("app"), Path("evaluations")]:
            for path in sorted(folder.rglob("*.py")):
                archive.write(path, "code/" + path.as_posix())
        for path in [Path("scripts/verify_evaluation.py"), Path("scripts/export_evaluation.py"), Path("scripts/complete_evaluation.py"), Path("requirements.txt"), Path("pyproject.toml")]:
            archive.write(path, "code/" + path.as_posix())
        for path in Path("evaluations/data").rglob("*.json"):
            archive.write(path, "code/" + path.as_posix())
    task_count = len({r["task_id"] for r in results})
    models = sorted({r.get("model", "unknown") for r in results})
    local_started = datetime.fromisoformat(manifest["created_at"]).astimezone(timezone(timedelta(hours=8))).isoformat()
    resumes = [read(p) for p in sorted(args.directory.glob("resume-*.json"))]
    lines = ["# 旅行规划 Agent 真实实验报告", "",
             f"实验开始时间：{local_started}（Asia/Shanghai）；模型：{', '.join(models)}。",
             f"继续执行记录：{len(resumes)}次，详细时间、完成数量与源码校验信息保存在证据包的resume文件。",
             f"本次执行覆盖{task_count}条任务、{len({r['category'] for r in results})}类场景，三个策略各重复{manifest['config']['repeats']}次，共{len(results)}次系统执行。",
             f"其中{sum(bool(r.get('llm_calls')) for r in results)}次执行调用了真实模型，{sum(not r.get('llm_calls') and bool(r.get('decision')) for r in results)}次由规划前规则直接答复。完整Benchmark包含100条任务。", "",
             "环境使用录制高德目录，故障由确定性规则注入，模型调用真实服务。所有策略共享任务、快照、temperature及seed；具体版本、依赖和配置见manifest.json。费用未配置价格来源，显示unknown。", "",
             "| 指标 | A | B | C |", "|---|---:|---:|---:|"]
    fields = [("执行数", "samples", False), ("调用模型的执行数", "model_invoked_samples", False), ("规划前直接答复数", "preflight_only_samples", False), ("可验证数", "verifiable_samples", False), ("无法验证数", "unverifiable_samples", False),
              ("任务成功率", "task_success_rate", True), ("成功数/全部执行", "success_fraction_all", True),
              ("需要生成行程的任务成功率", "plan_task_success_rate", True), ("需要拒绝或询问的任务成功率", "decision_task_success_rate", True),
              ("硬约束满足率", "constraint_satisfaction_rate", True), ("POI有效率", "poi_validity_rate", True),
              ("工具执行错误率", "tool_error_rate", True), ("故障恢复率", "recovery_rate", True),
              ("平均规划耗时ms", "average_latency_ms", False), ("包含初始化的平均耗时ms", "average_wall_latency_ms", False), ("平均工具调用数", "average_tool_calls", False),
              ("完整Token合计", "token_usage", False), ("Token完整执行数", "token_observed_samples", False), ("费用", "cost", False)]
    for label, key, percent in fields:
        lines.append(f"| {label} | " + " | ".join(metric(summary["strategies"][s][key], percent) for s in "ABC") + " |")
    observed = {s: sum((call.get("response") or {}).get("usage", {}).get("total_tokens", 0) for r in results if r["strategy"] == s for call in r.get("http_calls", []) if (call.get("response") or {}).get("usage")) for s in "ABC"}
    lines.extend(["", f"从已收到HTTP响应观察到的Token小计：A={observed['A']}，B={observed['B']}，C={observed['C']}。缺失响应的调用可能仍产生费用，小计不表示完整账单。", "",
                  "任务成功率使用pass/(pass+fail)，并单独展示无法验证数量。工具错误率计算执行异常和无效字段；工具选择及参数错误见错误分类。POI有效率是已输出地点的有效比例，不能单独代表任务完成率。平均耗时从Agent开始规划计量，不含独立进程和模型客户端初始化。", "",
                  "## 故障条件", "", "| 策略与故障 | 实际触发数 | 任务成功率 | 恢复率 | 平均工具次数 | 平均耗时ms |", "|---|---:|---:|---:|---:|---:|"])
    for condition, values in summary["fault_conditions"].items():
        if ":none:" in condition:
            continue
        lines.append(f"| {condition} | {values['fault_exposed_samples']} | {metric(values['task_success_rate'], True)} | {metric(values['recovery_rate'], True)} | {metric(values['average_tool_calls'])} | {metric(values['average_latency_ms'])} |")
    lines.extend(["", "## 错误轨迹", "", "出现次数包含恢复成功的执行及无法验证状态，最终失败次数单独计算。", "",
                  "| Agent配置 | 错误分类 | 出现次数 | 最终失败次数 | 无法验证次数 | 占该配置执行数 | 典型执行 |", "|---|---|---:|---:|---:|---:|---|"])
    for strategy in "ABC":
        stats = summary["strategies"][strategy]
        for kind, count in sorted(stats["error_counts"].items()):
            example = next(r["run_id"] for r in results if r["strategy"] == strategy and kind in r["grades"]["errors"])
            related = [r for r in results if r["strategy"] == strategy and kind in r["grades"]["errors"]]
            failed = sum(r["grades"]["task_success"] == "fail" for r in related)
            unknown = sum(r["grades"]["task_success"] == "unverifiable" for r in related)
            lines.append(f"| {strategy} | {kind} | {count} | {failed} | {unknown} | {metric(stats['error_proportions'][kind], True)} | {example} |")
    lines.extend(["", "各错误类别可重叠。report.html可以按错误类别筛选完整轨迹；evidence.zip包含每次执行JSON、SQLite、原始模型响应、工具响应、源代码快照和实验数据。", "",
                  "## 离线复现验证", "",
                  f"离线重新执行Agent：{verification['passed']}条结果及评分一致，{verification['failed']}条不一致，{verification['unverifiable']}条由于缺少完整模型响应无法重放。其中模型响应重放一致{verification.get('model_replay_passed', 'unknown')}条，规划前规则重新执行一致{verification.get('preflight_replay_passed', 'unknown')}条。此处仅验证框架复现能力，未进行额外模型推理。", "",
                  "```powershell", "cd backend", f"python -m zipfile -e {args.output.as_posix()}/evidence.zip .evaluation-work/evidence",
                  "cd .evaluation-work/evidence/code", "python -m scripts.verify_evaluation ..", "python -m evaluations report ..", "```", "",
                  "## 解释范围", "",
                  "- 同一任务的重复执行存在相关性，样本仅覆盖两座城市，不能外推为生产总体成功率。",
                  "- 如存在resume记录，实验曾中断后继续执行；此前已完成记录保留，未完成尝试另存于interrupted-attempts。执行数与Token小计使用完整执行记录，中断尝试的额外费用未知。",
                  "- 冻结目录匹配衡量相对于已采集候选的有效性，未收录地点的现实存在性保持未知。",
                  "- B/C读取已标注的结构化用户条件，用于规划前检查和生成后校验；本实验不测从自然语言自动提取所有约束的准确率。",
                  "- 预算和开放时间缺少完整外部证据，无法验证记录不进入可验证成功率分母。",
                  "- 条件冲突和缺失地点采用事先声明的答复规则；规划前规则答复单独统计，不归入模型推理次数。增强策略通过评测入口启用，生产TripPlan接口尚未承载询问用户的结构化答复。",
                  "- 故障分组中的transient为第一次调用出现故障，persistent为前三次调用连续出现故障。恢复率仅使用可验证的故障样本，无法验证数单独保存。",
                  "- 行程成功率与拒绝或询问的正确率分别报告；历史报告的题目范围和评分规则不同，不能直接计算前后成绩的提升幅度。",
                  "- 注入超时用于验证异常后的决策，未模拟网络等待时间；进程超时由独立测试验证。",
                  "- 工具恢复率包含满足硬约束的降级完成，模型连接错误也保留在真实执行结果中。", "",
                  "- 当前Agent由程序指定工具顺序，工具评分衡量指定接口的调用准确性。", "",
                  "## 简历描述", "",
                  f"- 为原生Function Calling旅行规划Agent构建100条任务、10类场景的自动评测系统，完成{task_count}条任务、三种策略、{len(results)}次系统执行，记录硬约束、POI有效性、工具错误、故障恢复及资源开销。",
                  f"- 实现冻结外部数据、六种确定性故障注入和严格模型响应重放；在固定高德目录及已标注用户条件下，对完整{task_count}条任务的本次实验记录，A与C任务成功率分别为{metric(summary['strategies']['A']['task_success_rate'], True)}和{metric(summary['strategies']['C']['task_success_rate'], True)}，完成{verification['passed']}条实际执行记录的离线一致性验证。", ""])
    (args.output / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    write(args.output / "observed-token-subtotals.json", observed)
    print(args.output.resolve())


if __name__ == "__main__":
    main()

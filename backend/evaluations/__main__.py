import argparse
import os
from pathlib import Path

from .benchmark import build, load
from .io import read, write
from .models import Experiment


def main():
    parser = argparse.ArgumentParser(description="旅行规划 Agent 可复现评测")
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--output", default="evaluations/data/benchmark.json")
    capture = commands.add_parser("capture")
    capture.add_argument("--output", default="evaluations/data/snapshot.json")
    runner = commands.add_parser("run")
    runner.add_argument("--benchmark", default="evaluations/data/benchmark.json")
    runner.add_argument("--snapshot", default="evaluations/data/snapshot.json")
    runner.add_argument("--config", default="evaluations/configs/comparison.json")
    runner.add_argument("--tasks", nargs="+")
    runner.add_argument("--output", required=True)
    for command in ["report", "replay-failures", "rerun-failures"]:
        cmd = commands.add_parser(command)
        cmd.add_argument("directory")
        if command != "report":
            cmd.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "build":
        write(args.output, [t.model_dump() for t in build()])
    elif args.command == "capture":
        from app.db import create_all

        from .capture import capture

        path = Path(args.output)
        if path.exists():
            raise FileExistsError("快照已存在，请为新采集指定不同路径")
        db = Path(".evaluation-work/capture.sqlite").resolve()
        if db.exists():
            raise FileExistsError("采集数据库已存在，请指定独立工作目录或手动清理")
        db.parent.mkdir(parents=True, exist_ok=True)
        os.environ["DATABASE_URL"] = f"sqlite:///{db.as_posix()}"
        create_all()
        write(path, capture())
    elif args.command == "run":
        from .report import report
        from .runner import run

        tasks = load(read(args.benchmark))
        if args.tasks:
            missing = set(args.tasks) - {t.task_id for t in tasks}
            if missing:
                raise ValueError(f"未知任务：{missing}")
            tasks = [t for t in tasks if t.task_id in args.tasks]
        output = run(tasks, read(args.snapshot), Experiment.model_validate(read(args.config)), args.output)
        print(report(output))
    elif args.command == "report":
        from .report import report

        print(report(args.directory))
    else:
        from .report import report, rows
        from .runner import execute, run

        directory = Path(args.directory)
        results = [r for r in rows(directory) if r["grades"]["task_success"] != "pass"]
        config = Experiment.model_validate(read(directory / "manifest.json")["config"])
        snapshot = read(directory / "snapshot.json")
        tasks = load(read(directory / "benchmark.json"))
        if args.command == "rerun-failures":
            selection = {(r["task_id"], r["strategy"], r["repeat"]) for r in results}
            run(tasks, snapshot, config, args.output, selected=selection)
            print(report(args.output))
        else:
            output = Path(args.output)
            output.mkdir(parents=True, exist_ok=False)
            by_id = {t.task_id: t for t in tasks}
            for result in results:
                replay = execute(by_id[result["task_id"]], snapshot, config, result["strategy"], replay=result["llm_calls"])
                replay["original_run_id"] = result["run_id"]
                replay["grade_matches"] = replay["grades"] == result["grades"]
                write(output / f"{result['run_id']}.json", replay)


if __name__ == "__main__":
    main()

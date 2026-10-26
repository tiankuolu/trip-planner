import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from evaluations.benchmark import load
from evaluations.io import digest, read, write
from evaluations.models import Experiment
from evaluations.report import rows
from evaluations.runner import isolated


def complete(directory):
    directory = directory.resolve()
    manifest = read(directory / "manifest.json")
    source = {p.as_posix(): p.read_text(encoding="utf-8") for folder in [Path("evaluations"), Path("app")] for p in sorted(folder.rglob("*.py"))}
    if digest(source) != manifest["source_hash"]:
        raise ValueError("源码与实验记录不同，不能合并执行")
    task_data, snapshot = read(directory / "benchmark.json"), read(directory / "snapshot.json")
    if digest(task_data) != manifest["benchmark_hash"] or digest(snapshot) != manifest["snapshot_hash"]:
        raise ValueError("实验任务或快照校验失败")
    tasks = load(task_data)
    config = Experiment.model_validate(manifest["config"])
    records = rows(directory)
    recorded_models = {r["model"] for r in records if r.get("model") not in {None, "unknown"}}
    model = os.getenv("LLM_MODEL_ID") or get_settings().openai_model
    if recorded_models and recorded_models != {model}:
        raise ValueError("模型名称与已有执行不同")
    expected = {(t.task_id, strategy, repeat) for t in tasks for strategy in config.strategies for repeat in range(config.repeats)}
    completed = {(r["task_id"], r["strategy"], r["repeat"]) for r in records}
    if not completed <= expected:
        raise ValueError("发现不属于当前实验的记录")
    for record in records:
        if record["agent_version"] != manifest["source_hash"] or record["prompt_version"] != manifest["prompt_hash"]:
            raise ValueError("已有记录的版本不一致")
    pending = expected - completed
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    session = {"started_at": datetime.now(timezone.utc).isoformat(), "model": model, "source_hash": manifest["source_hash"],
               "completed_before_resume": len(completed), "pending": len(pending), "status": "running"}
    session_path = directory / f"resume-{session_id}.json"
    write(session_path, session)
    print(f"Completed: {len(completed)}, pending: {len(pending)}", flush=True)
    by_id = {t.task_id: t for t in tasks}
    ordered = [(t.task_id, strategy, repeat) for repeat in range(config.repeats) for t in tasks for strategy in config.strategies
               if (t.task_id, strategy, repeat) in pending]
    interrupted = directory / "interrupted-attempts" / session_id
    for task_id, strategy, repeat in ordered:
        run_id = f"{task_id}-{strategy}-{repeat}"
        for path in [directory / f"{run_id}.json", directory / f"{run_id}.json.partial"]:
            if path.exists():
                interrupted.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, interrupted / path.name)
    try:
        with sqlite3.connect(directory / "results.sqlite") as db, ThreadPoolExecutor(max_workers=config.parallelism) as pool:
            futures = {pool.submit(isolated, by_id[task_id], snapshot, config, strategy, directory / f"{task_id}-{strategy}-{repeat}.json"):
                       (task_id, strategy, repeat) for task_id, strategy, repeat in ordered}
            for future in as_completed(futures):
                task_id, strategy, repeat = futures[future]
                run_id = f"{task_id}-{strategy}-{repeat}"
                result = future.result()
                result.update(run_id=run_id, repeat=repeat, agent_version=manifest["source_hash"], prompt_version=manifest["prompt_hash"], resume_session=session_id)
                write(directory / f"{run_id}.json", result)
                db.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (run_id, task_id, strategy, repeat, json.dumps(result, ensure_ascii=False)))
                db.commit()
                print(f"Completed {run_id}: {result['grades']['task_success']}", flush=True)
        session["status"] = "completed"
        session["completed_after_resume"] = len(rows(directory))
        session["ended_at"] = datetime.now(timezone.utc).isoformat()
        write(session_path, session)
    except BaseException:
        session["status"] = "interrupted"
        write(session_path, session)
        raise


def main():
    parser = argparse.ArgumentParser(description="继续执行已中断的评测并导出完整报告")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("报告目录已存在")
    lock = args.directory / ".completion.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    try:
        complete(args.directory)
        subprocess.run([sys.executable, "-m", "scripts.verify_evaluation", str(args.directory)], check=True)
        subprocess.run([sys.executable, "-m", "scripts.export_evaluation", str(args.directory), "--output", str(args.output)], check=True)
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()

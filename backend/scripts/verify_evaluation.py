import argparse
from pathlib import Path

from evaluations.benchmark import load
from evaluations.io import read, write
from evaluations.models import Experiment
from evaluations.report import rows
from evaluations.runner import execute


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    tasks = {t.task_id: t for t in load(read(args.directory / "benchmark.json"))}
    config = Experiment.model_validate(read(args.directory / "manifest.json")["config"])
    snapshot = read(args.directory / "snapshot.json")
    checks = []
    for recorded in rows(args.directory):
        calls = recorded.get("llm_calls", [])
        rule_decision = (recorded.get("decision") or {}).get("source") == "preflight" and not calls
        if (not calls and not rule_decision) or any("response" not in call for call in calls):
            checks.append({"run_id": recorded["run_id"], "status": "unverifiable", "reason": "缺少完整模型响应录制"})
            continue
        replay = execute(tasks[recorded["task_id"]], snapshot, config, recorded["strategy"], replay=calls)
        same = replay["grades"] == recorded["grades"] and replay["plan"] == recorded["plan"] and replay.get("decision") == recorded.get("decision")
        checks.append({"run_id": recorded["run_id"], "status": "pass" if same else "fail", "basis": "preflight_rules" if rule_decision else "recorded_model_response", "replay_error": replay.get("error")})
    output = {"mode": "offline_real_response_replay", "checks": checks,
              "passed": sum(c["status"] == "pass" for c in checks),
              "failed": sum(c["status"] == "fail" for c in checks),
              "unverifiable": sum(c["status"] == "unverifiable" for c in checks),
              "model_replay_passed": sum(c["status"] == "pass" and c.get("basis") == "recorded_model_response" for c in checks),
              "preflight_replay_passed": sum(c["status"] == "pass" and c.get("basis") == "preflight_rules" for c in checks)}
    write(args.directory / "replay-verification.json", output)
    print({k: v for k, v in output.items() if k != "checks"})
    if output["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

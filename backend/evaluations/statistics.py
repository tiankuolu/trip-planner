from collections import Counter, defaultdict

import numpy as np
from scipy.stats import binomtest


def repeat_status(statuses, mode):
    statuses = list(statuses)
    if not statuses or any(s not in {"pass", "fail", "unverifiable"} for s in statuses):
        raise ValueError("重复结果必须包含有效状态")
    if mode == "any":
        return "pass" if "pass" in statuses else "unverifiable" if "unverifiable" in statuses else "fail"
    if mode == "all":
        return "fail" if "fail" in statuses else "unverifiable" if "unverifiable" in statuses else "pass"
    raise ValueError("不支持的重复聚合方式")


def status_summary(statuses):
    counts = Counter(statuses)
    total = sum(counts.values())
    return {"pass": counts["pass"], "fail": counts["fail"], "unverifiable": counts["unverifiable"], "samples": total,
            "confirmed_rate": counts["pass"] / total if total else None,
            "identification_bounds": [counts["pass"] / total, (counts["pass"] + counts["unverifiable"]) / total] if total else None}


def index_runs(results):
    indexed = defaultdict(dict)
    for row in results:
        identity = (row["task_id"], row["repeat"])
        if identity in indexed[row["strategy"]]:
            raise ValueError("执行身份重复")
        indexed[row["strategy"]][identity] = row
    return indexed


def repeat_metrics(results, expected_repeats):
    if expected_repeats < 1:
        raise ValueError("重复次数必须为正数")
    output = {}
    for strategy, records in index_runs(results).items():
        grouped = defaultdict(dict)
        for (task_id, repeat), row in records.items():
            grouped[task_id][repeat] = row["grades"]["task_success"]
        if any(set(v) != set(range(expected_repeats)) for v in grouped.values()):
            raise ValueError("任务重复次数不完整")
        output[strategy] = {"repeat_count": expected_repeats, "tasks": len(grouped),
                            "any_pass": status_summary(repeat_status(v.values(), "any") for v in grouped.values()),
                            "all_pass": status_summary(repeat_status(v.values(), "all") for v in grouped.values())}
    return output


def paired_comparison(results, control, treatment, *, samples=10000, seed=20261024):
    if samples < 1 or control == treatment:
        raise ValueError("抽样次数必须为正数，比较配置必须不同")
    indexed = index_runs(results)
    left, right = indexed[control], indexed[treatment]
    if not left or left.keys() != right.keys():
        raise ValueError("配对执行不完整")
    tasks = sorted({task_id for task_id, _ in left})
    per_task, categories, statuses_left, statuses_right = [], [], [], []
    discordance = Counter()
    eligible = 0
    for task_id in tasks:
        identities = sorted(k for k in left if k[0] == task_id)
        a = [left[k]["grades"]["task_success"] for k in identities]
        b = [right[k]["grades"]["task_success"] for k in identities]
        if any(left[k]["category"] != right[k]["category"] for k in identities):
            raise ValueError("配对任务分类不同")
        per_task.append(float(np.mean([s == "pass" for s in b])) - float(np.mean([s == "pass" for s in a])))
        categories.append(left[identities[0]]["category"])
        statuses_left.extend(a)
        statuses_right.extend(b)
        all_a, all_b = repeat_status(a, "all"), repeat_status(b, "all")
        if "unverifiable" not in {all_a, all_b}:
            eligible += 1
            discordance[(all_a == "pass", all_b == "pass")] += 1
    differences = np.asarray(per_task)
    generator = np.random.default_rng(seed)
    draws = generator.integers(0, len(tasks), size=(samples, len(tasks)))
    bootstrapped = differences[draws].mean(axis=1)
    families = sorted(set(categories))
    family_sums = np.asarray([sum(d for d, c in zip(per_task, categories) if c == family) for family in families])
    family_sizes = np.asarray([categories.count(family) for family in families])
    family_draws = generator.integers(0, len(families), size=(samples, len(families)))
    clustered = family_sums[family_draws].sum(axis=1) / family_sizes[family_draws].sum(axis=1)
    lost, gained = discordance[(True, False)], discordance[(False, True)]
    summary_a, summary_b = status_summary(statuses_left), status_summary(statuses_right)
    low_a, high_a = summary_a["identification_bounds"]
    low_b, high_b = summary_b["identification_bounds"]
    return {"control": control, "treatment": treatment, "paired_tasks": len(tasks), "paired_runs": len(left),
            "control_statuses": summary_a, "treatment_statuses": summary_b,
            "confirmed_rate_difference": float(differences.mean()),
            "paired_task_bootstrap_95": np.quantile(bootstrapped, [0.025, 0.975]).tolist(),
            "category_cluster_bootstrap_95": np.quantile(clustered, [0.025, 0.975]).tolist(), "category_clusters": len(families),
            "unknown_identification_difference_bounds": [low_b - high_a, high_b - low_a],
            "mcnemar_all_pass": {"eligible_tasks": eligible, "excluded_tasks": len(tasks) - eligible, "lost": lost, "gained": gained,
                                  "p_value": float(binomtest(gained, gained + lost, 0.5).pvalue) if gained + lost else 1.0},
            "bootstrap_samples": samples, "bootstrap_seed": seed}

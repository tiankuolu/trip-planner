import pytest

from app.agents.trip_planner_agent import PLANNER_PROMPT
from evaluations.book_policy import DEFAULT_DAILY_RULE, USER_DAILY_RULE, workflow_messages
from evaluations.statistics import paired_comparison, repeat_metrics, repeat_status


@pytest.mark.parametrize("statuses,any_status,all_status", [
    (["pass", "pass"], "pass", "pass"),
    (["pass", "fail"], "pass", "fail"),
    (["pass", "unverifiable"], "pass", "unverifiable"),
    (["fail", "fail"], "fail", "fail"),
    (["fail", "unverifiable"], "unverifiable", "fail"),
    (["unverifiable", "unverifiable"], "unverifiable", "unverifiable"),
])
def test_three_state_repeat_aggregation(statuses, any_status, all_status):
    assert repeat_status(statuses, "any") == any_status
    assert repeat_status(statuses, "all") == all_status
    assert repeat_status(reversed(statuses), "all") == all_status


def mathematical_rows():
    return [{"task_id": str(task), "repeat": repeat, "category": str(task % 2),
             "strategy": variant, "grades": {"task_success": status}}
            for task in range(8) for repeat in range(2)
            for variant, status in [("control", "fail"), ("treatment", "pass")]]


def test_paired_statistics_use_task_units_and_exact_discordance():
    results = mathematical_rows()
    comparison = paired_comparison(results, "control", "treatment", samples=200)
    assert comparison["paired_tasks"] == 8
    assert comparison["paired_runs"] == 16
    assert comparison["confirmed_rate_difference"] == 1
    assert comparison["paired_task_bootstrap_95"] == [1, 1]
    assert comparison["category_cluster_bootstrap_95"] == [1, 1]
    assert comparison["mcnemar_all_pass"]["gained"] == 8
    assert comparison["mcnemar_all_pass"]["p_value"] == 2 / 256
    assert paired_comparison(list(reversed(results)), "control", "treatment", samples=200) == comparison


def test_unknowns_preserve_bounds_and_exclude_mcnemar():
    results = mathematical_rows()
    for row in results:
        if row["strategy"] == "treatment":
            row["grades"]["task_success"] = "unverifiable"
    comparison = paired_comparison(results, "control", "treatment", samples=100)
    assert comparison["unknown_identification_difference_bounds"] == [0, 1]
    assert comparison["mcnemar_all_pass"]["eligible_tasks"] == 0
    assert comparison["mcnemar_all_pass"]["excluded_tasks"] == 8
    assert comparison["treatment_statuses"]["unverifiable"] == 16


def test_incomplete_duplicate_and_misaligned_runs_fail():
    results = mathematical_rows()
    with pytest.raises(ValueError, match="重复"):
        repeat_metrics(results + [results[0]], 2)
    with pytest.raises(ValueError, match="配对"):
        paired_comparison(results[1:], "control", "treatment")
    with pytest.raises(ValueError, match="重复次数"):
        repeat_metrics(results[2:], 2)


def test_checklist_changes_prompt_without_mutating_input():
    messages = [{"role": "system", "content": PLANNER_PROMPT}, {"role": "user", "content": "规划旅行"}]
    original = messages[0]["content"]
    transformed = workflow_messages(messages, [{"kind": "daily_count", "value": [4, 4]}])
    assert DEFAULT_DAILY_RULE in original
    assert DEFAULT_DAILY_RULE not in transformed[0]["content"]
    assert USER_DAILY_RULE in transformed[0]["content"]
    assert "[4, 4]" in transformed[1]["content"]
    assert messages[0]["content"] == original
    assert messages[1]["content"] == "规划旅行"

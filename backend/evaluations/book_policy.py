import copy
import json

from .agent import RecordedLLM, ReplayLLM, StrategyPlanner

BOOK_COMMIT = "dbc046eb896ac4e39aa19c7774c8bf49583b89a6"
DEFAULT_DAILY_RULE = "3. 每天 2-3 个景点，按地理位置就近分组（参考经纬度），避免跨城折返；"
USER_DAILY_RULE = "3. 每日景点数量使用用户条件清单中的上下限；按地理位置就近分组，避免跨城折返；"
WORKFLOW_PROMPT = """\n按以下流程编排行程：
1. 依据用户条件确定城市、日期、人数和每日景点数量。
2. 从给定候选安排必选景点，再补足每日数量，核对每天的不同景点数量。
3. 安排酒店、交通和三餐，检查日期与数量。
4. 对已有价格和开放信息进行核实，缺少外部证据时明确说明尚未核实。
5. 逐项检查用户条件后输出原有JSON结构。遵守用户明确给出的数量范围。
"""


def workflow_messages(messages, contract):
    result = copy.deepcopy(messages)
    if DEFAULT_DAILY_RULE not in result[0]["content"]:
        raise ValueError("规划提示词版本不匹配")
    result[0]["content"] = result[0]["content"].replace(DEFAULT_DAILY_RULE, USER_DAILY_RULE) + WORKFLOW_PROMPT
    checklist = [{"检查项": row["kind"], "用户条件": row["value"]} for row in contract]
    last_user = next(i for i in range(len(result) - 1, -1, -1) if result[i]["role"] == "user")
    result[last_user]["content"] += "\n输出前逐项核对的用户条件清单：\n" + json.dumps(checklist, ensure_ascii=False)
    return result


class WorkflowLLM(RecordedLLM):
    def _create(self, messages, **kwargs):
        if "tools" not in kwargs:
            messages = workflow_messages(messages, self.user_contract)
        return super()._create(messages, **kwargs)


class WorkflowReplayLLM(ReplayLLM):
    def _create(self, messages, **kwargs):
        if "tools" not in kwargs:
            messages = workflow_messages(messages, self.user_contract)
        return super()._create(messages, **kwargs)


def workflow_llm_factory(recording, config, strategy):
    return WorkflowLLM(config, strategy) if recording is None else WorkflowReplayLLM(recording, config, strategy)


class NoReplanPlanner(StrategyPlanner):
    def _step_plan(self, *args, **kwargs):
        original = self.strategy
        self.strategy = "B"
        try:
            return super()._step_plan(*args, **kwargs)
        finally:
            self.strategy = original

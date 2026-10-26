import copy
import json
import time

import httpx
from openai.types.chat import ChatCompletion

from app.agents.trip_planner_agent import MultiAgentTripPlanner, PlanGenerationError
from app.services.llm_service import LLMService

from .decisions import DecisionResponse, contradiction, judge_decision, parse_decision
from .graders import grade_constraints

STRICT_PROMPT = """\n逐项核查用户的硬约束：城市、完整日期、人数、必选景点、每天数量、预算和开放条件。
用户明确指定的数量优先于默认每天2至3个。缺失价格或开放证据时明确说明无法确认。
人数写入travelers，房间数写入rooms；具体到访时间写入景点start_time/end_time。
如果用户条件在逻辑上互相冲突，请返回 {"infeasible":true,"reason":"说明具体冲突","days":[]}。
生成后重新核查全部条件再输出。"""

DECISION_PROMPT = """\n如果条件冲突或必选地点无法核实，请输出结构化答复，不生成行程：
{"response_type":"infeasible或clarification","reason":"具体原因及证据范围","question":"需要用户确认的问题",
"missing_pois":["未核实地点"],"conflicts":[{"kind":"daily_count","min":4,"max":1}],"source":"model"}。
没有对应问题的数组应为空。不得把目录未收录宣称为现实不存在。
缺少票价、住宿报价、开放时间时，在overall_suggestions中列出尚未核实的条件。
"""


class RecordedLLM(LLMService):
    def __init__(self, config, strategy):
        super().__init__()
        self.config, self.strategy = config, strategy
        self.calls, self.http_calls = [], []
        self.raw_plan = None
        self.feedback = ""
        self.user_contract = []
        self.selected_tool = "unobserved"
        self.checkpoint = lambda: None
        self.client.close()
        from openai import OpenAI

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=min(60, config.timeout_seconds), max_retries=0,
                             http_client=httpx.Client(event_hooks={"request": [self._request], "response": [self._response]}))

    def _request(self, request):
        self.http_calls.append({"payload": json.loads(request.content), "started": time.perf_counter(), "response": None})
        self.checkpoint()

    def _response(self, response):
        response.read()
        entry = self.http_calls[-1]
        entry["latency_ms"] = (time.perf_counter() - entry.pop("started")) * 1000
        entry["status_code"] = response.status_code
        if response.headers.get("content-type", "").startswith("application/json"):
            entry["response"] = response.json()
        else:
            entry["response"] = {"non_json_response": True}
        self.checkpoint()

    def _create(self, messages, **kwargs):
        kwargs["temperature"] = self.config.temperature
        kwargs["seed"] = self.config.seed
        entry = {"request": {"messages": copy.deepcopy(messages), **kwargs}}
        self.calls.append(entry)
        result = super()._create(messages, **kwargs)
        entry["response"] = result.model_dump(mode="json")
        self.checkpoint()
        return result

    def call_json(self, system_prompt, user_prompt, **kwargs):
        if self.strategy != "A":
            system_prompt += STRICT_PROMPT + DECISION_PROMPT
            user_prompt += "\n用户明确提出的条件：" + json.dumps(self.user_contract, ensure_ascii=False)
        if self.feedback:
            user_prompt += "\n上次输出存在以下问题，请重新规划：" + self.feedback
        result, meta = super().call_json(system_prompt, user_prompt, **kwargs)
        self.raw_plan = copy.deepcopy(result)
        return result, meta

    def call_with_tool(self, *args, **kwargs):
        arguments, meta = super().call_with_tool(*args, **kwargs)
        response = self.calls[-1].get("response", {})
        choices = response.get("choices", [])
        selected = choices[0].get("message", {}).get("tool_calls") or [] if choices else []
        self.selected_tool = selected[0]["function"]["name"] if selected else "unobserved"
        return arguments, meta


class ReplayLLM(RecordedLLM):
    def __init__(self, recording, config, strategy):
        self.config, self.strategy = config, strategy
        self.recording = copy.deepcopy(recording)
        self.calls, self.http_calls, self.trace = [], [], []
        self.raw_plan, self.feedback = None, ""
        self.position = 0
        self.user_contract = []
        self.selected_tool = "unobserved"
        self.checkpoint = lambda: None

    def _create(self, messages, **kwargs):
        kwargs["temperature"] = self.config.temperature
        kwargs["seed"] = self.config.seed
        request = {"messages": messages, **kwargs}
        if self.position >= len(self.recording):
            raise RuntimeError("模型录制已耗尽")
        entry = self.recording[self.position]
        if request != entry["request"]:
            raise RuntimeError("重放请求与录制请求不同")
        self.position += 1
        self.calls.append(copy.deepcopy(entry))
        if "response" not in entry:
            raise RuntimeError("该调用未录制模型响应，无法重放")
        return ChatCompletion.model_validate(entry["response"])


class ConstraintError(PlanGenerationError):
    pass


class StrategyPlanner(MultiAgentTripPlanner):
    def __init__(self, task, snapshot, strategy, llm, environment):
        super().__init__(llm=llm, tool_executor=environment)
        self.task, self.snapshot, self.strategy = task, snapshot, strategy
        self.environment = environment
        self.candidate_plan = None
        self.evidence_gaps = []
        self.llm.user_contract = [{"kind": c.kind, "value": c.value} for c in task.constraints if c.hard]

    def plan_trip(self, request, on_step=None, should_cancel=None):
        if self.strategy != "A":
            decision = contradiction(self.task)
            if decision:
                if on_step:
                    on_step({"step": "规划前条件检查", "ok": True, "mode": "preflight", "detail": decision["reason"]})
                raise DecisionResponse(decision)
        return super().plan_trip(request, on_step, should_cancel)

    def _step_attractions(self, request, result):
        pois = super()._step_attractions(request, result)
        if self.strategy == "A":
            return pois
        required = next((c.value for c in self.task.constraints if c.kind == "required_pois"), [])
        merged = {p["id"]: p for p in pois}
        for name in required:
            if any(p["name"] == name or name in p.get("aliases", []) for p in merged.values()):
                continue
            response = self._tool_step(step=f"必选景点核实({name})", tool_name="amap_search_poi",
                                       system_prompt="请调用景点搜索工具，keywords必须保留给定地点完整名称，city使用给定城市。",
                                       user_prompt=f"城市：{request.city}\n地点：{name}", result=result, count_key="count")
            for poi in (response or {}).get("pois", []):
                merged[poi["id"]] = poi
        missing = [name for name in required if not any(p["name"] == name or name in p.get("aliases", []) for p in merged.values())]
        searched = {c["arguments"].get("keywords") for c in self.environment.calls
                    if c["tool"] == "amap_search_poi" and not c.get("error") and not c.get("invalid_response")}
        if missing and set(missing) <= searched:
            raise DecisionResponse({"response_type": "clarification", "reason": "在当前城市的冻结目录中按完整名称查询后，仍无法核实必选地点。",
                                    "question": "请确认地点名称或补充地址。", "missing_pois": missing, "source": "preflight"})
        ordered = list(merged.values())
        ordered.sort(key=lambda p: not any(p["name"] == name or name in p.get("aliases", []) for name in required))
        return ordered

    def _validate(self, data, request, result):
        decision = parse_decision(data)
        if decision:
            proof_task = self.task.model_copy(update={"expected_response": decision["response_type"]})
            if not judge_decision(proof_task, decision, self.snapshot, self.environment.calls):
                raise ConstraintError("拒绝或询问缺少可验证证据。请生成符合用户条件的行程，并明确列出尚未核实的价格、开放时间等信息。")
            raise DecisionResponse(decision)
        return super()._validate(data, request, result)

    def _tool_step(self, **kwargs):
        try:
            data = super()._tool_step(**kwargs)
        except (ValueError, TypeError, KeyError):
            if self.strategy != "C":
                raise
            data = None
        if self.strategy == "C" and (data is None or not data.get(kwargs["count_key"])):
            self._emit({"step": "策略工具恢复", "tool": kwargs["tool_name"], "ok": True, "mode": "agent_strategy_retry"})
            self.environment.recovery_attempt = True
            try:
                data = super()._tool_step(**kwargs)
            finally:
                self.environment.recovery_attempt = False
        return data

    def _step_plan(self, request, pois, forecasts, hotels, result):
        attempts = 2 if self.strategy == "C" else 1
        for attempt in range(attempts):
            try:
                plan = super()._step_plan(request, pois, forecasts, hotels, result)
                self.candidate_plan = plan.model_dump()
                if self.strategy != "A":
                    checks = grade_constraints(self.task, plan.model_dump(), self.llm.raw_plan or {}, self.snapshot)
                    failed = [c for c in checks if c["status"] == "fail"]
                    if failed:
                        raise ConstraintError(json.dumps(failed, ensure_ascii=False))
                    self.evidence_gaps = [c["name"] for c in checks if c["status"] == "unverifiable"]
                    if self.evidence_gaps:
                        notice = "尚未核实的条件：" + "、".join(self.evidence_gaps)
                        plan.overall_suggestions += "\n" + notice
                        result.warnings.append(notice)
                return plan
            except PlanGenerationError as exc:
                if attempt + 1 == attempts:
                    raise
                self.llm.feedback = str(exc)
                self.candidate_plan = None
                self.llm.raw_plan = None
                self._emit({"step": "策略重新规划", "ok": True, "mode": "agent_replan", "detail": str(exc)})
        raise AssertionError("规划循环必须返回结果或抛出异常")

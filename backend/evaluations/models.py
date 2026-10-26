from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.schemas import TripRequest


class Constraint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["city", "dates", "required_pois", "daily_count", "budget", "travelers", "opening", "visit_window", "weather", "hotel", "infeasible"]
    value: Any
    source: str
    rule: str
    hard: bool = True


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    category: str
    difficulty: Literal["easy", "medium", "hard"]
    user_query: str
    request: TripRequest
    constraints: list[Constraint]
    soft_preferences: list[str] = Field(default_factory=list)
    environment_snapshot: str
    grading_config: dict = Field(default_factory=lambda: {"aggregation": "all_hard_and_valid_pois"})
    fault: dict | None = None
    expected_response: Literal["plan", "infeasible", "clarification"] = "plan"

    @model_validator(mode="after")
    def check_rules(self):
        if self.grading_config != {"aggregation": "all_hard_and_valid_pois"}:
            raise ValueError("不支持的评分聚合规则")
        if len({c.kind for c in self.constraints}) != len(self.constraints):
            raise ValueError("约束类型重复")
        if not self.constraints or not all(c.source and c.rule for c in self.constraints):
            raise ValueError("每条约束必须声明证据和规则")
        return self


class Check(BaseModel):
    name: str
    status: Literal["pass", "fail", "unverifiable", "not_applicable"]
    source: str
    detail: Any = None


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_type: Literal["infeasible", "clarification"]
    reason: str = Field(min_length=1)
    question: str = Field(min_length=1)
    missing_pois: list[str] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    source: Literal["model", "preflight"] = "model"


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategies: list[Literal["A", "B", "C"]] = ["A", "B", "C"]
    repeats: int = Field(default=1, ge=1)
    temperature: float = Field(default=0.0, ge=0, le=2)
    parallelism: int = Field(default=1, ge=1, le=8)
    seed: int = 42
    timeout_seconds: float = Field(default=240, gt=0)
    input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)
    price_source: str | None = None

    @model_validator(mode="after")
    def pricing(self):
        prices = (self.input_cost_per_million, self.output_cost_per_million)
        if any(p is not None for p in prices) and (any(p is None for p in prices) or not self.price_source):
            raise ValueError("费用计算必须同时提供输入输出单价和价格来源")
        if not self.strategies or len(set(self.strategies)) != len(self.strategies):
            raise ValueError("策略列表必须非空且唯一")
        return self

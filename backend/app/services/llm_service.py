"""LLM 服务：OpenAI 兼容客户端 + 原生 function calling + JSON 模式

为什么不再用 hello_agents
------------------------
原来用 SimpleAgent 的"文本 ReAct"协议（把 `[TOOL_CALL:name:args]` 写进
system prompt，再用正则解析）。但实际模型原生就会发出 function call，
于是模型吐的是自己的特殊标记（形如 `<｜｜DSML｜｜ calls>`），正则匹配不到，
工具永远不会被执行——整个 MCP 工具链 0 次真实调用。

这里改成标准 OpenAI function calling：
- 参数抽取用 `tools` + `tool_choice`（强制指定工具）；
- 结构化输出用 `response_format={"type":"json_object"}`。
所有兼容层（thinking 参数、tool_choice 被拒等）都做了自动降级。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from ..config import get_settings

JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(RuntimeError):
    """LLM 调用或解析失败"""


def _usage_of(resp: Any) -> Dict[str, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
    }


def extract_json(text: str) -> Dict[str, Any]:
    """从模型输出里尽力抠出一个 JSON 对象"""
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回为空")

    candidates: List[str] = []
    match = JSON_BLOCK.search(text)
    if match:
        candidates.append(match.group(1).strip())
    candidates.append(text)

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise LLMError(f"无法从模型输出中解析 JSON（前 200 字）：{text[:200]}")


class LLMService:
    """对 openai SDK 的薄封装，专注两件事：抽参数、出 JSON"""

    def __init__(self) -> None:
        settings = get_settings()

        self.api_key = (
            os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or settings.openai_api_key
        )
        self.base_url = (
            os.getenv("LLM_BASE_URL") or settings.openai_base_url or "https://api.openai.com/v1"
        )
        self.model = os.getenv("LLM_MODEL_ID") or settings.openai_model or "gpt-4o-mini"
        self.timeout = float(os.getenv("LLM_TIMEOUT", "120"))

        if not self.api_key:
            raise LLMError(
                "LLM_API_KEY 未配置。请在 backend/.env 设置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID"
            )

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)

        # DeepSeek 系模型默认开思考模式，思考模式下不支持强制 tool_choice，
        # 且响应更慢。默认关闭；用 LLM_THINKING=enabled 可恢复。
        thinking_disabled = os.getenv("LLM_THINKING", "disabled").strip().lower() != "enabled"
        self._extra_body: Optional[Dict[str, Any]] = (
            {"thinking": {"type": "disabled"}} if thinking_disabled else None
        )
        self._forced_tool = True
        self.trace: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ 底层
    def _create(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        """调用 chat/completions，并对不兼容参数做自动降级"""
        last_error: Optional[Exception] = None

        for _ in range(4):
            payload: Dict[str, Any] = {"model": self.model, "messages": messages, **kwargs}
            if self._extra_body is not None:
                payload["extra_body"] = self._extra_body
            if not self._forced_tool and "tool_choice" in payload:
                payload["tool_choice"] = "auto"

            try:
                return self.client.chat.completions.create(**payload)
            except Exception as exc:  # noqa: BLE001 - 需要按错误内容分支
                last_error = exc
                message = str(exc)

                if self._extra_body is not None and (
                    "thinking" in message or "extra_body" in message or "unknown_parameter" in message
                ):
                    self._extra_body = None
                    continue

                if "tool_choice" in message:
                    self._forced_tool = False
                    continue

                raise LLMError(f"LLM 调用失败：{message}") from exc

        raise LLMError(f"LLM 调用失败（重试耗尽）：{last_error}")

    # ------------------------------------------------------------------ 抽参数
    def call_with_tool(
        self,
        system_prompt: str,
        user_prompt: str,
        tool: Dict[str, Any],
        temperature: float = 0.1,
        max_tokens: int = 500,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """强制模型调用指定工具，返回 (工具参数, 调用元信息)

        模型没按格式走时，退化为"从文本里抠 JSON 参数"，仍然把控制权留在我们手里：
        工具一定由本地代码执行，模型只负责给参数。
        """
        tool_name = tool["function"]["name"]
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        started = time.perf_counter()
        resp = self._create(
            messages,
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            temperature=temperature,
            max_tokens=max_tokens,
        )
        elapsed = int((time.perf_counter() - started) * 1000)

        choice = resp.choices[0].message
        usage = _usage_of(resp)
        arguments: Optional[Dict[str, Any]] = None

        tool_calls = getattr(choice, "tool_calls", None) or []
        if tool_calls:
            raw_args = tool_calls[0].function.arguments or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = None
            if arguments is None:
                try:
                    arguments = extract_json(raw_args)
                except LLMError:
                    arguments = None

        if arguments is None:
            # 兜底：模型把参数写成了正文
            try:
                arguments = extract_json(choice.content or "")
            except LLMError as exc:
                raise LLMError(
                    f"模型未按要求返回 {tool_name} 的参数：{(choice.content or '')[:200]}"
                ) from exc

        meta = {
            "tool": tool_name,
            "mode": "function_call" if tool_calls else "text_fallback",
            "usage": usage,
            "latency_ms": elapsed,
        }
        self.trace.append(meta)
        return arguments, meta

    # ------------------------------------------------------------------ 出 JSON
    def call_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.5,
        max_tokens: int = 6000,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """要求模型输出 JSON 对象（DeepSeek/OpenAI 均支持 json_object）"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        started = time.perf_counter()

        try:
            resp = self._create(
                messages,
                response_format={"type": "json_object"},
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except LLMError:
            # 某些兼容端点不支持 response_format，退回纯文本 + 本地解析
            resp = self._create(
                messages, temperature=temperature, max_tokens=max_tokens
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        content = resp.choices[0].message.content or ""
        try:
            data = extract_json(content)
        except LLMError:
            # 再给一次机会，明确提醒只输出 JSON
            messages.append({"role": "assistant", "content": content[:2000]})
            messages.append(
                {
                    "role": "user",
                    "content": "上一条不是合法 JSON。请只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。",
                }
            )
            retry = self._create(
                messages, response_format={"type": "json_object"}, max_tokens=max_tokens
            )
            content = retry.choices[0].message.content or ""
            data = extract_json(content)

        meta = {
            "mode": "json_object",
            "usage": _usage_of(resp),
            "latency_ms": elapsed,
        }
        self.trace.append(meta)
        return data, meta

    # ------------------------------------------------------------------ 纯文本
    def chat(self, system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
        resp = self._create(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""


_llm: Optional[LLMService] = None


def get_llm() -> LLMService:
    global _llm
    if _llm is None:
        _llm = LLMService()
    return _llm


def reset_llm() -> None:
    global _llm
    _llm = None

"""Model adapters: one interface for every frontier model on the calibration panel.

    adapter = get_adapter("claude-opus-5-5")   # Anthropic SDK
    adapter = get_adapter("openai:gpt-x")      # any OpenAI-compatible endpoint (OpenAI, LiteLLM proxy,
                                               # Gemini's OpenAI-compatible API) via SEATRAIL_OPENAI_BASE_URL
    adapter = get_adapter("mock:golden")       # deterministic scripted model for tests and demos
    policy = adapter.session()                 # one conversation = one rollout

Tests never need an API key: register MockAdapters (or pass them to calibrate() directly).
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable

from ..sandbox_runner.runner import AgentTurn, Policy, ToolCall

# USD per million tokens (input, output). Override with SEATRAIL_MODEL_PRICES='{"model": [in, out]}'.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.0, 40.0),
}
PRICES.update({k: tuple(v) for k, v in json.loads(os.environ.get("SEATRAIL_MODEL_PRICES", "{}")).items()})


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    return (input_tokens * p_in + output_tokens * p_out) / 1_000_000


class ModelAdapter:
    name: str

    def session(self) -> Policy: ...


# ---------------------------------------------------------------------------
# Anthropic (Claude) via the official SDK
# ---------------------------------------------------------------------------
class _AnthropicSession(Policy):
    def __init__(self, adapter: "AnthropicAdapter"):
        self.a = adapter
        self.name = adapter.name
        self.messages: list = []

    def start(self, system, prompt, tools):
        self.system = system
        self.tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
                      for t in tools]
        self.messages = [{"role": "user", "content": prompt}]

    def act(self, results):
        if results:
            blocks = []
            for r in results:
                content: list | str = r.output
                if r.image_b64:
                    content = [{"type": "text", "text": r.output},
                               {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                            "data": r.image_b64}}]
                blocks.append({"type": "tool_result", "tool_use_id": r.call_id, "content": content,
                               **({"is_error": True} if r.is_error else {})})
            self.messages.append({"role": "user", "content": blocks})
        t0 = time.monotonic()
        resp = self.a.client.beta.messages.create(
            model=self.a.model, max_tokens=16000, system=self.system, tools=self.tools, messages=self.messages,
            thinking={"type": "adaptive"}, output_config={"effort": self.a.effort},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        latency = time.monotonic() - t0
        self.messages.append({"role": "assistant", "content": resp.content})
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in resp.content if b.type == "tool_use"]
        u = resp.usage
        return AgentTurn(text=text, tool_calls=calls, stop_reason=resp.stop_reason or "end_turn",
                         input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                         cost_usd=cost(self.a.model, u.input_tokens, u.output_tokens), latency_s=latency)


class AnthropicAdapter(ModelAdapter):
    def __init__(self, model: str, client=None, effort: str = "high"):
        import anthropic
        self.name = self.model = model
        self.client = client or anthropic.Anthropic()
        self.effort = effort

    def session(self):
        return _AnthropicSession(self)


# ---------------------------------------------------------------------------
# Non-Claude models through an OpenAI-compatible chat completions endpoint (LiteLLM, etc.)
# ---------------------------------------------------------------------------
class _OpenAISession(Policy):
    def __init__(self, adapter: "OpenAICompatibleAdapter"):
        self.a = adapter
        self.name = adapter.name

    def start(self, system, prompt, tools):
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["input_schema"]}} for t in tools]
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    def act(self, results):
        for r in results or []:
            self.messages.append({"role": "tool", "tool_call_id": r.call_id, "content": r.output})
        t0 = time.monotonic()
        resp = self.a.http.post(f"{self.a.base_url}/chat/completions", json={
            "model": self.a.model, "messages": self.messages, "tools": self.tools},
            headers={"authorization": f"Bearer {self.a.api_key}"}, timeout=600)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        self.messages.append(msg)
        calls = [ToolCall(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"] or "{}"))
                 for c in msg.get("tool_calls") or []]
        u = data.get("usage") or {}
        it, ot = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        return AgentTurn(text=msg.get("content") or "", tool_calls=calls,
                         stop_reason="tool_use" if calls else data["choices"][0].get("finish_reason", "stop"),
                         input_tokens=it, output_tokens=ot, cost_usd=cost(self.a.name, it, ot),
                         latency_s=time.monotonic() - t0)


class OpenAICompatibleAdapter(ModelAdapter):
    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None, http=None):
        import httpx
        self.name = f"openai:{model}"
        self.model = model
        self.base_url = (base_url or os.environ.get("SEATRAIL_OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("SEATRAIL_OPENAI_API_KEY", "")
        if not self.base_url:
            raise ValueError("set SEATRAIL_OPENAI_BASE_URL to use an OpenAI-compatible model")
        self.http = http or httpx.Client()

    def session(self):
        return _OpenAISession(self)


# ---------------------------------------------------------------------------
# Deterministic mock models
# ---------------------------------------------------------------------------
Script = Callable[[int], list[tuple[str, dict] | str]]


class _MockSession(Policy):
    def __init__(self, name: str, steps: list, tokens: tuple[int, int], model_for_price: str):
        self.name = name
        self.steps = steps
        self.tokens = tokens
        self.model_for_price = model_for_price
        self.i = 0

    def start(self, system, prompt, tools):
        self.i = 0

    def act(self, results):
        it, ot = self.tokens
        c = cost(self.model_for_price, it, ot)
        if self.i >= len(self.steps):
            return AgentTurn(text="I am done.", stop_reason="end_turn", input_tokens=it, output_tokens=ot, cost_usd=c)
        step = self.steps[self.i]
        self.i += 1
        if isinstance(step, str):
            if step == "!raise":
                raise RuntimeError("mock model endpoint unavailable")
            return AgentTurn(text=step, stop_reason="end_turn", input_tokens=it, output_tokens=ot, cost_usd=c)
        name, args = step
        return AgentTurn(text="", tool_calls=[ToolCall(f"m{self.i}", name, args)], stop_reason="tool_use",
                         input_tokens=it, output_tokens=ot, cost_usd=c)


class MockAdapter(ModelAdapter):
    """`script(k)` returns the tool calls for the k-th rollout (k = 0, 1, ...). A string step ends
    the turn with that text; "!raise" simulates an API outage."""

    def __init__(self, name: str, script: Script, tokens: tuple[int, int] = (1200, 300),
                 price_as: str = "claude-sonnet-5-5"):
        self.name = name
        self.script = script
        self.tokens = tokens
        self.price_as = price_as
        self._k = 0

    def session(self):
        steps = self.script(self._k)
        self._k += 1
        return _MockSession(self.name, steps, self.tokens, self.price_as)


_REGISTRY: dict[str, Callable[[], ModelAdapter]] = {}


def register(name: str, factory: Callable[[], ModelAdapter]) -> None:
    _REGISTRY[name] = factory


def get_adapter(name: str) -> ModelAdapter:
    if name in _REGISTRY:
        return _REGISTRY[name]()
    if name.startswith("claude-"):
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise ValueError(f"{name} needs ANTHROPIC_API_KEY on the server")
        return AnthropicAdapter(name)
    if name.startswith("openai:"):
        return OpenAICompatibleAdapter(name.split(":", 1)[1])
    raise ValueError(f"unknown model {name!r}; register a mock or use claude-* / openai:<model>")


def default_panel() -> list[str]:
    return [m.strip() for m in os.environ.get("SEATRAIL_CALIBRATION_MODELS", "claude-opus-5-5").split(",") if m.strip()]

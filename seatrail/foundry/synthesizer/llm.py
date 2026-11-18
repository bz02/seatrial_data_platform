"""JSON-in/JSON-out model calls for the synthesis stages, behind one small interface so every test
runs on deterministic mocks and production can swap providers.

    model.complete(stage, system, prompt, schema) -> (dict, cost_usd)

`stage` is "audit", "transform" or "critic"; real models ignore it, mocks use it to pick a reply.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from ..calibrator.adapters import cost


class ModelRefused(RuntimeError):
    pass


class TextModel:
    name = "model"

    def complete(self, stage: str, system: str, prompt: str, schema: dict) -> tuple[dict, float]: ...


class AnthropicTextModel(TextModel):
    """Claude with adaptive thinking and structured JSON output. Refusals fall back server-side."""

    def __init__(self, model: str, client=None, effort: str = "high"):
        import anthropic
        self.name = self.model = model
        self.client = client or anthropic.Anthropic()
        self.effort = effort

    def complete(self, stage, system, prompt, schema):
        resp = self.client.beta.messages.create(
            model=self.model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            raise ModelRefused(f"{self.model} declined the {stage} step")
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        return json.loads(text), cost(self.model, u.input_tokens, u.output_tokens)


Reply = dict | Callable[[str], dict] | list
class MockTextModel(TextModel):
    """`replies[stage]` is a dict, a list of dicts (one per call, last one repeats) or a function
    of the prompt. Each call is charged as `tokens` of `price_as`."""

    def __init__(self, name: str, replies: dict[str, Reply], tokens: tuple[int, int] = (3000, 800),
                 price_as: str = "claude-sonnet-5-5"):
        self.name = name
        self.replies = replies
        self.tokens = tokens
        self.price_as = price_as
        self.calls: list[tuple[str, str]] = []

    def complete(self, stage, system, prompt, schema):
        self.calls.append((stage, prompt))
        r: Any = self.replies[stage]
        if isinstance(r, list):
            n = sum(1 for s, _ in self.calls if s == stage) - 1
            r = r[min(n, len(r) - 1)]
        if callable(r):
            r = r(prompt)
        return json.loads(json.dumps(r)), cost(self.price_as, *self.tokens)


_REGISTRY: dict[str, Callable[[], TextModel]] = {}


def register(name: str, factory: Callable[[], TextModel]) -> None:
    _REGISTRY[name] = factory


def get_text_model(name: str) -> TextModel:
    if name in _REGISTRY:
        return _REGISTRY[name]()
    if name.startswith("claude-"):
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise ValueError(f"{name} needs ANTHROPIC_API_KEY on the server")
        return AnthropicTextModel(name)
    raise ValueError(f"unknown synthesis model {name!r}; register a mock or use claude-*")


def default_models() -> tuple[str, str]:
    """(author model, critic model). The critic should not be the model that wrote the task."""
    return (os.environ.get("SEATRAIL_SYNTH_MODEL", "claude-opus-5-5"),
            os.environ.get("SEATRAIL_CRITIC_MODEL", "claude-sonnet-5-5"))

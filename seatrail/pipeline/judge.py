"""Model-assisted grading of a trajectory's reasoning.

The verifier checks *what* was done. The judge checks *how well it was explained*: whether the
narration is correct, specific, consistent with the actions, and teachable. A strong model does
this with a fixed rubric and returns structured JSON. Without credentials, or when an enterprise
policy forbids sending data out, we use a transparent heuristic instead and say so in the result.
"""
from __future__ import annotations

import json
import os
import re

from .. import config
from .convert import decision_coverage, thought_text

RUBRIC = [
    ("correctness", "Is the reasoning factually and professionally correct for this domain?"),
    ("specificity", "Does it cite concrete evidence (amounts, ids, rules) rather than generic statements?"),
    ("consistency", "Do the stated reasons match the actions actually taken?"),
    ("coverage", "Are the important decisions, especially exceptions and judgment calls, explained?"),
    ("teachability", "Would a junior professional, or a model, learn the method from this trace?"),
]
FLAGS = ["reasoning_contradicts_actions", "generic_or_filler", "possible_copy_paste", "unsafe_or_policy_violation",
         "residual_sensitive_data", "task_not_completed"]

SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {"type": "integer", "description": "1-5"} for k, _ in RUBRIC},
        "overall": {"type": "integer", "description": "1-5"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "flags": {"type": "array", "items": {"type": "string", "enum": FLAGS}},
        "feedback_to_expert": {"type": "string"},
    },
    "required": [k for k, _ in RUBRIC] + ["overall", "strengths", "issues", "flags", "feedback_to_expert"],
    "additionalProperties": False,
}

SYSTEM = """You review expert work traces that will become training data for AI agents.
Each trace has a task, then numbered steps. A step has the expert's spoken reasoning (may be empty)
and the action they took. You also get the automated verification result when one exists.

Score each rubric item from 1 (poor) to 5 (excellent):
{rubric}

Then give an overall score from 1 to 5, short strengths and issues, any flags that apply, and two or
three sentences of feedback addressed to the expert telling them how to make the trace more useful.
Judge the reasoning, not the expert's writing style. Empty reasoning on routine steps is fine;
missing reasoning on exceptions and judgment calls is not."""


def _trace_text(record: dict, verification: dict | None, max_chars: int = 60000) -> str:
    lines = [f"TASK: {record['task']['title']}", f"INSTRUCTIONS: {record['task']['instructions']}", ""]
    for s in record["steps"]:
        a = s["action"]
        if a is None:
            lines.append(f"[{s['i']}] closing note: {s['thought']}")
            continue
        if a["tool"] == "view":
            act = f"view {a['args'].get('target')}"
        else:
            act = f"{a['tool']} {json.dumps(a['args'], ensure_ascii=False)}"
        lines.append(f"[{s['i']}] reasoning: {s['thought'] or '-'}\n     action: {act}")
    if verification:
        lines.append("")
        lines.append(f"VERIFICATION: score={verification['score']} passed={verification['passed']}")
        for c in verification["checks"]:
            lines.append(f"  {c['name']}: {c['detail']}")
    elif record["outcome"].get("label"):
        lines.append(f"\nSELF-REPORTED OUTCOME: {record['outcome']['label']}")
    text = "\n".join(lines)
    return text[:max_chars]


def _has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def judge(record: dict, verification: dict | None, policy: dict | None = None) -> dict:
    policy = policy or {}
    mode = policy.get("judge", config.JUDGE_ENABLED)
    if mode == "off":
        return heuristic(record, verification, reason="model grading disabled by policy")
    if mode == "auto" and not _has_credentials():
        return heuristic(record, verification, reason="no model credentials configured")
    try:
        return model_judge(record, verification, base_url=policy.get("judge_base_url"))
    except Exception as e:  # network, auth, refusal: never block the pipeline on the grader
        return heuristic(record, verification, reason=f"model grading failed: {type(e).__name__}")


def model_judge(record: dict, verification: dict | None, base_url: str | None = None) -> dict:
    import anthropic

    client = anthropic.Anthropic(base_url=base_url) if base_url else anthropic.Anthropic()
    rubric = "\n".join(f"- {k}: {q}" for k, q in RUBRIC)
    resp = client.beta.messages.create(
        model=config.JUDGE_MODEL,
        max_tokens=16000,
        system=SYSTEM.format(rubric=rubric),
        messages=[{"role": "user", "content": _trace_text(record, verification)}],
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("grader refused")
    text = next(b.text for b in resp.content if b.type == "text")
    out = json.loads(text)
    out["score"] = round((out["overall"] - 1) / 4, 4)
    out["mode"] = "model"
    out["model"] = resp.model
    return out


def heuristic(record: dict, verification: dict | None, reason: str = "") -> dict:
    """Cheap stand-in for the model grader. Rewards explained, specific, non-repetitive narration."""
    steps = [s for s in record["steps"] if s["action"] and s["action"]["tool"] not in ("view",)]
    n_explained, n_total = decision_coverage(record)
    coverage = n_explained / n_total if n_total else 0.0
    text = thought_text(record)
    words = text.split()
    numbers = len(re.findall(r"\$?\d[\d,]*\.?\d*", text))
    ids = len(re.findall(r"\b[A-Z]{1,4}-?\d{2,6}[A-Z]?\b", text))
    specificity = min(1.0, (numbers + ids) / max(4, len(steps) * 0.5))
    # Mean type-token ratio over 100-word windows, so long traces aren't penalized for length.
    windows = [words[i:i + 100] for i in range(0, len(words), 100)] or [[]]
    uniq = sum(len(set(w.lower() for w in win)) / len(win) for win in windows if win) / max(1, len([w for w in windows if w]))
    depth = min(1.0, len(words) / max(40, len(steps) * 8))
    passed = verification["passed"] if verification else record["outcome"].get("label") == "success"

    def five(x):
        return max(1, min(5, round(1 + 4 * x)))

    scores = {"correctness": five(1.0 if passed else 0.4), "specificity": five(specificity),
              "consistency": five(0.6 + 0.4 * coverage if text else 0.2), "coverage": five(coverage),
              "teachability": five((depth + coverage + min(1, uniq * 1.5)) / 3)}
    overall = round(sum(scores.values()) / len(scores))
    issues, flags = [], []
    if coverage < config.MIN_THOUGHT_COVERAGE:
        issues.append(f"Only {coverage:.0%} of decisions have spoken reasoning.")
    if specificity < 0.4:
        issues.append("Reasoning rarely cites amounts, ids or rules.")
    if len(words) >= 50 and uniq < 0.35:
        flags.append("generic_or_filler")
    if not passed:
        flags.append("task_not_completed")
    fb = []
    if coverage < 0.8:
        fb.append("Say why before each decision, especially exceptions.")
    if specificity < 0.6:
        fb.append("Mention the exact numbers, ids or rules you are comparing.")
    return {**scores, "overall": overall, "score": round((overall - 1) / 4, 4),
            "strengths": ["Reasoning covers most decisions."] if coverage >= 0.8 else [],
            "issues": issues, "flags": flags,
            "feedback_to_expert": " ".join(fb) or "Clear, well-explained trace.",
            "mode": "heuristic", "note": reason,
            "signals": {"coverage": round(coverage, 3), "specificity": round(specificity, 3),
                        "unique_word_ratio": round(uniq, 3), "words": len(words)}}

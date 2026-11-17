"""Rollouts, isolated grading, environment sanity checks, concurrency caps and the circuit breaker.

A *policy* is anything that, given the tool results of its last turn, returns the next turn. Model
adapters (calibrator/adapters.py), the golden solution and the empty submission are all policies,
so the sanity check exercises exactly the code path calibration uses.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field

from ... import db
from ..models import EnvSpec, GraderResult, Profile
from . import verifiers
from .env import SYSTEM_WORKSPACE, Environment, SheetEnv, build_env, task_prompt
from .grader_isolation import grader_workdir

MAX_SANDBOXES = int(os.environ.get("SEATRAIL_MAX_SANDBOXES", "8"))
_sandbox_slots = threading.BoundedSemaphore(MAX_SANDBOXES)


# ---------------------------------------------------------------------------
# Turns and trajectories
# ---------------------------------------------------------------------------
@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class AgentTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


@dataclass
class ToolOutcome:
    call_id: str
    name: str
    output: str
    is_error: bool = False
    env_error: bool = False
    image_b64: str | None = None


class Policy:
    name = "policy"

    def start(self, system: str, prompt: str, tools: list[dict]) -> None: ...
    def act(self, results: list[ToolOutcome] | None) -> AgentTurn: ...


@dataclass
class Trajectory:
    policy: str
    turns: list[dict] = field(default_factory=list)
    stop_reason: str = ""
    steps: int = 0
    submitted: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    final_text: str = ""
    reads: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    grader: dict = field(default_factory=dict)

    def to_jsonl(self) -> str:
        head = {k: v for k, v in asdict(self).items() if k != "turns"}
        return "\n".join(json.dumps(x, default=str) for x in [{"type": "summary", **head}, *self.turns]) + "\n"


def run_rollout(policy: Policy, env: Environment, spec: EnvSpec, max_steps: int | None = None,
                wall_clock_s: float | None = None) -> Trajectory:
    max_steps = max_steps or spec.max_steps
    deadline = time.monotonic() + (wall_clock_s or spec.wall_clock_s)
    traj = Trajectory(policy=policy.name, files=env.file_list())
    system = "" if isinstance(env, SheetEnv) else SYSTEM_WORKSPACE
    if isinstance(env, SheetEnv):
        from ...pipeline.agent import SYSTEM
        system = SYSTEM
    policy.start(system, task_prompt(spec, env), env.tools())
    results: list[ToolOutcome] | None = None
    stop = "max_steps"
    env_errors = 0
    while traj.steps < max_steps:
        if time.monotonic() > deadline:
            stop = "wall_clock_timeout"
            break
        try:
            turn = policy.act(results)
        except Exception as e:  # model API failure is an environment problem for calibration purposes
            stop = "env_error"
            traj.turns.append({"type": "error", "where": "policy", "error": f"{type(e).__name__}: {e}"})
            break
        traj.input_tokens += turn.input_tokens
        traj.output_tokens += turn.output_tokens
        traj.cost_usd += turn.cost_usd
        traj.latency_s += turn.latency_s
        if turn.text:
            traj.final_text = turn.text
        traj.turns.append({"type": "assistant", "text": turn.text, "stop_reason": turn.stop_reason,
                           "tool_calls": [asdict(c) for c in turn.tool_calls],
                           "usage": {"input": turn.input_tokens, "output": turn.output_tokens}})
        if turn.stop_reason == "refusal":
            stop = "refusal"
            break
        if not turn.tool_calls:
            stop = turn.stop_reason or "end_turn"
            break
        results = []
        for c in turn.tool_calls:
            traj.steps += 1
            r = env.call(c.name, c.args)
            env_errors += r.env_error
            results.append(ToolOutcome(c.id, c.name, r.output, r.is_error, r.env_error, r.image_b64))
            traj.turns.append({"type": "tool_result", "id": c.id, "name": c.name, "output": r.output[:4000],
                               "is_error": r.is_error, "env_error": r.env_error})
            if env.finished:
                break
        if env.finished:
            stop = "finished"
            break
        if env_errors >= 3:
            stop = "env_error"
            break
    traj.stop_reason = stop
    traj.submitted = env.finished
    traj.reads = list(dict.fromkeys(env.reads))
    if not traj.final_text and hasattr(env, "summary"):
        traj.final_text = env.summary()
    return traj


def grade_deliverables(spec: EnvSpec, sealed: dict, deliverables: dict[str, bytes]) -> GraderResult:
    """Host-side, post-rollout grading in a fresh directory. Ground truth only exists in there."""
    with grader_workdir(deliverables) as wd:
        return verifiers.grade(wd, sealed["grader"], spec)


def rollout_and_grade(policy: Policy, spec: EnvSpec, sealed: dict) -> tuple[Trajectory, GraderResult]:
    with _sandbox_slots:
        env = build_env(spec)
        try:
            traj = run_rollout(policy, env, spec)
            deliverables = env.deliverables()
        finally:
            env.close()
    if traj.stop_reason == "env_error":
        g = GraderResult(overall_score=0, passed=False, failure_mode="environment")
    else:
        g = grade_deliverables(spec, sealed, deliverables)
    traj.grader = g.model_dump()
    return traj, g


# ---------------------------------------------------------------------------
# Reference policies for the sanity check
# ---------------------------------------------------------------------------
class ScriptPolicy(Policy):
    """Plays a fixed list of tool calls, one per turn, then stops."""

    def __init__(self, name: str, calls: list[tuple[str, dict]]):
        self.name = name
        self.calls = calls
        self.i = 0

    def start(self, system, prompt, tools):
        self.i = 0

    def act(self, results):
        if self.i >= len(self.calls):
            return AgentTurn(text="done", stop_reason="end_turn")
        name, args = self.calls[self.i]
        self.i += 1
        return AgentTurn(tool_calls=[ToolCall(f"call_{self.i}", name, args)], stop_reason="tool_use")


def golden_policy(spec: EnvSpec, sealed: dict) -> ScriptPolicy:
    golden = sealed.get("golden") or {}
    if spec.profile == Profile.SHEET_ACCOUNTING:
        return ScriptPolicy("golden", golden.get("actions") or sheet_golden_actions(sealed["grader"]["answer"]))
    calls: list[tuple[str, dict]] = []
    write = "write" if spec.mode == "agentic_native" else None
    for path, content in (golden.get("files") or {}).items():
        if write and not content.startswith("base64:"):
            calls.append(("write", {"path": path, "content": content}))
        else:
            # auto mode (or binary): write through the shell, as an agent in that mode would
            calls.append(("bash", {"command": _shell_write(path, content)}))
    calls += [(c["name"], c["args"]) for c in golden.get("actions", [])]
    calls.append(("finish", {"summary": "golden solution"}))
    return ScriptPolicy("golden", calls)


def _shell_write(path: str, content: str) -> str:
    import base64
    import shlex
    data = content[7:] if content.startswith("base64:") else base64.b64encode(content.encode()).decode()
    d = os.path.dirname(path)
    mk = f"mkdir -p {shlex.quote(d)} && " if d else ""
    return f"{mk}printf %s {shlex.quote(data)} | base64 -d > {shlex.quote(path)}"


def empty_policy(spec: EnvSpec) -> ScriptPolicy:
    if spec.profile == Profile.SHEET_ACCOUNTING:
        return ScriptPolicy("empty", [("submit", {})])
    return ScriptPolicy("empty", [("finish", {"summary": ""})])


def sheet_golden_actions(answer: dict) -> list[tuple[str, dict]]:
    acts: list[tuple[str, dict]] = []
    for sheet, rows in answer.get("cells", {}).items():
        for rid, cols in rows.items():
            for col, val in cols.items():
                acts.append(("edit_cell", {"sheet": sheet, "row": rid, "column": col, "value": val}))
    for k, v in answer.get("fields", {}).items():
        acts.append(("set_field", {"field": k, "value": str(v)}))
    for e in answer.get("emails", []):
        words = " ".join(g[0] for g in e.get("must_mention_any", []))
        acts.append(("send_email", {"to": e["to"], "subject": "Follow-up", "body": words}))
    acts.append(("submit", {}))
    return acts


def run_env_sanity_check(task_id: int) -> dict:
    """Golden solution must score exactly 1.0 and an empty submission exactly 0.0, in the same
    pinned environment and through the same isolated grader. Stored per task version; a task
    cannot leave DRAFT (or REWORK) without a passing check for its current version."""
    from .. import store
    row = db.q1("SELECT version FROM foundry_tasks WHERE id=?", task_id)
    if not row:
        raise KeyError(f"task {task_id} not found")
    spec = EnvSpec.model_validate(store.task_env(task_id))
    sealed = store.task_sealed(task_id)
    _, golden = rollout_and_grade(golden_policy(spec, sealed), spec, sealed)
    _, empty = rollout_and_grade(empty_policy(spec), spec, sealed)
    passed = golden.overall_score >= 1.0 - 1e-9 and golden.passed and empty.overall_score == 0.0 and not empty.passed
    detail = {"golden": golden.model_dump(), "empty": empty.model_dump()}
    db.x("INSERT INTO sanity_checks (task_id, task_version, golden_score, empty_score, passed, detail, created_at)"
         " VALUES (?,?,?,?,?,?,?)", task_id, row["version"], golden.overall_score, empty.overall_score,
         int(passed), db.dumps(detail), db.now())
    problems = []
    if golden.overall_score < 1.0 or not golden.passed:
        problems.append(f"golden solution scored {golden.overall_score} ({golden.failure_mode})")
    if empty.overall_score != 0.0 or empty.passed:
        problems.append(f"empty submission scored {empty.overall_score}; the grader gives free credit")
    return {"task_id": task_id, "task_version": row["version"], "passed": passed, "golden_score": golden.overall_score,
            "empty_score": empty.overall_score, "problems": problems}


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
class CircuitBreaker:
    """Opens after `threshold` consecutive environment failures, so a broken image or a dead model
    endpoint does not burn the whole calibration budget producing fake 0% verdicts."""

    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self.consecutive = 0
        self.open = False
        self._lock = threading.Lock()

    def record(self, env_failure: bool) -> None:
        with self._lock:
            self.consecutive = self.consecutive + 1 if env_failure else 0
            if self.consecutive >= self.threshold:
                self.open = True

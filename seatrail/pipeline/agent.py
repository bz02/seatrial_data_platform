"""Frontier-model baseline: let today's best model attempt a sandbox task with the same tools an
expert uses, and grade it with the same hidden answer key.

Why: buyers pay for data on tasks models still fail. Running the baseline tells us which tasks are
worth collecting, lets us pay experts a bonus on them, and gives buyers a measured "model vs expert"
gap for every dataset. The transcript is kept so reviewers can see where the model went wrong.
"""
from __future__ import annotations

import json

from .. import config, db, sandbox
from ..verifier import verify

MAX_TURNS = 40

TOOL_SCHEMAS = {
    "view": {"target": {"type": "string", "description": "brief | sheet:<name> | inbox | email:<id> | fields"}},
    "edit_cell": {"sheet": {"type": "string"}, "row": {"type": "string", "description": "row id"},
                  "column": {"type": "string", "description": "column key"}, "value": {"type": "string"}},
    "set_field": {"field": {"type": "string"}, "value": {"type": "string", "description": "a number"}},
    "send_email": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
    "submit": {},
}

SYSTEM = """You are an experienced staff accountant working in a small company's back office.
You get a task and a workspace of spreadsheets, an inbox and answer fields. Use the tools to look
at the workspace, make the entries the task needs, send any emails it asks for, and call submit
when you are done. Only editable columns can be changed, and some columns accept a fixed list of
values. Before each change, say in one sentence why you are making it."""


def tools_for_api() -> list[dict]:
    out = []
    for t in sandbox.TOOLS:
        props = TOOL_SCHEMAS[t["name"]]
        out.append({"name": t["name"], "description": t["description"],
                    "input_schema": {"type": "object", "properties": props, "required": list(props),
                                     "additionalProperties": False}})
    return out


def observe(state: dict, task: dict, target: str) -> str:
    """What the expert UI would show for a view action, as text."""
    if target == "brief":
        return f"{task['title']}\n\n{task['brief']}"
    if target == "fields":
        return json.dumps([{k: f.get(k) for k in ("key", "label", "value", "readonly")} for f in state["fields"]])
    if target == "inbox":
        return json.dumps([{k: m[k] for k in ("id", "from", "date", "subject")} for m in state["inbox"]])
    if target.startswith("email:"):
        m = next((m for m in state["inbox"] if m["id"] == target[6:]), None)
        return json.dumps(m) if m else "no such email"
    if target.startswith("sheet:"):
        sh = next(s for s in state["sheets"] if s["name"] == target[6:])
        cols = [{k: c.get(k) for k in ("key", "label", "editable", "options") if c.get(k) is not None}
                for c in sh["columns"]]
        return json.dumps({"columns": cols, "rows": sh["rows"]})
    return "unknown target"


def _task_prompt(task: dict, state: dict) -> str:
    sheets = ", ".join(s["name"] for s in state["sheets"])
    return (f"TASK: {task['title']}\n\n{task['brief']}\n\nWorkspace sheets: {sheets}. "
            f"Inbox has {len(state['inbox'])} message(s). Answer fields: "
            + ", ".join(f["key"] for f in state["fields"]))


def run_baseline(task_id: int, client=None, model: str | None = None, max_turns: int = MAX_TURNS) -> dict:
    """Run one model attempt on a task and store the graded result in `baselines`."""
    import anthropic

    task = db.q1("SELECT * FROM tasks WHERE id=?", task_id)
    if not task:
        raise KeyError("task not found")
    model = model or config.BASELINE_MODEL
    client = client or anthropic.Anthropic()
    state = db.loads(task["initial_state"])
    messages: list = [{"role": "user", "content": _task_prompt(task, state)}]
    transcript: list[dict] = []
    steps = 0
    stop = "max_turns"
    for _ in range(max_turns):
        resp = client.beta.messages.create(
            model=model, max_tokens=16000, system=SYSTEM, tools=tools_for_api(), messages=messages,
            thinking={"type": "adaptive"}, output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            stop = "refusal"
            break
        messages.append({"role": "assistant", "content": resp.content})
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                transcript.append({"type": "think", "text": b.text.strip()})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not calls:
            stop = resp.stop_reason or "end_turn"
            break
        results = []
        for call in calls:
            args = dict(call.input or {})
            steps += 1
            try:
                state = sandbox.apply(state, call.name, args)
                out = observe(state, task, args.get("target", "")) if call.name == "view" else "ok"
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": out})
                transcript.append({"type": call.name, "payload": args})
            except sandbox.ActionError as e:
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": str(e), "is_error": True})
                transcript.append({"type": call.name, "payload": args, "error": str(e)})
        messages.append({"role": "user", "content": results})
        if state.get("submitted"):
            stop = "submitted"
            break
    v = verify(state, db.loads(task["answer_key"]), config.VERIFY_PASS_SCORE)
    bid = db.x("INSERT INTO baselines (task_id, model, score, passed, steps, transcript, created_at)"
               " VALUES (?,?,?,?,?,?,?)", task_id, model, v["score"], int(v["passed"]), steps,
               db.dumps({"stop": stop, "events": transcript, "checks": v["checks"]}), db.now())
    return {"id": bid, "task_id": task_id, "model": model, "score": v["score"], "passed": v["passed"],
            "steps": steps, "stop": stop}


def gap_report() -> list[dict]:
    """Per task family: how often the model passes vs how often approved expert work passes."""
    rows = db.q("SELECT t.template, b.task_id, b.passed, b.score FROM baselines b JOIN tasks t ON t.id=b.task_id"
                " WHERE b.id IN (SELECT MAX(id) FROM baselines GROUP BY task_id)")
    by: dict[str, dict] = {}
    for r in rows:
        d = by.setdefault(r["template"], {"template": r["template"], "tasks": 0, "model_passed": 0, "model_score": 0.0})
        d["tasks"] += 1
        d["model_passed"] += r["passed"]
        d["model_score"] += r["score"]
    for d in by.values():
        d["model_pass_rate"] = round(d["model_passed"] / d["tasks"], 3)
        d["model_mean_score"] = round(d.pop("model_score") / d["tasks"], 3)
        e = db.q1("SELECT AVG(json_extract(verification,'$.score')) s FROM records WHERE template=? AND"
                  " source='sandbox' AND status='approved'", d["template"])
        d["expert_mean_score"] = round(e["s"], 3) if e and e["s"] is not None else None
    return sorted(by.values(), key=lambda d: d["model_pass_rate"])

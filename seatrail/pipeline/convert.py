"""Turn an episode's event log into a training record (schema seatrail.trajectory.v1).

Two sources share one record format:
  * sandbox  - marketplace tasks in our spreadsheet + mail sandbox, with a hidden answer key
  * capture  - real work recorded in an enterprise's own web apps by the browser recorder

Narration ("think" events) is attached to the next action as that step's rationale.
"""
from __future__ import annotations

from .. import sandbox
from .pii import Redactor

SCHEMA = "seatrail.trajectory.v1"

BROWSER_TOOLS = [
    {"name": "navigate", "description": "Open a URL.", "parameters": {"url": "string"}},
    {"name": "click", "description": "Click an element.", "parameters": {"element": "element description"}},
    {"name": "type", "description": "Type into a field.", "parameters": {"element": "element description",
                                                                         "text": "string"}},
    {"name": "select", "description": "Choose an option.", "parameters": {"element": "element description",
                                                                          "option": "string"}},
    {"name": "press", "description": "Press a key.", "parameters": {"key": "string"}},
    {"name": "done", "description": "Declare the task finished.", "parameters": {}},
]
CAPTURE_ACTIONS = {"navigate", "click", "input", "select", "key", "submit"}


# ---------------------------------------------------------------------------
# Sandbox observations: what the actor saw after each action, as compact text
# ---------------------------------------------------------------------------
def _fmt(v, typ):
    if typ == "money" and isinstance(v, (int, float)):
        return f"{v:,.2f}"
    return "" if v is None else str(v)


def render_sheet(sheet: dict, max_rows: int = 60) -> str:
    cols = sheet["columns"]
    head = " | ".join(c["label"] + (" (editable)" if c.get("editable") else "") for c in cols)
    lines = [f"Sheet: {sheet['name']}", head]
    for r in sheet["rows"][:max_rows]:
        lines.append(" | ".join(_fmt(r.get(c["key"]), c["type"]) for c in cols))
    for c in cols:
        if c.get("options"):
            lines.append(f"Allowed values for {c['label']}: {', '.join(c['options'])}")
    return "\n".join(lines)


def observe(state: dict, target: str, brief: str) -> str:
    if target == "brief":
        return brief
    if target == "inbox":
        return "\n".join(f"[{m['id']}] {m['date']} from {m['from']}: {m['subject']}" for m in state["inbox"])
    if target.startswith("email:"):
        m = next((m for m in state["inbox"] if m["id"] == target[6:]), None)
        return f"From: {m['from']}\nSubject: {m['subject']}\n\n{m['body']}" if m else "No such email."
    if target.startswith("sheet:"):
        s = next(s for s in state["sheets"] if s["name"] == target[6:])
        return render_sheet(s)
    if target == "fields":
        return "\n".join(f"{f['label']} [{f['key']}]: {_fmt(f.get('value'), f['type']) or '(blank)'}"
                         + (" (read-only)" if f.get("readonly") else "") for f in state["fields"])
    return ""


def _steps_with_thoughts(events: list[dict], action_types: set[str], red: Redactor):
    """Yield (event, thought) pairs, where thought is the narration since the previous action."""
    pending: list[str] = []
    for e in events:
        if e.get("deleted"):
            continue
        if e["type"] == "think":
            t = red.text(str(e["payload"].get("text", "")).strip())
            if t:
                pending.append(t)
            continue
        if e["type"] in action_types:
            yield e, " ".join(pending)
            pending = []
    if pending:  # narration after the last action becomes a closing note
        yield {"type": "_closing", "ts": events[-1]["ts"] if events else 0, "payload": {}}, " ".join(pending)


def sandbox_record(session: dict, task: dict, events: list[dict], verification: dict,
                   redactor: Redactor) -> dict:
    initial = task["initial_state"]
    state = initial
    steps = []
    for e, thought in _steps_with_thoughts(events, sandbox.ACTION_TYPES, redactor):
        if e["type"] == "_closing":
            steps.append({"i": len(steps), "t": round(e["ts"], 1), "thought": thought, "action": None,
                          "observation": None})
            continue
        p = dict(e["payload"])
        if e["type"] == "send_email":
            p = {**p, "subject": redactor.text(p.get("subject", "")), "body": redactor.text(p.get("body", ""))}
        state = sandbox.apply(state, e["type"], e["payload"])
        if e["type"] == "view":
            obs = observe(state, p["target"], task["brief"])
        elif e["type"] == "submit":
            obs = "Task submitted."
        else:
            obs = "OK"
        steps.append({"i": len(steps), "t": round(e["ts"], 1), "thought": thought,
                      "action": {"tool": e["type"], "args": p}, "observation": obs})
    return {
        "schema": SCHEMA,
        "id": f"sandbox-{session['id']}",
        "source": "sandbox",
        "domain": task["domain"],
        "workflow": task["template"],
        "task": {"title": task["title"], "instructions": task["brief"], "difficulty": task["difficulty"]},
        "environment": {"kind": "spreadsheet_mail_sandbox", "tools": sandbox.TOOLS, "initial_state": initial,
                        "task_template": task["template"], "task_seed": task["seed"]},
        "steps": steps,
        "outcome": {"final_state": state, "verification": verification,
                    "label": "success" if verification.get("passed") else "failed"},
        "provenance": {"synthetic_environment": True, "duration_s": round(events[-1]["ts"], 1) if events else 0},
    }


def _element(target: dict | None, red: Redactor) -> str:
    if not target:
        return "page"
    bits = [target.get("role") or target.get("tag") or "element"]
    label = target.get("label") or target.get("text") or target.get("name") or target.get("placeholder")
    if label:
        bits.append(f'"{red.text(str(label))[:80]}"')
    return " ".join(bits)


def capture_record(session: dict, events: list[dict], redactor: Redactor, workflow: str, domain: str) -> dict:
    steps = []
    start_url = None
    for e, thought in _steps_with_thoughts(events, CAPTURE_ACTIONS, redactor):
        p = e["payload"]
        page = p.get("page") or {}
        if e["type"] == "_closing":
            steps.append({"i": len(steps), "t": round(e["ts"], 1), "thought": thought, "action": None,
                          "observation": None})
            continue
        t = e["type"]
        if t == "navigate":
            url = redactor.url(p.get("url", ""))
            start_url = start_url or url
            action = {"tool": "navigate", "args": {"url": url}}
        elif t == "click":
            action = {"tool": "click", "args": {"element": _element(p.get("target"), redactor)}}
        elif t == "input":
            text = "[MASKED]" if p.get("masked") else redactor.text(str(p.get("value", "")))
            action = {"tool": "type", "args": {"element": _element(p.get("target"), redactor), "text": text}}
        elif t == "select":
            action = {"tool": "select", "args": {"element": _element(p.get("target"), redactor),
                                                 "option": redactor.text(str(p.get("value", "")))}}
        elif t == "key":
            action = {"tool": "press", "args": {"key": str(p.get("key", ""))}}
        else:  # form submit
            action = {"tool": "click", "args": {"element": _element(p.get("target"), redactor) + " (submit)"}}
        obs = {"url": redactor.url(page.get("url", "")), "title": redactor.text(page.get("title", ""))}
        steps.append({"i": len(steps), "t": round(e["ts"], 1), "thought": thought, "action": action,
                      "observation": obs})
    goal = redactor.text(session.get("goal") or "")
    return {
        "schema": SCHEMA,
        "id": f"capture-{session['id']}",
        "source": "capture",
        "domain": domain,
        "workflow": workflow,
        "task": {"title": goal[:120], "instructions": goal, "difficulty": None},
        "environment": {"kind": "browser", "tools": BROWSER_TOOLS, "start_url": start_url},
        "steps": steps,
        "outcome": {"label": session.get("outcome") or "unknown", "verification": None},
        "provenance": {"synthetic_environment": False,
                       "duration_s": round(events[-1]["ts"], 1) if events else 0},
    }


# ---------------------------------------------------------------------------
# Export formats
# ---------------------------------------------------------------------------
def _tool_line(t: dict) -> str:
    params = ", ".join(f"{k}: {v}" for k, v in t["parameters"].items())
    return f"- {t['name']}({params}): {t['description']}"


def to_chat(record: dict) -> dict:
    """Agent SFT example: system + user task, then assistant turns that reason and call tools."""
    import json
    tools = record["environment"]["tools"]
    system = ("You are an expert {d} professional operating software through tools. Think step by step, "
              "explain each decision briefly, then act.\nTools:\n{t}").format(d=record["domain"],
                                                                            t="\n".join(_tool_line(t) for t in tools))
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": record["task"]["instructions"] or record["task"]["title"]}]
    n = 0
    for s in record["steps"]:
        if s["action"] is None:
            msgs.append({"role": "assistant", "content": s["thought"]})
            continue
        n += 1
        call_id = f"call_{n}"
        msgs.append({"role": "assistant", "content": s["thought"] or "",
                     "tool_calls": [{"id": call_id, "type": "function",
                                     "function": {"name": s["action"]["tool"],
                                                  "arguments": json.dumps(s["action"]["args"], ensure_ascii=False)}}]})
        obs = s["observation"]
        msgs.append({"role": "tool", "tool_call_id": call_id,
                     "content": obs if isinstance(obs, str) else json.dumps(obs, ensure_ascii=False)})
    return {"id": record["id"], "messages": msgs, "tools": tools,
            "metadata": {"domain": record["domain"], "workflow": record["workflow"], "source": record["source"],
                         "outcome": record["outcome"]["label"], "quality": record.get("quality", {}).get("score")}}


def to_rl_env(record: dict) -> dict | None:
    """RL environment spec: initial state + tools + verifier reference. Sandbox records only."""
    env = record["environment"]
    if env["kind"] != "spreadsheet_mail_sandbox":
        return None
    return {"id": record["id"], "task": record["task"], "tools": env["tools"], "initial_state": env["initial_state"],
            "verifier": {"template": env["task_template"], "seed": env["task_seed"],
                         "how": "seatrail.tasks.generate(template, seed)['answer'] + seatrail.verifier.verify"},
            "reference_trajectory": record["id"]}


def decision_coverage(record: dict, window: int = 3) -> tuple[int, int]:
    """(explained, total) over non-view actions. One narrated thought covers up to `window` actions,
    since people usually explain a small group of related clicks or edits at once."""
    total = explained = 0
    left = 0
    for s in record["steps"]:
        a = s["action"]
        if not a or a["tool"] == "view":
            continue
        total += 1
        if len(s["thought"].split()) >= 4:
            left = window
        if left > 0:
            explained += 1
            left -= 1
    return explained, total


def thought_text(record: dict) -> str:
    return " ".join(s["thought"] for s in record["steps"] if s.get("thought"))

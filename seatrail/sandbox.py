"""Sandbox engine for marketplace tasks.

The sandbox is event-sourced: the client sends actions, the server validates each one, appends it
to the event log, and applies it to the state. The event log is the trajectory we turn into
training data, so the server is the source of truth for what the expert did.
"""
from __future__ import annotations

import copy

# Actions an expert (or later, a model) can take. These double as the tool specs in exported data.
TOOLS = [
    {"name": "view", "description": "Look at part of the workspace.",
     "parameters": {"target": "brief | sheet:<name> | inbox | email:<id> | fields"}},
    {"name": "edit_cell", "description": "Set an editable cell in a sheet.",
     "parameters": {"sheet": "sheet name", "row": "row id", "column": "column key", "value": "new value"}},
    {"name": "set_field", "description": "Fill in a named answer field.",
     "parameters": {"field": "field key", "value": "number"}},
    {"name": "send_email", "description": "Send an email.",
     "parameters": {"to": "address", "subject": "text", "body": "text"}},
    {"name": "submit", "description": "Declare the task finished.", "parameters": {}},
]
ACTION_TYPES = {t["name"] for t in TOOLS}
NOTE_TYPES = {"think"}  # think-aloud narration; attached to the next action as its rationale


class ActionError(ValueError):
    pass


def _sheet(state: dict, name: str) -> dict:
    for s in state["sheets"]:
        if s["name"] == name:
            return s
    raise ActionError(f"no sheet named {name!r}")


def apply(state: dict, etype: str, p: dict) -> dict:
    """Validate one event and return the new state. Raises ActionError if the action is invalid."""
    if etype in NOTE_TYPES:
        if not str(p.get("text", "")).strip():
            raise ActionError("empty narration")
        return state
    if etype not in ACTION_TYPES:
        raise ActionError(f"unknown action {etype!r}")
    if state.get("submitted") and etype != "view":
        raise ActionError("task already submitted")
    s = copy.deepcopy(state)
    if etype == "view":
        target = str(p.get("target", ""))
        if not (target in ("brief", "inbox", "fields") or target.startswith("sheet:") or target.startswith("email:")):
            raise ActionError(f"bad view target {target!r}")
        if target.startswith("sheet:"):
            _sheet(s, target[6:])
    elif etype == "edit_cell":
        sheet = _sheet(s, str(p.get("sheet")))
        col = next((c for c in sheet["columns"] if c["key"] == p.get("column")), None)
        if not col or not col.get("editable"):
            raise ActionError(f"column {p.get('column')!r} is not editable")
        value = "" if p.get("value") is None else str(p.get("value")).strip()
        if value and col.get("options") and value not in col["options"]:
            raise ActionError(f"{value!r} is not one of {col['options']}")
        row = next((r for r in sheet["rows"] if r["id"] == p.get("row")), None)
        if row is None:
            raise ActionError(f"no row {p.get('row')!r}")
        row[col["key"]] = value
    elif etype == "set_field":
        f = next((f for f in s["fields"] if f["key"] == p.get("field")), None)
        if not f or f.get("readonly"):
            raise ActionError(f"field {p.get('field')!r} is not editable")
        v = p.get("value")
        if v in (None, ""):
            f["value"] = None
        else:
            try:
                f["value"] = round(float(str(v).replace(",", "").replace("$", "")), 2)
            except ValueError:
                raise ActionError(f"{v!r} is not a number")
    elif etype == "send_email":
        to = str(p.get("to", "")).strip()
        if "@" not in to:
            raise ActionError("email needs a recipient")
        s["outbox"].append({"to": to, "subject": str(p.get("subject", "")), "body": str(p.get("body", ""))})
    elif etype == "submit":
        s["submitted"] = True
    return s


def public_state(state: dict) -> dict:
    return state


def replay(initial: dict, events: list[dict]) -> dict:
    state = copy.deepcopy(initial)
    for e in events:
        state = apply(state, e["type"], e["payload"])
    return state

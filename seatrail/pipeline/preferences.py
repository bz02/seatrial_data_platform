"""Reviewer corrections → preference pairs, corrected SFT, and evaluation items.

Expert-data vendors mostly ship one "good" answer per task. When a reviewer fixes a step, we keep
both versions: the corrected trajectory becomes SFT data, and the pair (original step, fixed step)
becomes a DPO-style preference example at exactly the point where the human went wrong.
"""
from __future__ import annotations

import copy
import json

from .. import db
from .convert import to_chat

EVAL_SCHEMA = "seatrail.eval.v1"


def load_corrections(record_id: int) -> list[dict]:
    rows = db.q("SELECT step_i, issue, corrected_thought, corrected_action, note, reviewer_id, created_at"
                " FROM step_corrections WHERE record_id=? ORDER BY id", record_id)
    for r in rows:
        r["corrected_action"] = db.loads(r["corrected_action"])
    return rows


def apply_corrections(record: dict, corrections: list[dict]) -> dict:
    """A copy of the record with the latest correction for each step applied."""
    if not corrections:
        return record
    rec = copy.deepcopy(record)
    latest = {c["step_i"]: c for c in corrections}
    for s in rec["steps"]:
        c = latest.get(s["i"])
        if not c:
            continue
        if c["corrected_thought"]:
            s["thought"] = c["corrected_thought"]
        if c["corrected_action"]:
            s["action"] = c["corrected_action"]
            s["observation"] = "(corrected by reviewer; observation not re-run)"
        s["corrected"] = True
    rec.setdefault("provenance", {})["reviewer_corrections"] = len(latest)
    return rec


def preference_pairs(record: dict, corrections: list[dict]) -> list[dict]:
    """One pair per corrected step: same context, original step = rejected, corrected = chosen."""
    if not corrections:
        return []
    chat = to_chat(record)
    msgs = chat["messages"]
    # Map step index -> position of its assistant message in the chat transcript.
    positions, k = {}, 2
    for s in record["steps"]:
        positions[s["i"]] = k
        k += 1 if s["action"] is None else 2
    out = []
    latest = {c["step_i"]: c for c in corrections}
    for i, c in latest.items():
        if i not in positions:
            continue
        pos = positions[i]
        orig = msgs[pos]
        step = next(s for s in record["steps"] if s["i"] == i)
        action = c["corrected_action"] or step["action"]
        chosen = {"role": "assistant", "content": c["corrected_thought"] or step["thought"]}
        if action:
            chosen["tool_calls"] = [{"id": f"call_fix_{i}", "type": "function",
                                     "function": {"name": action["tool"],
                                                  "arguments": json.dumps(action["args"], ensure_ascii=False)}}]
        out.append({"id": f"{record['id']}-step{i}", "prompt": msgs[:pos], "chosen": [chosen], "rejected": [orig],
                    "tools": chat["tools"],
                    "metadata": {"issue": c["issue"], "note": c["note"], "workflow": record["workflow"],
                                 "domain": record["domain"], "source": record["source"]}})
    return out


def eval_item(record: dict) -> dict | None:
    """Evaluation task from a captured episode: the goal, where to start, and checkpoints the final
    state must satisfy (the last value typed or chosen in each field). Order is not graded."""
    if record["source"] != "capture":
        return None
    final: dict[str, dict] = {}
    for s in record["steps"]:
        a = s.get("action")
        if not a or a["tool"] not in ("type", "select"):
            continue
        val = a["args"].get("text", a["args"].get("option", ""))
        if val in ("", "[MASKED]"):
            continue
        final[a["args"]["element"]] = {"element": a["args"]["element"], "expected": val, "tool": a["tool"]}
    submitted = any(s.get("action") and "(submit)" in s["action"]["args"].get("element", "")
                    for s in record["steps"])
    checkpoints = list(final.values())
    if submitted:
        checkpoints.append({"element": "form", "expected": "submitted", "tool": "submit"})
    if not checkpoints:
        return None
    return {"schema": EVAL_SCHEMA, "id": record["id"] + "-eval", "domain": record["domain"],
            "workflow": record["workflow"], "task": record["task"]["instructions"],
            "start_url": record["environment"].get("start_url"), "tools": record["environment"]["tools"],
            "checkpoints": checkpoints,
            "grading": "Pass when every checkpoint holds in the final state. Order of steps is not graded.",
            "reference_trajectory": record["id"]}

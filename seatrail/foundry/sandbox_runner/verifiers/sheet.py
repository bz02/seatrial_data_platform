"""The accounting sandbox's answer-key verifier (seatrail/verifier.py) as a foundry grader.

Cells whose expected value already equals the initial value are left out, so an agent that does
nothing scores exactly 0 (the existing verifier gives a few percent for blank cells that should
stay blank).

grader spec: {"type": "sheet_answer_key", "answer": {...}, "pass_score": 0.9}
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .... import config, db
from ....verifier import verify
from ...models import CriterionResult, EnvSpec, GraderResult


def _no_free_credit(answer: dict, initial: dict) -> dict:
    a = copy.deepcopy(answer)
    sheets = {s["name"]: {r["id"]: r for r in s["rows"]} for s in initial["sheets"]}
    for sheet, rows in a.get("cells", {}).items():
        for rid in list(rows):
            cols = rows[rid]
            for col in list(cols):
                start = sheets.get(sheet, {}).get(rid, {}).get(col)
                if str(start or "").strip().lower() == str(cols[col] or "").strip().lower():
                    del cols[col]
            if not cols:
                del rows[rid]
    return a


def grade(workdir: Path, grader: dict, env: EnvSpec) -> GraderResult:
    p = workdir / "deliverables" / "state.json"
    if not p.exists():
        return GraderResult(overall_score=0, passed=False, failure_mode="missing_deliverable")
    state = json.loads(p.read_text())
    task = db.q1("SELECT initial_state FROM tasks WHERE id=?", env.sheet_task_id)
    answer = grader["answer"]
    if task:
        answer = _no_free_credit(answer, db.loads(task["initial_state"]))
    v = verify(state, answer, float(grader.get("pass_score", config.VERIFY_PASS_SCORE)))
    crit = [CriterionResult(criterion_id=c["name"], passed=c["score"] >= 1, score=c["score"], detail=c["detail"])
            for c in v["checks"]]
    mode = None if v["passed"] else ("missing_deliverable" if not v["submitted"] and v["score"] == 0 else "precision")
    return GraderResult(overall_score=v["score"], passed=v["passed"], failure_mode=mode,
                        sub_metrics={c["name"]: c["score"] for c in v["checks"]}, criteria_results=crit)

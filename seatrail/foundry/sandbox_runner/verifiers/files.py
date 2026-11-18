"""Text / JSON deliverable grader (e.g. MASK_RECONSTRUCT: rebuild a redacted clause or table).

grader spec:
    {"type": "file_match",
     "files": {"answer.json": {"total": 1250.5}, "clause.txt": "Termination ... 30 days"},
     "normalize_ws": true}

JSON files are compared key by key (numbers within 0.01); text files after whitespace
normalisation. A JSON deliverable that does not parse is a format violation.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ...models import CriterionResult, EnvSpec, GraderResult


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _eq(got, want) -> bool:
    if isinstance(want, (int, float)) and not isinstance(want, bool):
        try:
            return abs(float(got) - float(want)) <= 0.01
        except (TypeError, ValueError):
            return False
    if isinstance(want, str) and isinstance(got, str):
        return _norm(got) == _norm(want)
    return got == want


def grade(workdir: Path, grader: dict, env: EnvSpec) -> GraderResult:
    root = workdir / "deliverables"
    crit, missing, bad_format = [], 0, False
    for rel, want in grader.get("files", {}).items():
        p = root / rel
        if not p.exists():
            missing += 1
            crit.append(CriterionResult(criterion_id=rel, passed=False, score=0, detail="missing"))
            continue
        text = p.read_text(errors="replace")
        if isinstance(want, dict):
            try:
                got = json.loads(text)
            except json.JSONDecodeError:
                bad_format = True
                crit.append(CriterionResult(criterion_id=rel, passed=False, score=0, detail="not valid JSON"))
                continue
            if not isinstance(got, dict):
                bad_format = True
                got = {}
            for k, v in want.items():
                ok = k in got and _eq(got[k], v)
                crit.append(CriterionResult(criterion_id=f"{rel}:{k}", passed=ok, score=float(ok)))
        else:
            ok = _norm(text) == _norm(str(want)) if grader.get("normalize_ws", True) else text == want
            crit.append(CriterionResult(criterion_id=rel, passed=ok, score=float(ok)))
    score = sum(c.score for c in crit) / len(crit) if crit else 0.0
    mode = None
    if score < 1.0:
        mode = "missing_deliverable" if missing and missing == len(grader.get("files", {})) else \
            "format" if bad_format else "precision"
    return GraderResult(overall_score=round(score, 4), passed=score == 1.0, failure_mode=mode,
                        sub_metrics={"match": round(score, 4)}, criteria_results=crit)

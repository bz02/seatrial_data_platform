"""CAD grader: compare a STEP deliverable's geometry to the reference.

Geometry extraction is behind `metrics_for()`. In profile-cad-meche the image's
`validate_cad_model` (build123d/OCP) writes `<file>.metrics.json` next to the STEP file; this
grader reads that sidecar, or a JSON deliverable directly. That keeps the host free of the heavy CAD
kernel while the comparison logic stays here and is unit-tested.

grader spec:
    {"type": "cad_metrics", "file": "bracket.step",
     "reference": {"volume": 1250.0, "bbox": [50, 20, 10], "surface_points": [[x,y,z], ...]},
     "threshold": 0.95}

Every similarity must reach the threshold (default 0.95) to pass. The overall score is 1.0 or 0.0,
never in between: a half-right part is a wrong part.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from ...models import CriterionResult, EnvSpec, GraderResult


def metrics_for(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".metrics.json")
    for p in (sidecar, path):
        if p.exists():
            try:
                m = json.loads(p.read_text())
                if isinstance(m, dict) and "volume" in m:
                    return m
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    return None


def _rel_sim(a: float, b: float) -> float:
    if b == 0:
        return 1.0 if a == 0 else 0.0
    return max(0.0, 1.0 - abs(a - b) / abs(b))


def _chamfer(p: list, q: list) -> float:
    def one(a, b):
        return sum(min(math.dist(x, y) for y in b) for x in a) / len(a)
    return (one(p, q) + one(q, p)) / 2


def grade(workdir: Path, grader: dict, env: EnvSpec) -> GraderResult:
    path = workdir / "deliverables" / grader["file"]
    if not path.exists():
        return GraderResult(overall_score=0, passed=False, failure_mode="missing_deliverable")
    m = metrics_for(path)
    if m is None:
        return GraderResult(overall_score=0, passed=False, failure_mode="format")
    ref, thr = grader["reference"], float(grader.get("threshold", 0.95))
    sims = {"volume": _rel_sim(float(m["volume"]), float(ref["volume"]))}
    if "bbox" in ref:
        got = sorted(m.get("bbox", [0, 0, 0]))
        want = sorted(ref["bbox"])
        sims["bbox"] = sum(_rel_sim(g, w) for g, w in zip(got, want)) / 3
    if "surface_points" in ref and m.get("surface_points"):
        diag = math.hypot(*ref.get("bbox", [1, 1, 1])) or 1.0
        sims["surface"] = max(0.0, 1.0 - _chamfer(m["surface_points"], ref["surface_points"]) / (0.05 * diag))
    elif "surface_points" in ref:
        sims["surface"] = 0.0
    crit = [CriterionResult(criterion_id=k, passed=v >= thr, score=round(v, 4)) for k, v in sims.items()]
    ok = all(c.passed for c in crit)
    return GraderResult(overall_score=1.0 if ok else 0.0, passed=ok, failure_mode=None if ok else "precision",
                        sub_metrics={k: round(v, 4) for k, v in sims.items()}, criteria_results=crit)

"""Deterministic graders. Each takes the grader directory (deliverables only, plus whatever the
grader writes itself), the decrypted grader spec and the agent-visible EnvSpec, and returns a
GraderResult. Graders never give partial credit for doing nothing: an untouched workspace scores 0.

    pytest                 code: hidden tests, fail_to_pass / pass_to_pass, exit codes
    docx_tracked_changes   office: tracked insertions/deletions + XML structure checks
    cad_metrics            CAD: volume / bounding box / surface (Chamfer) similarity, gate >= 0.95
    file_match             text/JSON deliverables compared to expected content
    sheet_answer_key       the accounting sandbox's hidden answer key
"""
from __future__ import annotations

from pathlib import Path

from ...models import EnvSpec, GraderResult
from . import cad, code, docx, files, sheet

GRADERS = {
    "pytest": code.grade,
    "docx_tracked_changes": docx.grade,
    "cad_metrics": cad.grade,
    "file_match": files.grade,
    "sheet_answer_key": sheet.grade,
}


def grade(workdir: Path, grader: dict, env: EnvSpec) -> GraderResult:
    fn = GRADERS.get(grader.get("type", ""))
    if fn is None:
        return GraderResult(overall_score=0, passed=False, failure_mode="environment",
                            sub_metrics={}, criteria_results=[])
    return fn(workdir, grader, env)

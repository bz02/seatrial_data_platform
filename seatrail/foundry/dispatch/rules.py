"""Authoring rules enforced in the API and live in the workbench.

Prompt linter: a task prompt must read like a real request. Phrases that give away how the task was
built ("we removed", "planted error") block submission; softer tells ("missing", hard-coded
coordinates, a prompt that is too short or too long) are warnings.

Atomic rubric builder: every criterion cites where its ground truth lives (`file:line`,
`file:start-end` or `file#section`), uses no subjective words, and review/drafting archetypes carry
at least one DECOY_NEGATIVE or EXCLUDED_BOUNDARY item so a model cannot pass by flagging everything.
"""
from __future__ import annotations

import json
import re

from ..models import Archetype, CriterionKind

BLOCKING_PHRASES = ["we removed", "was removed", "planted error", "planted bug", "injected error", "injected flaw",
                    "deliberately introduced", "intentionally introduced", "hidden test", "answer key", "ground truth",
                    "golden solution"]
WARNING_WORDS = ["missing", "masked", "redacted section", "the error is", "the bug is"]
COORDS = re.compile(r"\(\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?(?:\s*,\s*-?\d+(?:\.\d+)?)?\s*\)"
                    r"|\b[xyz]\s*=\s*-?\d+(?:\.\d+)?\s*(?:mm|cm|in)?\s*,\s*[xyz]\s*=", re.I)
SUBJECTIVE = ["well written", "well-written", "appropriate", "appropriately", "clearly", "clear and concise",
              "good quality", "high quality", "high-quality", "reasonable", "adequate", "adequately", "nice",
              "professional tone", "makes sense", "sufficiently"]
EVIDENCE_REF = re.compile(r"^[\w./\- ]+(?::\d+(?:-\d+)?|#[\w\-. ]+)$")
NEEDS_DECOY = {Archetype.MULTI_HOP_FLAW, Archetype.MASK_RECONSTRUCT}


def lint_prompt(text: str) -> dict:
    low = (text or "").lower()
    errors = [f'prompt gives away the construction: "{p}"' for p in BLOCKING_PHRASES if p in low]
    warnings = [f'prompt may hint at the answer: "{w}"' for w in WARNING_WORDS if re.search(rf"\b{re.escape(w)}\b", low)]
    if COORDS.search(text or ""):
        warnings.append("prompt contains hard-coded coordinates; describe the geometry instead")
    words = len(re.findall(r"\w+", text or ""))
    if words < 12:
        warnings.append(f"prompt is very short ({words} words); real requests carry context")
    if words > 1500:
        warnings.append(f"prompt is very long ({words} words)")
    return {"errors": errors, "warnings": warnings, "words": words}


def criterion_problems(kind: str, spec_json: dict, evidence_ref: str) -> list[str]:
    out = []
    if not evidence_ref or not EVIDENCE_REF.match(evidence_ref.strip()):
        out.append("ground_truth_evidence_ref must point at file:line, file:start-end or file#section")
    text = json.dumps(spec_json or {}).lower()
    for w in SUBJECTIVE:
        if re.search(rf"\b{re.escape(w)}\b", text):
            out.append(f'subjective wording "{w}": rewrite as a checkable yes/no fact')
    if kind == CriterionKind.ATOMIC_JUDGE and not any(k in (spec_json or {}) for k in ("question", "check")):
        out.append("ATOMIC_JUDGE criteria need a single yes/no `question`")
    return out


def rubric_problems(archetype: str, criteria: list[dict]) -> list[str]:
    out = []
    if not criteria:
        return []   # "at least one criterion" is enforced when calibration starts (rubric_required)
    for c in criteria:
        for p in criterion_problems(c["kind"], c.get("spec_json") or {}, c.get("ground_truth_evidence_ref", "")):
            out.append(f"criterion {c.get('criterion_id', '?')}: {p}")
    if archetype in NEEDS_DECOY and not any(c["kind"] in (CriterionKind.DECOY_NEGATIVE, CriterionKind.EXCLUDED_BOUNDARY)
                                            for c in criteria):
        out.append(f"{archetype} tasks need at least one DECOY_NEGATIVE or EXCLUDED_BOUNDARY criterion")
    return out

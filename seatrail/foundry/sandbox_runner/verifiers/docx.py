"""Office grader: tracked changes and XML structure in a .docx (stdlib zip + XML, no Word needed).

grader spec:
    {"type": "docx_tracked_changes", "file": "contract.docx",
     "expect_insertions": ["30 days"],      # text that must appear inside <w:ins>
     "expect_deletions": ["60 days"],       # text that must appear inside <w:del>
     "forbid_text": ["TODO"],               # must not appear in the accepted text
     "min_paragraphs": 3}                   # structure must survive (no flattened document)

Score = share of expected tracked changes present, forced to 0 if a forbidden text or structure
check fails. Tracked changes are required because legal buyers review redlines, not clean copies.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from ...models import CriterionResult, EnvSpec, GraderResult

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _text(el) -> str:
    return "".join((t.text or "") for t in el.iter() if t.tag in (W + "t", W + "delText"))


def parse(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    ins = [_text(e) for e in root.iter(W + "ins")]
    dels = [_text(e) for e in root.iter(W + "del")]
    paras = root.findall(f".//{W}body/{W}p")
    accepted = []
    for p in paras:
        accepted.append("".join((t.text or "") for t in p.iter(W + "t")))
    return {"insertions": ins, "deletions": dels, "paragraphs": len(paras), "accepted_text": "\n".join(accepted)}


def grade(workdir: Path, grader: dict, env: EnvSpec) -> GraderResult:
    path = workdir / "deliverables" / grader["file"]
    if not path.exists():
        return GraderResult(overall_score=0, passed=False, failure_mode="missing_deliverable")
    try:
        doc = parse(path)
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return GraderResult(overall_score=0, passed=False, failure_mode="format")
    crit = []
    for kind, key in (("ins", "expect_insertions"), ("del", "expect_deletions")):
        pool = doc["insertions" if kind == "ins" else "deletions"]
        for want in grader.get(key, []):
            ok = any(want.lower() in x.lower() for x in pool)
            crit.append(CriterionResult(criterion_id=f"{kind}:{want}", passed=ok, score=float(ok)))
    gates = []
    for bad in grader.get("forbid_text", []):
        ok = bad.lower() not in doc["accepted_text"].lower()
        gates.append(CriterionResult(criterion_id=f"forbid:{bad}", passed=ok, score=float(ok)))
    if "min_paragraphs" in grader:
        ok = doc["paragraphs"] >= int(grader["min_paragraphs"])
        gates.append(CriterionResult(criterion_id="structure:min_paragraphs", passed=ok, score=float(ok),
                                     detail=f"{doc['paragraphs']} paragraphs"))
    change_score = sum(c.score for c in crit) / len(crit) if crit else 0.0
    gate_ok = all(g.passed for g in gates)
    score = change_score if gate_ok else 0.0
    mode = None if score == 1.0 else ("format" if not gate_ok else "precision")
    return GraderResult(overall_score=round(score, 4), passed=score == 1.0, failure_mode=mode,
                        sub_metrics={"tracked_changes": round(change_score, 4), "gates": float(gate_ok)},
                        criteria_results=crit + gates)

"""Hybrid synthesis: Audit -> Transform -> Devil's Advocate -> empirical sanity check -> DRAFT.

    1. Audit      an author model reads a compliance-approved seed and proposes places where a real
                  task can be cut (a function to break, a clause to mask), with line ranges.
    2. Transform  for each candidate it writes the task: the prompt a client would send, edits that
                  turn the seed into the agent's starting workspace, hidden tests or expected
                  answers, and atomic rubric criteria with evidence references.
    3. Rules      deterministic gates: edits apply cleanly, prompt linter, rubric rules, and leakage
                  checks (the removed text must not appear in the prompt or the workspace).
    4. Critic     a *different* model plays devil's advocate on the agent-visible package: is it
                  ambiguous, unsolvable, leaky, too easy, is the verifier wrong? A BLOCKER sends the
                  candidate back to Transform once with the critique; a second BLOCKER rejects it.
    5. Sanity     golden solution must score 1.0 and an empty submission 0.0 in the pinned sandbox.

Survivors become DRAFT tasks (origin SYNTH) owned by the SME who asked for them, so a human still
edits, submits and is accountable for every task. Every candidate, accepted or not, is a row in
`synth_runs` with the stage that stopped it, and all model spend goes to the ledger.
"""
from __future__ import annotations

import re
import uuid

from ... import db
from .. import store
from ..dispatch.rules import lint_prompt, rubric_problems
from ..models import (Archetype, DefectTag, EnvSpec, LedgerKind, Profile, RubricCriterionIn, SealedSpec, TaskIn)
from ..sandbox_runner.runner import empty_policy, golden_policy, rollout_and_grade, run_env_sanity_check
from .llm import TextModel

SUPPORTED = {Archetype.BUG_FIX: Profile.CODE_SWE, Archetype.MASK_RECONSTRUCT: Profile.DOC_OFFICE}
MAX_FILE_BYTES = 200_000
MAX_SEED_BYTES = 1_000_000

AUDIT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["candidates"],
    "properties": {"candidates": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["path", "line_start", "line_end", "archetype", "idea"],
        "properties": {"path": {"type": "string"}, "line_start": {"type": "integer"},
                       "line_end": {"type": "integer"},
                       "archetype": {"type": "string", "enum": [a.value for a in SUPPORTED]},
                       "idea": {"type": "string"}}}}},
}
_FILE = {"type": "object", "additionalProperties": False, "required": ["path", "content"],
         "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}
TRANSFORM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "instructions", "edits", "hidden_tests", "fail_to_pass", "pass_to_pass", "expected",
                 "criteria"],
    "properties": {
        "title": {"type": "string"}, "instructions": {"type": "string"},
        "edits": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": ["path", "find", "replace"],
                                             "properties": {"path": {"type": "string"}, "find": {"type": "string"},
                                                            "replace": {"type": "string"}}}},
        "hidden_tests": {"type": "array", "items": _FILE},
        "fail_to_pass": {"type": "array", "items": {"type": "string"}},
        "pass_to_pass": {"type": "array", "items": {"type": "string"}},
        "expected": {"type": "array", "items": _FILE},
        "criteria": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "question", "evidence_ref"],
            "properties": {"kind": {"type": "string", "enum": ["PROGRAMMATIC", "ATOMIC_JUDGE", "DECOY_NEGATIVE",
                                                               "EXCLUDED_BOUNDARY"]},
                           "question": {"type": "string"}, "evidence_ref": {"type": "string"}}}},
    },
}
CRITIC_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["solvable", "issues"],
    "properties": {"solvable": {"type": "boolean"}, "issues": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["tag", "severity", "detail"],
        "properties": {"tag": {"type": "string", "enum": [t.value for t in DefectTag]},
                       "severity": {"type": "string", "enum": ["BLOCKER", "MINOR"]},
                       "detail": {"type": "string"}}}}},
}

AUDIT_SYSTEM = """You audit source material for an AI evaluation dataset. Propose places where a realistic,
verifiable task can be cut from it: for code, a function whose behaviour hidden tests can pin down
(BUG_FIX: we will introduce a realistic defect there); for documents, a clause, table or section whose
content is fully determined by the rest of the material (MASK_RECONSTRUCT: we will remove it and ask
for it back). Prefer spots that need reading several files or sections to solve. Cite exact 1-based
line ranges in the files you were given. Return at most the number of candidates asked for."""

TRANSFORM_SYSTEM = """You turn one audited candidate into an evaluation task.
- `instructions` must read like a real request from a client or colleague. Never say that something was
  removed, planted, injected or hidden, and do not quote the answer.
- `edits` are exact find/replace pairs applied to the seed files to make the agent's starting workspace.
  Each `find` must occur exactly once in its file.
- BUG_FIX: write pytest files in `hidden_tests` (they run against the agent's files, which sit next to
  them), list the tests that must go from failing to passing in `fail_to_pass` as "file::test" and tests
  that must keep passing in `pass_to_pass`. Leave `expected` empty.
- MASK_RECONSTRUCT: put the files the agent must write and their exact expected content in `expected`.
  Leave the test fields empty. Include at least one DECOY_NEGATIVE or EXCLUDED_BOUNDARY criterion.
- `criteria` are atomic yes/no checks, each citing its evidence as file:line, file:start-end or
  file#section. No subjective words."""

CRITIC_SYSTEM = """You are the devil's advocate reviewing an evaluation task before it goes to a frontier-model
panel. You see exactly what the agent will see, plus a summary of how it is graded. Look for: answer
leakage (the prompt or workspace gives the answer away), ambiguity (more than one reasonable answer,
or the grader would fail a correct one), unsolvable (the information needed is not there), too easy
(one obvious edit, no reading needed), and a wrong or gameable verifier. Mark an issue BLOCKER only if
the task should not ship as is."""


class Rejected(Exception):
    def __init__(self, stage: str, reasons: list[str]):
        super().__init__(f"{stage}: {'; '.join(reasons)}")
        self.stage, self.reasons = stage, reasons


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------
def seed_files(asset_id: int) -> dict[str, str]:
    from ..ingestion.service import clean_dir
    a = store.get_source_asset(asset_id)
    if a.get("gate_status") != "APPROVED":
        raise store.sm.TransitionError("this source has not passed the compliance gate", "compliance_blocked")
    root = clean_dir(asset_id)
    out, total = {}, 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = p.read_text("utf-8")
        except UnicodeDecodeError:
            continue
        total += len(text)
        if total > MAX_SEED_BYTES:
            break
        out[str(p.relative_to(root))] = text
    if not out:
        raise ValueError("the seed has no readable text files")
    return out


def _numbered(files: dict[str, str]) -> str:
    parts = []
    for path, text in files.items():
        lines = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(text.splitlines(), 1))
        parts.append(f"=== {path} ===\n{lines}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------
def audit(model: TextModel, files: dict[str, str], n: int, archetype: str | None) -> tuple[list[dict], float]:
    want = f" Only propose {archetype} candidates." if archetype else ""
    out, c = model.complete("audit", AUDIT_SYSTEM, f"Propose up to {n} candidates.{want}\n\n{_numbered(files)}",
                            AUDIT_SCHEMA)
    good = []
    for cand in out.get("candidates", [])[: max(n * 2, n)]:
        problems = audit_problems(cand, files, archetype)
        good.append({**cand, "problems": problems})
    return good, c


def audit_problems(cand: dict, files: dict[str, str], archetype: str | None) -> list[str]:
    out = []
    if cand.get("archetype") not in [a.value for a in SUPPORTED]:
        out.append(f"unsupported archetype {cand.get('archetype')}")
    elif archetype and cand["archetype"] != archetype:
        out.append(f"asked for {archetype}")
    text = files.get(cand.get("path", ""))
    if text is None:
        out.append(f"no such file {cand.get('path')!r} in the seed")
    else:
        n = len(text.splitlines())
        if not (1 <= cand.get("line_start", 0) <= cand.get("line_end", 0) <= n):
            out.append(f"line range {cand.get('line_start')}-{cand.get('line_end')} is outside 1-{n}")
    return out


def transform(model: TextModel, files: dict[str, str], cand: dict, critique: list[dict] | None = None
              ) -> tuple[dict, float]:
    prompt = (f"Candidate ({cand['archetype']}): {cand['idea']}\nLocation: {cand['path']}:"
              f"{cand['line_start']}-{cand['line_end']}\n\n{_numbered(files)}")
    if critique:
        prompt += "\n\nA reviewer blocked your previous version. Fix every point:\n" + "\n".join(
            f"- [{i['tag']}] {i['detail']}" for i in critique)
    return model.complete("transform", TRANSFORM_SYSTEM, prompt, TRANSFORM_SCHEMA)


def build(files: dict[str, str], cand: dict, draft: dict) -> tuple[EnvSpec, SealedSpec, list[RubricCriterionIn]]:
    """Apply the edits and assemble the agent-visible spec, the sealed ground truth and the rubric.
    Raises Rejected("RULES") on anything a reviewer should never have to see."""
    arch = Archetype(cand["archetype"])
    problems: list[str] = []
    ws = dict(files)
    for e in draft.get("edits", []):
        cur = ws.get(e["path"])
        if cur is None:
            problems.append(f"edit targets unknown file {e['path']}")
        elif not e["find"] or cur.count(e["find"]) != 1:
            problems.append(f"edit `find` must occur exactly once in {e['path']} (found {cur.count(e['find'])})")
        elif e["find"] == e["replace"]:
            problems.append(f"edit on {e['path']} changes nothing")
        else:
            ws[e["path"]] = cur.replace(e["find"], e["replace"])
    if not draft.get("edits"):
        problems.append("no edits: the workspace would be the original seed")
    instr = draft.get("instructions", "")
    lint = lint_prompt(instr)
    problems += lint["errors"]
    low = instr.lower()
    for e in draft.get("edits", []):
        if len(e["find"].strip()) >= 12 and e["find"].strip().lower() in low:
            problems.append("leakage: the prompt quotes the original text the agent has to restore")
    changed = [e["path"] for e in draft.get("edits", []) if e["path"] in files]
    if arch == Archetype.BUG_FIX:
        tests = {t["path"]: t["content"] for t in draft.get("hidden_tests", [])}
        if not tests or not draft.get("fail_to_pass"):
            problems.append("BUG_FIX needs hidden tests and at least one fail_to_pass test")
        for p in tests:
            if p in ws:
                problems.append(f"leakage: hidden test {p} would overwrite or sit in the workspace")
        grader = {"type": "pytest", "tests": tests, "fail_to_pass": draft.get("fail_to_pass", []),
                  "pass_to_pass": draft.get("pass_to_pass", [])}
        golden = {"files": {p: files[p] for p in dict.fromkeys(changed)}}
        deliverables = list(dict.fromkeys(changed))
    else:
        expected = {x["path"]: x["content"] for x in draft.get("expected", [])}
        if not expected:
            problems.append("MASK_RECONSTRUCT needs at least one expected file")
        norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()  # noqa: E731
        blob = norm(" ".join(ws.values()) + " " + instr)
        for p, content in expected.items():
            if len(norm(content)) >= 12 and norm(content) in blob:
                problems.append(f"leakage: the expected content of {p} is still in the workspace or prompt")
        grader = {"type": "file_match", "files": expected, "normalize_ws": True}
        golden = {"files": expected}
        deliverables = list(expected)
    crit = [{"criterion_id": f"c{i}", "kind": c["kind"], "spec_json": {"question": c["question"]},
             "ground_truth_evidence_ref": c["evidence_ref"]} for i, c in enumerate(draft.get("criteria", []), 1)]
    if not crit:
        problems.append("no rubric criteria")
    problems += rubric_problems(arch, crit)
    if problems:
        raise Rejected("RULES", problems)
    env = EnvSpec(profile=SUPPORTED[arch], instructions=instr, workspace=ws, deliverables=deliverables,
                  required_reads=[cand["path"]], max_steps=40)
    sealed = SealedSpec(grader=grader, golden=golden)
    return env, sealed, [RubricCriterionIn(kind=c["kind"], spec_json=c["spec_json"],
                                           ground_truth_evidence_ref=c["ground_truth_evidence_ref"]) for c in crit]


def critic_view(env: EnvSpec, sealed: SealedSpec, criteria: list[RubricCriterionIn]) -> str:
    g = sealed.grader
    how = (f"pytest: tests {sorted(g['tests'])} must pass ({', '.join(g['fail_to_pass'])} currently fail)"
           if g["type"] == "pytest" else f"exact match (whitespace-normalised) of {sorted(g['files'])}")
    rubric = "\n".join(f"- [{c.kind}] {c.spec_json.get('question', '')} ({c.ground_truth_evidence_ref})"
                       for c in criteria)
    return (f"PROMPT:\n{env.instructions}\n\nDELIVERABLES: {env.deliverables}\nGRADED BY: {how}\n\nRUBRIC:\n{rubric}"
            f"\n\nWORKSPACE:\n{_numbered(env.workspace)}")


def devils_advocate(critic: TextModel, env: EnvSpec, sealed: SealedSpec, criteria: list[RubricCriterionIn]
                    ) -> tuple[dict, float]:
    return critic.complete("critic", CRITIC_SYSTEM, critic_view(env, sealed, criteria), CRITIC_SCHEMA)


def blockers(review: dict) -> list[dict]:
    out = [i for i in review.get("issues", []) if i.get("severity") == "BLOCKER"]
    if not review.get("solvable", True) and not any(i["tag"] == "UNSOLVABLE" for i in out):
        out.append({"tag": "UNSOLVABLE", "severity": "BLOCKER", "detail": "critic judged the task unsolvable"})
    return out


def empirical_check(env: EnvSpec, sealed: SealedSpec) -> list[str]:
    s = sealed.model_dump()
    _, golden = rollout_and_grade(golden_policy(env, s), env, s)
    _, empty = rollout_and_grade(empty_policy(env), env, s)
    out = []
    if golden.overall_score < 1.0 - 1e-9 or not golden.passed:
        out.append(f"golden solution scored {golden.overall_score} ({golden.failure_mode})")
    if empty.overall_score != 0.0 or empty.passed:
        out.append(f"empty submission scored {empty.overall_score}: the edit does not break anything the grader sees")
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def synthesize(asset_id: int, batch_id: int, model: TextModel, critic: TextModel, *, max_candidates: int = 3,
               archetype: str | None = None, author_id: int | None = None, max_revisions: int = 1) -> dict:
    if model.name == critic.name:
        raise ValueError("the devil's advocate must be a different model from the author model")
    batch = store.get_batch(batch_id)
    asset = store.get_source_asset(asset_id)
    files = seed_files(asset_id)
    group = uuid.uuid4().hex[:12]
    cands, audit_cost = audit(model, files, max_candidates, archetype)
    store.ledger(LedgerKind.MODEL_API, audit_cost, ref=f"synthesis {group} audit", batch_id=batch_id)
    results = []
    for cand in cands[:max_candidates]:
        spent, revisions, review, task_id = 0.0, 0, {}, None
        problems = cand.pop("problems")
        try:
            if problems:
                raise Rejected("AUDIT", problems)
            critique = None
            while True:
                draft, c = transform(model, files, cand, critique)
                spent += c
                env, sealed, criteria = build(files, cand, draft)
                review, c = devils_advocate(critic, env, sealed, criteria)
                spent += c
                block = blockers(review)
                if not block:
                    break
                if revisions >= max_revisions:
                    raise Rejected("CRITIC", [f"[{i['tag']}] {i['detail']}" for i in block])
                revisions += 1
                critique = block
            bad = empirical_check(env, sealed)
            if bad:
                raise Rejected("SANITY", bad)
            t = store.create_task(TaskIn(batch_id=batch_id, domain=asset.get("domain_tag") or batch["domain"],
                                         archetype=cand["archetype"], title=draft["title"] or cand["idea"][:80],
                                         author_id=author_id, source_asset_ids=[asset_id], env=env, sealed=sealed,
                                         origin="SYNTH"))
            task_id = t["id"]
            for c_in in criteria:
                store.add_criterion(task_id, c_in, author_id)
            run_env_sanity_check(task_id)
            store.ledger(LedgerKind.MODEL_API, spent, task_id, f"synthesis {group}")
            store.audit(task_id, "synthesized", author_id, group_id=group, model=model.name, critic=critic.name,
                        revisions=revisions, minor_issues=[i["tag"] for i in review.get("issues", [])])
            status, stage, reasons = "ACCEPTED", "", []
        except Rejected as r:
            status, stage, reasons = "REJECTED", r.stage, r.reasons
            store.ledger(LedgerKind.MODEL_API, spent, ref=f"synthesis {group} rejected", batch_id=batch_id)
        rid = db.x("INSERT INTO synth_runs (group_id, asset_id, batch_id, model_name, critic_model, candidate, status,"
                   " rejected_at, reasons, critic, revisions, task_id, cost_usd, created_at) VALUES"
                   " (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", group, asset_id, batch_id, model.name, critic.name,
                   db.dumps(cand), status, stage, db.dumps(reasons), db.dumps(review), revisions, task_id,
                   round(spent, 6), db.now())
        results.append({"id": rid, "candidate": cand, "status": status, "rejected_at": stage, "reasons": reasons,
                        "revisions": revisions, "task_id": task_id, "cost_usd": round(spent, 6)})
    accepted = [r for r in results if r["status"] == "ACCEPTED"]
    return {"group_id": group, "asset_id": asset_id, "batch_id": batch_id, "candidates": len(results),
            "accepted": len(accepted), "yield": round(len(accepted) / len(results), 4) if results else 0.0,
            "task_ids": [r["task_id"] for r in accepted], "results": results,
            "cost_usd": round(audit_cost + sum(r["cost_usd"] for r in results), 6)}


def runs(asset_id: int | None = None, group_id: str | None = None) -> list[dict]:
    where, args = [], []
    if asset_id is not None:
        where.append("asset_id=?")
        args.append(asset_id)
    if group_id:
        where.append("group_id=?")
        args.append(group_id)
    rows = db.q("SELECT * FROM synth_runs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id",
                *args)
    for r in rows:
        for k in ("candidate", "reasons", "critic"):
            r[k] = db.loads(r[k])
    return rows

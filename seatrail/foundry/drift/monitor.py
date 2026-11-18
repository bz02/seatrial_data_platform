"""Drift monitor and auto-hardening.

Frontier models improve every few months, so a task calibrated at 30% can be solved 90% of the time
by the next release. That silently devalues what a client bought and, for EVAL buyers, saturates
their benchmark. The monitor re-runs shipped tasks (QA_APPROVED and later) on the current panel
without touching their status (calibration_runs.purpose = 'DRIFT') and compares the pooled pass rate
with the one measured at calibration:

    current > batch.target_pass_rate_max                 SATURATED  -> harder evolution
    current - baseline >= DRIFT_DELTA (still in band)    DRIFTING   -> watch
    otherwise                                            STABLE

Harder evolution asks the synthesis author model for a child task built on the parent's *solved*
state (so the golden solution stays well defined), then runs the same rules, devil's advocate and
empirical sanity check as P5. The child is a DRAFT (origin EVOLVED, parent_task_id set) owned by the
parent's author, so it re-enters the normal SME -> calibration -> QA path. It is also what P7's
warranty replacements are made from when a client reports TOO_EASY.
"""
from __future__ import annotations

import os
import uuid

from ... import db
from .. import store
from ..calibrator.adapters import ModelAdapter, get_adapter
from ..calibrator.calibrate import _run_model, record_runs
from ..models import EnvSpec, LedgerKind, TaskIn, TaskStatus, ToolMode
from ..sandbox_runner.runner import CircuitBreaker, run_env_sanity_check
from ..synthesizer import pipeline
from ..synthesizer.llm import TextModel

S = TaskStatus
DRIFT_DELTA = float(os.environ.get("SEATRAIL_DRIFT_DELTA", "0.25"))
WATCHED = (S.QA_APPROVED, S.PACKAGED, S.DELIVERED, S.ACCEPTED)

EVOLVE_SYSTEM = pipeline.TRANSFORM_SYSTEM + """

You are evolving an existing task that frontier models now solve too often. You get the parent task's
prompt, its grader and the repository in its SOLVED state. Produce a new, harder task on that solved
state. Effective levers: take hints out of the prompt, make the fix need information spread across
several files or sections, add a plausible distractor the agent must not change, require handling an
edge case the parent's tests did not pin down (and test it). Do not just rename things or reword the
prompt. The new task must still have exactly one correct answer."""


def baseline_rate(task_id: int) -> float | None:
    g = db.q1("SELECT group_id FROM calibration_runs WHERE task_id=? AND purpose='CALIBRATION' ORDER BY id DESC"
              " LIMIT 1", task_id)
    if not g:
        return None
    r = db.q1("SELECT SUM(k_rollouts) n, SUM(pass_count) c FROM calibration_runs WHERE task_id=? AND group_id=?",
              task_id, g["group_id"])
    return r["c"] / r["n"] if r["n"] else None


def classify_drift(baseline: float | None, current: float, hi: float) -> str:
    if current > hi:
        return "SATURATED"
    if baseline is not None and current - baseline >= DRIFT_DELTA:
        return "DRIFTING"
    return "STABLE"


def check_task(task_id: int, adapters: list[ModelAdapter], k: int = 4, concurrency: int = 4) -> dict:
    t = store.get_task(task_id)
    if t["status"] not in WATCHED:
        raise store.sm.TransitionError(f"task is {t['status']}; drift checks cover shipped tasks", "not_shipped")
    b = store.get_batch(t["batch_id"])
    spec = EnvSpec.model_validate(t["env"])
    sealed = store.task_sealed(task_id)
    group = uuid.uuid4().hex[:12]
    breaker = CircuitBreaker()
    per_model = [_run_model(task_id, spec, sealed, a, k, concurrency, breaker, group) for a in adapters]
    record_runs(task_id, t["version"], per_model, k, b["target_pass_rate_min"], b["target_pass_rate_max"], group,
                purpose="DRIFT")
    n = sum(m["n"] for m in per_model)
    c = sum(m["c"] for m in per_model)
    current = c / n if n else 0.0
    base = baseline_rate(task_id)
    status = "ENVIRONMENT_ERROR" if breaker.open else classify_drift(base, current, b["target_pass_rate_max"])
    models = [m["model"] for m in per_model]
    rid = db.x("INSERT INTO drift_reports (task_id, group_id, baseline_rate, current_rate, models, status, created_at)"
               " VALUES (?,?,?,?,?,?,?)", task_id, group, base, round(current, 4), db.dumps(models), status, db.now())
    store.audit(task_id, "drift_check", None, group_id=group, baseline=base, current=round(current, 4), status=status)
    return get_report(rid)


def get_report(rid: int) -> dict:
    r = db.q1("SELECT * FROM drift_reports WHERE id=?", rid)
    r["models"] = db.loads(r["models"])
    return r


def reports(batch_id: int | None = None) -> list[dict]:
    sql = "SELECT d.* FROM drift_reports d JOIN foundry_tasks t ON t.id=d.task_id"
    rows = db.q(sql + (" WHERE t.batch_id=?" if batch_id else "") + " ORDER BY d.id",
                *([batch_id] if batch_id else []))
    for r in rows:
        r["models"] = db.loads(r["models"])
    return rows


# ---------------------------------------------------------------------------
# Harder evolution
# ---------------------------------------------------------------------------
def solved_files(task_id: int) -> dict[str, str]:
    t = store.get_task(task_id)
    golden = (store.task_sealed(task_id).get("golden") or {}).get("files") or {}
    files = {**(t["env"].get("workspace") or {}), **golden}
    return {p: v for p, v in files.items() if not v.startswith("base64:")}


def evolve(task_id: int, model: TextModel, critic: TextModel, current_rate: float | None = None,
           max_revisions: int = 1) -> dict:
    """Returns {"child_task_id": id} or {"rejected_at", "reasons"}."""
    if model.name == critic.name:
        raise ValueError("the devil's advocate must be a different model from the author model")
    parent = store.get_task(task_id)
    if parent["archetype"] not in [a.value for a in pipeline.SUPPORTED]:
        return {"child_task_id": None, "rejected_at": "AUDIT",
                "reasons": [f"no evolution strategy for {parent['archetype']} yet"]}
    files = solved_files(task_id)
    sealed = store.task_sealed(task_id)
    target = (parent["env"].get("deliverables") or [next(iter(files))])[0]
    cand = {"path": target if target in files else next(iter(files)), "line_start": 1, "line_end": 1,
            "archetype": parent["archetype"], "idea": f"harder evolution of task {task_id}"}
    rate = f"{current_rate:.0%}" if current_rate is not None else "too often"
    base_prompt = (f"Parent task {task_id} is now solved {rate} of the time by the frontier panel.\n\n"
                   f"PARENT PROMPT:\n{parent['env'].get('instructions', '')}\n\nPARENT GRADER:\n"
                   f"{db.dumps(sealed['grader'])}\n\nSOLVED STATE:\n{pipeline._numbered(files)}")
    spent, critique, revisions = 0.0, None, 0
    try:
        while True:
            prompt = base_prompt + ("" if not critique else "\n\nA reviewer blocked your previous version:\n" +
                                    "\n".join(f"- [{i['tag']}] {i['detail']}" for i in critique))
            draft, c = model.complete("evolve", EVOLVE_SYSTEM, prompt, pipeline.TRANSFORM_SCHEMA)
            spent += c
            env, new_sealed, criteria = pipeline.build(files, cand, draft)
            env = env.model_copy(update={"mode": ToolMode(parent["env"].get("mode") or env.mode),
                                         "max_steps": max(env.max_steps, parent["env"].get("max_steps") or 0)})
            review, c = pipeline.devils_advocate(critic, env, new_sealed, criteria)
            spent += c
            block = pipeline.blockers(review)
            if not block:
                break
            if revisions >= max_revisions:
                raise pipeline.Rejected("CRITIC", [f"[{i['tag']}] {i['detail']}" for i in block])
            revisions += 1
            critique = block
        bad = pipeline.empirical_check(env, new_sealed)
        if bad:
            raise pipeline.Rejected("SANITY", bad)
    except pipeline.Rejected as r:
        store.ledger(LedgerKind.MODEL_API, spent, task_id, f"evolution of task {task_id} rejected")
        return {"child_task_id": None, "rejected_at": r.stage, "reasons": r.reasons, "cost_usd": round(spent, 6)}
    child = store.create_task(TaskIn(batch_id=parent["batch_id"], domain=parent["domain"],
                                     archetype=parent["archetype"], title=(draft.get("title") or parent["title"])[:200],
                                     author_id=parent["author_id"], source_asset_ids=parent["source_asset_ids"],
                                     env=env, sealed=new_sealed, origin="EVOLVED", parent_task_id=task_id))
    for c_in in criteria:
        store.add_criterion(child["id"], c_in, parent["author_id"])
    run_env_sanity_check(child["id"])
    store.ledger(LedgerKind.MODEL_API, spent, child["id"], f"evolution of task {task_id}")
    store.audit(child["id"], "evolved", None, parent_task_id=task_id, revisions=revisions)
    store.audit(task_id, "evolution_created", None, child_task_id=child["id"])
    return {"child_task_id": child["id"], "revisions": revisions, "cost_usd": round(spent, 6)}


def run(task_ids: list[int] | None = None, batch_id: int | None = None, models: list[str] | None = None,
        adapters_factory=None, k: int = 4, concurrency: int = 4, harden: bool = True,
        author_model: TextModel | None = None, critic_model: TextModel | None = None) -> dict:
    """Check every watched task (or the listed ones); evolve the saturated ones."""
    if task_ids is None:
        sql = "SELECT id FROM foundry_tasks WHERE status IN (" + ",".join("?" * len(WATCHED)) + ")"
        args: list = list(WATCHED)
        if batch_id is not None:
            sql += " AND batch_id=?"
            args.append(batch_id)
        task_ids = [r["id"] for r in db.q(sql + " ORDER BY id", *args)]
    if harden and (author_model is None or critic_model is None):
        from ..synthesizer import llm
        a, c = llm.default_models()
        author_model = author_model or llm.get_text_model(a)
        critic_model = critic_model or llm.get_text_model(c)
    out = []
    for tid in task_ids:
        adapters = adapters_factory() if adapters_factory else [get_adapter(m) for m in (models or [])]
        rep = check_task(tid, adapters, k, concurrency)
        already = db.q1("SELECT id FROM foundry_tasks WHERE parent_task_id=? AND origin='EVOLVED'", tid)
        if harden and rep["status"] == "SATURATED" and not already:
            ev = evolve(tid, author_model, critic_model, rep["current_rate"])
            action = "EVOLVED" if ev["child_task_id"] else "EVOLUTION_FAILED"
            db.x("UPDATE drift_reports SET action=?, child_task_id=?, detail=? WHERE id=?", action, ev["child_task_id"],
                 "; ".join(ev.get("reasons", [])), rep["id"])
            rep = get_report(rep["id"])
        out.append(rep)
    counts: dict[str, int] = {}
    for r in out:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"checked": len(out), "by_status": counts, "evolved": [r["child_task_id"] for r in out if r["child_task_id"]],
            "reports": out}
